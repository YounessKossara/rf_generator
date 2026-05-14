# RF Generator — Project Memory

> Compact session-handoff summary. Read this FIRST in any new Claude session
> before touching the codebase. It replaces the need to grep through the
> codebase or re-read past conversations to get oriented.
>
> **Hard limit: this file MUST stay under 250 lines.** If you add a section,
> consider shrinking an older one. Future Claude sessions should be able to
> skim this in <30 seconds — anything longer belongs in code comments or a
> separate doc. Never paste this whole file into a prompt — section-scoped
> reads only.

## What this project is

FastAPI service at `127.0.0.1:8001` that takes a markdown test plan + a base
URL and produces a working Robot Framework `.robot` file + execution report.

- Frontend: minimal HTML/JS at `frontend/index.html`
- Backend: `main.py` (FastAPI)
- Pipeline: `md_parser` → `rf_generator` (LLM) → `rf_validator` → `rf_executor`
  (with optional `self_healer`)
- LLM provider: Groq via `tools/llm.py` (model: configurable; uses `langchain-groq`)

## Architecture (high level)

```
                          /api/generate-rf
markdown ──► md_parser ──► [recipe discovery] ──► [multi-module DOM recon] ──► LLM (batched 5 tcs) ──► .robot
                                                                          │
                                                                          ▼
                                              DOM-grounded selector validator
                                              + emoji stripper
                                              + Go To enforcement
                                              + typo fixes

                          /api/execute-rf
.robot ──► robot run ──► output.xml parsed ──► (if healable fail) ──► self_healer ──► retry (per-test)
```

The .robot file's `*** Settings ***`, `*** Variables ***`, and the
`Open App And Login` keyword are built in Python (`_build_header`).
**The LLM only writes test case BODIES.** This eliminates locator drift in
login across batches.

## Key files (where to look)

| Concern | File | Hot functions |
|---|---|---|
| Markdown → structured TCs | [rf_agent/md_parser.py](rf_agent/md_parser.py) | `parse_md` |
| Generation pipeline | [rf_agent/rf_generator.py](rf_agent/rf_generator.py) | `generate_rf_code`, `_build_header`, `_classify_test_to_module`, `_validate_selectors_against_dom`, `_inject_go_to`, `_clean_batch_code` |
| Per-domain login + module recon | [rf_agent/app_memory.py](rf_agent/app_memory.py) | `discover_login_recipe`, `discover_page_structure`, `discover_modules_batch`, `_extract_interactive_elements`, `cache_enabled`, `load_app_for_generation` |
| Syntax validation | [rf_agent/rf_validator.py](rf_agent/rf_validator.py) | `validate_rf_syntax`, `fix_rf_syntax` |
| Execution + healing loop | [rf_agent/rf_executor.py](rf_agent/rf_executor.py) | `execute_rf` |
| Self-healing | [rf_agent/self_healer.py](rf_agent/self_healer.py) | `_extract_credentials_from_rf`, `_extract_all_navigation_urls`, `fetch_page_html`, `heal_test_case` |
| LLM wrapper | [tools/llm.py](tools/llm.py) | `get_smart_llm`, `invoke_with_retry` |
| FastAPI entry | [main.py](main.py) | `/api/generate-rf`, `/api/execute-rf` |
| Manual healer CLI | [run_robot.py](run_robot.py) | invokes `execute_rf` from command line |

## How credentials are discovered (read carefully — used to be a top bug)

`_extract_default_credentials(test_cases, raw_md)` runs ordered regex patterns
against the structured test cases + the raw markdown text:

1. `Username: X, Password: Y` (label-first)
2. `Enter 'X' in Username field` (action-quoted, French + English)
3. `Username 'X'` / `Password 'Y'` (reverse-quoted)
4. `user / pass` ONLY when the line also contains a credential keyword (this
   avoids matching `croissant/décroissant` from French test titles)
5. Last-resort `\w+_user near a 6+ char word`

Each candidate runs through `_accept(u, p)` which rejects:
- Label words (`username`, `password`, `field`, etc.)
- Strings containing `locked`, `invalid`, `wrong` (we want the GOOD account)
- Strings with non-ASCII / accented characters (filters out French prose)

## How the multi-module reconnaissance works

1. After discovering the login recipe and capturing the **dashboard** DOM,
   `_extract_nav_links(html)` pulls all internal `<a href="/...">` anchors
   with their visible labels.
2. `_classify_test_to_module(tc, nav_links, base_root)` scores each test
   against each link using a generic keyword bank (`admin`, `pim`, `leave`,
   `recruitment`, `performance`, `time`, etc.) plus direct label/path matching.
3. Unique target module URLs are deduplicated.
4. `discover_modules_batch(base_url, recipe, user, pass, [urls])` opens **one**
   Playwright session, logs in, visits each module URL, and returns
   `{url: compact_dom}`.
5. In the batch prompt, each test gets its OWN per-test DOM block (only
   ~1500 chars per test) instead of the global dashboard DOM.
6. `_enforce_go_to_per_test` post-processes each batch and injects
   `Go To <module_url>` after `Open App And Login` if the LLM forgot.

This is what eliminates "single-dashboard HTML starvation".

## How the self-healer recovers a failed test

When a test fails with a healable error (selector not found / not visible /
locator errors), `rf_executor` loops up to 3 times per test:

1. Extract the failing test's block + error message.
2. Call `_extract_credentials_from_rf(tc_block)` — looks for
   `Open App And Login    <user>    <pass>` FIRST, falls back to legacy
   `Input Text` scraping if needed.
3. Call `_extract_all_navigation_urls(tc_block)` to get every `Go To` URL.
4. `fetch_page_html(base_url, needs_login=True, username, password,
   nav_urls=...)` logs in once, replays every navigation, and captures the
   DOM at the FINAL page (i.e. where the failure happened — NOT the dashboard).
5. `heal_test_case(...)` asks the LLM to fix the selector using THAT DOM
   plus the failure error.
6. Replace the test block, re-run ONLY that test (`--test "*TC-NNN*"`).
7. Merge results without re-running the whole suite.

## DOM-grounded selector validator (anti-hallucination safety net)

After each batch, `_validate_selectors_against_dom(test_body, dom_blob)`:

- For each line containing `xpath:` or `css:`, extract distinguishing tokens
  (`@class='...'`, `@id='...'`, `@placeholder='...'`, `@data-test='...'`,
  `@aria-label='...'`).
- If ANY token's value is NOT in the captured DOM AND is not generic
  (`submit`, `text`, `password`, ...), the locator is hallucinated.
- Downgrade to `xpath://*[contains(normalize-space(),'<nearby text>')]`
  using the closest quoted text literal on the same test case.
- Text-based xpath, css selectors, and generic `@type='submit'`-style
  locators are NEVER modified.

## Cache policy

| Where | Behavior |
|---|---|
| `load_app_for_generation(base_url)` | Returns `{}` unless `RF_USE_CACHE=1` is set. **Default = fresh discovery every run.** |
| `load_app(base_url)` | Unconditional — used by the healer to read whatever the generator just wrote. |
| `save_app(...)` | Always writes (file is an inspection log; never trusted at read time unless env flag is on). |

Set `RF_USE_CACHE=1` only when iterating fast on the same URL during dev.

## Generation rules baked into BASE_SYSTEM_PROMPT

- LLM only writes test bodies; never the `Open App And Login` keyword.
- Selectors must come from the PER-TEST PAGE DOM (or dashboard as fallback).
- After `Go To`, always `Sleep 2s` before interacting.
- No emojis in locators.
- Tests are SELF-CONTAINED — never assume state from a previous test.
- Optional UI actions use `Run Keyword And Return Status` so a missing button
  doesn't crash the whole test.
- Never invent test data: when "search for a user" doesn't specify which one,
  reuse the login username.

## Known-good behavior (don't regress)

- **saucedemo (`https://www.saucedemo.com`)**: 14/20 tests pass first-shot
  (70%) without self-healing. Baseline to preserve.
- **OrangeHRM (`https://opensource-demo.orangehrmlive.com`)**: under active
  improvement — the multi-module recon + per-test DOM + selector validator
  are designed to lift this rate without regressing saucedemo.

## Quick-test recipes

Manual run (no healing):
```
python -m robot output\robot_files\rf_gen_<id>.robot
```

Run with healing:
```
python run_robot.py output\robot_files\rf_gen_<id>.robot
```

FastAPI server:
```
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

## Environment variables

| Var | Purpose | Default |
|---|---|---|
| `RF_USE_CACHE` | If `1`, reuse cached recipe + page_structure for the generator | unset (fresh discovery) |
| `GROQ_API_KEY` | Groq LLM access | required |

## Recurring LLM mistakes the cleaner already fixes

| LLM mistake | Auto-fix in `_clean_batch_code` |
|---|---|
| `normal-space()` typo | rewritten to `normalize-space()` |
| `${LOCATION}` (undefined variable) | rewritten to `${_url}= Get Location` + Should Contain |
| `Page Should Contain xpath:// 15s` (invalid signature) | rewritten to `Wait Until Page Contains Element` |
| Section headers like `*** Test Cases ***` inside batch output | stripped |
| `[Documentation]` at column 0 | stripped |
| Markdown separators `--- TC-XXX ---` | stripped |
| Emojis inside locator literals | stripped |

## When something breaks — debugging order

1. `output/app_memory.json` — inspect what recipe and page_structure are stored.
2. `output/robot_files/rf_gen_<id>.robot` — read the generated file directly.
3. FastAPI logs from `uvicorn` show every step (📚 cache, 🌐 recon, 🤖 batches,
   🛡️ validator downgrades, 🔧 healer attempts).
4. Run the smoke test in `tests/smoke_helpers.py` if you change a helper —
   it covers credential extraction, classifier, Go To injection, and the
   selector validator.

## Working principles

- **Stay generic.** Anything hardcoded to one app (selector class, URL path,
  app name) is a bug. The agent must work on any URL + any MD.
- **Validate, don't trust.** The LLM hallucinates. Every output gets cleaned,
  validated, and downgraded as needed before it ships.
- **Healer is a safety net, not the main flow.** The generator must produce
  a `.robot` file that passes most tests when run manually with `python -m
  robot`. Healing exists for the long tail.

## Known recurring root-cause bugs (kill on sight)

1. **Wrong default credentials picked from create-user TEST DATA** —
   `_extract_default_credentials` used to take the FIRST `username:X password:Y`
   it found, which meant a TC's `Données de test suggérées: Username: newuser,
   Password: password123` (data for creating a user) would beat the real login
   `Username: Admin, Password: admin123` further down. Fix: `_score_candidate`
   ranks ALL Pattern-A matches by (a) frequency of the username in the MD,
   (b) proximity to login/connexion/authentif keywords, (c) negative score
   near ajout/création/create/add/new. Saucedemo unaffected (single candidate).

2. **Broken `${BASE_URL}/path` Go To** — `${BASE_URL}` is the login URL, so
   any `${BASE_URL}/admin/...` concatenates to `.../auth/login/admin/...`
   → guaranteed 404. Fix: `_force_go_to` rewrites a `${BASE_URL}/...` Go To
   to the classifier's absolute URL when one was identified. Any other URL
   is trusted. Saucedemo unaffected (classifier returns "" for saucedemo
   tests, so `_force_go_to` short-circuits at the top).

3. **Login tests duplicating the login** — LLM often writes
   `Open App And Login Admin admin123` AND THEN `Input Text Username Admin`
   + `Click submit`. The duplicate input fails because the username field is
   gone post-login. Fix: explicit "LOGIN TESTS" rule in `BASE_SYSTEM_PROMPT`
   telling the LLM to put credentials in keyword args and never re-enter
   them in the body.
