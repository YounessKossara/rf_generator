"""
Catalog-based constrained planner (Phase A primary path).
Asks the LLM for a structured JSON plan referencing only catalog IDs,
then renders it deterministically via step_renderer.
"""

import json as _json
import re as _re
from langchain.messages import SystemMessage, HumanMessage
from rf_agent.infrastructure.llm import get_smart_llm, invoke_with_retry
from rf_agent.rendering.step_renderer import render_robot_test, UnknownIdError, ALLOWED_KEYWORDS


def _slim_catalog(cat: dict) -> dict:
    if not cat:
        return {}
    return {
        "url":   cat.get("url", ""),
        "title": cat.get("title", ""),
        "elements": [
            {"id": e["id"], "role": e["role"], "label": e.get("label", "")}
            for e in cat.get("elements", [])
        ],
        "nav": [
            {"id": n["id"], "label": n.get("label", "")}
            for n in cat.get("nav", [])
        ],
    }


_CATALOG_SYSTEM_PROMPT = """You are a QA test planner. You will receive a test
case description and a CATALOG of every interactive element on the relevant
page(s). You MUST plan the test by emitting STRUCTURED JSON whose steps
reference IDs from the catalog.

ABSOLUTE RULES:
1. You may ONLY reference selector_id / nav_id values that EXIST in the catalog.
2. You may NEVER write raw selectors (no xpath:, no css:, no class names).
3. You may NEVER invent IDs not in the catalog.
4. If the test cannot be expressed with the IDs available, return an empty
   "steps" list — the system will gracefully fall back. Do NOT guess.
5. Return ONLY a valid JSON object, no markdown fences, no commentary.

ACTION VOCABULARY (these are the only allowed `keyword` values):
  open_app_and_login   { username, password }
  open_browser_only    {}
  go_to                { nav_id }                  - navigate via a catalog nav entry
  click                { selector_id }
  input                { selector_id, value }
  select               { selector_id, value }
  wait_visible         { selector_id }
  wait_page_contains   { text }
  page_should_contain  { text }
  page_should_not_contain { text }
  element_should_contain  { selector_id, text }
  get_element_count    { selector_id, var, expected? }
  assert_url_contains  { text }
  sleep                { seconds }  e.g. "2s"
  screenshot           { name }     - e.g. "TC-001_step3"
  log                  { message }

OUTPUT SHAPE (strict):
{
  "test_id": "TC-XXX",
  "title": "...",
  "documentation": "...",
  "steps": [
    { "keyword": "open_app_and_login", "username": "...", "password": "..." },
    { "keyword": "go_to", "nav_id": "nav_admin" },
    { "keyword": "input", "selector_id": "in_search_username", "value": "Admin" },
    { "keyword": "click", "selector_id": "btn_search" },
    { "keyword": "screenshot", "name": "TC-001_final" }
  ]
}

If a test needs to verify "logged in", the open_app_and_login keyword already
waits for the success indicator — do NOT add another verification step right
after it. Proceed to the actual test-specific action."""


def _llm_plan_one(test_case: dict, catalog: dict,
                  default_user: str, default_pass: str) -> dict:
    """
    Ask the LLM to produce a structured plan for ONE test, given the slim
    catalog. Returns the parsed JSON dict, or {} on any failure.
    """
    slim = _slim_catalog(catalog)
    catalog_json = _json.dumps(slim, ensure_ascii=False, indent=None, separators=(",", ":"))

    tc_id = test_case.get("id", "TC-???")
    title = test_case.get("title", "")
    pre = test_case.get("preconditions", [])
    if isinstance(pre, list):
        pre_text = "\n".join(f"  - {p}" for p in pre if str(p).strip())
    else:
        pre_text = f"  - {pre}" if pre else ""
    steps = test_case.get("steps", [])
    steps_text = ""
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict):
            steps_text += f"  {i}. {s.get('action','')} -> {s.get('expected','')}\n"
        else:
            steps_text += f"  {i}. {s}\n"
    expected = test_case.get("expected", "")

    creds_hint = ""
    if default_user and default_pass:
        creds_hint = (f"\nDEFAULT CREDENTIALS for `open_app_and_login` "
                      f"(use these unless the test specifies different ones): "
                      f"username={default_user}, password={default_pass}\n")

    human_prompt = (
        f"Test case to plan:\n"
        f"ID: {tc_id}\n"
        f"Title: {title}\n"
        f"Preconditions:\n{pre_text or '  (none)'}\n"
        f"Steps:\n{steps_text or '  (none)'}\n"
        f"Expected: {expected}\n"
        f"{creds_hint}\n"
        f"CATALOG (the ONLY selector_id / nav_id values you may reference):\n"
        f"{catalog_json[:6000]}\n\n"
        f"Return the structured JSON plan now."
    )

    try:
        resp = invoke_with_retry(
            get_smart_llm,
            [SystemMessage(content=_CATALOG_SYSTEM_PROMPT),
             HumanMessage(content=human_prompt)],
        )
        plan = _parse_plan_json(resp.content)
        if not isinstance(plan, dict) or not plan:
            print(f"   ⚠️  [PLAN] {tc_id}: could not extract structured plan from response")
            return {}
        plan.setdefault("test_id", tc_id)
        plan.setdefault("title", title)
        return plan
    except Exception as e:
        print(f"   ⚠️  [PLAN] LLM planning failed for {tc_id}: {e}")
        return {}


def _parse_plan_json(content: str) -> dict:
    """
    Robust JSON extraction for LLM planner output. Handles markdown fences,
    trailing junk, and extra data after valid JSON.
    """
    if not content:
        return {}
    content = content.strip()
    if content.startswith("```"):
        content = "\n".join(content.split("\n")[1:])
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        content = content.strip()

    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        pass

    start = content.find('{')
    if start == -1:
        return {}
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i in range(start, len(content)):
        ch = content[i]
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end > start:
        candidate = content[start:end]
        try:
            return _json.loads(candidate)
        except _json.JSONDecodeError:
            pass

    for cut in range(len(content), start, -1):
        try:
            return _json.loads(content[start:cut])
        except _json.JSONDecodeError:
            continue
    return {}


def _validate_plan_against_catalog(plan: dict, catalog: dict) -> bool:
    """
    Sanity-check the LLM's structured plan BEFORE rendering. Returns False if
    any selector_id / nav_id is missing or any keyword is unknown.
    """
    if not plan or not isinstance(plan, dict):
        return False
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    selectors = {e["id"] for e in catalog.get("elements", []) if e.get("id")}
    navs = {n["id"] for n in catalog.get("nav", []) if n.get("id")}
    for step in steps:
        if not isinstance(step, dict):
            return False
        kw = step.get("keyword", "")
        if kw not in ALLOWED_KEYWORDS:
            return False
        sid = step.get("selector_id")
        nid = step.get("nav_id")
        if sid and sid not in selectors:
            return False
        if nid and nid not in navs:
            return False
        if kw == "input" and step.get("value") is None:
            return False
    return True


def _try_catalog_plan(test_case: dict, catalog: dict,
                      default_user: str, default_pass: str) -> str:
    """
    Attempt the catalog-based planning path for ONE test. Returns the rendered
    Robot Framework test body on success, or "" on any failure.
    """
    if not catalog or not catalog.get("elements"):
        return ""
    plan = _llm_plan_one(test_case, catalog, default_user, default_pass)
    if not _validate_plan_against_catalog(plan, catalog):
        return ""
    try:
        result = render_robot_test(catalog, plan)
        return result.body
    except UnknownIdError as e:
        print(f"   ⚠️  [RENDER] {test_case.get('id', '?')}: {e}")
        return ""
    except Exception as e:
        print(f"   ⚠️  [RENDER] {test_case.get('id', '?')}: unexpected: {e}")
        return ""
