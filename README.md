# RF Generator Agent

> **End-of-studies project** — AI-powered Robot Framework test generation and self-healing agent, part of the **OmniPlatform** multi-agent SDLC pipeline.

---

## Table of Contents

- [Overview](#overview)
- [OmniPlatform — the full pipeline](#omniplatform--the-full-pipeline)
- [Agent roles](#agent-roles)
- [RF Generator — architecture](#rf-generator--architecture)
- [LLM strategy](#llm-strategy)
- [Screenshots](#screenshots)
- [Setup](#setup)
- [Environment variables](#environment-variables)
- [Running the server](#running-the-server)
- [API reference](#api-reference)
- [Local LLM support](#local-llm-support)
- [Project structure](#project-structure)
- [Engineering decisions](#engineering-decisions)

---

## Overview

**RF Generator** is an autonomous agent that converts a structured markdown test plan into a fully executable [Robot Framework](https://robotframework.org/) `.robot` test suite, runs it against a live application, and **automatically heals selector failures** without human intervention.

It is designed to operate both:
- **Standalone** — via REST API or direct Python call
- **In pipeline** — integrated into OmniPlatform, where it receives tasks from Mission Control and processes them autonomously

The agent is capable of:

| Capability | Description |
|---|---|
| **DOM discovery** | Crawls the target web app with Playwright to build a real element catalog |
| **Two-phase generation** | Phase A uses a constrained catalog-based planner; Phase B falls back to a free-text LLM batch generator |
| **Syntax validation** | Pre-checks the `.robot` file before execution |
| **Test execution** | Runs Robot Framework tests and parses `output.xml` |
| **Self-healing (L1)** | Browser-side JavaScript corrections (no LLM, instant) |
| **Self-healing (L2)** | LLM-based: re-fetches live DOM, sends error + HTML to the LLM, replaces the broken test case |
| **Report generation** | HTML/log report from Robot Framework + optional `.docx` export |
| **Platform integration** | Heartbeat, status, task handoff via Mission Control REST API |
| **Telegram notifications** | Task status updates via OmniAgentSupervisor bot |

---

## OmniPlatform — the full pipeline

OmniPlatform is a **multi-agent SDLC system** where each agent handles one phase of the software quality lifecycle. A single user requirement flows end-to-end without manual intervention.

```
                        ┌─────────────────────────────────────────────────────┐
                        │                 MISSION CONTROL                     │
                        │          (OmniPlatform orchestrator)                │
                        │    Task queue · Agent registry · Memory · Monitor   │
                        └─────────────────┬───────────────────────────────────┘
                                          │  heartbeat / task assignment
           ┌──────────────────────────────┼──────────────────────────────────┐
           │                             │                                    │
           ▼                             ▼                                    ▼
  ┌─────────────────┐          ┌──────────────────┐              ┌────────────────────┐
  │ user_story_agent│          │  use_cases_agent  │              │    test_agent       │
  │                 │  ──────► │  (OmniAgent)      │  ──────────► │  (Playwright E2E)  │
  │ Requirement →   │          │                   │              │                    │
  │ User Stories    │          │ Crawls app DOM,   │              │ Generates and      │
  │                 │          │ writes markdown   │              │ executes Playwright │
  └─────────────────┘          │ test plan         │              │ test scenarios      │
                               └─────────┬─────────┘              └────────────────────┘
                                         │
                                         │  markdown test plan
                                         ▼
                               ┌──────────────────────┐
                               │      rf_agent         │  ◄── THIS PROJECT
                               │                       │
                               │  .robot generation    │
                               │  + execution          │
                               │  + self-healing       │
                               │  + report             │
                               └──────────┬────────────┘
                                          │
                                          ▼
                               ┌──────────────────────┐
                               │   Mission Control     │
                               │   (task → review)     │
                               │   .robot file sync    │
                               │   to agent memory     │
                               └──────────────────────┘
```

**Telegram bot (OmniAgentSupervisor)** monitors all agents in real time. The supervisor can:
- Query live agent status (`/status`)
- Approve tasks pending human review (`/approve <task_id>`)
- Retry failed tasks (`/retry <task_id>`)

---

## Agent roles

| Agent | Technology | Responsibility |
|---|---|---|
| `user_story_agent` | Python + LangChain | Transforms a product brief into structured user stories |
| `use_cases_agent` | Python + Playwright + LLM | Crawls the target application, generates detailed markdown test scenarios |
| `test_agent` | Python + Playwright + LLM | Executes automated Playwright E2E tests from the markdown plan |
| **`rf_agent`** | **Python + Robot Framework + LLM** | **Generates, executes, and self-heals Robot Framework tests** |
| `mission_control` | Next.js (TypeScript) | Orchestrates all agents: task queue, memory, monitoring, GitHub sync, Telegram bot |

---

## RF Generator — architecture

### Generation pipeline (inside rf_agent)

```
POST /api/generate-rf
         │
         ▼
   parse_md()                   ← markdown → list of test cases
         │
         ▼
   discover_login_recipe()      ← Playwright: find login selectors, save to cache
         │
         ▼
   discover_page_structure()    ← Playwright: extract interactive elements of landing page
         │
         ▼
   discover_modules_batch()     ← Playwright: crawl each nav section (dashboard, forms…)
         │
         ▼
   discover_catalogs_batch()    ← build DOM catalog per module (element fingerprints)
         │
         ▼
   ┌─────────────────────────────────────────────┐
   │              GENERATION                      │
   │                                              │
   │  Phase A — catalog planner                   │
   │  ├─ _llm_plan_one()   LLM picks steps        │
   │  │   from the catalog (constrained output)   │
   │  └─ step_renderer.py  steps → RF keywords    │
   │                                              │
   │  Phase B — legacy batch (fallback)           │
   │  └─ LLM generates free-text RF code          │
   │     + safety nets: selector validation,      │
   │       emoji stripping, Go To injection       │
   └─────────────────────────────────────────────┘
         │
         ▼
   validate_rf_syntax()         ← pre-execution syntax check
         │
         ▼
   .robot file saved
```

### Execution + self-healing pipeline

```
POST /api/execute-rf
         │
         ▼
   robot run (subprocess / python API)
         │
         ▼
   parse output.xml
         │
    ┌────┴─────┐
  PASS        FAIL
    │            │
    │    is_healable_error()?
    │            │
    │        YES │                       NO (assertion failure)
    │            ▼                         ▼
    │    for each failing TC:          mark as failed (no heal)
    │    ├─ extract TC block
    │    ├─ Playwright: log in + navigate to failure page
    │    ├─ capture live DOM
    │    ├─ heal_test_case() → LLM generates fix
    │    ├─ replace TC in .robot file
    │    └─ re-run ONLY that TC (up to 3 attempts)
    │            │
    │      healed?──YES──► add to passed
    │            │
    │           NO──────► still_failing[]
    │
    ▼
   merge results (no full re-run)
         │
         ▼
   complete_task() → Mission Control
   upload_to_agent_memory() → .robot file synced
```

---

## LLM strategy

The agent uses a **tiered LLM strategy with automatic failover**:

```
                 ┌────────────────────────────────────┐
                 │           LLM Request               │
                 └────────────────┬───────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │   Groq — llama-3.3-70b      │  PRIMARY (fast, free tier)
                    │   (smart tasks)             │
                    │   Groq — llama-3.1-8b       │  FAST (form data, classification)
                    └────────────┬────────────────┘
                       Rate limit│ / Auth error
                                 │ rotate key (up to 3 keys)
                    ┌────────────▼────────────────┐
                    │   Cerebras API              │  FALLBACK (OpenAI-compatible)
                    │   gpt-oss-120b / llama3.1-8b│
                    └─────────────────────────────┘
                       (or any local LLM — see below)
```

Key rotation is **automatic**: if a Groq API key hits a rate limit or auth error, the agent silently rotates to the next configured key (`GROQ_API_KEY_1`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`). If all keys fail, it switches to the Cerebras fallback, which uses the OpenAI-compatible protocol.

---

## Screenshots

### OmniPlatform — Task Board (Mission Control)

The Kanban board shows all tasks flowing through the pipeline. Tasks move from **Commentaire → En cours → Révision → Révision qualité → Terminé**. `rf_agent` and `test_agent` tasks are visible with their status and execution results.

![Task Board](docs/screenshots/task_board.png)

---

### OmniAgentSupervisor — Telegram Bot

The supervisor bot reports live agent status. All four agents (`userstoryagent`, `testagent`, `rfagent`, `usecasesagent`) are shown with their connection state. Commands `/status`, `/approve`, `/retry` are available directly from Telegram.

![Telegram Bot](docs/screenshots/telegram_bot.png)

---

### Mission Control — System Monitor

Real-time system resource monitoring integrated in OmniPlatform: CPU usage, memory, disk, GPU, network I/O, and top processes.

![System Monitor](docs/screenshots/system_monitor.png)

---

### Mission Control — GitHub Issues Sync

GitHub Issues can be imported as tasks and assigned directly to an agent (`rf_agent`, `test_agent`, `use_cases_agent`, `user_story_agent`). Bidirectional sync links tasks back to their source issue.

![GitHub Sync](docs/screenshots/github_sync.png)

> **Note:** Add screenshots to `docs/screenshots/` to populate the images above.

---

## Setup

**Prerequisites:** Python 3.10+, Node.js (for Mission Control), a Groq API key (free tier), Playwright Chromium.

```bash
# 1. Clone
git clone https://github.com/your-username/rf_generator.git
cd rf_generator

# 2. Create virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
# source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browser
playwright install chromium

# 5. Configure environment variables
copy .env.example .env        # Windows
# cp .env.example .env        # Linux / macOS
# → Edit .env with your API keys (see section below)
```

---

## Environment variables

Copy `.env.example` to `.env` and fill in your values:

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY_1` | **Yes** | Primary Groq API key ([console.groq.com](https://console.groq.com)) |
| `GROQ_API_KEY_2` | No | Second Groq key — rotated automatically on rate-limit |
| `GROQ_API_KEY_3` | No | Third Groq key |
| `CEREBRAS_API_KEY` | No | Fallback LLM ([inference.cerebras.ai](https://inference.cerebras.ai)) |
| `TRELLO_API_KEY` | No | Post failure cards to a Trello board |
| `TRELLO_TOKEN` | No | Trello token |
| `TRELLO_BOARD_ID` | No | Target Trello board ID |
| `MC_BASE_URL` | No | Mission Control URL (default: `http://localhost:3000`) |
| `MC_AGENT_NAME` | No | Name registered in Mission Control (default: `rf_agent`) |
| `MC_API_KEY` | No | Mission Control authentication token |
| `RF_USE_CACHE` | No | Set to `1` to skip re-crawling already-known apps |

---

## Running the server

```bash
# Activate the virtual environment first
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # Linux / macOS

# Start the RF Agent server
python -m uvicorn rf_agent.api.server:app --port 8001 --reload
```

Open **http://localhost:8001** for the web UI.

The agent automatically:
- Registers itself with Mission Control on startup (if `MC_BASE_URL` is set)
- Starts a heartbeat loop (every 30 s) to receive pipeline tasks
- Processes incoming tasks autonomously without user input

To run in standalone mode (no Mission Control), simply leave `MC_BASE_URL` unset. The agent will still serve the REST API.

---

## API reference

All endpoints are served under `http://localhost:8001`.

---

### `POST /api/generate-rf`

Parse a markdown test plan and generate a `.robot` file. This is Step 1: no tests are executed yet.

**Request body:**
```json
{
  "markdown_content": "# SauceDemo Tests\n\n## TC-01 Valid Login\n...",
  "base_url": "https://www.saucedemo.com"
}
```

**Response:**
```json
{
  "test_cases_parsed": 5,
  "test_cases": [...],
  "rf_code": "*** Settings ***\n...",
  "validation": { "valid": true, "errors": [] },
  "base_url": "https://www.saucedemo.com",
  "test_name": "rf_gen_1720000000",
  "robot_file": "output/robot_files/rf_gen_1720000000.robot"
}
```

---

### `POST /api/execute-rf`

Execute a `.robot` file with self-healing. This is Step 2.

**Request body:**
```json
{
  "rf_code": "*** Settings ***\n...",
  "base_url": "https://www.saucedemo.com",
  "test_cases": [...]
}
```

**Response:**
```json
{
  "status": "completed",
  "total": 5,
  "passed": 4,
  "failed": 1,
  "healed_tests": ["TC-03"],
  "still_failing": ["TC-05"],
  "healing_attempts": { "TC-03": 2, "TC-05": 3 },
  "report_path": "output/rf_reports/rf_gen_1720000000/report.html",
  "failed_tests": ["TC-05: Page should contain 'Success'"],
  "passed_tests": ["TC-01", "TC-02", "TC-03", "TC-04"]
}
```

---

### `GET /api/report/{test_name}`
Serve the Robot Framework HTML report in the browser.

### `GET /api/log/{test_name}`
Serve the Robot Framework log (keyword-level detail).

### `GET /api/download-robot/{test_name}`
Download the generated `.robot` source file.

### `GET /api/download-docx/{test_name}`
Download the test results as a formatted `.docx` document.

---

## Local LLM support

The agent's Cerebras fallback uses the **OpenAI-compatible API protocol**. This means you can point it to any local LLM server that exposes the same interface — no code change needed.

### Ollama (recommended)

```bash
# Install Ollama: https://ollama.com
ollama pull llama3.3        # or any model you prefer
ollama serve                # starts at http://localhost:11434
```

In your `.env`:
```env
CEREBRAS_API_KEY=ollama      # any non-empty string
```

In `rf_agent/infrastructure/llm.py`, change the Cerebras block:
```python
return ChatOpenAI(
    api_key="ollama",
    base_url="http://localhost:11434/v1",   # Ollama OpenAI-compat endpoint
    model="llama3.3",
    temperature=0.1,
)
```

### LM Studio

```env
CEREBRAS_API_KEY=lm-studio
```

```python
base_url="http://localhost:1234/v1",  # LM Studio default port
model="your-loaded-model-name",
```

This lets you run the entire pipeline **100% locally** — no cloud API keys needed.

---

## Project structure

```
rf_generator/
│
├── main.py                         # Legacy entry point (delegates to rf_agent/api/server.py)
├── requirements.txt                # Python dependencies
├── pyproject.toml                  # Project metadata
├── .env.example                    # Template — copy to .env
├── .gitignore
│
├── rf_agent/                       # Main Python package
│   │
│   ├── api/                        # FastAPI layer
│   │   ├── server.py               # App factory, static files, startup/shutdown hooks
│   │   ├── routes_generation.py    # POST /api/generate-rf
│   │   └── routes_execution.py     # POST /api/execute-rf + report/download endpoints
│   │
│   ├── parsing/
│   │   └── md_parser.py            # Markdown → structured test case list
│   │
│   ├── discovery/                  # App reconnaissance (Playwright-based)
│   │   ├── cache.py                # App recipe persistence (output/app_memory.json)
│   │   ├── recipe.py               # Login form discovery (selectors, credentials)
│   │   ├── page_structure.py       # Post-login landing page DOM extraction
│   │   ├── modules.py              # Per-module nav recon (sidebar links → DOM snapshots)
│   │   ├── catalogs.py             # DOM catalog assembly per module
│   │   ├── dom_catalog.py          # Element data structures and fingerprinting
│   │   └── utils.py                # Shared: rf_to_playwright(), _extract_interactive_elements()
│   │
│   ├── generation/                 # RF code generation
│   │   ├── orchestrator.py         # generate_rf_code() — main public entry point
│   │   ├── catalog_planner.py      # Phase A: constrained LLM plans from DOM catalog
│   │   ├── legacy_planner.py       # Phase B: batch LLM + safety nets (fallback)
│   │   ├── header_builder.py       # *** Settings *** / *** Variables *** sections
│   │   ├── credential_extractor.py # Auto-detect default login credentials from HTML
│   │   ├── module_classifier.py    # Map tests to app modules, inject Go To keywords
│   │   └── selector_validator.py   # Validate/downgrade selectors against live DOM
│   │
│   ├── rendering/
│   │   └── step_renderer.py        # Catalog step IDs → Robot Framework keyword lines
│   │
│   ├── execution/
│   │   └── executor.py             # Run robot, parse output.xml, self-healing loop
│   │
│   ├── healing/
│   │   ├── llm_healer.py           # L2 healing: live DOM fetch + LLM fix (up to 3 attempts)
│   │   └── runtime_healer.py       # L1 healing: browser-side JS corrections (no LLM)
│   │
│   ├── reporting/
│   │   ├── syntax_validator.py     # RF syntax pre-check before execution
│   │   └── docx_reporter.py        # Export results to .docx
│   │
│   ├── infrastructure/
│   │   ├── llm.py                  # LLM client: Groq (primary) + key rotation + Cerebras fallback
│   │   └── trello.py               # Trello card creation on test failure
│   │
│   └── platform/
│       └── mission_control.py      # OmniPlatform: register, heartbeat, task receive/complete
│
├── scripts/
│   └── run_robot.py                # Standalone runner: python scripts/run_robot.py <file.robot>
│
├── frontend/                       # Static web UI (HTML/CSS/JS) served at /
│
├── tests/
│   ├── test_md_parser.py
│   └── test_step_renderer.py
│
├── examples/                       # Sample markdown test plans
│
└── docs/
    └── screenshots/                # Platform screenshots (referenced in README)
```

**Runtime output** (all git-ignored):

```
output/
├── app_memory.json          # Cached app recipes — avoids re-crawling known apps
├── robot_files/             # Generated .robot files
└── rf_reports/
    └── <test_name>/
        ├── report.html      # Robot Framework HTML report
        ├── log.html         # Keyword-level execution log
        ├── output.xml       # Raw results (parsed by executor)
        ├── screenshots/     # Captured on test failure
        └── heal_<TC>_<n>/  # Intermediate results per healing attempt
```

---

## Engineering decisions

### Why Robot Framework?

Robot Framework provides **keyword-driven testing** with a readable, tabular syntax that domain experts (not just developers) can understand. Choosing it over raw Pytest or Playwright scripts means the generated test files are auditable and editable by QA engineers who may not write code. It also integrates with CI/CD via standard XML output that most reporting tools understand.

### Two-phase generation (Phase A / Phase B)

Generating reliable `.robot` files from an LLM in a single shot is fragile — the model may hallucinate selectors that don't exist in the real DOM. The two-phase approach addresses this:

- **Phase A** constrains the LLM to pick from a pre-built catalog of elements actually observed by Playwright. It can only reference elements that exist. This produces highly accurate tests for apps with stable, well-structured HTML.
- **Phase B** is a free-text batch generation with post-processing safety nets (emoji stripping, selector validation, Go To injection). It handles edge cases where the catalog is too sparse or the test requires multi-step reasoning that the constrained planner can't express.

### Self-healing — two levels

A first level of healing (L1) uses browser-side JavaScript to try quick corrections — no LLM call, no latency. Only when L1 can't fix the issue does the agent escalate to L2: re-capture the live DOM from the exact page where the test failed (replaying all navigation), send the error + DOM to the LLM, and ask for a replacement test case.

**Healable vs. non-healable errors** are distinguished before any healing attempt. Assertion failures (`should contain`, `should be equal`) are real test failures — the feature is broken. Selector errors (`not found`, `timed out`, `ElementNotFound`) mean the generated selector no longer matches the DOM — the test can be fixed without changing its intent.

### App recipe caching

Crawling a full application DOM with Playwright takes tens of seconds. The `discovery/cache.py` module persists the discovered login recipe, module structure, and element catalogs to `output/app_memory.json`. On subsequent runs against the same base URL, the agent reuses the cache (`RF_USE_CACHE=1`), making generation near-instant for known apps.

### Mission Control integration — heartbeat pattern

The agent does not require a persistent WebSocket connection to Mission Control. Instead, it sends a heartbeat POST every 30 seconds. The response carries any `assigned_tasks` for this agent. This design means:
- The agent can restart independently without losing tasks (MC holds the queue)
- Network failures between agent and MC are non-fatal (next heartbeat picks up)
- The agent can run in standalone mode with zero changes when MC is absent

---

## Tech stack

| Layer | Technology | Role |
|---|---|---|
| API server | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | HTTP server, async routes |
| LLM (primary) | [Groq](https://console.groq.com/) — `llama-3.3-70b-versatile` | Code generation, healing, classification |
| LLM (fallback) | [Cerebras](https://inference.cerebras.ai/) / Ollama / LM Studio | Rate-limit failover, local option |
| LLM framework | [LangChain](https://www.langchain.com/) | Message formatting, model abstraction |
| Browser automation | [Playwright](https://playwright.dev/) (Python) | DOM discovery, login replay, healing fetch |
| Test runner | [Robot Framework](https://robotframework.org/) | Test execution, output.xml generation |
| HTTP client | [httpx](https://www.python-httpx.org/) | Mission Control comms, static HTML fetching |
| Report export | [python-docx](https://python-docx.readthedocs.io/) | `.docx` test reports |
| Config | [python-dotenv](https://pypi.org/project/python-dotenv/) | `.env` loading |
| Platform | OmniPlatform (Mission Control) | Task orchestration, Telegram bot, agent registry |

---

## Running tests

```bash
# Unit tests (venv must be active)
python -m pytest tests/ -v

# Quick import sanity check
python -c "from rf_agent.api.server import app; print('OK')"

# Run a .robot file directly (no server needed)
python scripts/run_robot.py output/robot_files/your_test.robot
```

---

## Git workflow

```bash
# Start from an up-to-date main
git checkout main
git pull origin main

# Create a feature branch
git checkout -b feature/your-feature-name

# Work, commit
git add rf_agent/your_module.py
git commit -m "feat: short description of what and why"

# Push
git push origin feature/your-feature-name

# Open a Pull Request on GitHub for review before merging to main
```

**Never commit** `.env`, `.venv/`, or `output/` — these are already in `.gitignore`.
