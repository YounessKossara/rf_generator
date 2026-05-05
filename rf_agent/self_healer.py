"""
RF Generator — Self-Healing Agent

When a test fails with selector errors ("not found", "not visible"),
this module automatically:
  1. Analyzes the error
  2. Fetches real DOM from the target page (with login if needed)
  3. Asks LLM to find the correct selector
  4. Returns fixed test case code
"""

import re
from tools.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# ── Error patterns that trigger healing ──
HEALABLE_PATTERNS = [
    "not found",
    "not visible",
    "Element with locator",
    "Element locator",
    "did not appear",
    "timed out",
    "ElementNotFound",
    "NoSuchElement",
    "Locator",
]

# ── Error patterns that should NOT trigger healing (real failures) ──
NON_HEALABLE_PATTERNS = [
    "should contain",
    "should be equal",
    "should not contain",
    "Page should contain",
    "Text should be",
    "Values should be",
]


def is_healable_error(error_message: str) -> bool:
    """Determine if the error is a selector issue (healable)."""
    error_lower = error_message.lower()
    for pattern in NON_HEALABLE_PATTERNS:
        if pattern.lower() in error_lower:
            return False
    for pattern in HEALABLE_PATTERNS:
        if pattern.lower() in error_lower:
            return True
    return False


def _needs_login(tc_rf_code: str) -> bool:
    """Check if a test case requires login before the failing action."""
    lines = tc_rf_code.lower()
    has_login = "input text" in lines and ("password" in lines or "admin" in lines)
    has_post_login = any(kw in lines for kw in [
        "logout", "dashboard", "my info", "profile", "welcome",
        "dropdown", "userdropdown", "sidebar", "menu"
    ])
    return has_login or has_post_login


def _extract_credentials_from_rf(tc_rf_code: str) -> tuple:
    """Extract username/password from a test case's Input Text lines."""
    username = "Admin"
    password = "admin123"
    for line in tc_rf_code.split("\n"):
        line_stripped = line.strip().lower()
        if "input text" in line_stripped and "username" in line_stripped:
            parts = line.strip().split()
            if len(parts) >= 4:
                username = parts[-1]
        if "input text" in line_stripped and "password" in line_stripped:
            parts = line.strip().split()
            if len(parts) >= 4:
                password = parts[-1]
    return username, password


async def fetch_page_html(url: str, needs_login: bool = False,
                          username: str = "Admin",
                          password: str = "admin123") -> str:
    """
    Fetch real DOM HTML using Playwright (headless Chrome).
    If needs_login=True, performs login first to get post-login DOM.
    """
    if HAS_PLAYWRIGHT:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=15000)
                await page.wait_for_timeout(3000)

                if needs_login:
                    try:
                        print("   🔑 [HEALER] Logging in to fetch post-login DOM...")
                        usr = page.locator("input[placeholder='Username']")
                        pwd = page.locator("input[placeholder='Password']")
                        if await usr.count() > 0:
                            await usr.fill(username)
                            await pwd.fill(password)
                            btn = page.locator("button[type='submit']")
                            if await btn.count() > 0:
                                await btn.click()
                                await page.wait_for_timeout(3000)
                                print("   ✅ [HEALER] Logged in, fetching post-login DOM")
                    except Exception as login_err:
                        print(f"   ⚠️  [HEALER] Login attempt failed: {login_err}")

                html = await page.content()
                await browser.close()
                return html[:4000]
        except Exception as e:
            print(f"   ⚠️  Playwright fetch failed: {e}, falling back to httpx")

    # Fallback: httpx (no JS, no login)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False) as client:
            resp = await client.get(url)
            return resp.text[:4000]
    except Exception as e:
        return f"Could not fetch page: {e}"


def extract_failed_selector(error_message: str) -> str:
    """Extract the failing selector from error message."""
    match = re.search(r"(?:xpath:)?//[^\s'\"]+(?:\[[^\]]*\])?", error_message)
    if match:
        return match.group(0)
    match = re.search(r"css:[^\s'\"]+", error_message)
    if match:
        return match.group(0)
    match = re.search(r"'([^']+)'", error_message)
    if match:
        return match.group(1)
    return ""


def extract_test_case_block(rf_code: str, tc_name: str) -> str:
    """Extract a single test case block from the full .robot file."""
    lines = rf_code.split("\n")
    tc_start = None
    tc_end = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("***") or stripped.startswith("#"):
            if tc_start is not None and stripped.startswith("***"):
                tc_end = i
                break
            continue
        if not line.startswith(" ") and not line.startswith("\t") and not stripped.startswith("***"):
            if tc_start is not None:
                tc_end = i
                break
            if tc_name.lower() in stripped.lower() or stripped.lower() in tc_name.lower():
                tc_start = i

    if tc_start is not None:
        if tc_end is None:
            tc_end = len(lines)
        return "\n".join(lines[tc_start:tc_end]).rstrip()
    return ""


def replace_test_case_block(rf_code: str, tc_name: str, new_tc_code: str) -> str:
    """Replace a single test case block in the .robot file with new code."""
    lines = rf_code.split("\n")
    tc_start = None
    tc_end = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("***") or stripped.startswith("#"):
            if tc_start is not None and stripped.startswith("***"):
                tc_end = i
                break
            continue
        if not line.startswith(" ") and not line.startswith("\t") and not stripped.startswith("***"):
            if tc_start is not None:
                tc_end = i
                break
            if tc_name.lower() in stripped.lower() or stripped.lower() in tc_name.lower():
                tc_start = i

    if tc_start is not None:
        if tc_end is None:
            tc_end = len(lines)

        clean_code = new_tc_code.strip()
        if clean_code.startswith("```"):
            code_lines = clean_code.split("\n")
            code_lines = code_lines[1:]
            if code_lines and code_lines[-1].strip() == "```":
                code_lines = code_lines[:-1]
            clean_code = "\n".join(code_lines)

        # Remove section headers the LLM might have added
        clean_lines = []
        skip_section = False
        for cline in clean_code.split("\n"):
            if cline.strip().startswith("*** Settings ***") or cline.strip().startswith("*** Variables ***"):
                skip_section = True
                continue
            if cline.strip().startswith("*** Test Cases ***"):
                skip_section = False
                continue
            if skip_section and (cline.startswith(" ") or cline.startswith("\t") or not cline.strip()):
                continue
            if not cline.startswith(" ") and not cline.startswith("\t") and cline.strip():
                skip_section = False
            clean_lines.append(cline)

        clean_code = "\n".join(clean_lines).strip()
        new_lines = lines[:tc_start] + clean_code.split("\n") + [""] + lines[tc_end:]
        return "\n".join(new_lines)

    return rf_code


def heal_test_case(
    tc_rf_code: str,
    error_message: str,
    page_html: str,
    base_url: str,
    attempt: int
) -> str:
    """Ask LLM to fix a failing test case using real DOM context."""
    failing_selector = extract_failed_selector(error_message)

    prompt = f"""You are a Robot Framework expert fixing a failing test.

FAILING TEST CODE:
{tc_rf_code}

ERROR MESSAGE:
{error_message}

FAILING SELECTOR: {failing_selector}

REAL PAGE HTML (from target app):
{page_html}

BASE URL: {base_url}
HEALING ATTEMPT: {attempt}/3

ORANGEHRM SPECIFIC RULES:
- Login inputs: placeholder='Username' and placeholder='Password'
- Submit: xpath://button[@type='submit']
- Logout is HIDDEN in a dropdown:
    Click Element    xpath://span[@class='oxd-userdropdown-tab']
    Sleep    1s
    Wait Until Element Is Visible    xpath://a[normalize-space()='Logout']    5s
    Click Element    xpath://a[normalize-space()='Logout']
- Errors: xpath://div[contains(@class,'oxd-alert')]
- Required field errors: xpath://span[contains(@class,'oxd-input-field-error')]

EMPTY FIELD RULES:
- Input Text ALWAYS requires 2 arguments: locator AND value
- For empty field tests, do NOT use Input Text. Just click submit directly.
- To clear a field: Clear Element Text    xpath://input[@placeholder='Username']

RULES:
- Return ONLY the fixed test case code (starting from TC name line)
- Do NOT include *** Settings ***, *** Variables ***, or *** Test Cases ***
- NEVER use @name selectors
- Use @placeholder, @type, text(), or class selectors from real HTML
- Add Wait Until Element Is Visible before interactions (15s timeout)
- Keep Set Screenshot Directory and Capture Page Screenshot lines
- Keep [Documentation] line
- Add Sleep    3s after Open Browser, Sleep    2s after clicks

Return ONLY the fixed test case RF code, nothing else."""

    response = invoke_with_retry(
        get_smart_llm,
        [
            SystemMessage(content="You are a Robot Framework self-healing expert. "
                          "Return ONLY valid RF test case code. No explanations, "
                          "no markdown fences, no section headers."),
            HumanMessage(content=prompt)
        ]
    )
    return response.content.strip()


def extract_tc_name_from_error(error_line: str) -> str:
    """Extract TC name from a failed test error line."""
    if ":" in error_line:
        return error_line.split(":")[0].strip()
    return error_line.strip()


def extract_base_url_from_rf_code(rf_code: str) -> str:
    """Extract ${BASE_URL} value from .robot file."""
    match = re.search(r'\$\{BASE_URL\}\s+(\S+)', rf_code)
    if match:
        return match.group(1)
    return ""
