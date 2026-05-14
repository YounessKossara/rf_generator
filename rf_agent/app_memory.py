"""
App Memory — Persists discovered login recipes and app patterns per base-URL domain.

Avoids re-running LLM reconnaissance on the same application and eliminates
hardcoded app-specific rules. Works with any web application.
"""

import asyncio
import concurrent.futures
import json
import os
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


def cache_enabled() -> bool:
    """
    Reading from the JSON cache is OPT-IN. Default = always re-discover live so
    the agent works correctly on any URL/MD without trusting stale state.
    Set RF_USE_CACHE=1 to re-enable cached reads (faster repeat iterations).
    Writes happen unconditionally — the file is just an inspection log.
    """
    return os.environ.get("RF_USE_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


# ── Persistence ────────────────────────────────────────────────────────────────

def load_all() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_app(base_url: str) -> dict:
    """
    Return the saved recipe for this domain, or {} if unknown.
    NOTE: callers in the GENERATOR path should use `load_app_for_generation`
    instead — this function is unconditional and is intended for the healer,
    which always wants whatever was last written by the generator in this run.
    """
    return load_all().get(_domain_key(base_url), {})


def load_app_for_generation(base_url: str) -> dict:
    """
    Cache-aware loader for the generator. Returns {} unless RF_USE_CACHE=1 is
    set, forcing fresh discovery every time. This guarantees the agent stays
    fully dynamic on any URL / MD by default.
    """
    if not cache_enabled():
        return {}
    return load_app(base_url)


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

    # DOM-grounded sanity check: XPath is CASE-SENSITIVE, so a selector with
    # `@placeholder='username'` will NEVER match an input with `placeholder="Username"`.
    # The LLM sometimes lower-cases attribute values when reading from HTML.
    # If any of the LLM's selector VALUES (placeholder/id/class/etc.) does not
    # appear VERBATIM (case-sensitive) in the actual HTML, override that
    # individual selector with the regex-derived one.
    if recipe and html:
        bad = []
        for k in keys:
            sel = recipe.get(k, "")
            if sel and not _selector_grounded_in_html(sel, html):
                bad.append(k)
        if bad:
            regex_recipe = _derive_recipe_via_regex(html)
            if regex_recipe:
                print(f"   🛠️  [MEMORY] LLM selectors not grounded in HTML "
                      f"(case mismatch / hallucinated value): {bad} — "
                      f"overriding with regex-derived selectors.")
                for k in bad:
                    if regex_recipe.get(k):
                        recipe[k] = regex_recipe[k]
    return recipe


def _selector_grounded_in_html(rf_sel: str, html: str) -> bool:
    """
    Verify the distinguishing literals in a recipe selector appear in the HTML
    AS THE SAME ATTRIBUTE (case-sensitive — XPath is case-sensitive).

      `@placeholder='username'`  must find  `placeholder="username"` in HTML
      `@class='foo'`             must find  class containing word `foo`
      `@type='submit'`           generic — case-insensitive substring is enough

    Substring-anywhere checks are NOT enough: e.g. `'username'` appears in
    `name="username"` even when the placeholder attribute is `"Username"`.
    Returns False when at least one literal is missing FROM ITS ATTRIBUTE.
    """
    if not rf_sel or not html:
        return True

    def _attr_has_value(attr: str, value: str) -> bool:
        if attr == "type":
            # Generic types ('submit', 'password', etc.) — case-insensitive,
            # location-agnostic substring is enough.
            return value.lower() in html.lower()
        if attr == "class":
            # `class` may have multiple space-separated tokens; the literal
            # must appear as a whole token within a class="..." attribute.
            pattern = (rf'class\s*=\s*[\'"][^\'"]*\b'
                       rf'{re.escape(value)}\b[^\'"]*[\'"]')
            return re.search(pattern, html) is not None
        # placeholder / id / data-test / name / aria-label — case-sensitive
        # exact value, within the SAME attribute in HTML.
        pattern = rf'\b{attr}\s*=\s*[\'"]{re.escape(value)}[\'"]'
        return re.search(pattern, html) is not None

    # Form A: @attr='value' — exact attribute equals value
    for m in re.finditer(
        r"@(class|id|placeholder|data-test|data-testid|aria-label|name|type)\s*=\s*['\"]([^'\"]+)['\"]",
        rf_sel,
    ):
        attr, value = m.group(1), m.group(2).strip()
        if value and not _attr_has_value(attr, value):
            return False
    # Form B: contains(@attr,'value') — value must appear inside the same attribute
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

                        # Wait for REAL navigation rather than a fixed sleep.
                        # SPA redirects can take >3s under load; using
                        # wait_for_url with a sensible timeout + network-idle
                        # wait removes the race where we captured the page
                        # BEFORE Vue.js redirected to the dashboard.
                        try:
                            await page.wait_for_url(
                                lambda u: ("auth/login" not in u
                                           and u.rstrip("/") != pre_login_url.rstrip("/")),
                                timeout=10000,
                            )
                        except Exception:
                            pass  # didn't navigate — login probably failed
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)

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
    # Suspiciously-tiny DOM means Playwright captured before the SPA mounted,
    # OR login silently failed leaving us on a near-empty shell. Either way,
    # caching this would produce a useless context for the LLM. Return "" so
    # the caller can fall back to its no-DOM behavior.
    if len(compact) < 400:
        print(f"   ⚠️  [MEMORY] Captured page_structure is only {len(compact)} chars — "
              f"too small to be useful (likely race / SPA not mounted). Not caching.")
        return ""
    save_app(base_url, {"page_structure": compact})
    print(f"   💾 [MEMORY] Saved page_structure for {_domain_key(base_url)} ({len(compact)} chars)")
    return compact


# ── Multi-module reconnaissance (one Playwright session, N pages) ─────────────

def discover_modules_batch(
    base_url: str,
    recipe: dict,
    username: str,
    password: str,
    module_urls: list,
) -> dict:
    """
    Single Playwright session: log in once, then visit each module URL and
    capture the DOM at each page. Returns {url: compact_dom_snippet} plus
    a special "__dashboard__" key holding the post-login landing-page DOM.

    This is the key piece that ends "single-dashboard HTML starvation" — the
    LLM (and the healer) get the actual DOM of every page each test will use.
    """
    if not module_urls and not (username and password):
        return {}
    print(f"   🌐 [MEMORY] Multi-module reconnaissance: 1 dashboard + "
          f"{len(module_urls)} sub-page(s)...")

    async def _fetch_all() -> dict:
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            print(f"   ⚠️  [MEMORY] Playwright not available: {e}")
            return {}

        results: dict = {}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(base_url, timeout=20000)
                await page.wait_for_timeout(2000)

                # Login (best-effort — same selectors used in discover_page_structure)
                login_ok = False
                if username and password:
                    usr_sel = rf_to_playwright(recipe.get("username_selector", "")) or "input[type='text']"
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    btn_sel = rf_to_playwright(recipe.get("submit_selector", "")) or "button[type='submit'],input[type='submit']"

                    try:
                        usr = page.locator(usr_sel).first
                        pwd = page.locator(pwd_sel).first
                        btn = page.locator(btn_sel).first
                        if await usr.count() > 0:
                            await usr.fill(username)
                        if await pwd.count() > 0:
                            await pwd.fill(password)
                        pre_url = page.url
                        if await btn.count() > 0:
                            await btn.click()
                            # Wait for REAL navigation (no fixed 3s race).
                            try:
                                await page.wait_for_url(
                                    lambda u: ("auth/login" not in u
                                               and u.rstrip("/") != pre_url.rstrip("/")),
                                    timeout=10000,
                                )
                            except Exception:
                                pass
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(1500)
                            try:
                                still_pwd = await page.locator("input[type='password']").count() > 0
                            except Exception:
                                still_pwd = False
                            url_changed = page.url.rstrip("/") != pre_url.rstrip("/")
                            login_ok = url_changed or not still_pwd
                            if login_ok:
                                print(f"   ✅ [MEMORY] Logged in as {username} — at {page.url}")
                            else:
                                print(f"   ⚠️  [MEMORY] Login appears to have FAILED — "
                                      f"URL still {page.url}.")
                    except Exception as e:
                        print(f"   ⚠️  [MEMORY] Login step error: {e}")

                # Capture dashboard DOM (whatever page we're on after login)
                try:
                    dashboard_html = await page.content()
                    results["__dashboard__"] = _extract_interactive_elements(dashboard_html)
                    if login_ok:
                        results["__dashboard_url__"] = page.url
                except Exception as e:
                    print(f"   ⚠️  [MEMORY] Dashboard capture failed: {e}")

                # Visit each module URL in turn — only when login worked,
                # otherwise we'd just capture the login page over and over.
                if login_ok:
                    for url in module_urls:
                        try:
                            print(f"   🔗 [MEMORY] Visiting {url}")
                            await page.goto(url, timeout=20000, wait_until="networkidle")
                            await page.wait_for_timeout(2000)
                            html = await page.content()
                            compact = _extract_interactive_elements(html)
                            results[url] = compact
                            print(f"   ✅ [MEMORY] Captured {url} ({len(compact)} chars)")
                        except Exception as e:
                            print(f"   ⚠️  [MEMORY] Failed to capture {url}: {e}")
                            results[url] = ""

                await browser.close()
        except Exception as e:
            print(f"   ⚠️  [MEMORY] Multi-module session failed: {e}")
        return results

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _fetch_all())
            return future.result(timeout=180)
    except Exception as e:
        print(f"   ⚠️  [MEMORY] Multi-module discovery timed out / failed: {e}")
        return {}


# ── Catalog-based discovery (Phase A — new path; runs alongside legacy) ──────

def discover_catalogs_batch(base_url: str, recipe: dict,
                            username: str, password: str,
                            module_urls: list) -> dict:
    """
    Phase A's catalog-producing companion to `discover_modules_batch`. One
    Playwright session: log in once, then walk each module URL and run the
    DOM catalog extractor. Returns:

        {
            "__login__":     <catalog of the login page>,
            "__dashboard__": <catalog of the post-login landing page>,
            "<module_url>":  <catalog>,  ...
        }

    Empty dict on Playwright failure. Caller (rf_generator) should treat an
    empty dict as "fall back to the legacy raw-HTML path" — Phase A's safety
    net invariant.
    """
    print(f"   📚 [CATALOG] Building catalogs for login + dashboard + "
          f"{len(module_urls)} sub-page(s)...")

    async def _run() -> dict:
        try:
            from playwright.async_api import async_playwright
            from rf_agent.dom_catalog import extract_catalog
        except Exception as e:
            print(f"   ⚠️  [CATALOG] dependencies unavailable: {e}")
            return {}

        results: dict = {}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context()
                page = await ctx.new_page()
                await page.goto(base_url, timeout=20000)

                # Wait for the SPA to actually mount the login form before
                # extracting the catalog. Use the password field's visibility
                # as the readiness signal (most reliable across frameworks).
                try:
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    await page.locator(pwd_sel).first.wait_for(state="visible", timeout=10000)
                except Exception:
                    # Generic fallback wait if the recipe selector doesn't match
                    await page.wait_for_timeout(3500)
                await page.wait_for_timeout(500)  # final settle

                # Login-page catalog BEFORE clicking submit
                try:
                    results["__login__"] = await extract_catalog(page)
                    print(f"   ✅ [CATALOG] Login page: {len(results['__login__']['elements'])} elements")
                except Exception as e:
                    print(f"   ⚠️  [CATALOG] Login-page extraction failed: {e}")

                # Login
                login_ok = False
                if username and password:
                    usr_sel = rf_to_playwright(recipe.get("username_selector", "")) or "input[type='text']"
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    btn_sel = rf_to_playwright(recipe.get("submit_selector", "")) or "button[type='submit']"
                    try:
                        pre_url = page.url
                        await page.locator(usr_sel).first.fill(username)
                        await page.locator(pwd_sel).first.fill(password)
                        await page.locator(btn_sel).first.click()
                        try:
                            await page.wait_for_url(
                                lambda u: ("auth/login" not in u
                                           and u.rstrip("/") != pre_url.rstrip("/")),
                                timeout=10000,
                            )
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)
                        try:
                            still_pwd = await page.locator("input[type='password']").count() > 0
                        except Exception:
                            still_pwd = False
                        login_ok = (page.url.rstrip("/") != pre_url.rstrip("/")) or not still_pwd
                        if login_ok:
                            print(f"   ✅ [CATALOG] Logged in as {username} — at {page.url}")
                    except Exception as e:
                        print(f"   ⚠️  [CATALOG] Login step error: {e}")

                # Dashboard catalog (whatever page we land on)
                if login_ok:
                    try:
                        results["__dashboard__"] = await extract_catalog(page)
                        print(f"   ✅ [CATALOG] Dashboard: "
                              f"{len(results['__dashboard__']['elements'])} elements, "
                              f"{len(results['__dashboard__']['nav'])} nav links")
                    except Exception as e:
                        print(f"   ⚠️  [CATALOG] Dashboard extraction failed: {e}")

                    # Module-page catalogs
                    for url in module_urls:
                        try:
                            print(f"   🔗 [CATALOG] Visiting {url}")
                            await page.goto(url, timeout=20000, wait_until="networkidle")
                            await page.wait_for_timeout(1500)
                            cat = await extract_catalog(page)
                            results[url] = cat
                            print(f"   ✅ [CATALOG] Captured {url} "
                                  f"({len(cat['elements'])} elements)")
                        except Exception as e:
                            print(f"   ⚠️  [CATALOG] Failed to capture {url}: {e}")
                            results[url] = {}

                await browser.close()
        except Exception as e:
            print(f"   ⚠️  [CATALOG] session error: {e}")
        return results

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _run())
            return future.result(timeout=180)
    except Exception as e:
        print(f"   ⚠️  [CATALOG] outer wrapper failed: {e}")
        return {}


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
