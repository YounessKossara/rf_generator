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

async def _fetch_rendered_login_html(base_url: str) -> str:
    """
    Fetch the login page DOM **after** JavaScript has rendered it. Used as a
    fallback when the static HTML (e.g. httpx) only returns the React/Vue shell.
    """
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


def _static_html_lacks_form(html: str) -> bool:
    """True when the HTML appears to be a JS shell with no real form elements."""
    if not html:
        return True
    lowered = html.lower()
    # Need at least an input tag and either a password field or a submit button.
    if "<input" not in lowered:
        return True
    if "type=\"password\"" not in lowered and "type='password'" not in lowered:
        return True
    return False


def discover_login_recipe(html: str, base_url: str) -> dict:
    """
    Use the LLM to extract the login recipe from real page HTML.
    Falls back to a Playwright-rendered DOM when the static HTML is missing
    form elements (typical for SPAs).
    Returns a dict with selector keys; empty dict on failure.
    """
    print("   🧠 [MEMORY] Discovering login recipe from page HTML...")

    if _static_html_lacks_form(html):
        print("   🔁 Static HTML missing form elements — re-fetching rendered DOM via Playwright...")
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _fetch_rendered_login_html(base_url))
                rendered = future.result(timeout=30)
            if rendered:
                html = rendered
        except Exception as e:
            print(f"   ⚠️  [MEMORY] Rendered-HTML fetch failed: {e}")

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
- Even if the HTML looks like a SPA shell, if password/username/submit elements ARE
  present, derive selectors from them. Do NOT excuse a null answer with
  "the form is dynamically loaded" — work with whatever is in the HTML.

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

    # Fallback: when the LLM returns all-null selectors but the HTML clearly
    # has a login form, derive selectors via regex directly.
    keys = ("username_selector", "password_selector", "submit_selector")
    if not recipe or all(not recipe.get(k) for k in keys):
        regex_recipe = _derive_recipe_via_regex(html)
        if regex_recipe:
            print(f"   🛠️  [MEMORY] LLM gave nulls — derived recipe from HTML via regex.")
            # Preserve any non-null fields from the LLM (app_type, notes, etc.)
            merged = {**recipe, **regex_recipe}
            recipe = merged
    return recipe


def _derive_recipe_via_regex(html: str) -> dict:
    """
    Best-effort selector extraction from real HTML using regex. Only used as
    a fallback when the LLM returns all-null selectors. Returns {} if nothing
    usable is found.
    """
    if not html:
        return {}

    # Find the password input first — it's the most reliable anchor.
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

    # Username/text input — pick the first non-password text input.
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

    # Submit — universal selector works for both <input type=submit> and <button type=submit>.
    submit_sel = "xpath://*[@type='submit']"

    if not username_sel:
        return {}

    return {
        "username_selector": username_sel,
        "password_selector": password_sel,
        "submit_selector": submit_sel,
    }


# ── Post-login page structure discovery ───────────────────────────────────────

def _extract_interactive_elements(html: str) -> str:
    """
    Extract interactive + text-bearing element opening tags from full page HTML.
    Returns a compact string of button/input/select/a/option/h1-h6/p/label/span/div
    tags with their IDs, classes, placeholders and visible text. Works generically
    across React, Vue, Angular and plain server-rendered apps.
    """
    results = []
    seen: set = set()

    # 1. Interactive controls + headings/paragraphs/labels with inner text
    for m in re.finditer(
        r'<(button|input|select|option|a|h[1-6]|p|label|textarea)\b([^>]{0,400}?)(/?>)((?:[^<]{0,120})?)',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, closing, inner = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        # Inputs/buttons must have at least one identifying attr; headings/p/label are kept for their text
        is_text_tag = tag.lower() in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'label')
        if not is_text_tag and not re.search(
            r'\b(id|class|placeholder|type|value|href|aria-label|data-test|name)\s*=',
            attrs, re.IGNORECASE
        ):
            continue
        # Skip purely decorative <p>/<h*> with no text
        if is_text_tag and not inner:
            continue
        entry = f"<{tag}{attrs}{closing}"
        if inner and closing == '>':
            entry += inner + f"</{tag}>"
        entry = entry[:320].strip()
        key = entry[:90]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    # 2. Spans and divs with an id/class + optional inline text
    for m in re.finditer(
        r'<(span|div)\b([^>]{0,300}(?:id|class)=[^>]{0,200})>([^<]{0,80})',
        html, re.IGNORECASE | re.DOTALL
    ):
        tag, attrs, text = m.group(1), m.group(2), m.group(3).strip()
        if not re.search(r'\b(id|class)\s*=', attrs, re.IGNORECASE):
            continue
        # Skip noisy decorative spans with no class hint or text
        if not text and len(attrs) < 30:
            continue
        entry = (f"<{tag}{attrs}>" + (text if text else ""))[:320].strip()
        key = entry[:90]
        if key not in seen:
            seen.add(key)
            results.append(entry)

    if results:
        return "\n".join(results[:200])[:9000]
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
                        pre_login_url = page.url
                        await btn.click()
                        await page.wait_for_timeout(3000)

                        # Validate that login actually succeeded — URL changed
                        # OR the password field is no longer present.
                        post_login_url = page.url
                        try:
                            still_has_password = await page.locator("input[type='password']").count() > 0
                        except Exception:
                            still_has_password = False

                        url_changed = post_login_url.rstrip("/") != pre_login_url.rstrip("/")
                        login_succeeded = url_changed or not still_has_password

                        if not login_succeeded:
                            print(f"   ⚠️  [MEMORY] Login appears to have FAILED — "
                                  f"URL still {post_login_url}, password field still present.")
                            print(f"   ⚠️  [MEMORY] Skipping page_structure cache to avoid storing the login DOM.")
                            await browser.close()
                            return ""
                        print(f"   ✅ [MEMORY] Logged in as {username} — now at {post_login_url}")

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
