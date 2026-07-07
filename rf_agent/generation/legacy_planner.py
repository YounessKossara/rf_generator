"""
Legacy batch planner (Phase B fallback path).
Sends batches of test cases to the LLM as free-text and applies safety nets
to the output: emoji stripping, selector downgrade, truncation backfill,
and Go To injection.
"""

import re as _re
from langchain.messages import SystemMessage, HumanMessage
from rf_agent.infrastructure.llm import get_smart_llm, invoke_with_retry
from rf_agent.generation.selector_validator import _strip_emojis_from_locators
from rf_agent.generation.module_classifier import _force_go_to


BASE_SYSTEM_PROMPT = """You are an expert in Robot Framework and SeleniumLibrary.
You are generating individual test case bodies — NOT full .robot files.
The *** Settings ***, *** Variables ***, and *** Keywords *** sections are
already built. Generate ONLY test case content.

RULES:
- Do NOT output any *** ... *** section headers
- Do NOT output Open Browser, Set Window Size, or Set Screenshot Directory — the keyword handles these
- Do NOT write the login sequence — call Open App And Login instead
- Use SeleniumLibrary keywords: Input Text, Click Element, Wait Until Page Contains,
  Wait Until Element Is Visible, Get Text, Get Element Count, Page Should Contain
- Use ${BASE_URL} for URLs
- Add [Documentation] tag to each test case
- Return ONLY valid Robot Framework test case code, no markdown, no explanations
- Use 4 spaces for indentation under test cases
- Each keyword call must be on its own line with proper indentation

═══════════════════════════════════════════════
  KEYWORD USAGE — MANDATORY
═══════════════════════════════════════════════

Two keywords are pre-built. Choose the right one:

  Open App And Login    <username>    <password>
    → Use for ALL tests including login-failure and locked-user tests.
      The keyword enters the credentials and clicks submit regardless.
      The keyword INTERNALLY waits (best-effort) for the success indicator.
      The test body then checks what happened (error message, redirect, etc.)

  Open Browser Only
    → Use ONLY for access-control tests that navigate WITHOUT entering
      any credentials at all (e.g. go directly to a protected URL and verify
      the app redirects to the login page). This is NOT for login-error tests.

Do NOT write Close Browser — Test Teardown handles it automatically.

═══════════════════════════════════════════════
  CRITICAL: DO NOT RE-VERIFY LOGIN IN THE TEST BODY
═══════════════════════════════════════════════

`Open App And Login` ALREADY does a best-effort wait for the success indicator
(e.g. "Dashboard", "Products", etc.). Do NOT add ANOTHER wait/assert for the
success indicator at the start of the test body.

The very first instruction in the test body (after `Open App And Login`)
must be the FIRST TEST-SPECIFIC ACTION (the verification or interaction
the test case is actually about). For example:

  WRONG (redundant + brittle — DO NOT DO THIS):
    Open App And Login    Admin    admin123
    Wait Until Element Is Visible    xpath://div[contains(text(),'Dashboard')]    15s   ← REDUNDANT
    Wait Until Element Is Visible    xpath://h1[contains(text(),'Welcome')]    15s     ← INVENTED

  RIGHT:
    Open App And Login    Admin    admin123
    Wait Until Element Is Visible    <selector for the actual feature being tested>    15s

═══════════════════════════════════════════════
  SELECTOR DERIVATION — MANDATORY
═══════════════════════════════════════════════

The real page HTML is provided in the user message.
You MUST derive ALL selectors from that HTML — do not guess or invent them.

If you cannot find a selector for a feature in the provided HTML:
  - Use a generic xpath that searches by visible TEXT, e.g.:
      xpath://*[contains(normalize-space(),'Quick Launch')]
      xpath://button[normalize-space()='Save']
  - Or skip the feature-specific assertion and just assert that the page
    loaded by checking generic post-login markers (a logout button, a
    user menu icon, the URL path).
NEVER fabricate a class name, id, or data-test attribute that is not in the HTML.

General priority for selectors:
  1. @id                  → xpath://button[@id='submit-btn']
  2. @placeholder         → xpath://input[@placeholder='Email']
  3. @type                → xpath://input[@type='password']
  4. button/link text     → xpath://button[normalize-space()='Save']
  5. aria-label           → xpath://*[@aria-label='Close menu']
  6. visible text content → xpath://*[contains(normalize-space(),'Dashboard')]
  7. exact @class         → xpath://div[@class='employee-card']  (for counting)
  8. contains(@class)     → xpath://div[contains(@class,'card')]  (for clicking one)

For COUNTING: always use EXACT @class to avoid counting nested child elements.
For FINDING/CLICKING: contains(@class,...) or text() is more robust.

NEVER use @name attributes.
NEVER invent a selector — verify it exists in the provided HTML.

═══════════════════════════════════════════════
  XPATH SELECTOR RULES
═══════════════════════════════════════════════

CLASS SELECTORS:
  For FINDING/CLICKING one element: use contains to be robust:
    xpath://div[contains(@class,'item')]
  For COUNTING elements: use EXACT class to avoid over-matching:
    xpath://div[@class='item']
  NEVER use contains(@class,...) for counting — it matches child divs too

AFTER LOGOUT:
  Wait for the login input to reappear (its placeholder text is in the recipe).

WAIT STRATEGY — MANDATORY:
  Before EVERY interaction: Wait Until Element Is Visible    <locator>    15s
  Exception: <select> tags — see SELECT / DROPDOWN below.

═══════════════════════════════════════════════
  SCREENSHOT RULES
═══════════════════════════════════════════════

The keyword captures _initial.png and _after_login.png automatically.
In the test body use the TC ID as prefix: TC-006_final.png

═══════════════════════════════════════════════
  COMMON MISTAKES — DO NOT REPEAT
═══════════════════════════════════════════════

NEVER REPEAT LOGIN:
  After Open App And Login, the user IS already logged in.
  Do NOT write Input Text for username/password or click the submit button again.

LOGIN-PAGE ELEMENTS DISAPPEAR AFTER LOGIN:
  After Open App And Login you are on the POST-LOGIN page. The login form is gone.
  NEVER wait for or assert on elements that only exist on the LOGIN page (the
  username/password input, the submit button, classes containing "login_"
  or "login-credentials" or starting with "form_login"). Use post-login
  selectors that you can verify in the PAGE HTML provided below.

UNDEFINED VARIABLES:
  NEVER use any variable not assigned earlier in the same test case.
  Always assign before use:
    ${count}=    Get Element Count    <locator>
    Should Be Equal As Integers    ${count}    6

  Robot Framework does NOT define ${LOCATION}, ${CURRENT_URL}, or ${URL} automatically.
  To check the current URL you MUST first call Get Location and assign the return:
    ${current_url}=    Get Location
    Should Contain    ${current_url}    /dashboard
  NEVER write ${LOCATION}, ${URL}, ${CURRENT_URL} directly — they don't exist.

SIDE MENU / BURGER MENU:
  After clicking a menu toggle, wait for a menu ITEM to be visible, not a CSS class.

RESET / CLEAR STATE:
  After a reset, look at the HTML to understand what appears after reset.
  Do NOT assert on state that the test description does not actually claim.

EXTERNAL NAVIGATION:
  When a link navigates to an external domain, verify the URL changed:
    Wait Until Location Contains    expected-domain.com    15s
  NEVER use Wait Until Page Contains for external page content.

PAGE CONTAINS vs ELEMENT VISIBLE:
  Wait Until Page Contains works for VISIBLE page text only.
  For elements with placeholder attributes, use Element Is Visible:
    Wait Until Element Is Visible    xpath://input[@placeholder='Username']    15s

MULTI-STEP FLOWS:
  Read the test description carefully. Some features require navigating
  through multiple pages to reach the target (the button may not exist yet).
  Use the PAGE HTML to verify which buttons exist on the CURRENT page.
  NEVER wait for a button that only appears on a later page without navigating there first.

SYNTAX:
  NEVER write Input Text with only one argument — it requires locator AND value.
  For empty field tests, skip Input Text and click submit directly.
  NEVER use Maximize Browser Window — Set Window Size is already called.

SELECT / DROPDOWN ELEMENTS:
  Selenium's "Wait Until Element Is Visible" often fails on native <select> tags
  because the browser renders them with zero size. For <select>:
    Wait Until Page Contains Element    xpath://select[@id='SOME_ID']    15s
    Select From List By Label           xpath://select[@id='SOME_ID']    Some Option
  Use Select From List By Label / By Value / By Index — never Click Element on a <select>
  followed by Click Element on an <option>.

NEGATIVE LOGIN TESTS:
  For tests where login is EXPECTED to fail (locked-out user, wrong password,
  empty fields), the test body must NOT wait for the post-login success page.
  Instead verify an error message — derive its selector FROM THE PAGE HTML
  (look for elements appearing after a failed submit, e.g. an alert/role=alert,
  a class containing 'error', 'alert', 'danger', 'message-error', etc.).
  Generic robust pattern when you cannot identify a specific selector:
    Sleep    1s
    ${err_visible}=    Run Keyword And Return Status    Page Should Contain Element    xpath://*[contains(@class,'alert') or contains(@class,'error') or @role='alert']
    Should Be True    ${err_visible}

LOGIN TESTS (positive AND negative) — credentials go in Open App And Login args:
  Whatever credentials the test description specifies, pass them as ARGUMENTS
  to Open App And Login. NEVER write Input Text for the username/password
  fields in the test body, and NEVER re-click submit — the keyword already
  does all three of those steps internally.

  Positive example ("connexion réussie avec Admin / admin123"):
      Open App And Login    Admin    admin123
      # then verify the dashboard, NOT re-enter Admin/admin123

  Negative example ("connexion avec invalid_user / wrong_password"):
      Open App And Login    invalid_user    wrong_password
      # then verify the error message — DO NOT add another Input Text or Click submit

  WRONG (do not do this — duplicates the login and breaks the test):
      Open App And Login    Admin    admin123
      Input Text    xpath://input[@placeholder='Username']    Admin       ← DON'T
      Input Text    xpath://input[@type='password']    admin123             ← DON'T
      Click Element    xpath://*[@type='submit']                            ← DON'T

ASSERTION COUNTS:
  Re-clicking the same Add-to-cart / toggle button does NOT add a duplicate
  in most apps — they toggle. Only assert counts that the test description
  actually claims.

DO NOT INVENT VALUES OR DATA:
  If a test step says "search for an existing user", and the test description
  does not specify which one, use the SAME username that was used to log in
  (e.g. for OrangeHRM use "Admin"). Never invent strings like "existingemployee"
  or "John Doe" unless the test description provides them.

NEVER PUT EMOJIS OR PICTOGRAPHS IN LOCATORS:
  Real DOM never contains 📦, 🛒, 🏠, etc. Locators built from those characters
  always fail. Use the visible TEXT label instead:
    WRONG: xpath://*[contains(text(),'🛒')]
    RIGHT: xpath://*[contains(normalize-space(),'Cart')]
  Same for ANY locator value — no emojis in @class, @id, @placeholder, etc.

TESTS MUST BE SELF-CONTAINED:
  Each test case is INDEPENDENT — assume nothing from a previous test:
    - The browser is freshly opened (Open App And Login restarts it).
    - The user lands on the post-login DASHBOARD, NOT on the module page
      from a previous test.
    - If your test needs to be on a sub-module page, the FIRST action after
      Open App And Login MUST be `Go To <module-url>` followed by `Sleep 2s`.
  Never assume the app remembers a filter, a search term, a selected row,
  or any other state from another test case.

ACTIONS THAT MAY NOT BE AVAILABLE IN THE DOM:
  If the test step describes an action (e.g. "change password",
  "click the Admin role dropdown", "open the side menu") whose UI element
  is NOT visible in the PER-TEST PAGE DOM provided above, do NOT invent a
  class or id. Use a TEXT-based xpath and wrap a possibly-flaky click in
  Run Keyword And Return Status so the test does not crash on a missing UI:

    ${ok}=    Run Keyword And Return Status    Click Element    xpath://*[contains(normalize-space(),'Change Password')]
    Run Keyword If    not ${ok}    Log    Action 'Change Password' not available in current build, skipping.

  Only do this for the optional-action step. Mandatory assertions stay strict."""


def _clean_batch_code(code: str) -> str:
    """
    Strip section headers, spurious column-0 [Tag] lines, and markdown separators
    from LLM batch output.
    """
    typo_fixes = [
        (r'\bnormal-space\(\)', 'normalize-space()'),
    ]
    for pattern, repl in typo_fixes:
        code = _re.sub(pattern, repl, code)

    code = _strip_emojis_from_locators(code)

    code = _re.sub(
        r'(\n\s*)Get Location\s*\n(\s*)Should Contain\s+\$\{LOCATION\}\s+([^\n]+)',
        r'\1${_url}=    Get Location\n\2Should Contain    ${_url}    \3',
        code,
        flags=_re.IGNORECASE,
    )

    def _fix_location(m):
        indent = m.group(1)
        rest = m.group(2)
        return (f"{indent}${{_url}}=    Get Location\n"
                f"{indent}Should Contain    ${{_url}}    {rest}")
    code = _re.sub(
        r'(\n[ \t]+)Should Contain\s+\$\{LOCATION\}\s+([^\n]+)',
        _fix_location, code, flags=_re.IGNORECASE,
    )

    lines = code.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("***") and stripped.endswith("***"):
            continue
        if stripped.startswith("[") and not line.startswith(" ") and not line.startswith("\t"):
            continue
        if stripped.startswith("---") and stripped.endswith("---"):
            continue
        if (stripped.endswith(":")
                and not line.startswith(" ")
                and not line.startswith("\t")
                and not stripped.startswith("TC-")):
            continue
        if _re.match(r'TC-\d+', stripped) and (line.startswith(" ") or line.startswith("\t")):
            line = stripped
        m = _re.match(r'^(\s+)Page Should Contain\s+(xpath:[^\s]+)\s+(\d+s?)\s*$', line)
        if m:
            line = f"{m.group(1)}Wait Until Page Contains Element    {m.group(2)}    {m.group(3)}"
        cleaned.append(line)
    return "\n".join(cleaned)


def _extract_tc_body(batch_code: str, tc_id: str) -> str:
    """
    Return the lines for `tc_id` in `batch_code` (TC header + indented body).
    Empty string if the TC isn't present.
    """
    lines = batch_code.split("\n")
    start = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not (ln.startswith(" ") or ln.startswith("\t")):
            if start is None and stripped.startswith(tc_id):
                start = i
            elif start is not None and _re.match(r'^TC-\d+', stripped):
                return "\n".join(lines[start:i]).rstrip()
    if start is not None:
        return "\n".join(lines[start:]).rstrip()
    return ""


def _is_test_truncated(body: str) -> bool:
    """
    A test body is considered truncated when it's missing the bones a real
    Robot Framework test should have.
    """
    if not body or not body.strip():
        return True
    lines = [ln for ln in body.split("\n")[1:] if ln.strip()]
    if len(lines) < 2:
        return True
    keyword_lines = [ln for ln in lines if (ln.startswith(" ") or ln.startswith("\t"))
                     and not ln.strip().startswith("[")]
    if len(keyword_lines) < 2:
        return True
    for ln in keyword_lines:
        if _re.match(r'^\s*Open\s+App\s*$', ln):
            return True
    return False


def _backfill_truncated_tests(batch_code: str, batch: list, test_to_module: dict,
                              module_dom: dict, page_html: str, login_hint: str,
                              default_user: str, default_pass: str,
                              system_prompt: str) -> str:
    """
    For each TC in the batch whose generated body is missing or truncated,
    regenerate that single test with a tiny prompt and splice it back in.
    """
    truncated = []
    for tc in batch:
        tc_id = tc.get("id", "")
        if not tc_id:
            continue
        body = _extract_tc_body(batch_code, tc_id)
        if _is_test_truncated(body):
            truncated.append(tc)
    if not truncated:
        return batch_code

    print(f"   ⚠️  Detected {len(truncated)} truncated test case(s) — retrying as singletons...")
    ex_user = default_user or "<USERNAME>"
    ex_pass = default_pass or "<PASSWORD>"

    for tc in truncated:
        tc_id = tc.get("id", "")
        title = tc.get("title", "")
        steps_text = ""
        for i, step in enumerate(tc.get("steps", []), 1):
            if isinstance(step, dict):
                steps_text += f"  {i}. {step.get('action','')} -> {step.get('expected','')}\n"
            else:
                steps_text += f"  {i}. {step}\n"
        mod_url = test_to_module.get(tc_id, "")
        single_dom = ""
        if mod_url and module_dom.get(mod_url):
            single_dom = (f"\nDOM at target page ({mod_url}):\n"
                          f"{module_dom[mod_url][:700]}\n")
        elif page_html:
            single_dom = f"\nDashboard DOM:\n{page_html[:1500]}\n"

        single_prompt = (
            f"Generate exactly ONE Robot Framework test case body.\n\n"
            f"Test ID: {tc_id}\nTitle: {title}\nSteps:\n{steps_text}\n"
            f"{single_dom}\n"
            f"RULES:\n"
            f"- Start with `{tc_id} {title}` at column 0.\n"
            f"- Call `Open App And Login    {ex_user}    {ex_pass}` "
            f"(or use the test's specific credentials).\n"
            f"- If target URL given above, first action after Open App And Login "
            f"must be `Go To <url>` then `Sleep 2s`.\n"
            f"- Use Wait Until Element Is Visible 15s before each interaction.\n"
            f"- Derive selectors from the DOM above; use text-based xpath when "
            f"unsure: `xpath://*[contains(normalize-space(),'Some Text')]`.\n"
            f"- End with `Capture Page Screenshot    {tc_id}_final.png`.\n"
            f"- Return ONLY the test body, no markdown, no headers."
        )
        try:
            resp = invoke_with_retry(
                get_smart_llm,
                [SystemMessage(content=system_prompt),
                 HumanMessage(content=single_prompt)],
            )
            new_body = resp.content.strip()
            if new_body.startswith("```"):
                new_body = "\n".join(new_body.split("\n")[1:])
                if new_body.endswith("```"):
                    new_body = new_body.rsplit("```", 1)[0]
            new_body = _clean_batch_code(new_body).strip()

            old = _extract_tc_body(batch_code, tc_id)
            if old:
                batch_code = batch_code.replace(old, new_body, 1)
            else:
                batch_code = batch_code.rstrip() + "\n\n" + new_body + "\n"
            print(f"   ✅ Backfilled {tc_id}")
        except Exception as e:
            print(f"   ⚠️  Singleton retry for {tc_id} failed: {e}")

    return batch_code


def _enforce_go_to_per_test(batch_code: str, batch: list, test_to_module: dict) -> str:
    """
    For each test in the batch with a known target module URL, ensure the
    generated body contains a `Go To <url>` step right after `Open App And Login`.
    """
    if not test_to_module:
        return batch_code
    lines = batch_code.split("\n")
    test_starts = [i for i, ln in enumerate(lines)
                   if _re.match(r'^TC-\d+', ln.strip()) and not (ln.startswith(" ") or ln.startswith("\t"))]
    if not test_starts:
        return batch_code
    test_starts.append(len(lines))

    fixed = lines[: test_starts[0]] if test_starts else lines[:]
    for i in range(len(test_starts) - 1):
        start, end = test_starts[i], test_starts[i + 1]
        block = "\n".join(lines[start:end])
        m = _re.match(r'^(TC-\d+)', lines[start].strip())
        if not m:
            fixed.extend(lines[start:end])
            continue
        tc_id = m.group(1)
        target = test_to_module.get(tc_id, "")
        if target:
            block = _force_go_to(block, target)
        fixed.extend(block.split("\n"))
    return "\n".join(fixed)
