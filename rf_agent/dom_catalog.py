"""
DOM Catalog Extractor — the contract between live DOM and the LLM planner.

Given a Playwright page, produce a structured catalog of every interactive
element on that page. Every selector in the catalog is VERIFIED to match at
least one element at extraction time, so the planner can never pick a
hallucinated selector — only choose from a bounded action space.

This is the heart of the "constrained planner + deterministic executor"
architecture. The LLM consumes the catalog and emits selector_ids; the
renderer maps ids back to selectors. There is no path for the LLM to invent
a selector.
"""

import asyncio
import concurrent.futures
import re
from typing import Optional


# ── Synthetic ID helpers ──────────────────────────────────────────────────────

_SAFE_ID_RX = re.compile(r"[^a-z0-9]+")


def _slug(s: str, max_len: int = 28) -> str:
    """Lowercase alphanumeric slug for synthetic IDs (in_search_username, etc.)."""
    if not s:
        return ""
    s = _SAFE_ID_RX.sub("_", s.lower()).strip("_")
    return s[:max_len] or ""


def _build_synthetic_id(role: str, label: str, fallback_index: int) -> str:
    """
    Produce a stable synthetic ID for the catalog. Roles map to short prefixes
    so structured plans stay readable: in_, btn_, sel_, lnk_, chk_, etc.
    """
    role_prefix = {
        "text_input":     "in",
        "password_input": "in_pw",
        "textarea":       "ta",
        "select":         "sel",
        "checkbox":       "chk",
        "radio":          "radio",
        "button":         "btn",
        "link":           "lnk",
        "tab":            "tab",
        "heading":        "h",
    }.get(role, "el")
    slug = _slug(label) or f"x{fallback_index}"
    return f"{role_prefix}_{slug}"


# ── Verified selector derivation ──────────────────────────────────────────────

async def _verify(page, sel: str) -> int:
    """Return the number of elements matching `sel`, 0 on any error."""
    try:
        # Convert RF-style 'xpath:...' to Playwright 'xpath=...'
        pw_sel = sel
        if sel.startswith("xpath:"):
            pw_sel = "xpath=" + sel[6:]
        return await page.locator(pw_sel).count()
    except Exception:
        return 0


async def _derive_stable_selector(page, handle, role: str) -> Optional[str]:
    """
    Try selectors in priority order until one matches count() == 1.
    Returns the RF-style selector string, or None when nothing stable was found.

    Priority order (stability-first, NOT readability-first):
      1. @id (most stable — usually deterministic)
      2. @data-test / @data-testid (test-author intent)
      3. @aria-label (a11y — usually static)
      4. @name (form-element internal name — VERY stable for inputs)
      5. @placeholder (CAN drift with i18n; ranked below @name)
      6. normalize-space()=<text> for buttons/links/headings with stable text
      7. contains(@class,'<class>') if a single class token is unique

    No positional fallback — `(//input)[29]` is too brittle (shifts whenever
    the SPA renders one extra element). Elements without a stable selector
    are EXCLUDED from the catalog entirely. That's the right failure mode:
    an unselectable element is one we cannot reliably script against, so the
    LLM should not be offered it as an option.
    """
    tag = (await handle.evaluate("e => e.tagName")).lower()

    attrs = await handle.evaluate("""
        e => ({
            id: e.getAttribute('id'),
            datatest: e.getAttribute('data-test'),
            datatestid: e.getAttribute('data-testid'),
            arialabel: e.getAttribute('aria-label'),
            placeholder: e.getAttribute('placeholder'),
            name: e.getAttribute('name'),
            type: e.getAttribute('type'),
            cls: e.getAttribute('class') || '',
            text: (e.textContent || '').trim().slice(0, 60),
            href: e.getAttribute('href'),
        })
    """)

    candidates = []

    if attrs["id"]:
        candidates.append(f"xpath://{tag}[@id='{attrs['id']}']")
        candidates.append(f"xpath://*[@id='{attrs['id']}']")
    if attrs["datatest"]:
        candidates.append(f"xpath://{tag}[@data-test='{attrs['datatest']}']")
    if attrs["datatestid"]:
        candidates.append(f"xpath://{tag}[@data-testid='{attrs['datatestid']}']")
    if attrs["arialabel"]:
        candidates.append(f"xpath://{tag}[@aria-label='{attrs['arialabel']}']")
    # @name ranks ABOVE @placeholder — form-element internal names are more
    # stable than placeholders, which can shift with i18n or late-binding.
    if attrs["name"]:
        candidates.append(f"xpath://{tag}[@name='{attrs['name']}']")
    if attrs["placeholder"]:
        candidates.append(f"xpath://{tag}[@placeholder='{attrs['placeholder']}']")

    # Visible-text selector for elements where text content is the identity
    if role in ("button", "link", "tab", "heading") and attrs["text"]:
        t = attrs["text"]
        if "'" in t and '"' not in t:
            candidates.append(f'xpath://{tag}[normalize-space()="{t}"]')
        else:
            candidates.append(f"xpath://{tag}[normalize-space()='{t}']")

    # Class-token selectors (only single distinctive class)
    for cls in (attrs["cls"] or "").split():
        if 3 <= len(cls) <= 40 and not cls.startswith("oxd-input--"):
            candidates.append(f"xpath://{tag}[contains(@class,'{cls}')]")

    # First candidate with count()==1 wins
    for sel in candidates:
        if await _verify(page, sel) == 1:
            return sel

    # No stable selector found — return None so this element is excluded from
    # the catalog. Smaller catalog, higher quality. The LLM cannot pick a
    # fragile positional fallback because no such option is offered.
    return None


# ── Role + label inference (this is where bounded heuristics live) ────────────

async def _infer_role(handle) -> str:
    """Map a DOM element handle to one of the catalog's role tags."""
    info = await handle.evaluate("""
        e => ({
            tag: e.tagName.toLowerCase(),
            type: (e.getAttribute('type') || '').toLowerCase(),
            role: (e.getAttribute('role') || '').toLowerCase(),
            contenteditable: e.getAttribute('contenteditable'),
        })
    """)
    tag = info["tag"]
    typ = info["type"]
    if tag == "input":
        if typ == "password":
            return "password_input"
        if typ == "checkbox":
            return "checkbox"
        if typ == "radio":
            return "radio"
        if typ in ("submit", "button"):
            return "button"
        # text, email, search, number, tel, url, date, '' all collapse to text_input
        return "text_input"
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag == "button":
        return "button"
    if tag == "a":
        return "link"
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return "heading"
    if info["role"] == "tab":
        return "tab"
    if info["role"] == "button":
        return "button"
    return "text_input" if info["contenteditable"] else "button"


async def _infer_label(handle, role: str) -> str:
    """
    Human label for an element using W3C-grounded accessible-name signals.

    Priority order (each step is a STRUCTURAL relationship between the input
    and its label — never a free-form text proximity walk):

      1. aria-label
      2. aria-labelledby → resolve referenced element IDs
      3. element.labels (W3C HTMLLabelElement collection — works when a
         <label for=ID> or wrapping <label> is properly associated)
      4. closest('label') ancestor (implicit label)
      5. Same form-group: walk up to find the nearest "form group" container
         (class containing input-group / form-group / form-field / control /
         wrapper, OR role="group") and look for a <label>-like child WITHIN
         that container. Strictly bounded — never crosses form-group walls.
      6. placeholder
      7. Visible text content (buttons / links / headings / tabs)
      8. title attribute
      9. name attribute (last resort)

    KEY CHANGE FROM THE OLD VERSION: removed the unbounded
    parent.previousElementSibling walk that grabbed labels from NEIGHBORING
    form groups. That was producing labels like "Username" on an autocomplete
    input that just happened to live near the Username search box.
    """
    return await handle.evaluate(r"""
        (e) => {
            const max = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
            const tag = e.tagName.toLowerCase();

            // 1. aria-label
            const aria = e.getAttribute('aria-label');
            if (aria && aria.trim()) return max(aria);

            // 2. aria-labelledby — resolve referenced element IDs
            const lb = e.getAttribute('aria-labelledby');
            if (lb) {
                const parts = lb.split(/\s+/).map(id => {
                    const ref = document.getElementById(id);
                    return ref ? (ref.textContent || '').trim() : '';
                }).filter(Boolean);
                if (parts.length) return max(parts.join(' '));
            }

            // 3. e.labels — W3C HTMLLabelElement.labels collection (form elements)
            try {
                if (e.labels && e.labels.length > 0) {
                    const t = (e.labels[0].textContent || '').trim();
                    if (t) return max(t);
                }
            } catch (err) { /* not a labellable element */ }

            // 4. <label> ancestor (implicit-label form)
            const lab = e.closest('label');
            if (lab && lab !== e) {
                const t = (lab.textContent || '').trim();
                if (t) return max(t);
            }

            // 5. Same form-group label search.
            //    Walk up the ancestor chain (max 6). At EACH ancestor that
            //    looks like a form-group, try to find a <label>-like child;
            //    if found AND it isn't an ancestor of `e`, use it. Keep
            //    walking otherwise — the BEM wrapper `oxd-input-group__input`
            //    is recognised as form-group-ish but has no label inside;
            //    its parent `oxd-input-group` is the real form-group.
            const isGroup = (n) => {
                if (n.getAttribute('role') === 'group') return true;
                const cls = (n.className || '').toString();
                if (!cls) return false;
                // Match form-group tokens as whole class-name suffixes only,
                // stripping BEM modifier `--xxx`. This rejects `oxd-input-group__input`
                // (BEM child wrapper) but accepts `oxd-input-group`, `form-group--required`.
                const tokens = ['input-group', 'form-group', 'form-field', 'form-item',
                                'field-wrapper', 'control-group', 'form-control'];
                for (const c of cls.split(/\s+/)) {
                    const base = c.toLowerCase().split('--')[0];  // strip BEM modifier
                    for (const t of tokens) {
                        if (base === t || base.endsWith('-' + t) || base.endsWith('_' + t)) {
                            return true;
                        }
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
                    if (t) return max(t);
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

            // 6. placeholder
            const ph = e.getAttribute('placeholder');
            if (ph && ph.trim()) return max(ph);

            // 7. Visible text for content-bearing elements
            if (['button', 'a', 'h1','h2','h3','h4','h5','h6'].includes(tag)) {
                const t = (e.textContent || '').trim();
                if (t) return max(t);
            }

            // 8. title
            const ti = e.getAttribute('title');
            if (ti && ti.trim()) return max(ti);

            // 9. name (last resort — uses the form-field internal name)
            const nm = e.getAttribute('name');
            if (nm && nm.trim()) return max(nm);

            return '';
        }
    """)


# ── Catalog extractor (the main entry point) ──────────────────────────────────

async def extract_catalog(page) -> dict:
    """
    Visit every interactive element on `page` and produce the verified catalog.
    Returns:
        {
            "url": str,
            "title": str,
            "elements": [{"id","role","label","selector"}, ...],
            "nav":      [{"id","label","href"}, ...]
        }
    The catalog INVARIANT: every selector returns count() >= 1 here.
    """
    url = page.url
    try:
        title = await page.title()
    except Exception:
        title = ""

    # 1. Collect all candidate elements (one pass, single selector list)
    candidate_locator = page.locator(
        "input, button, textarea, select, "
        "a[href], "
        "[role='button'], [role='tab'], "
        "h1, h2, h3, h4, h5, h6"
    )
    count = await candidate_locator.count()
    handles = []
    for i in range(min(count, 400)):  # safety cap
        try:
            handles.append(candidate_locator.nth(i))
        except Exception:
            continue

    elements = []
    nav = []
    used_ids = set()
    fallback_index = 0

    for handle in handles:
        try:
            # Catalog invariant: every element must be (visible AND enabled).
            # Disabled/readonly inputs trigger InvalidElementStateException
            # when Robot tries to type into them. Off-screen elements aren't
            # interactable either.
            is_visible = await handle.is_visible()
            if not is_visible:
                continue
            try:
                is_enabled = await handle.is_enabled()
            except Exception:
                is_enabled = True
            if not is_enabled:
                continue
            # Readonly is a separate signal that is_enabled doesn't always
            # cover (e.g. <input readonly> may still report enabled=True).
            # ONLY check the HTML `readonly` attribute — `aria-readonly` is
            # set transiently by some frameworks (Vue.js) and produced
            # false-positives that emptied the login-page catalog.
            is_readonly = await handle.evaluate("e => e.hasAttribute('readonly')")
            if is_readonly:
                continue
        except Exception:
            continue

        try:
            role = await _infer_role(handle)
            label = (await _infer_label(handle, role)) or ""
            selector = await _derive_stable_selector(page, handle, role)
        except Exception:
            continue

        if not selector:
            # No stable selector found — exclude (catalog invariant: every
            # selector must verify and be stable). This is the right failure
            # mode: the LLM cannot be offered an unscriptable element.
            continue

        # Synthetic ID, dedupe by suffixing _2, _3, ...
        fallback_index += 1
        base_id = _build_synthetic_id(role, label, fallback_index)
        eid = base_id
        n = 2
        while eid in used_ids:
            eid = f"{base_id}_{n}"
            n += 1
        used_ids.add(eid)

        entry = {"id": eid, "role": role, "label": label[:80], "selector": selector}
        elements.append(entry)

        # Navigation entries (anchors with absolute internal hrefs)
        if role == "link":
            try:
                href = await handle.evaluate("e => e.href")  # always absolute
            except Exception:
                href = ""
            if href:
                nav.append({"id": eid, "label": label[:80], "href": href})

    return {
        "url": url,
        "title": title,
        "elements": elements,
        "nav": nav,
    }


# ── Convenience: thread-pooled wrapper for sync callers ──────────────────────

def extract_catalog_from_url_sync(base_url: str, post_login_steps=None,
                                   username: str = "", password: str = "",
                                   recipe: dict = None) -> dict:
    """
    Thread-pool wrapper: launches a Playwright context, performs an optional
    login + post-login navigation steps, then extracts the catalog of the
    final page. Returns {} on Playwright failure.

    post_login_steps: list of "goto" URLs to navigate through after login.
    The catalog is captured at the LAST URL visited.
    """
    async def _run() -> dict:
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            print(f"   ⚠️  [CATALOG] Playwright unavailable: {e}")
            return {}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(base_url, timeout=20000)
                await page.wait_for_timeout(2000)

                # Optional login
                if username and password and recipe:
                    from rf_agent.app_memory import rf_to_playwright
                    usr_sel = rf_to_playwright(recipe.get("username_selector", "")) or "input[type='text']"
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    btn_sel = rf_to_playwright(recipe.get("submit_selector", "")) or "button[type='submit']"
                    try:
                        pre_url = page.url
                        await page.locator(usr_sel).first.fill(username)
                        await page.locator(pwd_sel).first.fill(password)
                        await page.locator(btn_sel).first.click()
                        try:
                            await page.wait_for_url(
                                lambda u: ("auth/login" not in u
                                           and u.rstrip("/") != pre_url.rstrip("/")),
                                timeout=10000,
                            )
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                    except Exception as e:
                        print(f"   ⚠️  [CATALOG] Login error: {e}")

                # Post-login navigation
                for step_url in (post_login_steps or []):
                    try:
                        await page.goto(step_url, timeout=20000, wait_until="networkidle")
                        await page.wait_for_timeout(1500)
                    except Exception as e:
                        print(f"   ⚠️  [CATALOG] Nav to {step_url} failed: {e}")

                catalog = await extract_catalog(page)
                await browser.close()
                return catalog
        except Exception as e:
            print(f"   ⚠️  [CATALOG] extraction failed: {e}")
            return {}

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _run())
            return future.result(timeout=120)
    except Exception as e:
        print(f"   ⚠️  [CATALOG] outer wrapper failed: {e}")
        return {}
