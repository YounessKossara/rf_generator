"""
RF Generator — Robot Framework Code Generator

Uses LLM to generate Robot Framework (.robot) code from parsed test cases.
Performs per-domain app reconnaissance to discover login selectors, then caches
in app_memory.json for reuse across runs. No hardcoded app-specific rules.

Architecture:
  - Settings / Variables / Keywords sections are built in Python from app_memory.
    The LLM never writes login selectors — it only writes test case bodies.
  - All test cases call the pre-built `Open App And Login` keyword.
  - This eliminates locator drift across batches permanently.
"""

import re as _re
import httpx
from tools.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage
from rf_agent.app_memory import (load_app, save_app, discover_login_recipe,
                                  build_login_context, discover_page_structure)


# ── System prompt — focuses on test case BODIES only ──────────────────────────

BASE_SYSTEM_PROMPT = """You are an expert in Robot Framework and SeleniumLibrary.
You are generating individual test case bodies — NOT full .robot files.
The *** Settings ***, *** Variables ***, and *** Keywords *** sections are
already built. Generate ONLY test case content.

RULES:
- Do NOT output any *** ... *** section headers
- Do NOT output Open Browser, Set Window Size, or Set Screenshot Directory — the keyword handles these
- Do NOT write the login sequence — call Open App And Login instead
- Use SeleniumLibrary keywords: Input Text, Click Element, Wait Until Page Contains,
  Wait Until Element Is Visible, Get Text, Get Element Count
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
      The test body then checks what happened (error message, redirect, etc.)

  Open Browser Only
    → Use ONLY for access-control tests that navigate WITHOUT entering
      any credentials at all (e.g. go directly to /cart.html and verify
      the app redirects to login). This is NOT for login-error tests.

Do NOT write Close Browser — Test Teardown handles it automatically.

═══════════════════════════════════════════════
  SELECTOR DERIVATION — MANDATORY
═══════════════════════════════════════════════

The real page HTML is provided in the user message.
You MUST derive ALL selectors from that HTML — do not guess or invent them.

General priority for selectors:
  1. @id                  → xpath://button[@id='submit-btn']
  2. @placeholder         → xpath://input[@placeholder='Email']
  3. @type                → xpath://input[@type='password']
  4. button/link text     → xpath://button[normalize-space()='Add to Cart']
  5. aria-label           → xpath://*[@aria-label='Close menu']
  6. exact @class         → xpath://div[@class='product-card']  (for counting)
  7. contains(@class)     → xpath://div[contains(@class,'card')]  (for clicking one)

For COUNTING: always use EXACT @class to avoid counting nested child elements.
For FINDING/CLICKING: contains(@class,...) or text() is more robust.

NEVER use @name attributes.
NEVER invent a selector — verify it exists in the provided HTML.

═══════════════════════════════════════════════
  XPATH SELECTOR RULES
═══════════════════════════════════════════════

NEVER USE @name SELECTORS. Priority order:
  1. @id or @placeholder   → xpath://input[@placeholder='First Name']
  2. @type                 → xpath://input[@type='password']
  3. text content          → xpath://button[normalize-space()='Add to cart']
  4. CSS class             → css:.some-class-name
  5. contains text         → xpath://*[contains(text(),'Dashboard')]

CLASS SELECTORS:
  For FINDING/CLICKING one element: use contains to be robust:
    xpath://div[contains(@class,'item')]
  For COUNTING elements: use EXACT class to avoid over-matching:
    xpath://div[@class='item']
  NEVER use contains(@class,...) for counting — it matches child divs too

AFTER LOGOUT:
  Wait for the login input to appear, not text "Login":
    Wait Until Element Is Visible    xpath://input[@placeholder='Username']    15s

WAIT STRATEGY — MANDATORY:
  Before EVERY interaction: Wait Until Element Is Visible    <locator>    15s

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
  If a user account loads slowly, just increase the wait time for the landing page.

UNDEFINED VARIABLES:
  NEVER use any variable not assigned earlier in the same test case.
  Always assign before use:
    ${count}=    Get Element Count    <locator>
    Should Be Equal As Integers    ${count}    6

SIDE MENU / BURGER MENU:
  After clicking a menu toggle, wait for a menu ITEM to be visible, not a CSS class:
    Wait Until Element Is Visible    xpath://a[normalize-space()='All Items']    15s
  NEVER wait for CSS classes that describe menu animation state.

RESET / CLEAR STATE:
  After a reset action, verify the app returned to initial state.
  Look at the HTML to understand what appears after reset (typically add-to-cart buttons).
  Do NOT expect a cart badge after clearing the cart.

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
  Common patterns derived from the PAGE HTML context:
    - To reach a checkout form: add item to cart → navigate to cart → click checkout
    - To remove an item: navigate to cart first, then click remove
    - To reset/clear state: open the side menu first, then click the reset option
  Use the PAGE HTML to verify which buttons exist on the CURRENT page.
  NEVER wait for a button that only appears on a later page without navigating there first.

SYNTAX:
  NEVER write Input Text with only one argument — it requires locator AND value.
  For empty field tests, skip Input Text and click submit directly.
  NEVER use Maximize Browser Window — Set Window Size is already called."""


# ── Python-built file header (Settings + Variables + Keywords) ─────────────────

def _build_header(base_url: str, recipe: dict) -> str:
    """
    Build the Settings, Variables, and Keywords sections from app_memory.
    Selectors come directly from the recipe — the LLM never touches them.
    """
    usr = recipe.get("username_selector") or "xpath://input[@placeholder='Username']"
    pwd = recipe.get("password_selector") or "xpath://input[@type='password']"
    btn = recipe.get("submit_selector")   or "xpath://*[@type='submit']"
    success = recipe.get("success_indicator", "")
    success_wait = f"    Wait Until Page Contains    {success}    15s\n" if success else ""

    return (
        "*** Settings ***\n"
        "Library    SeleniumLibrary\n"
        "Test Teardown    Run Keyword And Ignore Error    Close All Browsers\n"
        "\n"
        "*** Variables ***\n"
        f"${{BASE_URL}}    {base_url}\n"
        "${SCREENSHOT_ROOT}    output/screenshots\n"
        "\n"
        "*** Keywords ***\n"
        "Open App And Login\n"
        "    [Arguments]    ${username}    ${password}\n"
        "    ${_safe}=    Evaluate    ''.join('_' if c in r'<>:\"/\\|?*()' else c for c in \"\"\"${TEST NAME}\"\"\")\n"
        "    Open Browser    ${BASE_URL}    chrome    "
        "options=add_argument(\"--incognito\");add_argument(\"--disable-popup-blocking\")\n"
        "    Set Window Size    1920    1080\n"
        "    Set Screenshot Directory    ${SCREENSHOT_ROOT}\n"
        "    Sleep    3s\n"
        "    Capture Page Screenshot    ${_safe}_initial.png\n"
        f"    Wait Until Element Is Visible    {usr}    15s\n"
        f"    Input Text    {usr}    ${{username}}\n"
        f"    Wait Until Element Is Visible    {pwd}    15s\n"
        f"    Input Text    {pwd}    ${{password}}\n"
        f"    Wait Until Element Is Visible    {btn}    15s\n"
        f"    Click Element    {btn}\n"
        "    Sleep    2s\n"
        f"{success_wait}"
        "    Capture Page Screenshot    ${_safe}_after_login.png\n"
        "\n"
        "Open Browser Only\n"
        "    Open Browser    ${BASE_URL}    chrome    "
        "options=add_argument(\"--incognito\");add_argument(\"--disable-popup-blocking\")\n"
        "    Set Window Size    1920    1080\n"
        "    Set Screenshot Directory    ${SCREENSHOT_ROOT}\n"
        "\n"
        "*** Test Cases ***"
    )


# ── Batch output cleanup ───────────────────────────────────────────────────────

def _clean_batch_code(code: str) -> str:
    """
    Strip section headers, spurious column-0 [Tag] lines, and markdown separators
    from LLM batch output. Applied to all batches since the header is built by Python.
    Also normalizes accidental indentation of TC-NNN lines back to column 0.
    """
    lines = code.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Remove *** ... *** section headers
        if stripped.startswith("***") and stripped.endswith("***"):
            continue
        # Remove any [Tag] at column 0 (not indented = not inside a test body)
        if stripped.startswith("[") and not line.startswith(" ") and not line.startswith("\t"):
            continue
        # Remove markdown-style "--- TC-XXX ---" separators
        if stripped.startswith("---") and stripped.endswith("---"):
            continue
        # Remove LLM prose preambles at column 0 ending in ':' (e.g. "Here are the test cases:")
        if (stripped.endswith(":")
                and not line.startswith(" ")
                and not line.startswith("\t")
                and not stripped.startswith("TC-")):
            continue
        # Normalize TC-NNN test case names to column 0 — LLM sometimes indents them
        if _re.match(r'TC-\d+', stripped) and (line.startswith(" ") or line.startswith("\t")):
            line = stripped
        cleaned.append(line)
    return "\n".join(cleaned)


# ── Credential extractor ──────────────────────────────────────────────────────

def _extract_default_credentials(test_cases: list[dict]) -> tuple[str, str]:
    """
    Scan test case steps/titles for the first valid (non-error) credential pair.
    Returns (username, password) or ("", "") if none found.
    """
    for tc in test_cases:
        text = tc.get("title", "") + " " + tc.get("expected", "")
        for step in tc.get("steps", []):
            text += " " + (step.get("action", "") if isinstance(step, dict) else str(step))

        for pattern in [
            # "user / pass" with at least one space around slash
            r'\b([\w.@+-]{3,})\s+/\s+([\w.@+-]{4,})\b',
            # "user/pass" no spaces but minimum length
            r'\b([\w.@+-]{3,})\s*/\s*([\w.@+-]{5,})\b',
            # "username: xxx password: yyy" (English or French)
            r'(?:username|user|email|login|identifiant)\s*[=:]\s*(\S{3,}).*?(?:password|pass(?:word)?|pwd|mot\s*de\s*passe)\s*[=:]\s*(\S{4,})',
            # "word_user ... longword" — requires password ≥6 chars to avoid French articles
            r'\b(\w+_user)\b[^.\n]{0,80}?\b(\w{6,})\b',
        ]:
            m = _re.search(pattern, text, _re.IGNORECASE | _re.DOTALL)
            if m:
                u, p = m.group(1), m.group(2)
                if not any(x in u.lower() for x in ["locked", "invalid", "wrong", "bad"]) and u != p:
                    print(f"   👤 [CREDS] Found credentials: {u}")
                    return u, p
    print("   ⚠️  [CREDS] No credentials found in test cases")
    return "", ""


# ── HTML fetcher ───────────────────────────────────────────────────────────────

def _fetch_page_html(url: str) -> str:
    """Fetch login page HTML, returning elements relevant to form discovery."""
    try:
        with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
            resp = client.get(url)
            html = resp.text

        elements = _re.findall(
            r'<(?:input|button|a|select|textarea|form|label|div[^>]*class="[^"]*'
            r'(?:login|form|nav|menu|dropdown|sidebar)[^"]*")[^>]*'
            r'(?:/>|>[^<]*</(?:input|button|a|select|textarea|form|label|div)>|>)',
            html,
            _re.IGNORECASE | _re.DOTALL,
        )
        if elements:
            return "\n".join(elements[:60])[:4000]
        return html[:3000]
    except Exception as e:
        print(f"   ⚠️  Could not fetch page HTML: {e}")
        return ""


# ── Main generator ─────────────────────────────────────────────────────────────

def generate_rf_code(test_cases: list[dict], base_url: str) -> str:
    """
    Generate Robot Framework code in batches.

    Flow:
      1. Load or discover login recipe from app_memory.
      2. Build Settings + Variables + Keywords in Python (no LLM involvement).
      3. Ask LLM to generate ONLY test case bodies, calling Open App And Login.
      4. Clean each batch output to strip stray headers/separators.
      5. Concatenate header + cleaned batches.
    """
    # ── Step 1: Recipe ─────────────────────────────────────────────────────────
    print("   📚 Checking app memory...")
    stored = load_app(base_url)
    # Use whatever is stored (even with null selectors — _build_header has fallbacks)
    recipe = stored if stored else {}

    if not recipe:
        print("   🔍 Fetching login page HTML to discover recipe...")
        login_page_html = _fetch_page_html(base_url)
        if login_page_html:
            recipe = discover_login_recipe(login_page_html, base_url)
            if recipe:
                save_app(base_url, recipe)
        else:
            print("   ⚠️  Could not fetch HTML — using generic selectors.")
    else:
        print(f"   📖 [MEMORY] Cached recipe for {base_url} ({recipe.get('app_type', 'unknown')})")

    # ── Step 1b: Post-login page structure for selector derivation ─────────────
    page_html = stored.get("page_structure", "")
    if not page_html:
        # Always attempt discovery — _build_header fallbacks handle null recipe selectors
        username, password = _extract_default_credentials(test_cases)
        if username:
            page_html = discover_page_structure(base_url, recipe, username, password)
        else:
            print("   ⚠️  Could not extract credentials — no HTML context for selector derivation.")

    # ── Step 2: Build the file header in Python ────────────────────────────────
    header = _build_header(base_url, recipe)

    # Build a compact keyword-usage hint for the LLM prompts
    _usr = recipe.get("username_selector", "xpath://input[@placeholder='Username']")
    _pwd = recipe.get("password_selector", "xpath://input[@type='password']")
    _btn = recipe.get("submit_selector",   "xpath://*[@type='submit']")
    login_hint = (
        f"Two keywords are pre-built:\n"
        f"\n"
        f"  Open App And Login    <username>    <password>\n"
        f"    Opens browser (incognito), sets window size, logs in using:\n"
        f"      username field : {_usr}\n"
        f"      password field : {_pwd}\n"
        f"      submit button  : {_btn}\n"
        f"    Takes _initial.png and _after_login.png screenshots automatically.\n"
        f"    Use this for ALL tests that require the user to be logged in.\n"
        f"\n"
        f"  Open Browser Only\n"
        f"    Opens browser (incognito), sets window size — does NOT log in.\n"
        f"    Use ONLY for tests that verify unauthenticated/access-control behavior.\n"
        f"\n"
        f"  Do NOT open browser, set window size, or write login steps manually."
    )

    # ── Step 3: Batch generation ───────────────────────────────────────────────
    batch_size = 5
    batches = [test_cases[i:i + batch_size] for i in range(0, len(test_cases), batch_size)]
    all_test_bodies = ""

    for idx, batch in enumerate(batches):
        print(f"   🤖 Generating batch {idx + 1}/{len(batches)} ({len(batch)} tests)...")

        tc_text_parts = []
        for tc in batch:
            part = f"ID: {tc['id']} | Title: {tc['title']}\n"
            if tc.get("steps"):
                part += "Steps:\n"
                for i, step in enumerate(tc["steps"], 1):
                    if isinstance(step, dict):
                        part += f"  {i}. {step['action']} → {step.get('expected', '')}\n"
                    else:
                        part += f"  {i}. {step}\n"
            if tc.get("expected"):
                part += f"Expected result: {tc['expected']}\n"
            tc_text_parts.append(part)

        batch_tcs_text = "\n".join(tc_text_parts)

        html_context_block = ""
        if page_html:
            html_context_block = f"""
PAGE HTML (real DOM from {base_url} after login — derive ALL selectors from this):
{page_html[:4500]}

MANDATORY: Use ONLY selectors that exist in the HTML above.
Look at @id, @class, @placeholder, button text, link text, aria-label in the HTML.
NEVER invent a selector that is not present in this HTML.
"""

        human_prompt = f"""Generate Robot Framework test case bodies for the *** Test Cases *** section.

{login_hint}
{html_context_block}
Test cases to generate:
{batch_tcs_text}

MANDATORY RULES:
- Do NOT output any *** ... *** section headers.
- Do NOT output [Documentation] or any [Tag] at column 0 (they belong INSIDE test cases, indented).
- Start each test case with its ID and title at column 0, e.g.:
    TC-006 Sort Products By Price
        [Documentation]    ...
        Open App And Login    standard_user    secret_sauce
        ...
        Capture Page Screenshot    TC-006_final.png
- Start every test with Open App And Login UNLESS the test checks unauthenticated access,
  in which case use Open Browser Only instead.
- Do NOT write Open Browser, Set Window Size, or login input steps — they are in the keyword.
- Do NOT write Close Browser — Test Teardown handles it automatically.
- Use Wait Until Element Is Visible (15s) before every element interaction.
- Derive selectors from the PAGE HTML provided above — never guess.
- Take screenshots for test-specific actions with the TC ID prefix (e.g. TC-006_final.png).
- Return ONLY the test case code, no markdown fences, no explanations."""

        messages = [
            SystemMessage(content=BASE_SYSTEM_PROMPT),
            HumanMessage(content=human_prompt),
        ]

        response = invoke_with_retry(get_smart_llm, messages)
        batch_code = response.content.strip()

        # Strip markdown fences
        if batch_code.startswith("```"):
            batch_code = "\n".join(batch_code.split("\n")[1:])
            if batch_code.endswith("```"):
                batch_code = batch_code.rsplit("```", 1)[0]

        # Strip stray section headers, column-0 tags, separators
        batch_code = _clean_batch_code(batch_code)

        all_test_bodies += "\n" + batch_code.strip() + "\n"

    # ── Step 4: Assemble final file ────────────────────────────────────────────
    return header + "\n" + all_test_bodies.strip()
