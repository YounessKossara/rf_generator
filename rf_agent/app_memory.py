"""
App Memory — Persists discovered login recipes and app patterns per base-URL domain.

Avoids re-running LLM reconnaissance on the same application and eliminates
hardcoded app-specific rules. Works with any web application.
"""

import asyncio
import concurrent.futures
import json
import re
from pathlib import Path

from tools.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage


MEMORY_FILE = Path("output/app_memory.json")


# ── Key helpers ────────────────────────────────────────────────────────────────

def _domain_key(url: str) -> str:
    """Normalise a URL to a stable per-domain key."""
    m = re.match(r'https?://[^/]+', url)
    return m.group(0).rstrip('/') if m else url.rstrip('/')


# ── Persistence ────────────────────────────────────────────────────────────────

def load_all() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_app(base_url: str) -> dict:
    """Return the saved recipe for this domain, or {} if unknown."""
    return load_all().get(_domain_key(base_url), {})


def save_app(base_url: str, data: dict):
    """Merge and persist app data for this domain."""
    all_data = load_all()
    key = _domain_key(base_url)
    all_data[key] = {**all_data.get(key, {}), **data}
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(all_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"   💾 [MEMORY] Saved recipe for {key}")


# ── RF ↔ Playwright selector conversion ───────────────────────────────────────

def rf_to_playwright(rf_sel: str) -> str:
    """
    Convert a Robot Framework selector string to Playwright-compatible format.
      RF:         xpath://input[@placeholder='Username']  →  xpath=//input[@placeholder='Username']
      RF:         css:button.login-btn                   →  button.login-btn
    """
    if not rf_sel:
        return rf_sel
    if rf_sel.startswith("xpath:"):
        return "xpath=" + rf_sel[6:]   # "xpath:" → "xpath=", rest stays
    if rf_sel.startswith("css:"):
        return rf_sel[4:]              # strip "css:" prefix
    return rf_sel


# ── LLM-powered discovery ─────────────────────────────────────────────────────

def discover_login_recipe(html: str, base_url: str) -> dict:
    """
    Use the LLM to extract the login recipe from real page HTML.
    Returns a dict with selector keys; empty dict on failure.
    """
    print("   🧠 [MEMORY] Discovering login recipe from page HTML...")

    prompt = f"""Analyze this HTML from {base_url} and extract the login form details.

HTML:
{html[:3000]}

Return a JSON object with exactly these keys (use null if not found):
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
        return recipe
    except Exception as e:
        print(f"   ⚠️  [MEMORY] Could not discover recipe: {e}")
        return {}


# ── Post-login page structure discovery ───────────────────────────────────────

def _extract_interactive_elements(html: str) -> str:
    """
    Extract interactive element opening tags from full page HTML.
    Returns a compact string of button/input/select/a/option/span/div tags
    with their IDs, classes, placeholders and visible text — much more useful
    than a raw HTML[:4000] slice that mostly contains <head> content.
    """
    results = []
    seen: set = set()

    # buttons, inputs, selects, options, links with inner text
    for m in re.finditer(
        r'<(button|input|select|option|a)\b([^>]{0,400}?)(/?>)((?:[^<]{0,80})?)',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, closing, inner = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if not re.search(r'\b(id|class|placeholder|type|value|href)\s*=', attrs, re.IGNORECASE):
            continue
        entry = f"<{tag}{attrs}{closing}"
        if inner and closing == '>':
            entry += inner + f"</{tag}>"
        entry = entry[:300].strip()
        key = entry[:80]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    # spans and divs with id or meaningful class + optional text
    for m in re.finditer(
        r'<(span|div)\b([^>]{0,300}(?:id|class)=[^>]{0,200})>([^<]{0,60})',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, text = m.group(1), m.group(2), m.group(3).strip()
        if not re.search(r'\b(id|class)\s*=', attrs, re.IGNORECASE):
            continue
        entry = (f"<{tag}{attrs}>" + (text if text else ""))[:300].strip()
        key = entry[:80]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    if results:
        return "\n".join(results[:150])[:7000]
    return html[:4000]


def discover_page_structure(base_url: str, recipe: dict,
                            username: str = "", password: str = "") -> str:
    """
    Fetch the post-login page DOM using Playwright, extract interactive elements,
    cache the result in app_memory. Uses a thread pool to work from any context.
    """
    print(f"   🌐 [MEMORY] Fetching post-login page structure for {base_url}...")

    async def _fetch() -> str:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(base_url, timeout=15000)
                await page.wait_for_timeout(2000)

                if username and password:
                    usr_sel = rf_to_playwright(recipe.get("username_selector", "")) or "input[type='text']"
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    btn_sel = rf_to_playwright(recipe.get("submit_selector", "")) or "button[type='submit'],input[type='submit']"

                    usr = page.locator(usr_sel).first
                    pwd = page.locator(pwd_sel).first
                    btn = page.locator(btn_sel).first

                    if await usr.count() > 0:
                        await usr.fill(username)
                    if await pwd.count() > 0:
                        await pwd.fill(password)
                    if await btn.count() > 0:
                        await btn.click()
                        await page.wait_for_timeout(3000)
                        print(f"   ✅ [MEMORY] Logged in as {username}")

                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            print(f"   ⚠️  [MEMORY] Playwright fetch failed: {e}")
            return ""

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _fetch())
            full_html = future.result(timeout=45)
    except Exception as e:
        print(f"   ⚠️  [MEMORY] Page structure discovery failed: {e}")
        return ""

    if not full_html:
        return ""

    compact = _extract_interactive_elements(full_html)
    save_app(base_url, {"page_structure": compact})
    print(f"   💾 [MEMORY] Saved page_structure for {_domain_key(base_url)} ({len(compact)} chars)")
    return compact


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_login_context(recipe: dict, base_url: str) -> str:
    """
    Build a prompt-ready context block from the stored recipe.
    Injected into the LLM system prompt at generation and healing time.
    """
    if not recipe:
        return ""

    lines = [
        "═══════════════════════════════════════════════",
        f"  APP-SPECIFIC LOGIN RECIPE (discovered from {base_url})",
        "═══════════════════════════════════════════════",
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
