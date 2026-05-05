"""
RF Generator — AI-powered Robot Framework test generation platform.

FastAPI entry point with endpoints:
  POST /api/generate-rf      — Parse MD, generate RF code, validate, execute
  GET  /api/report/{name}    — Serve HTML report
  GET  /api/download/{name}  — Download .robot file
  GET  /                     — Serve UI
"""

import sys
import asyncio
import os
import re
import time

# Fix Windows console encoding for emoji/unicode output
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path

from rf_agent.md_parser import parse_md
from rf_agent.rf_generator import generate_rf_code
from rf_agent.rf_validator import validate_rf_syntax, fix_rf_syntax
from rf_agent.rf_executor import execute_rf
from rf_agent.rf_docx_reporter import generate_rf_docx_report
from tools.trello import create_failure_card

# ── Config Mission Control ──
MC_BASE_URL = os.getenv("MC_BASE_URL", "http://localhost:3000")
MC_API_KEY  = os.getenv("MC_API_KEY", "")

# ── App ──
app = FastAPI(title="RF Generator — OmniPlatform")

# ── Output directories ──
os.makedirs("frontend", exist_ok=True)
os.makedirs("output/robot_files", exist_ok=True)
os.makedirs("output/rf_reports", exist_ok=True)

# ── Static files ──
app.mount("/static", StaticFiles(directory="frontend"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")


# ══════════════════════════════════════════
#  Mission Control Integration
# ══════════════════════════════════════════

async def register_to_mission_control():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            res = await client.post(
                f"{MC_BASE_URL}/api/agents/register",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={
                    "name": "rf-generator",
                    "role": "tester",
                    "capabilities": [
                        "rf-generation",
                        "rf-execution",
                        "test-parsing",
                        "report-generation",
                        "trello-integration"
                    ],
                    "framework": "python-fastapi"
                }
            )
            print(f"✅ RF Generator registered in Mission Control (status {res.status_code})")
    except Exception as e:
        print(f"⚠️  Mission Control not available — running standalone. ({e})")


async def update_agent_status(status: str):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.put(
                f"{MC_BASE_URL}/api/agents",
                headers={"Authorization": f"Bearer {MC_API_KEY}"},
                json={"name": "rf-generator", "status": status}
            )
    except Exception:
        pass


async def heartbeat_loop():
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                res = await client.post(
                    f"{MC_BASE_URL}/api/agents/rf-generator/heartbeat",
                    headers={"Authorization": f"Bearer {MC_API_KEY}"},
                    json={
                        "token_usage": {
                            "model": "llama-3.3-70b-versatile",
                            "inputTokens": 0,
                            "outputTokens": 0
                        }
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    print(f"💓 Heartbeat OK")
        except Exception:
            pass
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup():
    await register_to_mission_control()
    asyncio.create_task(heartbeat_loop())


# ══════════════════════════════════════════
#  Request / Response Models
# ══════════════════════════════════════════

class RFRequest(BaseModel):
    markdown_content: str
    base_url: str = "http://localhost:8080"


# ══════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════

@app.get("/")
async def serve_ui():
    return FileResponse("frontend/index.html")


@app.post("/api/generate-rf")
async def generate_rf(req: RFRequest):
    """
    Full pipeline:
      1. Parse markdown → structured test cases
      2. Generate RF code via LLM
      3. Validate (max 2 fix attempts)
      4. Execute .robot file
      5. Create Trello cards for failed tests
      6. Return results
    """
    await update_agent_status("busy")

    try:
        # ── Step 1: Parse markdown ──
        print("📝 Step 1: Parsing markdown...")
        test_cases = parse_md(req.markdown_content)

        if not test_cases:
            raise HTTPException(
                status_code=400,
                detail="No test cases found in the markdown content. "
                       "Make sure your TCs use the format: TC001 - Title"
            )

        print(f"   ✅ Found {len(test_cases)} test case(s)")
        for tc in test_cases:
            print(f"      → {tc['id']}: {tc['title']}")

        # ── Step 2: Generate RF code ──
        print("🤖 Step 2: Generating Robot Framework code via LLM...")
        rf_code = generate_rf_code(test_cases, req.base_url)
        print(f"   ✅ Generated {len(rf_code)} characters of RF code")

        # ── Step 3: Validate (max 2 fix attempts) ──
        print("🔍 Step 3: Validating RF syntax...")
        validation = validate_rf_syntax(rf_code)
        fix_attempts = 0

        while not validation["valid"] and fix_attempts < 2:
            fix_attempts += 1
            print(f"   ⚠️  Validation failed ({len(validation['errors'])} errors). Fix attempt {fix_attempts}/2...")
            for err in validation["errors"]:
                print(f"      → {err}")

            rf_code = fix_rf_syntax(rf_code, validation["errors"])
            validation = validate_rf_syntax(rf_code)

        if validation["valid"]:
            print("   ✅ RF syntax is valid!")
        else:
            print("   ⚠️  RF code still has issues after 2 fix attempts, proceeding anyway...")
            for err in validation["errors"]:
                print(f"      → {err}")

        # ── Step 4: Execute ──
        print("🚀 Step 4: Executing Robot Framework tests...")
        test_name = f"rf_run_{int(time.time())}"
        execution_result = await execute_rf(rf_code, test_name)
        print(f"   ✅ Execution result: {execution_result['status']}")

        # ── Step 5: Create Trello cards for failures ──
        if execution_result.get("failed_tests"):
            print("📋 Step 5: Creating Trello cards for failed tests...")
            for failed_test in execution_result["failed_tests"]:
                try:
                    create_failure_card(
                        test_id=failed_test.split(":")[0].strip() if ":" in failed_test else failed_test,
                        error_details=failed_test,
                    )
                except Exception as e:
                    print(f"   ⚠️  Trello card creation failed: {e}")

        # ── Step 5b: Generate DOCX report ──
        docx_path = None
        try:
            print("📝 Step 5b: Generating DOCX report...")
            docx_results = {
                **execution_result,
                "test_name": test_name,
            }
            docx_bytes = generate_rf_docx_report(
                results=docx_results,
                rf_code=rf_code,
                test_cases=test_cases,
                base_url=req.base_url,
            )
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
            docx_file = Path(f"output/rf_reports/{safe_name}/report.docx")
            docx_file.parent.mkdir(parents=True, exist_ok=True)
            docx_file.write_bytes(docx_bytes)
            docx_path = str(docx_file)
            print(f"   ✅ DOCX report saved: {docx_path}")
        except Exception as e:
            print(f"   ⚠️  DOCX generation failed: {e}")

        # ── Step 6: Return results ──
        response = {
            "status": execution_result.get("status", "completed"),
            "test_cases_parsed": len(test_cases),
            "test_cases": test_cases,
            "rf_code": rf_code,
            "validation": validation,
            "execution": {
                "total": execution_result.get("total", 0),
                "passed": execution_result.get("passed", 0),
                "failed": execution_result.get("failed", 0),
                "failed_tests": execution_result.get("failed_tests", []),
            },
            "report_path": execution_result.get("report_path"),
            "log_path": execution_result.get("log_path"),
            "robot_file": execution_result.get("robot_file"),
            "docx_path": docx_path,
            "test_name": test_name,
        }

        await update_agent_status("idle")
        return response

    except HTTPException:
        await update_agent_status("idle")
        raise
    except Exception as e:
        await update_agent_status("idle")
        print(f"❌ Pipeline error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/report/{test_name}")
async def get_report(test_name: str):
    """Serve the HTML report file."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    report_path = Path(f"output/rf_reports/{safe_name}/report.html")

    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(str(report_path), media_type="text/html")


@app.get("/api/log/{test_name}")
async def get_log(test_name: str):
    """Serve the log file."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    log_path = Path(f"output/rf_reports/{safe_name}/log.html")

    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log not found")

    return FileResponse(str(log_path), media_type="text/html")


@app.get("/api/download/{test_name}")
async def download_robot_file(test_name: str):
    """Download the generated .robot file."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    robot_path = Path(f"output/robot_files/{safe_name}.robot")

    if not robot_path.exists():
        raise HTTPException(status_code=404, detail="Robot file not found")

    return FileResponse(
        str(robot_path),
        media_type="text/plain",
        filename=f"{safe_name}.robot",
    )


@app.get("/api/download-docx/{test_name}")
async def download_docx_report(test_name: str):
    """Download the generated DOCX report."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    docx_path = Path(f"output/rf_reports/{safe_name}/report.docx")

    if not docx_path.exists():
        raise HTTPException(status_code=404, detail="DOCX report not found")

    return FileResponse(
        str(docx_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{safe_name}_report.docx",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=False)