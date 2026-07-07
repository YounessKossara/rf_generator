"""
Execution routes — POST /api/execute-rf and GET report/log/download endpoints.
"""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.routing import APIRouter
from pydantic import BaseModel

from rf_agent.execution.executor import execute_rf
from rf_agent.reporting.docx_reporter import generate_rf_docx_report
from rf_agent.infrastructure.trello import create_failure_card
from rf_agent.platform.mission_control import update_status

router = APIRouter()


class ExecuteRequest(BaseModel):
    rf_code: str
    base_url: str
    test_cases: list = []
    test_name: str = ""


@router.post("/api/execute-rf")
async def execute_rf_endpoint(req: ExecuteRequest):
    """
    Step 2: Execute existing Robot Framework code with self-healing.
    """
    import time
    await update_status("busy")
    try:
        rf_code = req.rf_code
        test_name = req.test_name or f"rf_run_{int(time.time())}"

        print(f"Executing Robot Framework tests: {test_name}")
        execution_result = await execute_rf(rf_code, test_name)

        final_rf_code = rf_code
        robot_file = execution_result.get("robot_file")
        if robot_file and Path(robot_file).exists():
            final_rf_code = Path(robot_file).read_text(encoding="utf-8")

        final_failures = execution_result.get("failed_tests", [])
        if final_failures:
            print("Creating Trello cards for still-failing tests...")
            for failed_test in final_failures:
                try:
                    create_failure_card(
                        test_id=failed_test.split(":")[0].strip() if ":" in failed_test else failed_test,
                        error_details=failed_test,
                    )
                except Exception:
                    pass

        docx_path = None
        try:
            print("Generating DOCX report...")
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
            print(f"DOCX failed: {e}")

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
        print(f"Execution error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/report/{test_name}")
async def get_report(test_name: str):
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    report_path = Path(f"output/rf_reports/{safe_name}/report.html")
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(str(report_path), media_type="text/html")


@router.get("/api/log/{test_name}")
async def get_log(test_name: str):
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    log_path = Path(f"output/rf_reports/{safe_name}/log.html")
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Log not found")
    return FileResponse(str(log_path), media_type="text/html")


@router.get("/api/download/{test_name}")
async def download_robot_file(test_name: str):
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    robot_path = Path(f"output/robot_files/{safe_name}.robot")
    if not robot_path.exists():
        raise HTTPException(status_code=404, detail="Robot file not found")
    return FileResponse(
        str(robot_path),
        media_type="text/plain",
        filename=f"{safe_name}.robot",
    )


@router.get("/api/download-docx/{test_name}")
async def download_docx_report(test_name: str):
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)
    docx_path = Path(f"output/rf_reports/{safe_name}/report.docx")
    if not docx_path.exists():
        raise HTTPException(status_code=404, detail="DOCX report not found")
    return FileResponse(
        str(docx_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{safe_name}_report.docx",
    )
