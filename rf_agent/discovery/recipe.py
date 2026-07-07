"""
Login recipe discovery — LLM-powered extraction of login form selectors.
"""

import asyncio
import concurrent.futures
import json
import re

from langchain.messages import SystemMessage, HumanMessage

from rf_agent.infrastructure.llm import get_smart_llm, invoke_with_retry
from rf_agent.discovery.cache import save_app


def _fetch_rendered_login_html_sync(base_url: str) -> str:
    async def _fetch() -> str:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(base_url, timeout=15000)
                await page.wait_for_timeout(2500)
                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            print(f"   ⚠️  Playwright login HTML fetch failed: {e}")
            return ""
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, _fetch()).result(timeout=30)
    except Exception:
        return ""


def _static_html_lacks_form(html: str) -> bool:
    if not html:
        return True
    lowered = html.lower()
    if "<input" not in lowered:
        return True
    if "type=\"password\"" not in lowered and "type='password'" not in lowered:
        return True
    return False


def _selector_grounded_in_html(rf_sel: str, html: str) -> bool:
    if not rf_sel or not html:
        return True

    def _attr_has_value(attr: str, value: str) -> bool:
        if attr == "type":
            return value.lower() in html.lower()
        if attr == "class":
            pattern = (rf'class\s*=\s*[\'"][^\'"]*\b'
                       rf'{re.escape(value)}\b[^\'"]*[\'"]')
            return re.search(pattern, html) is not None
        pattern = rf'\b{attr}\s*=\s*[\'"]{re.escape(value)}[\'"]'
        return re.search(pattern, html) is not None

    for m in re.finditer(
        r"@(class|id|placeholder|data-test|data-testid|aria-label|name|type)\s*=\s*['\"]([^'\"]+)['\"]",
        rf_sel,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if value and not _attr_has_value(attr, value):
            return False
    for m in re.finditer(
        r"contains\(\s*@(class|id|placeholder|data-test|data-testid|name|aria-label)\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
        rf_sel,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if not value:
            continue
        if attr == "class":
            pattern = (rf'class\s*=\s*[\'"][^\'"]*'
                       rf'{re.escape(value)}[^\'"]*[\'"]')
            if re.search(pattern, html) is None:
                return False
        else:
            pattern = (rf'\b{attr}\s*=\s*[\'"][^\'"]*'
                       rf'{re.escape(value)}[^\'"]*[\'"]')
            if re.search(pattern, html) is None:
                return False
    return True


def _derive_recipe_via_regex(html: str) -> dict:
    if not html:
        return {}
    pwd_match = re.search(
        r'<input\b[^>]*type=["\']password["\'][^>]*>',
        html, re.IGNORECASE,
    )
    if not pwd_match:
        return {}

    def _attr(tag: str, name: str) -> str:
        m = re.search(rf'\b{name}=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        return m.group(1) if m else ""

    pwd_tag = pwd_match.group(0)
    pwd_id = _attr(pwd_tag, "id")
    pwd_placeholder = _attr(pwd_tag, "placeholder")

    if pwd_id:
        password_sel = f"xpath://input[@id='{pwd_id}']"
    elif pwd_placeholder:
        password_sel = f"xpath://input[@placeholder='{pwd_placeholder}']"
    else:
        password_sel = "xpath://input[@type='password']"

    username_sel = ""
    for m in re.finditer(r'<input\b[^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        type_attr = _attr(tag, "type").lower()
        if type_attr in ("password", "submit", "checkbox", "radio", "hidden"):
            continue
        if type_attr and type_attr not in ("text", "email", "tel"):
            continue
        u_id = _attr(tag, "id")
        u_placeholder = _attr(tag, "placeholder")
        if u_id:
            username_sel = f"xpath://input[@id='{u_id}']"
        elif u_placeholder:
            username_sel = f"xpath://input[@placeholder='{u_placeholder}']"
        else:
            username_sel = "xpath://input[@type='text']"
        break

    submit_sel = "xpath://*[@type='submit']"
    if not username_sel:
        return {}
    return {
        "username_selector": username_sel,
        "password_selector": password_sel,
        "submit_selector": submit_sel,
    }


def discover_login_recipe(html: str, base_url: str) -> dict:
    """Use LLM to extract login recipe from page HTML. Falls back to regex."""
    print("   \U0001f9e0 [MEMORY] Discovering login recipe from page HTML...")

    if _static_html_lacks_form(html):
        print("   \U0001f501 Static HTML missing form elements — re-fetching rendered DOM via Playwright...")
        rendered = _fetch_rendered_login_html_sync(base_url)
        if rendered:
            html = rendered

    prompt = f"""Analyze this HTML from {base_url} and extract the login form details.

HTML:
{html[:5000]}

Return a JSON object with exactly these keys:
{{
  "username_selector": "RF-style selector for the username/email input",
  "password_selector": "RF-style selector for the password input",
  "submit_selector":   "RF-style selector for the login/submit button",
  "success_indicator": "Exact text visible on page AFTER successful login (e.g. 'Dashboard', 'Products', 'Home')",
  "app_type":          "Brief description (e.g. 'Vue.js SPA', 'React app', 'Django')",
  "notes":             "Special quirks if any (e.g. 'submit is input not button', 'logout in dropdown menu')"
}}

SELECTOR FORMAT — use Robot Framework style:
  xpath://input[@placeholder='Username']    ← preferred for inputs with placeholder
  xpath://input[@type='password']           ← for password fields
  xpath://button[normalize-space()='Login'] ← for buttons with text
  xpath://*[@type='submit']                 ← ALWAYS use this for submit; works for both <input> and <button>
  css:button.login-btn                      ← CSS fallback

CRITICAL — for the submit control ALWAYS use:
  xpath://*[@type='submit']
This is universal and avoids failures when the element is <input type="submit"> vs <button type="submit">.

MANDATORY:
- If you find ANY <input> or <button> tag in the HTML, you MUST return real selectors
  derived from those tags. Do NOT return all four selectors as null.
- Returning null is ONLY acceptable when the HTML truly contains no form-like elements.

Return ONLY the JSON object, no markdown fences, no explanation."""

    try:
        resp = invoke_with_retry(
            get_smart_llm,
            [
                SystemMessage(content=(
                    "You are an expert at analysing web HTML to extract login-form selectors. "
                    "Return only a valid JSON object."
                )),
                HumanMessage(content=prompt),
            ],
        )
        content = resp.content.strip()
        if content.startswith("```"):
            content = "\n".join(content.split("\n")[1:])
            content = content.rsplit("```", 1)[0]
        recipe = json.loads(content)
        print(f"   ✅ [MEMORY] Recipe discovered: {recipe.get('app_type', 'unknown app')}")
    except Exception as e:
        print(f"   ⚠️  [MEMORY] LLM recipe parse failed: {e}")
        recipe = {}

    keys = ("username_selector", "password_selector", "submit_selector")
    if not recipe or all(not recipe.get(k) for k in keys):
        regex_recipe = _derive_recipe_via_regex(html)
        if regex_recipe:
            print("   \U0001f6e0️  [MEMORY] LLM gave nulls — derived recipe from HTML via regex.")
            recipe = {**recipe, **regex_recipe}

    if recipe and html:
        bad = []
        for k in keys:
            sel = recipe.get(k, "")
            if sel and not _selector_grounded_in_html(sel, html):
                bad.append(k)
        if bad:
            regex_recipe = _derive_recipe_via_regex(html)
            if regex_recipe:
                print(f"   \U0001f6e0️  [MEMORY] LLM selectors not grounded in HTML "
                      f"(case mismatch / hallucinated value): {bad} — "
                      f"overriding with regex-derived selectors.")
                for k in bad:
                    if regex_recipe.get(k):
                        recipe[k] = regex_recipe[k]
    return recipe


def build_login_context(recipe: dict, base_url: str) -> str:
    """Build a prompt-ready context block from the stored recipe."""
    if not recipe:
        return ""

    lines = [
        "═" * 47,
        f"  APP-SPECIFIC LOGIN RECIPE (discovered from {base_url})",
        "═" * 47,
        f"App type : {recipe.get('app_type', 'unknown')}",
    ]
    if recipe.get("notes"):
        lines.append(f"Notes    : {recipe['notes']}")

    lines += [
        "",
        "Use this EXACT login sequence in every test case — do NOT deviate:",
    ]
    if recipe.get("username_selector"):
        lines.append(f"    Wait Until Element Is Visible    {recipe['username_selector']}    15s")
        lines.append(f"    Input Text    {recipe['username_selector']}    <USERNAME_FROM_TEST>")
    if recipe.get("password_selector"):
        lines.append(f"    Wait Until Element Is Visible    {recipe['password_selector']}    15s")
        lines.append(f"    Input Text    {recipe['password_selector']}    <PASSWORD_FROM_TEST>")
    if recipe.get("submit_selector"):
        lines.append(f"    Click Element    {recipe['submit_selector']}")
    if recipe.get("success_indicator"):
        lines.append(f"    Wait Until Page Contains    {recipe['success_indicator']}    15s")

    lines += [
        "",
        "MANDATORY: Use ONLY the selectors above for login.",
        "Do NOT substitute button for input or vice-versa.",
        "Do NOT invent alternative selectors.",
    ]
    return "\n".join(lines)
