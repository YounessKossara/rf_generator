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
from mission_control import (
    register_agent,
    update_status,
    heartbeat_loop
)

# ── App ──
app = FastAPI(title="RF Generator — OmniPlatform")

# ── Output directories ──
os.makedirs("frontend", exist_ok=True)
os.makedirs("output/robot_files", exist_ok=True)
os.makedirs("output/rf_reports", exist_ok=True)
os.makedirs("output/screenshots", exist_ok=True)

# ── Static files ──
app.mount("/static", StaticFiles(directory="frontend"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")


@app.on_event("startup")
async def startup():
    await register_agent()
    asyncio.create_task(heartbeat_loop())


# ══════════════════════════════════════════
#  Request / Response Models
# ══════════════════════════════════════════

# ══════════════════════════════════════════
#  Request / Response Models
# ══════════════════════════════════════════

class RFRequest(BaseModel):
    markdown_content: str
    base_url: str = "http://localhost:8080"

class ExecuteRequest(BaseModel):
    rf_code: str
    base_url: str
    test_cases: list = []  # Metadata for reports
    test_name: str = ""    # Optional predefined name


# ══════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════

@app.get("/")
async def serve_ui():
    return FileResponse("frontend/index.html")


@app.post("/api/generate-rf")
async def generate_rf_endpoint(req: RFRequest):
    """
    Step 1: Parse markdown and generate RF code via LLM.
    Returns the code for review/editing.
    """
    await update_status("busy")
    try:
        # 1. Parse markdown
        print("📝 Parsing markdown...")
        test_cases = parse_md(req.markdown_content)
        if not test_cases:
            raise HTTPException(status_code=400, detail="No test cases found.")

        # 2. Generate RF code
        print("🤖 Generating Robot Framework code via LLM (Batch Mode)...")
        rf_code = generate_rf_code(test_cases, req.base_url, raw_md=req.markdown_content)

        # 3. Save to output directory
        test_name = f"rf_gen_{int(time.time())}"
        robot_dir = Path("output/robot_files")
        robot_dir.mkdir(parents=True, exist_ok=True)
        robot_path = robot_dir / f"{test_name}.robot"
        robot_path.write_text(rf_code, encoding="utf-8")
        print(f"📄 Generated code saved to: {robot_path}")

        # 4. Validate
        print("🔍 Validating RF syntax...")
        validation = validate_rf_syntax(rf_code)

        await update_status("idle")
        return {
            "test_cases_parsed": len(test_cases),
            "test_cases": test_cases,
            "rf_code": rf_code,
            "validation": validation,
            "base_url": req.base_url,
            "test_name": test_name,
            "robot_file": str(robot_path)
        }
    except Exception as e:
        await update_status("idle")
        print(f"❌ Generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/execute-rf")
async def execute_rf_endpoint(req: ExecuteRequest):
    """
    Step 2: Execute existing Robot Framework code with self-healing.
    """
    await update_status("busy")
    try:
        rf_code = req.rf_code
        test_name = req.test_name or f"rf_run_{int(time.time())}"
        
        # 1. Execute (with self-healing)
        print(f"🚀 Executing Robot Framework tests: {test_name}")
        execution_result = await execute_rf(rf_code, test_name)
        
        # Use potentially updated code after healing
        final_rf_code = rf_code
        robot_file = execution_result.get("robot_file")
        if robot_file and Path(robot_file).exists():
            final_rf_code = Path(robot_file).read_text(encoding="utf-8")

        # 2. Trello cards for still-failing
        final_failures = execution_result.get("failed_tests", [])
        if final_failures:
            print("📋 Creating Trello cards for still-failing tests...")
            for failed_test in final_failures:
                try:
                    create_failure_card(
                        test_id=failed_test.split(":")[0].strip() if ":" in failed_test else failed_test,
                        error_details=failed_test,
                    )
                except Exception: pass

        # 3. DOCX report
        docx_path = None
        try:
            print("📝 Generating DOCX report...")
            docx_results = {**execution_result, "test_name": test_name}
            docx_bytes = generate_rf_docx_report(
                results=docx_results,
                rf_code=final_rf_code,
                test_cases=req.test_cases,
                base_url=req.base_url,
            )
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
            docx_file = Path(f"output/rf_reports/{safe_name}/report.docx")
            docx_file.parent.mkdir(parents=True, exist_ok=True)
            docx_file.write_bytes(docx_bytes)
            docx_path = str(docx_file)
        except Exception as e:
            print(f"⚠️ DOCX failed: {e}")

        await update_status("idle")
        return {
            "status": execution_result.get("status", "completed"),
            "execution": {
                "total": execution_result.get("total", 0),
                "passed": execution_result.get("passed", 0),
                "failed": execution_result.get("failed", 0),
                "failed_tests": execution_result.get("failed_tests", []),
                "passed_tests": execution_result.get("passed_tests", []),
            },
            "healing": {
                "healing_attempts": execution_result.get("healing_attempts", {}),
                "healed_tests": execution_result.get("healed_tests", []),
                "still_failing": execution_result.get("still_failing", []),
            },
            "report_path": execution_result.get("report_path"),
            "log_path": execution_result.get("log_path"),
            "robot_file": execution_result.get("robot_file"),
            "docx_path": docx_path,
            "test_name": test_name,
            "rf_code": final_rf_code
        }
    except Exception as e:
        await update_status("idle")
        print(f"❌ Execution error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════
#  Mission Control Autonomous Handoff Logic
# ══════════════════════════════════════════

async def generate_rf(req: RFRequest):
    """
    Compatibility wrapper for autonomous handoff (mission_control.py).
    Executes the full pipeline in one shot.
    """
    # 1. Generate
    gen_data = await generate_rf_endpoint(req)
    
    # 2. Execute
    exec_req = ExecuteRequest(
        rf_code=gen_data["rf_code"],
        base_url=gen_data["base_url"],
        test_cases=gen_data["test_cases"]
    )
    exec_data = await execute_rf_endpoint(exec_req)
    
    # Merge results
    return {**gen_data, **exec_data}


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