# RF Generator

AI-powered Robot Framework test generation agent. Converts a markdown test plan into executable `.robot` files, runs them against a live app, and automatically heals selector failures using LLM + live DOM capture.

Part of the **OmniPlatform** SDLC pipeline (RF Generator → Mission Control).

---

## How it works

```
Markdown test plan
      │
      ▼
  parse_md()          ← extracts test cases from markdown
      │
      ▼
discover_page_structure()   ← Playwright crawls the target app
      │
      ▼
  Phase A (catalog)   ← LLM plans steps against real DOM catalog
      │  (falls back to)
  Phase B (batch)     ← free-text LLM generation with safety nets
      │
      ▼
  .robot file         ← saved to output/robot_files/
      │
      ▼
  robot run           ← executed via robotframework
      │
      ▼
  self-healing        ← selector failures → LLM fix → retry (up to 3x)
      │
      ▼
  results JSON + HTML report
```

---

## Project structure

```
rf_generator/
├── main.py                         # FastAPI app entry point (delegates to rf_agent/api/)
├── requirements.txt
├── pyproject.toml
├── .env.example                    # copy to .env and fill in your keys
│
├── rf_agent/
│   ├── api/
│   │   ├── server.py               # FastAPI app, static mounts, startup
│   │   ├── routes_generation.py    # POST /api/generate-rf
│   │   └── routes_execution.py     # POST /api/execute-rf + report endpoints
│   │
│   ├── parsing/
│   │   └── md_parser.py            # markdown → test case list
│   │
│   ├── discovery/
│   │   ├── cache.py                # app recipe persistence (output/app_memory.json)
│   │   ├── recipe.py               # login recipe discovery via Playwright + LLM
│   │   ├── page_structure.py       # page DOM extraction
│   │   ├── modules.py              # multi-module recon (nav links → per-module DOM)
│   │   ├── catalogs.py             # DOM catalog build (element fingerprints)
│   │   ├── dom_catalog.py          # element catalog data structures
│   │   └── utils.py                # shared helpers (rf_to_playwright, extract_interactive_elements)
│   │
│   ├── generation/
│   │   ├── orchestrator.py         # generate_rf_code() — main entry point
│   │   ├── catalog_planner.py      # Phase A: constrained LLM plans against catalog
│   │   ├── legacy_planner.py       # Phase B: batch LLM generation + safety nets
│   │   ├── header_builder.py       # *** Settings *** / *** Variables *** blocks
│   │   ├── credential_extractor.py # detect default credentials from page HTML
│   │   ├── module_classifier.py    # classify tests to app modules + inject Go To
│   │   └── selector_validator.py   # validate/downgrade selectors against live DOM
│   │
│   ├── rendering/
│   │   └── step_renderer.py        # catalog step IDs → RF keyword lines
│   │
│   ├── execution/
│   │   └── executor.py             # run robot, parse output.xml, self-healing loop
│   │
│   ├── healing/
│   │   ├── llm_healer.py           # LLM-based healing (fetch DOM → ask LLM → fix)
│   │   └── runtime_healer.py       # browser-side JS healing (no LLM, fast)
│   │
│   ├── reporting/
│   │   ├── syntax_validator.py     # RF syntax pre-check before execution
│   │   └── docx_reporter.py        # export results to .docx
│   │
│   ├── infrastructure/
│   │   ├── llm.py                  # LLM client (Groq primary, Cerebras fallback)
│   │   └── trello.py               # Trello card creation for failures
│   │
│   └── platform/
│       └── mission_control.py      # OmniPlatform status updates
│
├── scripts/
│   └── run_robot.py                # standalone robot runner (no server needed)
│
├── tests/
│   ├── test_md_parser.py
│   └── test_step_renderer.py
│
├── frontend/                       # static UI served at http://localhost:8001
└── examples/                       # sample markdown test plans
```

---

## Setup

**Prerequisites:** Python 3.10+, a Groq API key (free tier works), Playwright Chromium.

```bash
# 1. Clone
git clone <repo-url>
cd rf_generator

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browser
playwright install chromium

# 5. Configure environment
copy .env.example .env          # Windows
# cp .env.example .env          # Linux / macOS
# Edit .env and add your API keys
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY_1` | Yes | Primary LLM (Groq `llama-3.3-70b-versatile`) |
| `GROQ_API_KEY_2` | No | Secondary Groq key (rotated on rate-limit) |
| `CEREBRAS_API_KEY` | No | Fallback LLM provider |
| `TRELLO_API_KEY` | No | Post failure cards to Trello |
| `TRELLO_TOKEN` | No | Trello token |
| `TRELLO_BOARD_ID` | No | Target Trello board |
| `MC_BASE_URL` | No | Mission Control URL (default: `http://localhost:8000`) |
| `MC_AGENT_NAME` | No | Agent name shown in Mission Control |
| `RF_USE_CACHE` | No | Set to `1` to skip re-crawling known apps |

---

## Running the server

```bash
.\.venv\Scripts\python.exe -m uvicorn rf_agent.api.server:app --port 8001 --reload
```

Open **http://localhost:8001** for the web UI.

---

## API endpoints

### `POST /api/generate-rf`
Parse a markdown test plan and generate a `.robot` file.

```json
{
  "markdown_content": "# Login Tests\n## TC-01 Valid Login\n...",
  "base_url": "http://localhost:8080"
}
```

Returns: parsed test cases, generated RF code, syntax validation, saved file path.

---

### `POST /api/execute-rf`
Execute a `.robot` file with self-healing.

```json
{
  "rf_code": "*** Settings ***\n...",
  "base_url": "http://localhost:8080",
  "test_cases": [...]
}
```

Returns: pass/fail counts, healed tests, still-failing tests, HTML report path.

---

### `GET /api/report/{test_name}`
Serve the Robot Framework HTML report.

### `GET /api/log/{test_name}`
Serve the Robot Framework log.

### `GET /api/download-robot/{test_name}`
Download the generated `.robot` file.

### `GET /api/download-docx/{test_name}`
Download the test results as a `.docx` report.

---

## Usage example

1. Write a markdown test plan (see `examples/` for samples):

```markdown
# SauceDemo Tests

## TC-01 Valid Login
Login with standard_user / secret_sauce and verify dashboard loads.

## TC-02 Invalid Login
Login with wrong_user / wrong_pass and verify error message appears.
```

2. Send it to the generator:

```bash
curl -X POST http://localhost:8001/api/generate-rf \
  -H "Content-Type: application/json" \
  -d '{"markdown_content": "...", "base_url": "https://www.saucedemo.com"}'
```

3. The agent will:
   - Crawl the target app to discover its DOM structure
   - Generate a `.robot` file tailored to the real selectors
   - Validate syntax before returning

4. Execute and self-heal:

```bash
curl -X POST http://localhost:8001/api/execute-rf \
  -H "Content-Type: application/json" \
  -d '{"rf_code": "...", "base_url": "https://www.saucedemo.com", "test_cases": [...]}'
```

Failing tests with selector errors are automatically fixed (up to 3 attempts each).

---

## Tech stack

| Layer | Technology |
|---|---|
| API server | FastAPI + Uvicorn |
| LLM | Groq (`llama-3.3-70b-versatile`) / Cerebras fallback |
| LLM framework | LangChain |
| Browser automation | Playwright (headless Chromium) |
| Test execution | Robot Framework |
| Docx export | python-docx |
| HTTP client | httpx |
| Config | python-dotenv |

---

## Output files

All generated files go under `output/` (git-ignored):

```
output/
├── app_memory.json          # cached app recipes (avoids re-crawling)
├── robot_files/             # generated .robot files
└── rf_reports/
    └── <test_name>/
        ├── report.html      # Robot Framework report
        ├── log.html         # Robot Framework log
        ├── output.xml       # raw results
        └── screenshots/     # captured on failure
```
