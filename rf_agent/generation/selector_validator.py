"""
Selector validation — strips emoji hallucinations from locators and
downgrades class/id/placeholder selectors that don't appear in the captured DOM.
"""

import re as _re


_EMOJI_RX = _re.compile(
    "["
    "\U0001F300-\U0001F6FF"   # symbols & pictographs
    "\U0001F900-\U0001F9FF"   # supplemental symbols
    "\U0001FA70-\U0001FAFF"   # extended pictographs
    "\U00002600-\U000027BF"   # misc symbols & dingbats
    "\U0001F1E6-\U0001F1FF"   # flags
    "︀-️"           # variation selectors (often follow emojis)
    "‍"                  # zero-width joiner (in emoji sequences)
    "⃣"                  # combining enclosing keycap
    "]+",
    flags=_re.UNICODE,
)


def _strip_emojis_from_locators(code: str) -> str:
    """
    Remove emojis from inside RF locator strings only. We rewrite each xpath
    `contains(text(),'…')` / `normalize-space(),'…'` literal by purging
    pictographs, then strip degenerate empty contains() calls.
    """
    def _fix_literal(m: _re.Match) -> str:
        before, quote, content, closing = m.group(1), m.group(2), m.group(3), m.group(4)
        new_content = _EMOJI_RX.sub("", content).strip()
        new_content = _re.sub(r'\s{2,}', ' ', new_content)
        return f"{before}{quote}{new_content}{closing}"

    pattern = _re.compile(
        r"(contains\((?:text\(\)|normalize-space\(\))\s*,\s*|normalize-space\(\)\s*=\s*)"
        r"(['\"])(.*?)(\2)",
        _re.DOTALL,
    )
    new_code = pattern.sub(_fix_literal, code)

    out_lines = []
    for line in new_code.split("\n"):
        if "xpath:" in line or "css:" in line:
            line = _EMOJI_RX.sub("", line)
        out_lines.append(line)
    return "\n".join(out_lines)


_GENERIC_ATTR_VALUES = {
    "submit", "button", "text", "password", "email", "search", "checkbox", "radio",
    "hidden", "file", "tel", "url", "number",  # input types
    "main", "navigation", "dialog", "alert",   # ARIA roles
}


def _extract_selector_tokens(line: str) -> list:
    """
    Return distinguishing literal values used in a Selenium locator on this
    line: things like the class name, id value, placeholder text, data-test
    value, aria-label. Generic input types are EXCLUDED.
    """
    tokens = []
    for m in _re.finditer(
        r"@(class|id|placeholder|data-test|data-testid|data-test-id|name|aria-label)\s*=\s*"
        r"['\"]([^'\"]+)['\"]",
        line,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if not value or value.lower() in _GENERIC_ATTR_VALUES:
            continue
        tokens.append((attr, value))
    for m in _re.finditer(
        r"contains\(\s*@(class|id|placeholder|data-test|data-testid|name|aria-label)\s*,\s*"
        r"['\"]([^'\"]+)['\"]\s*\)",
        line,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if not value or value.lower() in _GENERIC_ATTR_VALUES:
            continue
        tokens.append((attr, value))
    return tokens


def _selector_grounded(line: str, dom_blob: str) -> tuple:
    """
    Returns (is_grounded, suspicious_token).

    A locator is "grounded" when every distinguishing token it references
    appears IN THE SAME ATTRIBUTE in the captured DOM.
    """
    tokens = _extract_selector_tokens(line)
    contains_tokens = []
    for m in _re.finditer(
        r"contains\(\s*@(class|id|placeholder|data-test|data-testid|name|aria-label)\s*,\s*"
        r"['\"]([^'\"]+)['\"]\s*\)",
        line,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if value and value.lower() not in _GENERIC_ATTR_VALUES:
            contains_tokens.append((attr, value))

    if not tokens and not contains_tokens:
        return True, ""

    for attr, value in tokens:
        if attr == "class":
            pattern = (rf'class\s*=\s*[\'"][^\'"]*\b'
                       rf'{_re.escape(value)}\b[^\'"]*[\'"]')
            if not _re.search(pattern, dom_blob):
                return False, f"@{attr}='{value}'"
        else:
            pattern = rf'\b{attr}\s*=\s*[\'"]{_re.escape(value)}[\'"]'
            if not _re.search(pattern, dom_blob):
                return False, f"@{attr}='{value}'"

    for attr, value in contains_tokens:
        if attr == "class":
            pattern = (rf'class\s*=\s*[\'"][^\'"]*'
                       rf'{_re.escape(value)}[^\'"]*[\'"]')
        else:
            pattern = (rf'\b{attr}\s*=\s*[\'"][^\'"]*'
                       rf'{_re.escape(value)}[^\'"]*[\'"]')
        if not _re.search(pattern, dom_blob):
            return False, f"contains(@{attr},'{value}')"

    return True, ""


def _step_text_for_line(line_idx: int, lines: list, batch_steps_text: str = "") -> str:
    """
    Walk backwards from the locator line to find the best visible-text hint
    to use as a text-based fallback.
    """
    for i in range(line_idx, max(line_idx - 6, -1), -1):
        cur = lines[i]
        m = _re.search(r"['\"]([A-Z][^'\"]{2,40})['\"]", cur)
        if m:
            return m.group(1).strip()

    for i in range(line_idx, -1, -1):
        cur = lines[i]
        m = _re.match(r'\s*\[Documentation\]\s+(.+)$', cur, _re.IGNORECASE)
        if m:
            words = m.group(1).strip().split()[:5]
            if words:
                return " ".join(words)
        m = _re.match(r'^(TC-\d+)\s+(.+)$', cur.rstrip())
        if m:
            words = m.group(2).strip().split()[:5]
            if words:
                return " ".join(words)
            break
    return ""


def _downgrade_to_text_locator(line: str, fallback_text: str) -> str:
    """
    Replace the entire `xpath:...` / `css:...` token on this line with a
    text-based xpath built from `fallback_text`.
    """
    if not fallback_text:
        return line
    safe_text = fallback_text.replace("'", "")
    new_locator = f"xpath://*[contains(normalize-space(),'{safe_text}')]"
    return _re.sub(r"(xpath:|css:)[^\s]+", new_locator, line, count=1)


def _validate_selectors_against_dom(test_body: str, dom_blob: str) -> str:
    """
    Post-LLM safety net: for each generated locator that references a specific
    class/id/placeholder/data-test/aria-label value, check whether that value
    appears in the captured DOM blob. If NOT, replace with a text-based xpath.
    """
    if not dom_blob:
        return test_body

    out = []
    downgraded = 0
    dom_blob_lower = dom_blob.lower()
    lines = test_body.split("\n")
    for i, line in enumerate(lines):
        if "xpath:" not in line and "css:" not in line:
            out.append(line)
            continue
        ok, bad = _selector_grounded(line, dom_blob)
        if ok:
            out.append(line)
            continue
        fallback = _step_text_for_line(i, lines)
        if not fallback:
            out.append(line)
            continue
        if fallback.lower() not in dom_blob_lower:
            out.append(line)
            continue
        new_line = _downgrade_to_text_locator(line, fallback)
        if new_line != line:
            downgraded += 1
        out.append(new_line)

    if downgraded:
        try:
            print(f"   [VALIDATOR] Downgraded {downgraded} hallucinated locator(s) to text-based xpath.")
        except Exception:
            pass
    return "\n".join(out)
