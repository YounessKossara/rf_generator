"""
Shared helpers used by multiple discovery submodules.
"""

import re


def rf_to_playwright(rf_sel: str) -> str:
    """Convert a Robot Framework selector to Playwright-compatible format."""
    if not rf_sel:
        return rf_sel
    if rf_sel.startswith("xpath:"):
        return "xpath=" + rf_sel[6:]
    if rf_sel.startswith("css:"):
        return rf_sel[4:]
    return rf_sel


def _extract_interactive_elements(html: str) -> str:
    """
    Extract interactive + text-bearing element opening tags from full page HTML.
    Returns a compact string of button/input/select/a/option/h1-h6/p/label/span/div
    tags with their IDs, classes, placeholders and visible text.
    """
    results = []
    seen: set = set()

    for m in re.finditer(
        r'<(button|input|select|option|a|h[1-6]|p|label|textarea)\b([^>]{0,400}?)(/?>)((?:[^<]{0,120})?)',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, closing, inner = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        is_text_tag = tag.lower() in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'label')
        if not is_text_tag and not re.search(
            r'\b(id|class|placeholder|type|value|href|aria-label|data-test|name)\s*=',
            attrs, re.IGNORECASE
        ):
            continue
        if is_text_tag and not inner:
            continue
        entry = f"<{tag}{attrs}{closing}"
        if inner and closing == '>':
            entry += inner + f"</{tag}>"
        entry = entry[:320].strip()
        key = entry[:90]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    for m in re.finditer(
        r'<(span|div)\b([^>]{0,300}(?:id|class)=[^>]{0,200})>([^<]{0,80})',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, text = m.group(1), m.group(2), m.group(3).strip()
        if not re.search(r'\b(id|class)\s*=', attrs, re.IGNORECASE):
            continue
        if not text and len(attrs) < 30:
            continue
        entry = (f"<{tag}{attrs}>" + (text if text else ""))[:320].strip()
        key = entry[:90]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    if results:
        return "\n".join(results[:200])[:9000]
    return html[:4000]
