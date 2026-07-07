"""
Catalog-based discovery (Phase A) — builds verified DOM catalogs per page.
"""

import asyncio
import concurrent.futures

from rf_agent.discovery.utils import rf_to_playwright


def discover_catalogs_batch(base_url: str, recipe: dict,
                            username: str, password: str,
                            module_urls: list) -> dict:
    """
    Phase A's catalog-producing companion to discover_modules_batch. One
    Playwright session: log in once, then walk each module URL and run the
    DOM catalog extractor. Returns:

        {
            "__login__":     <catalog of the login page>,
            "__dashboard__": <catalog of the post-login landing page>,
            "<module_url>":  <catalog>,  ...
        }

    Empty dict on Playwright failure.
    """
    print(f"   \U0001f4da [CATALOG] Building catalogs for login + dashboard + "
          f"{len(module_urls)} sub-page(s)...")

    async def _run() -> dict:
        try:
            from playwright.async_api import async_playwright
            from rf_agent.discovery.dom_catalog import extract_catalog
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

                try:
                    pwd_sel = rf_to_playwright(recipe.get("password_selector", "")) or "input[type='password']"
                    await page.locator(pwd_sel).first.wait_for(state="visible", timeout=10000)
                except Exception:
                    await page.wait_for_timeout(3500)
                await page.wait_for_timeout(500)

                try:
                    results["__login__"] = await extract_catalog(page)
                    print(f"   ✅ [CATALOG] Login page: {len(results['__login__']['elements'])} elements")
                except Exception as e:
                    print(f"   ⚠️  [CATALOG] Login-page extraction failed: {e}")

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

                if login_ok:
                    try:
                        results["__dashboard__"] = await extract_catalog(page)
                        print(f"   ✅ [CATALOG] Dashboard: "
                              f"{len(results['__dashboard__']['elements'])} elements, "
                              f"{len(results['__dashboard__']['nav'])} nav links")
                    except Exception as e:
                        print(f"   ⚠️  [CATALOG] Dashboard extraction failed: {e}")

                    for url in module_urls:
                        try:
                            print(f"   \U0001f517 [CATALOG] Visiting {url}")
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
            return pool.submit(asyncio.run, _run()).result(timeout=180)
    except Exception as e:
        print(f"   ⚠️  [CATALOG] outer wrapper failed: {e}")
        return {}
