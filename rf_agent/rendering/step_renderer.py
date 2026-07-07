"""
Step Renderer — deterministic mapping from structured plan to Robot Framework.

The LLM emits structured steps that reference `selector_id` / `nav_id` values.
This module looks each ID up in the catalog and emits the corresponding Robot
Framework keyword lines. No LLM. No string surgery on raw HTML.

If a structured step references an ID that's NOT in the catalog, the renderer
raises `UnknownIdError`. Phase A's safety net catches this and falls back to
the legacy LLM-text path for THAT test only.
"""

from dataclasses import dataclass
from typing import Optional


# ── Action vocabulary ─────────────────────────────────────────────────────────

ALLOWED_KEYWORDS = {
    "open_app_and_login",   # username, password
    "open_browser_only",
    "go_to",                # nav_id OR url
    "click",                # selector_id
    "input",                # selector_id, value
    "select",               # selector_id, value
    "wait_visible",         # selector_id
    "wait_page_contains",   # text
    "page_should_contain",  # text
    "page_should_not_contain",  # text
    "element_should_contain",   # selector_id, text
    "get_element_count",        # selector_id, var (assert equal optional)
    "assert_url_contains",      # text
    "sleep",                    # seconds (string like "2s")
    "screenshot",               # name (without _final suffix)
    "log",                      # message
}


class UnknownIdError(Exception):
    """The structured plan referenced a selector_id / nav_id not in the catalog."""


@dataclass
class RenderResult:
    body: str
    unknown_refs: list  # list of (step_index, kind, missing_id) — for telemetry


# ── Catalog lookup helpers ────────────────────────────────────────────────────

def _index_catalog(catalog: dict) -> tuple:
    """
    Build:
      - {id: selector}  — for direct lookup
      - {nav_id: href}  — for Go To
      - {id: element}   — full catalog entry (label/role) — used by Smart wrappers
                          so the runtime healer can find a replacement by
                          (label, role) when the primary selector fails.
    """
    if not catalog:
        return {}, {}, {}
    elements = catalog.get("elements", [])
    selectors = {e["id"]: e["selector"] for e in elements if e.get("id")}
    full = {e["id"]: e for e in elements if e.get("id")}
    navs = {n["id"]: n["href"] for n in catalog.get("nav", []) if n.get("id") and n.get("href")}
    return selectors, navs, full


def _rf_escape(s: str) -> str:
    """Escape a string so it can be safely inlined as a Robot Framework argument."""
    if s is None:
        return ""
    # Robot uses backslash as escape; also collapse newlines/tabs to spaces.
    return str(s).replace("\\", "\\\\").replace("\n", " ").replace("\t", " ").strip()


# ── Per-keyword renderers ─────────────────────────────────────────────────────

def _render_step(step: dict, selectors: dict, navs: dict,
                 full: dict, tc_id: str) -> str:
    """
    Render a single structured step to one or more indented RF lines.

    Interaction keywords (click / input / select / wait_visible) emit Smart
    wrappers — `Smart Click`, `Smart Input`, etc. — that the header builder
    defines once per test file. The wrappers retry with a healed selector
    (via the runtime healer library) if the primary selector isn't visible.
    """
    kw = step.get("keyword", "")
    if kw not in ALLOWED_KEYWORDS:
        raise UnknownIdError(f"unknown keyword '{kw}'")

    INDENT = "    "
    if kw == "open_app_and_login":
        user = step.get("username", "")
        pwd = step.get("password", "")
        return f"{INDENT}Open App And Login    {user}    {pwd}"

    if kw == "open_browser_only":
        return f"{INDENT}Open Browser Only"

    if kw == "go_to":
        nav_id = step.get("nav_id", "")
        url = step.get("url", "")
        if nav_id:
            if nav_id not in navs:
                raise UnknownIdError(f"unknown nav_id '{nav_id}'")
            return f"{INDENT}Go To    {navs[nav_id]}\n{INDENT}Sleep    2s"
        if url:
            return f"{INDENT}Go To    {url}\n{INDENT}Sleep    2s"
        raise UnknownIdError("go_to without nav_id or url")

    if kw in ("click", "input", "select", "wait_visible", "element_should_contain",
              "get_element_count"):
        sid = step.get("selector_id", "")
        if not sid or sid not in selectors:
            raise UnknownIdError(f"unknown selector_id '{sid}'")
        sel = selectors[sid]
        elem = full.get(sid, {})
        label = _rf_escape(elem.get("label", ""))
        role = _rf_escape(elem.get("role", ""))

        if kw == "click":
            return f"{INDENT}Smart Click    {label}    {role}    {sel}"
        if kw == "input":
            value = step.get("value", "") or "${EMPTY}"
            return f"{INDENT}Smart Input    {label}    {role}    {sel}    {value}"
        if kw == "select":
            value = step.get("value", "")
            return f"{INDENT}Smart Select By Label    {label}    {role}    {sel}    {value}"
        if kw == "wait_visible":
            return f"{INDENT}Smart Wait    {label}    {role}    {sel}"
        if kw == "element_should_contain":
            text = step.get("text", "")
            return (f"{INDENT}Smart Wait    {label}    {role}    {sel}\n"
                    f"{INDENT}Element Should Contain    {sel}    {text}")
        if kw == "get_element_count":
            var = step.get("var", "count")
            expected = step.get("expected")
            out = f"{INDENT}${{{var}}}=    Get Element Count    {sel}"
            if expected is not None:
                out += f"\n{INDENT}Should Be Equal As Integers    ${{{var}}}    {expected}"
            return out

    if kw == "wait_page_contains":
        text = step.get("text", "")
        return f"{INDENT}Wait Until Page Contains    {text}    15s"
    if kw == "page_should_contain":
        text = step.get("text", "")
        return f"{INDENT}Page Should Contain    {text}"
    if kw == "page_should_not_contain":
        text = step.get("text", "")
        return f"{INDENT}Page Should Not Contain    {text}"
    if kw == "assert_url_contains":
        text = step.get("text", "")
        return (f"{INDENT}${{_url}}=    Get Location\n"
                f"{INDENT}Should Contain    ${{_url}}    {text}")
    if kw == "sleep":
        secs = step.get("seconds", "2s")
        return f"{INDENT}Sleep    {secs}"
    if kw == "screenshot":
        name = step.get("name") or f"{tc_id}_final"
        # Robot will reject filename chars; keep this safe
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return f"{INDENT}Capture Page Screenshot    {safe}.png"
    if kw == "log":
        msg = step.get("message", "")
        return f"{INDENT}Log    {msg}"

    raise UnknownIdError(f"unhandled keyword '{kw}'")


# ── Public renderer ───────────────────────────────────────────────────────────

def render_robot_test(catalog: dict, plan: dict) -> RenderResult:
    """
    Render a single structured test case plan to Robot Framework text.

    plan shape:
        {
            "test_id": "TC-001",
            "title":   "...",
            "documentation": "...",   (optional)
            "steps": [ { keyword + args }, ... ]
        }

    On unknown selector_id / nav_id / keyword the renderer raises UnknownIdError
    so the caller (rf_generator) can fall back to the legacy text-LLM path
    for that test only. SauceDemo never reaches the failure branch because
    its catalog covers every selector the LLM picks.
    """
    if not plan:
        raise UnknownIdError("empty plan")

    tc_id = plan.get("test_id", "TC-???")
    title = plan.get("title", "")
    doc = plan.get("documentation", title)
    steps = plan.get("steps", [])
    if not isinstance(steps, list) or not steps:
        raise UnknownIdError(f"{tc_id}: steps must be a non-empty list")

    selectors, navs, full = _index_catalog(catalog)
    unknown_refs = []

    lines = [f"{tc_id} {title}".rstrip()]
    if doc:
        lines.append(f"    [Documentation]    {doc}")

    for i, step in enumerate(steps):
        try:
            rendered = _render_step(step, selectors, navs, full, tc_id)
            lines.append(rendered)
        except UnknownIdError as e:
            unknown_refs.append((i, step.get("keyword", ""), str(e)))
            # Bail out — better to fail this test cleanly than render a
            # broken hybrid. Caller will fall back to legacy generation.
            raise

    # Final screenshot if the plan didn't already include one
    body = "\n".join(lines)
    if "Capture Page Screenshot" not in body:
        body += f"\n    Capture Page Screenshot    {tc_id}_final.png"

    return RenderResult(body=body, unknown_refs=unknown_refs)


# ── Catalog merge helper (multi-page tests) ───────────────────────────────────

def merge_catalogs(*catalogs) -> dict:
    """
    Merge multiple page catalogs into one. Used when a test interacts with
    elements across several pages (e.g. login page + module page). Later
    catalogs override earlier ones on ID collision.
    """
    out = {"url": "", "title": "", "elements": [], "nav": []}
    seen_e = set()
    seen_n = set()
    for cat in catalogs:
        if not cat:
            continue
        for e in cat.get("elements", []):
            eid = e.get("id")
            if eid and eid not in seen_e:
                seen_e.add(eid)
                out["elements"].append(e)
        for n in cat.get("nav", []):
            nid = n.get("id")
            if nid and nid not in seen_n:
                seen_n.add(nid)
                out["nav"].append(n)
    return out
