"""
Generation routes — POST /api/generate-rf and the generate_rf compatibility wrapper.
"""

import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.routing import APIRouter
from pydantic import BaseModel

from rf_agent.parsing.md_parser import parse_md
from rf_agent.generation.orchestrator import generate_rf_code
from rf_agent.reporting.syntax_validator import validate_rf_syntax
from rf_agent.platform.mission_control import update_status

router = APIRouter()


class RFRequest(BaseModel):
    markdown_content: str
    base_url: str = "http://localhost:8080"


@router.post("/api/generate-rf")
async def generate_rf_endpoint(req: RFRequest):
    """
    Step 1: Parse markdown and generate RF code via LLM.
    Returns the code for review/editing.
    """
    await update_status("busy")
    try:
        print("Parsing markdown...")
        test_cases = parse_md(req.markdown_content)
        if not test_cases:
            raise HTTPException(status_code=400, detail="No test cases found.")

        print("Generating Robot Framework code via LLM (Batch Mode)...")
        rf_code = generate_rf_code(test_cases, req.base_url, raw_md=req.markdown_content)

        test_name = f"rf_gen_{int(time.time())}"
        robot_dir = Path("output/robot_files")
        robot_dir.mkdir(parents=True, exist_ok=True)
        robot_path = robot_dir / f"{test_name}.robot"
        robot_path.write_text(rf_code, encoding="utf-8")
        print(f"Generated code saved to: {robot_path}")

        print("Validating RF syntax...")
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
        print(f"Generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def generate_rf(req: RFRequest):
    """
    Compatibility wrapper for autonomous handoff (mission_control.py).
    Executes the full pipeline in one shot.
    """
    from rf_agent.api.routes_execution import execute_rf_endpoint, ExecuteRequest

    gen_data = await generate_rf_endpoint(req)

    exec_req = ExecuteRequest(
        rf_code=gen_data["rf_code"],
        base_url=gen_data["base_url"],
        test_cases=gen_data["test_cases"]
    )
    exec_data = await execute_rf_endpoint(exec_req)

    return {**gen_data, **exec_data}
