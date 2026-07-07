"""
Post-login page structure discovery — captures interactive DOM after authentication.
"""

import asyncio
import concurrent.futures

from rf_agent.discovery.cache import save_app, _domain_key
from rf_agent.discovery.utils import rf_to_playwright, _extract_interactive_elements


def discover_page_structure(base_url: str, recipe: dict,
                            username: str = "", password: str = "") -> str:
    """
    Fetch the post-login page DOM using Playwright, extract interactive elements,
    cache the result in app_memory.
    """
    print(f"   \U0001f310 [MEMORY] Fetching post-login page structure for {base_url}...")

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
                        try:
                            await page.wait_for_url(
                                lambda u: ("auth/login" not in u
                                           and u.rstrip("/") != pre_login_url.rstrip("/")),
                                timeout=10000,
                            )
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1500)

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
            full_html = pool.submit(asyncio.run, _fetch()).result(timeout=45)
    except Exception as e:
        print(f"   ⚠️  [MEMORY] Page structure discovery failed: {e}")
        return ""

    if not full_html:
        return ""

    compact = _extract_interactive_elements(full_html)
    if len(compact) < 400:
        print(f"   ⚠️  [MEMORY] Captured page_structure is only {len(compact)} chars — "
              f"too small to be useful (likely race / SPA not mounted). Not caching.")
        return ""
    save_app(base_url, {"page_structure": compact})
    print(f"   \U0001f4be [MEMORY] Saved page_structure for {_domain_key(base_url)} ({len(compact)} chars)")
    return compact
