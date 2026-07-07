"""
Orchestrator — sole public entry point for RF code generation.
Coordinates discovery, catalog-based planning (Phase A), and legacy
batch generation (Phase B fallback).
"""

import re as _re
from langchain.messages import SystemMessage, HumanMessage
from rf_agent.infrastructure.llm import get_smart_llm, invoke_with_retry
from rf_agent.app_memory import (load_app_for_generation, save_app,
                                  discover_page_structure,
                                  discover_modules_batch, discover_catalogs_batch,
                                  cache_enabled)
from rf_agent.rendering.step_renderer import merge_catalogs
from rf_agent.generation.header_builder import _build_header
from rf_agent.generation.credential_extractor import _extract_default_credentials
from rf_agent.generation.module_classifier import (
    _looks_like_login_page, _fetch_page_html,
    _extract_nav_links, _classify_test_to_module,
)
from rf_agent.generation.catalog_planner import _try_catalog_plan
from rf_agent.generation.legacy_planner import (
    BASE_SYSTEM_PROMPT, _clean_batch_code,
    _backfill_truncated_tests, _enforce_go_to_per_test,
)
from rf_agent.generation.selector_validator import _validate_selectors_against_dom
from rf_agent.discovery.recipe import discover_login_recipe


def generate_rf_code(test_cases: list, base_url: str, raw_md: str = "") -> str:
    """
    Generate Robot Framework code in batches.

    Flow:
      1. Load or discover login recipe from app_memory.
      2. Build Settings + Variables + Keywords in Python (no LLM involvement).
      3. Ask LLM to generate ONLY test case bodies, calling Open App And Login.
      4. Clean each batch output to strip stray headers/separators.
      5. Concatenate header + cleaned batches.

    raw_md is the original markdown text — used to extract credentials from
    metadata lines that the structured parser may not preserve.
    """
    default_user, default_pass = _extract_default_credentials(test_cases, raw_md=raw_md)

    if cache_enabled():
        print("   📚 [MEMORY] RF_USE_CACHE=1 — reading cached recipe if available...")
    else:
        print("   🔄 [MEMORY] Cache disabled (default) — fresh discovery this run.")
    stored = load_app_for_generation(base_url)
    recipe = stored if stored else {}

    recipe_keys = ("username_selector", "password_selector", "submit_selector")
    needs_recipe = not recipe or all(not recipe.get(k) for k in recipe_keys)
    if needs_recipe:
        print("   🔍 Fetching login page HTML to discover recipe...")
        login_page_html = _fetch_page_html(base_url)
        if login_page_html or True:
            new_recipe = discover_login_recipe(login_page_html, base_url)
            if new_recipe and any(new_recipe.get(k) for k in recipe_keys):
                recipe = {**recipe, **new_recipe}
                save_app(base_url, recipe)
            else:
                print("   ⚠️  [MEMORY] Recipe discovery yielded nothing usable.")
        else:
            print("   ⚠️  Could not fetch HTML — using generic selectors.")
    else:
        print(f"   📖 [MEMORY] Cached recipe for {base_url} ({recipe.get('app_type', 'unknown')})")

    page_html = stored.get("page_structure", "")
    cached_is_login = _looks_like_login_page(page_html)
    cached_too_small = bool(page_html) and len(page_html) < 400
    if not page_html or cached_is_login or cached_too_small:
        if cached_too_small:
            print(f"   🔄 [MEMORY] Cached page_structure is only {len(page_html)} chars — re-discovering...")
        elif cached_is_login and page_html:
            print("   🔄 [MEMORY] Cached page_structure looks like a login page — re-discovering...")
        else:
            print("   🌐 [MEMORY] No cached page_structure — discovering post-login DOM...")
        if default_user and default_pass:
            new_html = discover_page_structure(base_url, recipe, default_user, default_pass)
            if new_html and not _looks_like_login_page(new_html):
                page_html = new_html
                print(f"   ✅ [MEMORY] page_structure cached ({len(page_html)} chars).")
            elif new_html:
                print("   ⚠️  [MEMORY] Post-login discovery returned a login-looking DOM — keeping for context.")
                page_html = new_html
            else:
                print("   ⚠️  [MEMORY] Post-login discovery failed — LLM will rely on text-based fallback selectors.")
        else:
            print("   ⚠️  Could not extract credentials — no HTML context for selector derivation.")

    base_root = _re.sub(r'/[a-z]+/index\.php/.*$', '', base_url, flags=_re.IGNORECASE)
    base_root = _re.sub(r'/+$', '', base_root)
    nav_links = _extract_nav_links(page_html)

    test_to_module: dict = {}
    module_dom: dict = {}
    module_catalogs: dict = {}

    unique_modules: set = set()
    if nav_links:
        for tc in test_cases:
            mod_url = _classify_test_to_module(tc, nav_links, base_root)
            tc_id = tc.get("id", "")
            if mod_url and tc_id:
                test_to_module[tc_id] = mod_url
                unique_modules.add(mod_url)

    if default_user and default_pass:
        if unique_modules:
            print(f"   🧭 [MEMORY] Classified {len(test_to_module)}/{len(test_cases)} tests "
                  f"to {len(unique_modules)} unique sub-page(s).")
        else:
            print("   🧭 [MEMORY] Single-page app — building login + dashboard catalogs only.")
        module_catalogs = discover_catalogs_batch(
            base_url, recipe, default_user, default_pass, sorted(unique_modules)
        )
        if unique_modules:
            module_dom = discover_modules_batch(
                base_url, recipe, default_user, default_pass, sorted(unique_modules)
            )
            fresh_dashboard = module_dom.get("__dashboard__", "")
            if fresh_dashboard and not _looks_like_login_page(fresh_dashboard):
                page_html = fresh_dashboard
    else:
        print("   ⚠️  [MEMORY] No credentials — skipping multi-module reconnaissance.")

    header = _build_header(base_url, recipe)

    _usr = recipe.get("username_selector", "xpath://input[@placeholder='Username']")
    _pwd = recipe.get("password_selector", "xpath://input[@type='password']")
    _btn = recipe.get("submit_selector",   "xpath://*[@type='submit']")
    _success = recipe.get("success_indicator", "")
    creds_hint = (
        f"DEFAULT CREDENTIALS (use these for EVERY test that needs login,\n"
        f"unless the test description specifies different credentials):\n"
        f"  username : {default_user}\n"
        f"  password : {default_pass}\n\n"
        if (default_user and default_pass)
        else ""
    )
    login_hint = (
        f"{creds_hint}"
        f"Two keywords are pre-built:\n"
        f"\n"
        f"  Open App And Login    <username>    <password>\n"
        f"    Opens browser (incognito), sets window size, logs in using:\n"
        f"      username field : {_usr}\n"
        f"      password field : {_pwd}\n"
        f"      submit button  : {_btn}\n"
        f"    The keyword INTERNALLY waits (best-effort) for the success indicator"
        f"{(' ' + repr(_success)) if _success else ''}.\n"
        f"    DO NOT add any extra Wait/Page Should Contain for that success indicator after the keyword.\n"
        f"    Takes _initial.png and _after_login.png screenshots automatically.\n"
        f"    Use this for ALL tests that require the user to be logged in.\n"
        f"\n"
        f"  Open Browser Only\n"
        f"    Opens browser (incognito), sets window size — does NOT log in.\n"
        f"    Use ONLY for tests that verify unauthenticated/access-control behavior.\n"
        f"\n"
        f"  Do NOT open browser, set window size, or write login steps manually."
    )

    batch_size = 5
    batches = [test_cases[i:i + batch_size] for i in range(0, len(test_cases), batch_size)]
    all_test_bodies = ""

    nav_hint = ""
    if nav_links:
        nav_lines = [f"  Go To    {base_root}{href}    # {label or '(no label)'}"
                     for href, label in nav_links[:25]]
        nav_hint = (
            "\nKNOWN INTERNAL NAVIGATION LINKS (use Go To with the absolute URL "
            "when the test needs to be on a sub-module page):\n"
            + "\n".join(nav_lines)
            + "\n"
        )

    planned_bodies: dict = {}
    if module_catalogs:
        print(f"   🧱 [PLAN] Attempting catalog-based plans for {len(test_cases)} tests...")
        for tc in test_cases:
            tc_id = tc.get("id", "")
            if not tc_id:
                continue
            mod_url = test_to_module.get(tc_id, "")
            cat = module_catalogs.get(mod_url) if mod_url else None
            login_cat = module_catalogs.get("__login__", {})
            dash_cat = module_catalogs.get("__dashboard__", {})
            merged = merge_catalogs(login_cat, dash_cat, cat) if (cat or dash_cat) else login_cat
            if not merged or not merged.get("elements"):
                continue
            body = _try_catalog_plan(tc, merged, default_user, default_pass)
            if body:
                planned_bodies[tc_id] = body
        print(f"   🧱 [PLAN] Catalog path produced {len(planned_bodies)}/"
              f"{len(test_cases)} test bodies; rest fall back to legacy.")

    for idx, batch in enumerate(batches):
        if all(tc.get("id", "") in planned_bodies for tc in batch):
            for tc in batch:
                all_test_bodies += "\n" + planned_bodies[tc["id"]].strip() + "\n"
            print(f"   ✅ [PLAN] Batch {idx + 1}/{len(batches)} fully covered by catalog plans.")
            continue

        print(f"   🤖 Generating batch {idx + 1}/{len(batches)} ({len(batch)} tests)...")

        tc_text_parts = []
        per_test_dom_parts = []
        for tc in batch:
            tc_id = tc.get("id", "")
            part = f"ID: {tc_id} | Title: {tc.get('title','')}\n"
            if tc.get("preconditions"):
                pc_lines = tc["preconditions"]
                if isinstance(pc_lines, list):
                    pc_lines = [str(p).strip("- ").strip() for p in pc_lines if str(p).strip()]
                    if pc_lines:
                        part += "Preconditions:\n"
                        for pc in pc_lines:
                            part += f"  - {pc}\n"
                elif str(pc_lines).strip():
                    part += f"Preconditions: {pc_lines}\n"
            if tc.get("steps"):
                part += "Steps:\n"
                for i, step in enumerate(tc["steps"], 1):
                    if isinstance(step, dict):
                        part += f"  {i}. {step['action']} → {step.get('expected', '')}\n"
                    else:
                        part += f"  {i}. {step}\n"
            if tc.get("expected"):
                part += f"Expected result: {tc['expected']}\n"

            mod_url = test_to_module.get(tc_id, "")
            if mod_url:
                part += f"TARGET PAGE URL: {mod_url}\n"
                part += (f"  → First action after Open App And Login MUST be: "
                         f"Go To    {mod_url}\n")
            tc_text_parts.append(part)

            tc_dom = module_dom.get(mod_url, "") if mod_url else ""
            if tc_dom:
                per_test_dom_parts.append(
                    f"--- DOM for {tc_id} ({mod_url}) ---\n{tc_dom[:1000]}"
                )

        batch_tcs_text = "\n".join(tc_text_parts)

        per_test_dom_block = ""
        if per_test_dom_parts:
            per_test_dom_block = (
                "\nPER-TEST PAGE DOM (real DOM captured by Playwright at the target URL):\n"
                + "\n".join(per_test_dom_parts)
                + "\n"
            )

        dashboard_cap = 2000 if per_test_dom_parts else 3500

        html_context_block = ""
        if page_html:
            html_context_block = f"""
DASHBOARD DOM (post-login landing page from {base_url}):
{page_html[:dashboard_cap]}
{nav_hint}{per_test_dom_block}
SELECTOR DERIVATION RULES:
- For each test, derive selectors from its PER-TEST PAGE DOM (above) when one exists.
- For tests without a per-test DOM block, use the DASHBOARD DOM.
- NEVER invent a class/id/data-test attribute that is not present in the DOM you were given.
- When the test requires a specific sub-module page, the FIRST step after
  Open App And Login MUST be `Go To <TARGET PAGE URL>` (already supplied above).
- After Go To, add `Sleep 2s` so the SPA can render before you interact.
"""

        ex_user = default_user or "<USERNAME>"
        ex_pass = default_pass or "<PASSWORD>"

        human_prompt = f"""Generate Robot Framework test case bodies for the *** Test Cases *** section.

{login_hint}
{html_context_block}
Test cases to generate:
{batch_tcs_text}

MANDATORY RULES:
- Do NOT output any *** ... *** section headers.
- Do NOT output [Documentation] or any [Tag] at column 0 (they belong INSIDE test cases, indented).
- Start each test case with its ID and title at column 0, e.g.:
    TC-006 Some test title
        [Documentation]    ...
        Open App And Login    {ex_user}    {ex_pass}
        Go To    <TARGET PAGE URL if the test specifies one>
        Sleep    2s
        Wait Until Element Is Visible    <selector for the FEATURE BEING TESTED>    15s
        ...
        Capture Page Screenshot    TC-006_final.png
- For EVERY test that needs a logged-in session, use Open App And Login with
  {ex_user} / {ex_pass} — UNLESS the test description specifies different credentials.
- For tests that check unauthenticated access only, use Open Browser Only.
- Do NOT write Open Browser, Set Window Size, or login input steps — they are in the keyword.
- Do NOT write Close Browser — Test Teardown handles it automatically.
- Do NOT add a Wait/Page Should Contain for the success indicator right after Open App And Login.
- Use Wait Until Element Is Visible (15s) before every element interaction (except <select>).
- Derive selectors from the PER-TEST PAGE DOM (or the dashboard DOM as fallback) — never guess.
- For features absent from BOTH DOM blocks, use a TEXT-based xpath like
  `xpath://*[contains(normalize-space(),'Some Visible Text')]`.
- Take screenshots for test-specific actions with the TC ID prefix (e.g. TC-006_final.png).
- Return ONLY the test case code, no markdown fences, no explanations."""

        messages = [
            SystemMessage(content=BASE_SYSTEM_PROMPT),
            HumanMessage(content=human_prompt),
        ]

        try:
            response = invoke_with_retry(get_smart_llm, messages)
            batch_code = response.content.strip()
        except Exception as batch_err:
            err_msg = str(batch_err).lower()
            if ("context" in err_msg and "length" in err_msg) or "too long" in err_msg or "8192" in err_msg:
                print(f"   ⚠️  Batch {idx + 1} overflowed LLM context — splitting into singletons...")
                fallback_pieces = []
                for tc in batch:
                    single_tc_text = next(
                        (p for p in tc_text_parts if p.lstrip().startswith(f"ID: {tc.get('id','')}")),
                        f"ID: {tc.get('id','')} | Title: {tc.get('title','')}\n",
                    )
                    mod_url_one = test_to_module.get(tc.get("id", ""), "")
                    tc_dom_one = module_dom.get(mod_url_one, "") if mod_url_one else ""
                    single_dom = ""
                    if tc_dom_one:
                        single_dom = (f"\nPER-TEST PAGE DOM (only this test):\n"
                                      f"--- DOM for {tc.get('id','')} ({mod_url_one}) ---\n"
                                      f"{tc_dom_one[:700]}\n")
                    elif page_html:
                        single_dom = f"\nDASHBOARD DOM:\n{page_html[:1500]}\n"

                    single_prompt = (
                        f"Generate the Robot Framework test case body for the *** Test Cases *** "
                        f"section.\n\n{single_dom}\n\nTest case:\n{single_tc_text}\n\n"
                        f"Rules:\n"
                        f"- Start with `TC-NNN <title>` at column 0.\n"
                        f"- Call `Open App And Login    {ex_user}    {ex_pass}` (or test-specific creds).\n"
                        f"- If target sub-module URL is given, first step after Open App And Login "
                        f"must be `Go To <url>` then `Sleep 2s`.\n"
                        f"- Wait Until Element Is Visible 15s before each interaction.\n"
                        f"- Derive selectors from the DOM above; use text-based xpath fallback.\n"
                        f"- Return ONLY the test body, no markdown, no headers."
                    )
                    try:
                        single_resp = invoke_with_retry(
                            get_smart_llm,
                            [SystemMessage(content=BASE_SYSTEM_PROMPT),
                             HumanMessage(content=single_prompt)],
                        )
                        fallback_pieces.append(single_resp.content.strip())
                    except Exception as single_err:
                        print(f"   ⚠️  Singleton retry for {tc.get('id','')} also failed: {single_err}")
                batch_code = "\n\n".join(fallback_pieces)
            else:
                raise

        if batch_code.startswith("```"):
            batch_code = "\n".join(batch_code.split("\n")[1:])
            if batch_code.endswith("```"):
                batch_code = batch_code.rsplit("```", 1)[0]

        batch_code = _clean_batch_code(batch_code)
        batch_code = _enforce_go_to_per_test(batch_code, batch, test_to_module)

        dom_blob_parts = [page_html or ""]
        for tc in batch:
            mod_url = test_to_module.get(tc.get("id", ""), "")
            if mod_url and module_dom.get(mod_url):
                dom_blob_parts.append(module_dom[mod_url])
        batch_code = _validate_selectors_against_dom(
            batch_code, "\n".join(dom_blob_parts)
        )

        batch_code = _backfill_truncated_tests(
            batch_code, batch, test_to_module, module_dom, page_html,
            login_hint, default_user, default_pass, BASE_SYSTEM_PROMPT,
        )

        if planned_bodies:
            for tc in batch:
                tc_id = tc.get("id", "")
                if tc_id and tc_id in planned_bodies:
                    body = planned_bodies[tc_id]
                    if tc_id in batch_code:
                        start = batch_code.find(tc_id)
                        m_next = _re.search(r'\n(TC-\d+)\b', batch_code[start + 1:])
                        if m_next:
                            end = start + 1 + m_next.start()
                            batch_code = (batch_code[:start] + body + "\n"
                                          + batch_code[end:])
                        else:
                            batch_code = batch_code[:start] + body + "\n"
                    else:
                        batch_code += "\n\n" + body + "\n"

        all_test_bodies += "\n" + batch_code.strip() + "\n"

    return header + "\n" + all_test_bodies.strip()
