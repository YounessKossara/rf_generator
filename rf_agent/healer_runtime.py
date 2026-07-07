"""
Phase B — Runtime selector remap library for Robot Framework.

When a step's primary selector is not visible / not found within the timeout,
the test calls `Heal Selector By Label` which runs JavaScript in the LIVE
browser to find a fresh element matching the target (label, role), then
returns a freshly-derived selector. The test retries the step with the new
selector.

This is the Phase B "deterministic remap" the user approved:
  - No LLM call at runtime.
  - No catalog re-extraction round-trip — we use the live browser session
    that SeleniumLibrary already owns.
  - Single retry per step; tests fail cleanly if no match found.

The JavaScript reuses the same W3C-grounded label inference as
`rf_agent.dom_catalog.extract_catalog`, so generation-time and runtime
agree on what "label X for role Y" means.
"""

_HEAL_JS = r"""
const wantLabel = (arguments[0] || '').replace(/\s+/g, ' ').trim();
const wantRole  = (arguments[1] || '').trim();

function maxLabel(s) {
    return (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
}

function inferLabel(e) {
    const tag = e.tagName.toLowerCase();
    const aria = e.getAttribute('aria-label');
    if (aria && aria.trim()) return maxLabel(aria);

    const lb = e.getAttribute('aria-labelledby');
    if (lb) {
        const parts = lb.split(/\s+/).map(id => {
            const ref = document.getElementById(id);
            return ref ? (ref.textContent || '').trim() : '';
        }).filter(Boolean);
        if (parts.length) return maxLabel(parts.join(' '));
    }

    try {
        if (e.labels && e.labels.length > 0) {
            const t = (e.labels[0].textContent || '').trim();
            if (t) return maxLabel(t);
        }
    } catch (err) {}

    const labA = e.closest('label');
    if (labA && labA !== e) {
        const t = (labA.textContent || '').trim();
        if (t) return maxLabel(t);
    }

    // Bounded form-group walk (BEM-aware, mirrors dom_catalog.py)
    const isGroup = (n) => {
        if (n.getAttribute('role') === 'group') return true;
        const cls = (n.className || '').toString();
        if (!cls) return false;
        const tokens = ['input-group', 'form-group', 'form-field', 'form-item',
                        'field-wrapper', 'control-group', 'form-control'];
        for (const c of cls.split(/\s+/)) {
            const base = c.toLowerCase().split('--')[0];
            for (const t of tokens) {
                if (base === t || base.endsWith('-' + t) || base.endsWith('_' + t)) return true;
            }
        }
        return false;
    };
    const findLabelIn = (container) => {
        let lbl = container.querySelector('label');
        if (lbl && lbl.contains(e)) lbl = null;
        if (!lbl) {
            const cands = container.querySelectorAll(
                '[class*="-label" i], [class*="label-" i], [class^="label" i]'
            );
            for (const c of cands) {
                if (!c.contains(e)) { lbl = c; break; }
            }
        }
        if (lbl) {
            const t = (lbl.textContent || '').trim();
            if (t) return maxLabel(t);
        }
        return '';
    };
    let node = e.parentElement;
    for (let depth = 0; depth < 6 && node; depth++) {
        if (isGroup(node)) {
            const found = findLabelIn(node);
            if (found) return found;
        }
        node = node.parentElement;
    }

    const ph = e.getAttribute('placeholder');
    if (ph && ph.trim()) return maxLabel(ph);

    if (['button', 'a', 'h1','h2','h3','h4','h5','h6'].includes(tag)) {
        const t = (e.textContent || '').trim();
        if (t) return maxLabel(t);
    }

    const ti = e.getAttribute('title');
    if (ti && ti.trim()) return maxLabel(ti);

    const nm = e.getAttribute('name');
    if (nm && nm.trim()) return maxLabel(nm);

    return '';
}

function inferRole(e) {
    const tag = e.tagName.toLowerCase();
    const typ = (e.getAttribute('type') || '').toLowerCase();
    const role = (e.getAttribute('role') || '').toLowerCase();
    if (tag === 'input') {
        if (typ === 'password') return 'password_input';
        if (typ === 'checkbox') return 'checkbox';
        if (typ === 'radio') return 'radio';
        if (typ === 'submit' || typ === 'button') return 'button';
        return 'text_input';
    }
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select') return 'select';
    if (tag === 'button') return 'button';
    if (tag === 'a') return 'link';
    if (['h1','h2','h3','h4','h5','h6'].includes(tag)) return 'heading';
    if (role === 'tab') return 'tab';
    if (role === 'button') return 'button';
    return 'button';
}

function buildSelector(e) {
    const tag = e.tagName.toLowerCase();
    const id = e.getAttribute('id');
    const dt = e.getAttribute('data-test');
    const dti = e.getAttribute('data-testid');
    const al = e.getAttribute('aria-label');
    const nm = e.getAttribute('name');
    const ph = e.getAttribute('placeholder');
    const quote = (s) => s.includes("'") && !s.includes('"') ? '"' : "'";
    const wrap = (s) => { const q = quote(s); return `${q}${s}${q}`; };

    if (id) return `xpath://${tag}[@id=${wrap(id)}]`;
    if (dt) return `xpath://${tag}[@data-test=${wrap(dt)}]`;
    if (dti) return `xpath://${tag}[@data-testid=${wrap(dti)}]`;
    if (al) return `xpath://${tag}[@aria-label=${wrap(al)}]`;
    if (nm) return `xpath://${tag}[@name=${wrap(nm)}]`;
    if (ph) return `xpath://${tag}[@placeholder=${wrap(ph)}]`;
    const text = (e.textContent || '').trim().slice(0, 60);
    if (text && ['button', 'a', 'h1','h2','h3','h4','h5','h6'].includes(tag)) {
        return `xpath://${tag}[normalize-space()=${wrap(text)}]`;
    }
    // Last-resort: any unique class token
    const cls = (e.getAttribute('class') || '').split(/\s+/);
    for (const c of cls) {
        if (c.length >= 4 && c.length <= 40 && !c.startsWith('oxd-input--')) {
            const sel = `//${tag}[contains(@class,'${c}')]`;
            try {
                const cnt = document.evaluate(`count(${sel})`, document, null,
                    XPathResult.NUMBER_TYPE, null).numberValue;
                if (cnt === 1) return `xpath:${sel}`;
            } catch (err) {}
        }
    }
    return '';
}

const all = document.querySelectorAll(
    'input, button, textarea, select, a[href], ' +
    '[role="button"], [role="tab"], h1, h2, h3, h4, h5, h6'
);

// First pass: exact label + role match
for (const e of all) {
    if (!e.offsetParent && e.getClientRects().length === 0) continue;
    if (e.disabled || e.hasAttribute('disabled')) continue;
    if (e.hasAttribute('readonly')) continue;
    if (inferRole(e) !== wantRole) continue;
    if (maxLabel(inferLabel(e)) !== wantLabel) continue;
    const sel = buildSelector(e);
    if (sel) return sel;
}

// Second pass: case-insensitive substring match (more forgiving for i18n)
const lowerWant = wantLabel.toLowerCase();
for (const e of all) {
    if (!e.offsetParent && e.getClientRects().length === 0) continue;
    if (e.disabled || e.hasAttribute('disabled')) continue;
    if (e.hasAttribute('readonly')) continue;
    if (inferRole(e) !== wantRole) continue;
    const lbl = maxLabel(inferLabel(e)).toLowerCase();
    if (!lbl) continue;
    if (lbl.indexOf(lowerWant) === -1 && lowerWant.indexOf(lbl) === -1) continue;
    const sel = buildSelector(e);
    if (sel) return sel;
}

return '';
"""


def heal_selector_by_label(label: str, role: str) -> str:
    """
    Robot keyword: find a fresh selector for an element matching (label, role)
    in the live browser page. Returns "" when no match (caller decides — the
    Smart wrappers then let the original failure surface to keep the test
    honest).

    BuiltIn is imported lazily so this module can be imported in non-Robot
    contexts (smoke tests, static analysis) without requiring `robot` to be
    installed at module-load time.
    """
    try:
        from robot.libraries.BuiltIn import BuiltIn
    except Exception as e:
        print(f"[healer] Robot not available: {e}")
        return ""
    try:
        sl = BuiltIn().get_library_instance("SeleniumLibrary")
        driver = sl.driver
    except Exception as e:
        BuiltIn().log(f"Heal Selector By Label: could not access driver: {e}", level="WARN")
        return ""
    try:
        result = driver.execute_script(_HEAL_JS, label or "", role or "")
        if result:
            BuiltIn().log(f"Heal Selector By Label: '{label}' ({role}) -> {result}", level="INFO")
            return result
        BuiltIn().log(f"Heal Selector By Label: no match for '{label}' ({role})", level="WARN")
        return ""
    except Exception as e:
        BuiltIn().log(f"Heal Selector By Label failed for '{label}' ({role}): {e}", level="WARN")
        return ""
