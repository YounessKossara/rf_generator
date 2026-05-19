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

import json as _json
import re as _re
import httpx
from tools.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage
from rf_agent.app_memory import (load_app_for_generation, save_app, discover_login_recipe,
                                  build_login_context, discover_page_structure,
                                  discover_modules_batch, discover_catalogs_batch,
                                  cache_enabled)
from rf_agent.step_renderer import (render_robot_test, merge_catalogs,
                                     UnknownIdError, ALLOWED_KEYWORDS)


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
    # IMPORTANT: do NOT make this a hard wait — negative login tests (locked user,
    # wrong credentials) intentionally never reach the success indicator. We use
    # Run Keyword And Return Status so the keyword always succeeds; the test
    # body is responsible for verifying success or failure.
    success_wait = (
        f"    Run Keyword And Return Status    Wait Until Page Contains    {success}    8s\n"
        if success else ""
    )

    return (
        "*** Settings ***\n"
        "Library    SeleniumLibrary\n"
        "Library    String\n"
        # Phase B: runtime selector remap library — imports `heal_selector_by_label`
        # as a Robot keyword. NO LLM at runtime; pure browser-side JS.
        "Library    rf_agent.healer_runtime\n"
        "Test Teardown    Run Keyword And Ignore Error    Close All Browsers\n"
        "\n"
        "*** Variables ***\n"
        f"${{BASE_URL}}    {base_url}\n"
        "${SCREENSHOT_ROOT}    output/screenshots\n"
        "\n"
        "*** Keywords ***\n"
        "Open App And Login\n"
        "    [Arguments]    ${username}    ${password}\n"
        "    ${_pre}=    Replace String    ${TEST NAME}    \"    _\n"
        "    ${_safe}=    Evaluate    ''.join('_' if c in r'<>:\"/\\|?*()' else c for c in \"\"\"${_pre}\"\"\")\n"
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
        # ── Phase B Smart wrappers ────────────────────────────────────────
        # Each interaction is wrapped so that if the primary selector is not\n"
        # visible within 15s, the test calls the runtime healer to find a\n"
        # fresh selector by (label, role) in the LIVE browser and retries.\n"
        "Resolved Selector\n"
        "    [Arguments]    ${label}    ${role}    ${primary}\n"
        "    ${ok}=    Run Keyword And Return Status    Wait Until Element Is Visible    ${primary}    15s\n"
        "    IF    ${ok}\n"
        "        RETURN    ${primary}\n"
        "    END\n"
        "    Log    Healing '${label}' (${role}) — primary selector not visible    level=WARN\n"
        "    ${new}=    Heal Selector By Label    ${label}    ${role}\n"
        "    Should Not Be Empty    ${new}    Could not heal '${label}' (${role}) — element not on page\n"
        "    Wait Until Element Is Visible    ${new}    10s\n"
        "    RETURN    ${new}\n"
        "\n"
        "Smart Click\n"
        "    [Arguments]    ${label}    ${role}    ${primary}\n"
        "    ${sel}=    Resolved Selector    ${label}    ${role}    ${primary}\n"
        "    Click Element    ${sel}\n"
        "\n"
        "Smart Input\n"
        "    [Arguments]    ${label}    ${role}    ${primary}    ${value}\n"
        "    ${sel}=    Resolved Selector    ${label}    ${role}    ${primary}\n"
        "    Input Text    ${sel}    ${value}\n"
        "\n"
        "Smart Wait\n"
        "    [Arguments]    ${label}    ${role}    ${primary}\n"
        "    Resolved Selector    ${label}    ${role}    ${primary}\n"
        "\n"
        "Smart Select By Label\n"
        "    [Arguments]    ${label}    ${role}    ${primary}    ${value}\n"
        "    ${ok}=    Run Keyword And Return Status    Wait Until Page Contains Element    ${primary}    15s\n"
        "    ${sel}=    Set Variable If    ${ok}    ${primary}    ${EMPTY}\n"
        "    IF    not ${ok}\n"
        "        ${sel}=    Heal Selector By Label    ${label}    ${role}\n"
        "        Should Not Be Empty    ${sel}    Could not heal '${label}' (${role}) — select not on page\n"
        "        Wait Until Page Contains Element    ${sel}    10s\n"
        "    END\n"
        "    Select From List By Label    ${sel}    ${value}\n"
        "\n"
        "*** Test Cases ***"
    )


# ── Batch output cleanup ───────────────────────────────────────────────────────

def _clean_batch_code(code: str) -> str:
    """
    Strip section headers, spurious column-0 [Tag] lines, and markdown separators
    from LLM batch output. Applied to all batches since the header is built by Python.
    Also normalizes accidental indentation of TC-NNN lines back to column 0
    and fixes recurring LLM typos.
    """
    # Fix common LLM typos that break Robot Framework or XPath
    typo_fixes = [
        # "normal-space()" → "normalize-space()"
        (r'\bnormal-space\(\)', 'normalize-space()'),
        # "Wait Until Page Contains    xpath://..."  is fine, but:
        # "Page Should Contain    xpath://..." with arguments is invalid (no timeout arg)
        # — leave it, it's a different keyword
    ]
    for pattern, repl in typo_fixes:
        code = _re.sub(pattern, repl, code)

    # Strip emojis / non-ASCII pictographs from inside xpath literals — they
    # never match real DOM and crash the locator parser.
    code = _strip_emojis_from_locators(code)

    # Auto-rewrite the unsupported "${LOCATION}" pattern when it's used right
    # after `Get Location`. We replace the pair with a proper assignment.
    code = _re.sub(
        r'(\n\s*)Get Location\s*\n(\s*)Should Contain\s+\$\{LOCATION\}\s+([^\n]+)',
        r'\1${_url}=    Get Location\n\2Should Contain    ${_url}    \3',
        code,
        flags=_re.IGNORECASE,
    )
    # Standalone "${LOCATION}" (not preceded by an assignment) → safe stub.
    # Replace the bare "Should Contain    ${LOCATION}    X" with a Get-Location
    # assignment + check, keeping the same indentation.
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
        # Fix Page Should Contain when followed by xpath + timeout (invalid syntax —
        # Page Should Contain takes a single text/locator). Convert to Wait Until.
        m = _re.match(r'^(\s+)Page Should Contain\s+(xpath:[^\s]+)\s+(\d+s?)\s*$', line)
        if m:
            line = f"{m.group(1)}Wait Until Page Contains Element    {m.group(2)}    {m.group(3)}"
        cleaned.append(line)
    return "\n".join(cleaned)


# ── Emoji stripper + DOM-grounded selector validator ─────────────────────────

# Unicode ranges for emojis / pictographs / dingbats commonly hallucinated by LLMs.
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
        # Collapse multiple spaces left over from emoji removal
        new_content = _re.sub(r'\s{2,}', ' ', new_content)
        # `closing` IS the closing quote (backref to group 2) — don't double it.
        return f"{before}{quote}{new_content}{closing}"

    # Match contains(text(),'...'), contains(normalize-space(),'...'), and
    # normalize-space()='...' patterns. Quote-aware via backrefs.
    pattern = _re.compile(
        r"(contains\((?:text\(\)|normalize-space\(\))\s*,\s*|normalize-space\(\)\s*=\s*)"
        r"(['\"])(.*?)(\2)",
        _re.DOTALL,
    )
    new_code = pattern.sub(_fix_literal, code)

    # Also strip any leftover emojis floating inside generic xpath text (rare)
    # but only on lines that look like locators (start with "xpath:" or contain "://").
    out_lines = []
    for line in new_code.split("\n"):
        if "xpath:" in line or "css:" in line:
            line = _EMOJI_RX.sub("", line)
        out_lines.append(line)
    return "\n".join(out_lines)


# Selector attribute values that ARE legitimately app-agnostic and should never
# be downgraded just because they happen not to appear in the captured DOM.
_GENERIC_ATTR_VALUES = {
    "submit", "button", "text", "password", "email", "search", "checkbox", "radio",
    "hidden", "file", "tel", "url", "number",  # input types
    "main", "navigation", "dialog", "alert",   # ARIA roles
}


def _extract_selector_tokens(line: str) -> list:
    """
    Return distinguishing literal values used in a Selenium locator on this
    line: things like the class name, id value, placeholder text, data-test
    value, aria-label. Used to check the locator against captured DOM.
    Generic input types (submit/text/password/email) are EXCLUDED — they're
    not hallucination candidates.
    """
    tokens = []
    # @attr='value'  /  @attr="value"  for attr in (class|id|placeholder|data-test|name|aria-label)
    for m in _re.finditer(
        r"@(class|id|placeholder|data-test|data-testid|data-test-id|name|aria-label)\s*=\s*"
        r"['\"]([^'\"]+)['\"]",
        line,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if not value:
            continue
        if value.lower() in _GENERIC_ATTR_VALUES:
            continue
        # type="..." values like 'submit' would be caught above too — skip them.
        tokens.append((attr, value))
    # contains(@class,'foo')
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
    appears IN THE SAME ATTRIBUTE in the captured DOM:

      @placeholder='Username'   must find  `placeholder="Username"` somewhere
      @class='foo'              must find  class containing word `foo`
      contains(@class,'foo')    must find  class containing substring `foo`

    Substring-anywhere checks (the older behavior) are too loose:
    `'Username'` appears in `<label>Username</label>` even when no input has
    `placeholder="Username"`. The attribute-aware check catches that.

    Saucedemo non-regression: real saucedemo selectors (inventory_item,
    product_sort_container, shopping_cart_link) ARE in the right attributes
    in saucedemo's DOM → grounded.
    """
    tokens = _extract_selector_tokens(line)
    # Also pick up contains(@attr,'value') tokens
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
        # Pure text-based / generic selector — never flag.
        return True, ""

    # Strict (exact attribute value) checks
    for attr, value in tokens:
        if attr == "class":
            # Token must appear as a word in a class="..." attribute
            pattern = (rf'class\s*=\s*[\'"][^\'"]*\b'
                       rf'{_re.escape(value)}\b[^\'"]*[\'"]')
            if not _re.search(pattern, dom_blob):
                return False, f"@{attr}='{value}'"
        else:
            # placeholder / id / data-test / name / aria-label — exact value
            pattern = rf'\b{attr}\s*=\s*[\'"]{_re.escape(value)}[\'"]'
            if not _re.search(pattern, dom_blob):
                return False, f"@{attr}='{value}'"

    # Looser (substring within attribute) checks for contains(@attr,'...')
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
    to use as a text-based fallback. Priority order:

      1. Any capitalized quoted literal within 6 lines above (closest = best).
      2. The [Documentation] line of the enclosing test case.
      3. The TC-NNN title at the top of the enclosing test case.
      4. "" if nothing usable is found (validator then leaves locator alone).
    """
    # 1) Nearby quoted text
    for i in range(line_idx, max(line_idx - 6, -1), -1):
        cur = lines[i]
        m = _re.search(r"['\"]([A-Z][^'\"]{2,40})['\"]", cur)
        if m:
            return m.group(1).strip()

    # 2 & 3) Walk up to the start of this test case
    for i in range(line_idx, -1, -1):
        cur = lines[i]
        m = _re.match(r'\s*\[Documentation\]\s+(.+)$', cur, _re.IGNORECASE)
        if m:
            words = m.group(1).strip().split()[:5]
            if words:
                return " ".join(words)
        # Reached the TC header (column 0, starts with TC-)
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
    text-based xpath built from `fallback_text`. Leaves indentation + keyword
    name + timeout intact.
    """
    if not fallback_text:
        return line  # nothing safe to fall back to — leave as-is
    safe_text = fallback_text.replace("'", "")
    new_locator = f"xpath://*[contains(normalize-space(),'{safe_text}')]"
    # \S+(?:[ ]\S+)* matches the full locator including single spaces inside
    # quoted attribute values (e.g. @placeholder='Employee Name'), stopping at
    # RF's argument separator (2+ spaces) — unlike [^\s]+ which stops too early.
    return _re.sub(r"(xpath:|css:)\S+(?:[ ]\S+)*", new_locator, line, count=1)


def _validate_selectors_against_dom(test_body: str, dom_blob: str) -> str:
    """
    Post-LLM safety net: for each generated locator that references a specific
    class/id/placeholder/data-test/aria-label value, check whether that value
    appears anywhere in the captured DOM blob (which includes the dashboard
    DOM, per-test module DOM, and login DOM). If NOT, the locator is almost
    certainly hallucinated — replace it with a text-based xpath built from the
    nearest visible-text hint on the same test case.

    Conservative: text-based xpath, css selectors, and @type='submit'-style
    generic locators are never modified.
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
            # No nearby text — keep original; the healer can still fix it later.
            out.append(line)
            continue
        # DOM-grounding gate on the FALLBACK text itself: if the fallback (often
        # the TC's [Documentation] line or its French title) does not appear in
        # the actual captured DOM, the downgrade would just produce another
        # broken selector. Leave the original alone so the healer can try.
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


# ── Credential extractor ──────────────────────────────────────────────────────

# Words that look like credentials but are really form labels — must never be
# accepted as a username or a password.
_LABEL_WORDS = {
    "username", "user", "userid", "user_name", "users",
    "login", "logon", "signin", "sign-in",
    "email", "e-mail", "mail",
    "identifiant", "identifiants", "utilisateur",
    "password", "passwd", "pass", "pwd", "secret", "motdepasse", "mdp",
    "first", "firstname", "last", "lastname", "name",
    "submit", "button", "field", "value", "input",
}


def _is_label_word(s: str) -> bool:
    return s.strip().lower() in _LABEL_WORDS


def _score_candidate(u: str, p: str, position: int, full_text: str) -> int:
    """
    Rank credential candidates. Higher = more likely to be the real login pair.

      +5 if username appears 3+ times in the MD (real login users are
         referenced repeatedly across TCs).
      +2 if it appears 2+ times.
      +3 if the match is within 200 chars of login / connexion / authentif
         keywords (i.e. the TC around the match IS an authentication test).
      -4 if the match is within 200 chars of ajout / création / create / add /
         new / nouveau keywords (i.e. it is create-new-user TEST DATA, not a
         login credential).

    SauceDemo non-regression: saucedemo MD has exactly one credential
    candidate, so ranking always returns the same answer.
    """
    score = 0
    freq = len(_re.findall(rf'\b{_re.escape(u)}\b', full_text, _re.IGNORECASE))
    if freq >= 3:
        score += 5
    elif freq >= 2:
        score += 2
    ctx = full_text[max(0, position - 200): position + 50].lower()
    if any(w in ctx for w in ("login", "connexion", "authentif", "log in")):
        score += 3
    if any(w in ctx for w in ("ajout", "création", "creation", "create",
                              "add ", "+ add", "new ", "nouveau", "nouvelle")):
        score -= 4
    return score


def _extract_default_credentials(test_cases: list[dict],
                                 raw_md: str = "") -> tuple[str, str]:
    """
    Scan test case steps/titles AND the raw MD (when provided) for the first
    valid (non-error) credential pair. Patterns are tried in most-specific-first
    order, and any match where the extracted username or password is a known
    label word is rejected.
    Returns (username, password) or ("", "") if none found.
    """
    # Build a search text that also includes raw markdown (catches lines like
    # "Données de test suggérées: `Username: Admin, Password: admin123`" which
    # the structured parser does not always preserve).
    full_text = raw_md + "\n"
    for tc in test_cases:
        full_text += " " + tc.get("title", "") + " " + tc.get("expected", "")
        for step in tc.get("steps", []):
            if isinstance(step, dict):
                full_text += " " + step.get("action", "") + " " + step.get("expected", "")
            else:
                full_text += " " + str(step)

    bad_user_substrings = ["locked", "invalid", "wrong", "bad", "incorrect", "invalide"]

    def _accept(u: str, p: str) -> bool:
        u = u.strip().rstrip(".,;:'\"`")
        p = p.strip().rstrip(".,;:'\"`")
        if not u or not p or u == p:
            return False
        if _is_label_word(u) or _is_label_word(p):
            return False
        if any(x in u.lower() for x in bad_user_substrings):
            return False
        # Username and password should be ASCII-ish — reject accented words that
        # are clearly natural-language (e.g. "croissant/décroissant").
        if not _re.match(r'^[\w.@+\-]+$', u) or not _re.match(r'^[\w.@+\-]+$', p):
            return False
        return True

    # Pattern A: "Username: Admin … Password: admin123"  (FR + EN, label first)
    # Multi-shot + ranked: an MD can contain BOTH the real login credentials
    # AND create-new-user test data (e.g. TC-002's "Username: newuser,
    # Password: password123" is data for creating a user, not for logging in).
    # We collect ALL candidates and rank them via _score_candidate so login
    # credentials win over create-user payloads. SauceDemo has exactly one
    # candidate so ranking returns the same answer — non-regression preserved.
    pat_a = (r'(?:username|user|email|login|identifiant|utilisateur|user\s*name)'
             r'\s*[=:]\s*[`"\']?(\S{3,40}?)[`"\']?[\s,;]+'
             r'(?:.{0,60}?)(?:password|pass(?:word)?|pwd|mot\s*de\s*passe|mdp)'
             r'\s*[=:]\s*[`"\']?(\S{4,40}?)[`"\']?(?:[\s,;.`"\']|$)')
    candidates = []
    for m in _re.finditer(pat_a, full_text, _re.IGNORECASE | _re.DOTALL):
        u_raw = m.group(1).strip().rstrip(".,;:'\"`")
        p_raw = m.group(2).strip().rstrip(".,;:'\"`")
        if _accept(u_raw, p_raw):
            candidates.append((
                _score_candidate(u_raw, p_raw, m.start(), full_text),
                m.start(), u_raw, p_raw,
            ))
    if candidates:
        # Highest score wins; ties broken by earliest position in the document.
        candidates.sort(key=lambda c: (-c[0], c[1]))
        best = candidates[0]
        try:
            print(f"   [CREDS] Picked best of {len(candidates)} candidate(s): "
                  f"{best[2]} (score={best[0]})")
        except Exception:
            pass
        return best[2], best[3]

    # Pattern B: French "Saisir 'Admin' dans le champ Username"
    #            English "Enter 'admin' in the Username field"
    #            "Type 'foo' into the Password input"
    fr_en_action = r'(?:saisir|entrer|taper|enter|type|input|fill(?:\s+in)?|set)'
    field_word = r'(?:champ|field|input|zone)'
    user_label = r'(?:username|user\s*name|user|email|login|identifiant|utilisateur)'
    pass_label = r'(?:password|pass(?:word)?|pwd|mot\s*de\s*passe|mdp)'

    user_value = ""
    pass_value = ""
    # Bridge between the captured value and the field label — must NOT cross
    # another quote (otherwise we'd jump past the value into the next step).
    bridge = r"[^`'\"\n.]{0,60}?"
    # Username: "<action> 'X' [in/dans] [the/le] [field/champ] Username"
    for m in _re.finditer(
        rf"{fr_en_action}\s+[`'\"]([^`'\"\n]{{2,40}})[`'\"]{bridge}{user_label}\b",
        full_text, _re.IGNORECASE,
    ):
        cand = m.group(1).strip()
        if cand and not _is_label_word(cand):
            user_value = cand
            break
    # Password: same pattern with password label
    for m in _re.finditer(
        rf"{fr_en_action}\s+[`'\"]([^`'\"\n]{{2,40}})[`'\"]{bridge}{pass_label}\b",
        full_text, _re.IGNORECASE,
    ):
        cand = m.group(1).strip()
        if cand and not _is_label_word(cand):
            pass_value = cand
            break
    if user_value and pass_value and _accept(user_value, pass_value):
        print(f"   👤 [CREDS] Found credentials (action-quoted): {user_value}")
        return user_value, pass_value

    # Pattern C: "username 'Admin'" or "Username = Admin" reverse-quote
    rev_user = _re.search(rf"{user_label}\s*[:=]?\s*[`'\"]([^`'\"\n]{{3,40}})[`'\"]",
                          full_text, _re.IGNORECASE)
    rev_pass = _re.search(rf"{pass_label}\s*[:=]?\s*[`'\"]([^`'\"\n]{{4,40}})[`'\"]",
                          full_text, _re.IGNORECASE)
    if rev_user and rev_pass:
        u, p = rev_user.group(1).strip(), rev_pass.group(1).strip()
        if _accept(u, p):
            print(f"   👤 [CREDS] Found credentials (label-quoted): {u}")
            return u, p

    # Pattern D: classic "user / pass" — only when a credential keyword is nearby
    # in the same line, to avoid matching natural-language phrases like
    # "croissant/décroissant".
    cred_context = (r'(?:username|user|email|login|identifiant|utilisateur|'
                    r'password|pwd|cred|account|compte)')
    for line in full_text.split("\n"):
        if not _re.search(cred_context, line, _re.IGNORECASE):
            continue
        m = _re.search(r'\b([\w.@+\-]{3,})\s*/\s*([\w.@+\-]{5,})\b', line)
        if m and _accept(m.group(1), m.group(2)):
            print(f"   👤 [CREDS] Found credentials (slash, with context): {m.group(1)}")
            return m.group(1), m.group(2)

    # Pattern E: last-resort "\w+_user near a 6+ char word"
    m = _re.search(r'\b(\w+_user)\b[^.\n]{0,80}?\b(\w{6,})\b', full_text, _re.IGNORECASE)
    if m and _accept(m.group(1), m.group(2)):
        print(f"   👤 [CREDS] Found credentials (fallback _user): {m.group(1)}")
        return m.group(1), m.group(2)

    print("   ⚠️  [CREDS] No credentials found in test cases")
    return "", ""


# ── Login-page detection (used to invalidate stale page_structure cache) ──────

_LOGIN_PAGE_INDICATORS = [
    "login-button", "login_button", "login_credentials", "login_credentials_wrap",
    "login_logo", "login_container", "login_wrapper", "login-box", "login_box",
    'data-test="username"', 'data-test="password"', 'data-test="login-button"',
    'data-test="login-container"',
    'name="login-button"', 'id="login-button"',
]


def _looks_like_login_page(html: str) -> bool:
    """
    Heuristic: returns True if the cached page_structure looks like the LOGIN
    page rather than a post-login DOM. We only flag it when several login-only
    indicators appear together — a single password field could appear on a
    real post-login page (e.g. an account settings page).
    """
    if not html:
        return True
    lowered = html.lower()
    hits = sum(1 for ind in _LOGIN_PAGE_INDICATORS if ind.lower() in lowered)
    return hits >= 2


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


# ── Nav-link extraction + per-test classifier ─────────────────────────────────

def _extract_nav_links(html: str) -> list:
    """
    Pull <a href="/..."> entries out of dashboard-DOM HTML, preserving any
    visible label text near the anchor. Returns [(href, label), ...] deduped.
    """
    nav_links = []
    if not html:
        return nav_links
    for m in _re.finditer(
        r'<a\b[^>]*href=["\'](/[^"\']{3,120})["\'][^>]*>(?:[^<]{0,80})?'
        r'(?:<[^>]+>([^<]{0,60})</[^>]+>)?',
        html, _re.IGNORECASE | _re.DOTALL,
    ):
        href = m.group(1)
        if any(href.endswith(x) for x in (".png", ".jpg", ".css", ".js")):
            continue
        inner = (m.group(2) or "").strip()
        # Look for any visible text near the link (broader window)
        near_text = m.group(0)
        for tm in _re.finditer(r'>([A-Za-z][^<]{2,40})<', near_text):
            inner = tm.group(1).strip()
            break
        # Also scan attribute values for human-readable hints
        for tm in _re.finditer(r'(?:title|aria-label|data-text)=["\']([^"\']{2,60})["\']', m.group(0)):
            if not inner:
                inner = tm.group(1).strip()
        nav_links.append((href, inner))
    seen_h = set()
    uniq = []
    for href, label in nav_links:
        if href not in seen_h:
            seen_h.add(href)
            uniq.append((href, label))
    return uniq


def _label_from_url(href: str) -> str:
    """Best-effort human label from a URL path: '/admin/viewSystemUsers' → 'admin viewSystemUsers'."""
    return _re.sub(r'[/_\-]+', ' ', href).strip()


def _tc_text(tc: dict) -> str:
    """Concatenate title + preconditions + steps into one searchable string."""
    parts = [str(tc.get("title", "")), str(tc.get("expected", ""))]
    pcs = tc.get("preconditions", [])
    if isinstance(pcs, list):
        parts.extend(str(p) for p in pcs)
    else:
        parts.append(str(pcs))
    for step in tc.get("steps", []):
        if isinstance(step, dict):
            parts.append(str(step.get("action", "")))
            parts.append(str(step.get("expected", "")))
        else:
            parts.append(str(step))
    return " ".join(parts).lower()


# Generic module keyword bank — apps tend to use the same vocabulary regardless
# of brand. Each entry maps a keyword to URL-path hints. The classifier scores a
# nav link by counting how many keyword hints land in BOTH the test text and
# the link's URL/label.
_MODULE_KEYWORDS = [
    # (keyword pattern in test text, regex hint that should appear in href/label)
    (r'\b(admin(?:istrator)?|user\s+management|user[s]?\b|gestion\s+(?:des?\s+)?utilisateur|role)', r'admin|user'),
    (r'\b(p\.?i\.?m\.?|employ(?:e|ee|é)|personnel|hr\b|human\s+resource)', r'pim|employee|personnel'),
    (r'\b(leave|cong[ée]|absence|holiday|vacation)', r'leave|absence'),
    (r'\b(time(?:sheet)?|attendance|punch|feuille\s+de\s+temps|pointage)', r'time|attendance|timesheet'),
    (r'\b(recruit(?:ment)?|candidate|candidat|vacanc(?:y|ies)|application)', r'recruit|candidate|vacancy'),
    (r'\b(performance|review|appraisal|kpi|objectif)', r'performance|review'),
    (r'\b(directory|annuaire|org[\s-]?chart)', r'directory|org'),
    (r'\b(buzz|feed|news\s*feed|social|post|publication)', r'buzz|feed|social'),
    (r'\b(dashboard|tableau\s+de\s+bord|home\s*page)', r'dashboard|home'),
    (r'\b(claim|frais|expense|reimburs)', r'claim|expense'),
    (r'\b(maintenance|nettoyage|purge)', r'maintenance'),
    (r'\b(report|rapport|statistic|analytics)', r'report|analytic'),
    (r'\b(my\s+info|profile|profil|mes\s+infos)', r'myinfo|profile|mydetails'),
    (r'\b(setting|config|param[èe]tre)', r'setting|config'),
]


def _classify_test_to_module(tc: dict, nav_links: list, base_root: str) -> str:
    """
    Decide which nav link a test most likely targets. Returns absolute URL or "".
    Score = direct keyword match between test text and link URL/label.

    Veto: tests about AUTHENTICATION (login, logout, change password) operate
    from the dropdown on the topbar and don't navigate to any module. Returning
    "" here keeps them on the post-login landing page where the dropdown lives.
    """
    if not nav_links:
        return ""
    text = _tc_text(tc)
    if not text.strip():
        return ""

    # Veto: authentication-flow tests never navigate to a module.
    # They use the topbar profile dropdown (Logout / Change Password) which is
    # available on any post-login page. Classifying them to /pim/viewMyDetails
    # (because "profil" is in the text) is a known false-positive.
    auth_veto = _re.compile(
        r'\b(d[ée]connexion|deconnexion|logout|log\s*out|sign\s*out|'
        r'connexion\s+(?:r[ée]ussie|[ée]chou[ée]e|valide|invalide)|'
        r'change\s+password|modifi(?:cation|er)\s+(?:du\s+|de\s+)?mot\s+de\s+passe|'
        r'authentif|sign\s*in)\b',
        _re.IGNORECASE,
    )
    if auth_veto.search(text):
        return ""

    best_score = 0
    best_url = ""

    for href, label in nav_links:
        href_l = href.lower()
        label_l = (label or "").lower()
        url_text = (label_l + " " + _label_from_url(href_l)).strip()
        score = 0

        # Direct label substring match in test text (strong signal)
        if label_l and len(label_l) >= 3 and label_l in text:
            score += 4
        # Generic keyword bank
        for kw_pat, url_hint_pat in _MODULE_KEYWORDS:
            if _re.search(kw_pat, text, _re.IGNORECASE) and _re.search(url_hint_pat, url_text, _re.IGNORECASE):
                score += 3
        # Word-by-word URL-segment match (handles vendor-specific paths)
        for seg in _re.findall(r'[a-z]{4,}', _label_from_url(href_l)):
            if _re.search(rf'\b{_re.escape(seg)}', text, _re.IGNORECASE):
                score += 1

        if score > best_score:
            best_score = score
            best_url = href

    if best_url and best_score >= 3:
        return base_root + best_url if best_url.startswith("/") else best_url
    return ""


def _inject_go_to(test_body: str, target_url: str) -> str:
    """
    Ensure a `Go To <target_url>` step is present right after `Open App And Login`
    when the test needs to be on a sub-module page. Idempotent — does nothing
    if a Go To with the same URL is already present.
    """
    if not target_url:
        return test_body
    # Already navigates to this URL?
    if _re.search(rf'(?im)^\s*Go To\s+{_re.escape(target_url)}\s*$', test_body):
        return test_body
    # Already navigates somewhere — trust the LLM's choice rather than overriding.
    if _re.search(r'(?im)^\s*Go To\s+\S+', test_body):
        return test_body

    lines = test_body.split("\n")
    new_lines = []
    injected = False
    for line in lines:
        new_lines.append(line)
        if not injected and _re.match(r'^\s*Open App And Login\b', line, _re.IGNORECASE):
            indent = _re.match(r'^(\s*)', line).group(1)
            new_lines.append(f"{indent}Go To    {target_url}")
            new_lines.append(f"{indent}Sleep    2s")
            injected = True
    return "\n".join(new_lines)


def _force_go_to(block: str, target: str) -> str:
    """
    Ensure the FIRST `Go To` in `block` points to `target`. Conservative
    override rules:
      - target == ""     → no-op (saucedemo path: classifier returns "" for
                           tests with no sub-module match)
      - no Go To found   → inject one after Open App And Login (via _inject_go_to)
      - existing Go To URL == target                  → no-op
      - existing Go To URL starts with ${BASE_URL}    → REWRITE (provably wrong,
                           since BASE_URL is the login URL and ${BASE_URL}/foo
                           always concatenates to .../auth/login/foo → 404)
      - existing URL is on the SAME domain BUT a different MODULE path than
        target → REWRITE. The LLM picked a wrong module for this test (e.g.
        `/buzz/viewBuzz` for a Time-at-Work dashboard widget). The classifier
        had a strong target signal; trust it.
      - any other URL (e.g. genuinely external)    → TRUST it.
    """
    if not target:
        return block
    m = _re.search(r'(?im)^(\s*)Go To\s+(\S+)\s*$', block)
    if not m:
        return _inject_go_to(block, target)
    existing = m.group(2)
    if existing == target:
        return block
    if existing.startswith("${BASE_URL}"):
        new_line = f"{m.group(1)}Go To    {target}"
        return block[:m.start()] + new_line + block[m.end():]

    # Same-domain wrong-module override
    if "://" in existing and "://" in target:
        try:
            from urllib.parse import urlparse
            ex_p, tg_p = urlparse(existing), urlparse(target)
            if ex_p.netloc and ex_p.netloc == tg_p.netloc:
                # Same host. Compare module segments (skip generic infrastructure
                # path segments like 'web', 'index.php', 'app').
                def _module_key(path: str) -> str:
                    segs = [s for s in path.split('/')
                            if s and s not in ('web', 'index.php', 'app', 'spa')]
                    return segs[0] if segs else ''
                if _module_key(ex_p.path) != _module_key(tg_p.path):
                    new_line = f"{m.group(1)}Go To    {target}"
                    return block[:m.start()] + new_line + block[m.end():]
        except Exception:
            pass
    return block  # trust LLM's URL


# ── Main generator ─────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# Phase A: catalog-based constrained planning path
# ══════════════════════════════════════════════════════════════════════════════

# Sized-down catalog view for the LLM (drops the long `selector` field —
# the LLM only needs id+role+label to plan; selectors are looked up at render
# time). Keeps prompts compact and prevents the LLM from copy-pasting raw
# selectors from the catalog into its output.

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
after it. Proceed to the actual test-specific action.

RULE 6 (MANDATORY): The FIRST step of EVERY test MUST be either
'open_app_and_login' or 'open_browser_only'. No exceptions.
  - Use 'open_app_and_login' when the test requires successful login.
  - Use 'open_browser_only' when the test operates on the login page itself
    (failed login, locked user, unauthenticated direct URL access).
After 'open_app_and_login' succeeds the browser is on the post-login page —
do NOT reference any login-form selector_id (username field, password field,
submit button) in any subsequent step."""


def _llm_plan_one(test_case: dict, catalog: dict,
                  default_user: str, default_pass: str) -> dict:
    """
    Ask the LLM to produce a structured plan for ONE test, given the slim
    catalog. Returns the parsed JSON dict, or {} on any failure (caller then
    falls back to legacy generation for that test).
    """
    slim = _slim_catalog(catalog)
    catalog_json = _json.dumps(slim, ensure_ascii=False, indent=None, separators=(",", ":"))

    # Test case description block
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
    Robust JSON extraction for LLM planner output.

    Cerebras (and sometimes other backends) occasionally appends commentary
    or duplicate keys AFTER a valid JSON object, producing errors like
    `Extra data: line N column 1`. This parser:

      1. Strips markdown fences if present.
      2. Tries plain json.loads.
      3. On failure, regex-finds the first balanced {...} block and tries
         parsing that.
      4. On failure, progressively truncates from the end until parse
         succeeds (drops trailing junk after the JSON closing brace).
      5. Returns {} if nothing parseable was found.

    No app-specific logic. Just robust syntax recovery.
    """
    if not content:
        return {}
    content = content.strip()
    if content.startswith("```"):
        content = "\n".join(content.split("\n")[1:])
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        content = content.strip()

    # Direct parse
    try:
        return _json.loads(content)
    except _json.JSONDecodeError:
        pass

    # Find first balanced JSON object via brace counting
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

    # Progressive truncation as final fallback
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
    first_kw = steps[0].get("keyword", "") if steps else ""
    if first_kw not in ("open_app_and_login", "open_browser_only"):
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
    Robot Framework test body on success, or "" on any failure (signals the
    caller to use the legacy patch-based path for that test).
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


def generate_rf_code(test_cases: list[dict], base_url: str, raw_md: str = "") -> str:
    """
    Generate Robot Framework code in batches.

    Flow:
      1. Load or discover login recipe from app_memory.
      2. Build Settings + Variables + Keywords in Python (no LLM involvement).
      3. Ask LLM to generate ONLY test case bodies, calling Open App And Login.
      4. Clean each batch output to strip stray headers/separators.
      5. Concatenate header + cleaned batches.

    raw_md is the original markdown text — used to extract credentials from
    metadata lines like "Données de test suggérées:" that the structured
    parser may not preserve.
    """
    # ── Step 0: Extract credentials early — needed for both discovery AND prompt ──
    default_user, default_pass = _extract_default_credentials(test_cases, raw_md=raw_md)

    # ── Step 1: Recipe ─────────────────────────────────────────────────────────
    if cache_enabled():
        print("   📚 [MEMORY] RF_USE_CACHE=1 — reading cached recipe if available...")
    else:
        print("   🔄 [MEMORY] Cache disabled (default) — fresh discovery this run.")
    stored = load_app_for_generation(base_url)
    # Use whatever is stored (even with null selectors — _build_header has fallbacks)
    recipe = stored if stored else {}

    # Recipe is missing OR has all-null selectors → re-discover.
    recipe_keys = ("username_selector", "password_selector", "submit_selector")
    needs_recipe = not recipe or all(not recipe.get(k) for k in recipe_keys)
    if needs_recipe:
        print("   🔍 Fetching login page HTML to discover recipe...")
        login_page_html = _fetch_page_html(base_url)
        if login_page_html or True:  # discover_login_recipe handles empty html via Playwright
            new_recipe = discover_login_recipe(login_page_html, base_url)
            if new_recipe and any(new_recipe.get(k) for k in recipe_keys):
                recipe = {**recipe, **new_recipe}  # preserve any non-null fields already cached
                save_app(base_url, recipe)
            else:
                print("   ⚠️  [MEMORY] Recipe discovery yielded nothing usable.")
        else:
            print("   ⚠️  Could not fetch HTML — using generic selectors.")
    else:
        print(f"   📖 [MEMORY] Cached recipe for {base_url} ({recipe.get('app_type', 'unknown')})")

    # ── Step 1b: Post-login page structure for selector derivation ─────────────
    page_html = stored.get("page_structure", "")
    cached_is_login = _looks_like_login_page(page_html)
    # Also reject suspiciously-tiny cached DOMs (race / SPA didn't mount the
    # previous time). This way RF_USE_CACHE=1 never silently feeds the LLM
    # a poisoned 69-char shell DOM.
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

    # ── Step 1c: Multi-module reconnaissance ───────────────────────────────────
    # Extract nav links from the dashboard, classify each test, then visit every
    # unique target page in ONE Playwright session. This ends the "single-DOM
    # starvation" problem — the LLM gets the actual DOM of each sub-page.
    base_root = _re.sub(r'/[a-z]+/index\.php/.*$', '', base_url, flags=_re.IGNORECASE)
    base_root = _re.sub(r'/+$', '', base_root)  # strip trailing slash
    nav_links = _extract_nav_links(page_html)

    test_to_module: dict = {}            # tc_id -> absolute url
    module_dom: dict = {}                # absolute_url -> compact dom (legacy path)
    module_catalogs: dict = {}           # absolute_url -> catalog (Phase A primary)

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
        # Phase A primary path: build verified catalogs for every page the
        # tests will touch. For single-page apps, this still produces
        # __login__ + __dashboard__ catalogs, which is exactly what the
        # constrained-planner path needs.
        module_catalogs = discover_catalogs_batch(
            base_url, recipe, default_user, default_pass, sorted(unique_modules)
        )
        # Fallback for SPAs where page_html was too small to extract nav links
        # (e.g. Vue.js apps not yet mounted at fetch time).
        # Guard: only fires when nav_links is empty AND unique_modules is still
        # empty. SauceDemo always has non-empty nav_links from its page_html,
        # so this block is unreachable for SauceDemo.
        if not nav_links and not unique_modules:
            catalog_nav = module_catalogs.get("__dashboard__", {}).get("nav", [])
            if catalog_nav:
                catalog_nav_links = [(n["href"], n.get("label", "")) for n in catalog_nav]
                for tc in test_cases:
                    mod_url = _classify_test_to_module(tc, catalog_nav_links, base_root)
                    tc_id = tc.get("id", "")
                    if mod_url and tc_id:
                        test_to_module[tc_id] = mod_url
                        unique_modules.add(mod_url)
                if unique_modules:
                    print(f"   🧭 [MEMORY] Fallback: classified {len(test_to_module)}/"
                          f"{len(test_cases)} tests via catalog nav links.")
                    sub_catalogs = discover_catalogs_batch(
                        base_url, recipe, default_user, default_pass, sorted(unique_modules)
                    )
                    module_catalogs.update(sub_catalogs)
        if unique_modules:
            # Legacy HTML recon stays as the Phase A safety net. Both paths run
            # in the same Playwright session-equivalent: same login, same URLs.
            module_dom = discover_modules_batch(
                base_url, recipe, default_user, default_pass, sorted(unique_modules)
            )
            # If multi-module recon picked up a fresh dashboard DOM, prefer it.
            fresh_dashboard = module_dom.get("__dashboard__", "")
            if fresh_dashboard and not _looks_like_login_page(fresh_dashboard):
                page_html = fresh_dashboard
    else:
        print("   ⚠️  [MEMORY] No credentials — skipping multi-module reconnaissance.")

    # ── Step 2: Build the file header in Python ────────────────────────────────
    header = _build_header(base_url, recipe)

    # Build a compact keyword-usage hint for the LLM prompts
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

    # ── Step 3: Batch generation ───────────────────────────────────────────────
    batch_size = 5
    batches = [test_cases[i:i + batch_size] for i in range(0, len(test_cases), batch_size)]
    all_test_bodies = ""

    # Build the global nav-links hint once (small, shared across batches)
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

    # ── Phase A primary path: catalog-based constrained planning ──────────────
    # For every test we have a catalog for, ask the LLM for a structured plan
    # and render it deterministically. Tests that succeed here SKIP the legacy
    # batch generation entirely. Tests that fail (or have no catalog) fall
    # through to the legacy path, which still runs through ALL existing patch
    # safety nets. SauceDemo non-regression: when classifier returns "" for
    # every saucedemo TC (no nav matches), `test_to_module` is empty, so the
    # catalog-path predicate `mod_url and module_catalogs.get(mod_url)` is
    # always False — every saucedemo TC takes the legacy path. Bit-identical.
    planned_bodies: dict = {}   # tc_id -> rendered body, when catalog path succeeded
    if module_catalogs:
        print(f"   🧱 [PLAN] Attempting catalog-based plans for {len(test_cases)} tests...")
        for tc in test_cases:
            tc_id = tc.get("id", "")
            if not tc_id:
                continue
            mod_url = test_to_module.get(tc_id, "")
            cat = module_catalogs.get(mod_url) if mod_url else None
            # Merge with login-page catalog so login-flow tests can reference
            # both the login form and any post-login element if needed.
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
        # If EVERY test in this batch already has a catalog-rendered body,
        # skip the legacy LLM batch call entirely — pure deterministic output.
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

            # Inline the per-test target URL so the LLM sees it next to the test
            mod_url = test_to_module.get(tc_id, "")
            if mod_url:
                part += f"TARGET PAGE URL: {mod_url}\n"
                part += (f"  → First action after Open App And Login MUST be: "
                         f"Go To    {mod_url}\n")
            tc_text_parts.append(part)

            # Build a per-test DOM block ONLY when we have a real sub-page DOM
            tc_dom = module_dom.get(mod_url, "") if mod_url else ""
            if tc_dom:
                # Cap per-test DOM tighter when the Cerebras fallback (~8192-token
                # context) is in the rotation. Combined with the dashboard cap
                # below this keeps total prompt size safely under that ceiling.
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

        # Adaptive dashboard cap:
        #   - When per-test DOMs are present, the dashboard DOM is largely
        #     redundant (each per-test DOM already has its module's elements),
        #     so we trim it hard to fit within Cerebras' 8192-token fallback.
        #   - When no per-test DOMs exist (SauceDemo path: no sub-modules match
        #     the classifier), we keep the original 3500-char cap — zero
        #     behavioral change for SauceDemo.
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

        # Use the actual extracted creds in the example so the LLM does not
        # hallucinate generic ones for tests where the MD does not specify them.
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
            # Defensive fallback: if the batch prompt overflows the fallback
            # LLM's context window (e.g. Cerebras' 8192-token ceiling), retry
            # the batch one test at a time with a stripped-down prompt. The
            # output is concatenated as if it had come from a single batch.
            err_msg = str(batch_err).lower()
            if ("context" in err_msg and "length" in err_msg) or "too long" in err_msg or "8192" in err_msg:
                print(f"   ⚠️  Batch {idx + 1} overflowed LLM context — splitting into singletons...")
                fallback_pieces = []
                for tc in batch:
                    single_tc_text = next(
                        (p for p in tc_text_parts if p.lstrip().startswith(f"ID: {tc.get('id','')}")),
                        f"ID: {tc.get('id','')} | Title: {tc.get('title','')}\n",
                    )
                    # Build a tiny per-test DOM block (single test only)
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
                # Not a context-length error — re-raise so the caller knows.
                raise

        # Strip markdown fences
        if batch_code.startswith("```"):
            batch_code = "\n".join(batch_code.split("\n")[1:])
            if batch_code.endswith("```"):
                batch_code = batch_code.rsplit("```", 1)[0]

        # Strip stray section headers, column-0 tags, separators
        batch_code = _clean_batch_code(batch_code)

        # Belt-and-suspenders: even if the LLM forgot, force-inject Go To for
        # tests that we know target a sub-module URL.
        batch_code = _enforce_go_to_per_test(batch_code, batch, test_to_module)

        # DOM-grounded safety net: downgrade hallucinated class/id/placeholder
        # locators (values not in any captured DOM) to text-based xpath.
        # Combines dashboard DOM + every per-test module DOM for this batch.
        dom_blob_parts = [page_html or ""]
        for tc in batch:
            mod_url = test_to_module.get(tc.get("id", ""), "")
            if mod_url and module_dom.get(mod_url):
                dom_blob_parts.append(module_dom[mod_url])
        batch_code = _validate_selectors_against_dom(
            batch_code, "\n".join(dom_blob_parts)
        )

        # Per-test completeness check: detect truncated test cases (e.g. when
        # Cerebras silently cuts the output mid-way: "TC-019\n    Open App\n").
        # For any TC in the batch whose generated body is missing or trivially
        # short, retry that one test in isolation with a stripped prompt.
        batch_code = _backfill_truncated_tests(
            batch_code, batch, test_to_module, module_dom, page_html,
            login_hint, default_user, default_pass, BASE_SYSTEM_PROMPT,
        )

        # For tests in this batch that ALREADY have a catalog-rendered body,
        # replace the legacy body with the catalog one (Phase A primary path
        # wins over the safety net). Tests not in `planned_bodies` keep their
        # legacy output untouched.
        if planned_bodies:
            for tc in batch:
                tc_id = tc.get("id", "")
                if tc_id and tc_id in planned_bodies:
                    # Replace the test's block in batch_code with the plan body
                    body = planned_bodies[tc_id]
                    if tc_id in batch_code:
                        # Find the start of this TC in batch_code and the next TC line
                        start = batch_code.find(tc_id)
                        # Find the next TC- header to delimit
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

    # ── Step 4: Assemble final file ────────────────────────────────────────────
    rf_out = header + "\n" + all_test_bodies.strip()
    if default_user and default_pass:
        rf_out = _re.sub(
            r'^(\s+Open App And Login)\s*$',
            rf'\1    {default_user}    {default_pass}',
            rf_out,
            flags=_re.MULTILINE
        )
    return rf_out


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
    Robot Framework test should have. Examples that count as truncated:
      - Only the TC header line (no body)
      - Just `Open App` (broken mid-keyword)
      - Fewer than 3 keyword calls under the TC header
    """
    if not body or not body.strip():
        return True
    lines = [ln for ln in body.split("\n")[1:] if ln.strip()]
    if len(lines) < 2:
        return True
    # Count actual keyword calls (indented, not [Tag], not blank)
    keyword_lines = [ln for ln in lines if (ln.startswith(" ") or ln.startswith("\t"))
                     and not ln.strip().startswith("[")]
    if len(keyword_lines) < 2:
        return True
    # Detect "Open App" without "And Login" — a known Cerebras truncation
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
    This catches Cerebras silent truncations that don't raise an exception.
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

            # Splice the new body in, replacing the (truncated) old one
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
    Uses `_force_go_to`, which:
      - injects a Go To when none exists,
      - rewrites a broken `${BASE_URL}/...` Go To (provably wrong since
        BASE_URL is the login URL),
      - leaves any legitimate absolute URL untouched.
    SauceDemo path: classifier returns "" for all tests → loop body is a no-op.
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
        # Find the matching test case by ID
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
