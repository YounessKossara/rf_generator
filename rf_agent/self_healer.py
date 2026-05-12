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
from rf_agent.app_memory import rf_to_playwright, _extract_interactive_elements

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


def _needs_login(tc_rf_code: str, success_indicator: str = "") -> bool:
    """
    Check if a test case requires login before the failing action.
    Looks for password field interaction (universal across apps).
    """
    lines = tc_rf_code.lower()
    has_password_interaction = "input text" in lines and "password" in lines

    post_login_indicators = [
        "logout", "dashboard", "my info", "profile", "welcome",
        "dropdown", "menu", "sidebar", "account"
    ]
    if success_indicator:
        post_login_indicators.append(success_indicator.lower())

    has_post_login_content = any(kw in lines for kw in post_login_indicators)
    return has_password_interaction or has_post_login_content


def _extract_credentials_from_rf(tc_rf_code: str) -> tuple:
    """
    Extract username/password from the test case body.

    Priority order:
      1. `Open App And Login    <user>    <pass>`  (current architecture)
      2. `Input Text` selectors with user/password keywords (legacy / direct logins)
    """
    # ── Priority 1: parse the Open App And Login keyword call ──
    for line in tc_rf_code.split("\n"):
        m = re.match(
            r'^\s*Open App And Login\s+(\S+)\s+(\S+)\s*$',
            line, re.IGNORECASE,
        )
        if m:
            return m.group(1), m.group(2)

    # ── Priority 2: legacy Input Text scraping ──
    username = None
    password = None

    for line in tc_rf_code.split("\n"):
        stripped = line.strip()
        if not stripped.lower().startswith("input text"):
            continue

        parts = re.split(r'\s{2,}', stripped)
        if len(parts) < 3:
            continue

        selector = parts[1].lower()
        value = parts[2].strip()

        if any(kw in selector for kw in ["username", "user", "email", "login", "userid", "user_name"]):
            username = value
        elif any(kw in selector for kw in ["password", "pass", "pwd", "secret"]):
            password = value

    return username or "", password or ""


def _extract_target_url(tc_rf_code: str) -> str:
    """
    Extract the LAST navigation URL referenced by the test body. This is the
    page where the failure most likely occurred (so the healer should fetch its
    DOM, not the dashboard's).
    """
    last = ""
    for line in tc_rf_code.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("go to") or lower.startswith("navigate to") or lower.startswith("go back"):
            parts = re.split(r'\s{2,}', stripped, maxsplit=1)
            if len(parts) < 2:
                parts = stripped.split(None, 2)
            if len(parts) >= 2:
                last = parts[-1].strip()
    return last


def _extract_all_navigation_urls(tc_rf_code: str) -> list:
    """All navigation URLs in the order they appear — replay these on heal."""
    urls = []
    for line in tc_rf_code.split("\n"):
        stripped = line.strip()
        m = re.match(r'^(?:Go\s+To|Navigate\s+To)\s+(\S+)', stripped, re.IGNORECASE)
        if m:
            urls.append(m.group(1).strip())
    return urls


async def fetch_page_html(url: str, needs_login: bool = False,
                          username: str = "",
                          password: str = "",
                          target_url: str = "",
                          login_recipe: dict = None,
                          nav_urls: list = None) -> str:
    """
    JIT live-DOM capture for the healer.

    Logs in (best-effort) using the provided recipe, replays every navigation
    URL in `nav_urls` (or just `target_url` for backwards compat), and returns
    the compact interactive-element extract of the FINAL page — i.e. the DOM
    of the page where the test actually failed, NOT the dashboard.

    Returns plain HTML on Playwright failure (httpx fallback) so the LLM still
    has SOMETHING to work from.
    """
    nav_urls = list(nav_urls) if nav_urls else []
    if target_url and target_url not in nav_urls:
        nav_urls.append(target_url)

    if HAS_PLAYWRIGHT:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=15000)
                await page.wait_for_timeout(3000)

                if needs_login and username and password:
                    try:
                        print(f"   🔑 [HEALER] Logging in as {username}...")

                        # Use recipe selectors if available, otherwise generic fallbacks
                        recipe = login_recipe or {}
                        usr_sel = rf_to_playwright(recipe.get("username_selector", "")) if recipe.get("username_selector") else ""
                        pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) if recipe.get("password_selector") else ""
                        btn_sel = rf_to_playwright(recipe.get("submit_selector", "")) if recipe.get("submit_selector") else ""

                        if not usr_sel:
                            usr_sel = "input[type='text'], input[placeholder*='user' i], input[placeholder*='email' i]"
                        if not pwd_sel:
                            pwd_sel = "input[type='password']"
                        if not btn_sel:
                            btn_sel = "button[type='submit'], input[type='submit']"

                        usr = page.locator(usr_sel).first
                        pwd = page.locator(pwd_sel).first
                        btn = page.locator(btn_sel).first

                        if await usr.count() > 0 and await pwd.count() > 0:
                            await usr.fill(username)
                            await pwd.fill(password)
                            if await btn.count() > 0:
                                pre_url = page.url
                                await btn.click()
                                await page.wait_for_timeout(3000)
                                # Sanity: did login actually succeed?
                                try:
                                    still_pwd = await page.locator("input[type='password']").count() > 0
                                except Exception:
                                    still_pwd = False
                                url_changed = page.url.rstrip("/") != pre_url.rstrip("/")
                                if url_changed or not still_pwd:
                                    print(f"   ✅ [HEALER] Logged in — at {page.url}")
                                else:
                                    print(f"   ⚠️  [HEALER] Login appears FAILED — "
                                          f"DOM will be the login page.")
                    except Exception as login_err:
                        print(f"   ⚠️  [HEALER] Login attempt failed: {login_err}")

                # Replay every navigation URL the failing test goes through.
                # The DOM we capture afterwards is the page where the failure
                # most likely occurred — NOT the dashboard.
                for nav_url in nav_urls:
                    try:
                        print(f"   🔗 [HEALER] Replaying navigation to {nav_url}")
                        await page.goto(nav_url, timeout=20000, wait_until="networkidle")
                        await page.wait_for_timeout(2000)
                    except Exception as nav_err:
                        print(f"   ⚠️  [HEALER] Navigation to {nav_url} failed: {nav_err}")

                if nav_urls:
                    print(f"   📍 [HEALER] Capturing DOM at {page.url}")

                full_html = await page.content()
                await browser.close()
                # Use the same compact-element extractor as the generator so
                # the LLM sees buttons/inputs/headings rather than raw markup.
                compact = _extract_interactive_elements(full_html)
                return compact or full_html[:4000]
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
    attempt: int,
    app_recipe: dict = None
) -> str:
    """Ask LLM to fix a failing test case using real DOM context and app-specific recipe."""
    failing_selector = extract_failed_selector(error_message)
    app_recipe = app_recipe or {}

    # Build dynamic app context from recipe instead of hardcoded rules
    app_context = ""
    if app_recipe:
        app_context = f"""
APP-SPECIFIC CONTEXT (discovered from {base_url}):
- App type: {app_recipe.get('app_type', 'unknown')}
- Login username field: {app_recipe.get('username_selector', 'unknown')}
- Login password field: {app_recipe.get('password_selector', 'unknown')}
- Login submit button: {app_recipe.get('submit_selector', 'unknown')}
- Success indicator after login: {app_recipe.get('success_indicator', 'unknown')}
- Special notes: {app_recipe.get('notes', 'none')}

MANDATORY: Use ONLY the selectors above for login. Do NOT substitute them."""

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

{app_context}

GENERIC RULES:
- NEVER use @name selectors
- Prefer: @placeholder, @type, text content, CSS classes
- Use Wait Until Element Is Visible before every interaction (15s timeout)
- Add Sleep 2s after page navigation, Sleep 3s after Open Browser
- Use Set Window Size 1920 1080, never Maximize Browser Window
- For empty field tests, skip Input Text and click submit directly

RULES FOR FIXING:
- Return ONLY the fixed test case code (starting from TC name line)
- Do NOT include *** Settings ***, *** Variables ***, or *** Test Cases ***
- Keep [Documentation] line and Set Screenshot Directory line
- Keep all Capture Page Screenshot lines
- Use proper indentation (4 spaces under test case)

Return ONLY the fixed RF code, no explanations, no markdown fences."""

    response = invoke_with_retry(
        get_smart_llm,
        [
            SystemMessage(content=(
                "You are a Robot Framework self-healing expert. "
                "Return ONLY valid RF test case code. No explanations, no markdown, no section headers."
            )),
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
