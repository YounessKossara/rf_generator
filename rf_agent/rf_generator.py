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

ASSERTION COUNTS:
  Re-clicking the same Add-to-cart / toggle button does NOT add a duplicate
  in most apps — they toggle. Only assert counts that the test description
  actually claims.

DO NOT INVENT VALUES OR DATA:
  If a test step says "search for an existing user", and the test description
  does not specify which one, use the SAME username that was used to log in
  (e.g. for OrangeHRM use "Admin"). Never invent strings like "existingemployee"
  or "John Doe" unless the test description provides them."""


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
    pat_a = (r'(?:username|user|email|login|identifiant|utilisateur|user\s*name)'
             r'\s*[=:]\s*[`"\']?(\S{3,40}?)[`"\']?[\s,;]+'
             r'(?:.{0,60}?)(?:password|pass(?:word)?|pwd|mot\s*de\s*passe|mdp)'
             r'\s*[=:]\s*[`"\']?(\S{4,40}?)[`"\']?(?:[\s,;.`"\']|$)')
    m = _re.search(pat_a, full_text, _re.IGNORECASE | _re.DOTALL)
    if m and _accept(m.group(1), m.group(2)):
        u, p = m.group(1).strip().rstrip(".,;:'\"`"), m.group(2).strip().rstrip(".,;:'\"`")
        print(f"   👤 [CREDS] Found credentials (label-first): {u}")
        return u, p

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


# ── Main generator ─────────────────────────────────────────────────────────────

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
    print("   📚 Checking app memory...")
    stored = load_app(base_url)
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
    if not page_html or cached_is_login:
        if cached_is_login and page_html:
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

    for idx, batch in enumerate(batches):
        print(f"   🤖 Generating batch {idx + 1}/{len(batches)} ({len(batch)} tests)...")

        tc_text_parts = []
        for tc in batch:
            part = f"ID: {tc['id']} | Title: {tc['title']}\n"
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
            tc_text_parts.append(part)

        batch_tcs_text = "\n".join(tc_text_parts)

        html_context_block = ""
        nav_hint = ""
        if page_html:
            # Extract <a href="..."> links that look like internal module navigation.
            nav_links = []
            for m in _re.finditer(
                r'<a\b[^>]*href=["\'](/[^"\']{3,120})["\'][^>]*>(?:[^<]{0,80})?'
                r'(?:<[^>]+>([^<]{0,60})</[^>]+>)?',
                page_html, _re.IGNORECASE | _re.DOTALL,
            ):
                href = m.group(1)
                # Skip pure asset / external paths
                if any(href.endswith(x) for x in (".png", ".jpg", ".css", ".js")):
                    continue
                inner = (m.group(2) or "").strip()
                # Look for any visible text near the link
                near_text = m.group(0)
                for tm in _re.finditer(r'>([A-Za-z][^<]{2,40})<', near_text):
                    inner = tm.group(1).strip()
                    break
                nav_links.append((href, inner))
            # Deduplicate
            seen_h = set()
            uniq_links = []
            for href, label in nav_links:
                if href not in seen_h:
                    seen_h.add(href)
                    uniq_links.append((href, label))
            if uniq_links:
                nav_lines = [f"  Go To    {{base}}{href}    # {label or '(no label)'}"
                             for href, label in uniq_links[:25]]
                # Strip /web/index.php/auth/login from the example base
                base_root = _re.sub(r'/[a-z]+/index\.php/.*$', '', base_url, flags=_re.IGNORECASE)
                nav_hint = (
                    f"\nKNOWN INTERNAL NAVIGATION LINKS (use Go To with the absolute URL "
                    f"to navigate to a module if the test's preconditions require it):\n"
                    + "\n".join(line.replace("{base}", base_root) for line in nav_lines)
                    + "\n"
                )

            html_context_block = f"""
PAGE HTML (real DOM from {base_url} after login — derive ALL selectors from this):
{page_html[:4500]}
{nav_hint}
MANDATORY: Use ONLY selectors that exist in the HTML above.
Look at @id, @class, @placeholder, button text, link text, aria-label in the HTML.
NEVER invent a selector that is not present in this HTML.
If a test's PRECONDITIONS state the user is on a specific module page (e.g.
"on the user-management page", "on the employee-list page", "on recruitment page"),
the FIRST step of the test body (after Open App And Login) must NAVIGATE there
using either `Go To    <module-url>` (preferred when the URL is in the
KNOWN INTERNAL NAVIGATION LINKS list) or by clicking the matching menu link.
Do NOT assume the user already is on that page — they are on the post-login
landing page (the dashboard) until you navigate.
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
        Wait Until Element Is Visible    <selector for the FEATURE BEING TESTED>    15s
        ...
        Capture Page Screenshot    TC-006_final.png
- For EVERY test that needs a logged-in session, use Open App And Login with
  {ex_user} / {ex_pass} — UNLESS the test description specifies different credentials
  (e.g. invalid-credentials tests).
- For tests that check unauthenticated access only, use Open Browser Only.
- Do NOT write Open Browser, Set Window Size, or login input steps — they are in the keyword.
- Do NOT write Close Browser — Test Teardown handles it automatically.
- Do NOT add a Wait/Page Should Contain for the success indicator right after Open App And Login —
  the keyword has already waited for it. Move directly to the test-specific action.
- Use Wait Until Element Is Visible (15s) before every element interaction (except <select>).
- Derive selectors from the PAGE HTML provided above — never guess class names.
- For features NOT visible in the PAGE HTML (e.g. requires navigating somewhere first),
  use a TEXT-based xpath like `xpath://*[contains(normalize-space(),'Quick Launch')]`
  rather than inventing class names.
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
