"""
Multi-module reconnaissance — single Playwright session visiting N module URLs.
"""

import asyncio
import concurrent.futures

from rf_agent.discovery.utils import rf_to_playwright, _extract_interactive_elements


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
    """
    if not module_urls and not (username and password):
        return {}
    print(f"   \U0001f310 [MEMORY] Multi-module reconnaissance: 1 dashboard + "
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

                try:
                    dashboard_html = await page.content()
                    results["__dashboard__"] = _extract_interactive_elements(dashboard_html)
                    if login_ok:
                        results["__dashboard_url__"] = page.url
                except Exception as e:
                    print(f"   ⚠️  [MEMORY] Dashboard capture failed: {e}")

                if login_ok:
                    for url in module_urls:
                        try:
                            print(f"   \U0001f517 [MEMORY] Visiting {url}")
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
            return pool.submit(asyncio.run, _fetch_all()).result(timeout=180)
    except Exception as e:
        print(f"   ⚠️  [MEMORY] Multi-module discovery timed out / failed: {e}")
        return {}
