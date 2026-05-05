"""
RF Generator — Robot Framework Executor

Executes the .robot file and returns results with parsed output.
"""

import os
import subprocess
from pathlib import Path

try:
    from xml.etree import ElementTree as ET
except ImportError:
    ET = None


async def execute_rf(rf_code: str, test_name: str) -> dict:
    """
    Execute a Robot Framework test file and return results.

    Steps:
      1. Save rf_code to output/robot_files/{test_name}.robot
      2. Run robot with subprocess (timeout=120s)
      3. Parse output.xml for results
      4. Return structured results dict

    Args:
        rf_code: The Robot Framework code to execute.
        test_name: A name for the test run (used for file/folder naming).

    Returns:
        Dict with status, total, passed, failed, report_path, failed_tests.
    """
    # Sanitize test name for filesystem
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)

    # Create output directories
    robot_files_dir = Path("output/robot_files")
    reports_dir = Path(f"output/rf_reports/{safe_name}")
    screenshots_dir = Path(f"output/rf_reports/{safe_name}/screenshots")
    robot_files_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Save .robot file
    robot_file_path = robot_files_dir / f"{safe_name}.robot"
    robot_file_path.write_text(rf_code, encoding="utf-8")
    print(f"📄 Robot file saved: {robot_file_path}")

    # Step 2: Execute with Robot Framework
    # Try to use the robot module directly first, fall back to subprocess
    try:
        import robot  # noqa: F401
        use_module = True
    except ImportError:
        use_module = False

    if use_module:
        try:
            from robot import run as rf_run

            rc = rf_run(
                str(robot_file_path),
                outputdir=str(reports_dir),
                output="output.xml",
                report="report.html",
                log="log.html",
                consolecolors="off",
                variable=[f"SCREENSHOT_ROOT:{str(screenshots_dir)}/"],
            )
            print(f"🤖 Robot Framework finished with return code: {rc}")
        except Exception as e:
            return {
                "status": "error",
                "error": f"Robot Framework execution failed: {str(e)}",
                "robot_file": str(robot_file_path),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "report_path": None,
                "failed_tests": [],
            }
    else:
        # Fallback: use subprocess
        cmd = [
            "python", "-m", "robot",
            "--outputdir", str(reports_dir),
            "--output", "output.xml",
            "--report", "report.html",
            "--log", "log.html",
            "--variable", f"SCREENSHOT_ROOT:{str(screenshots_dir)}/",
            str(robot_file_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(Path.cwd()),
            )
            print(f"🤖 Robot Framework finished with return code: {result.returncode}")
            if result.stdout:
                print(f"📋 STDOUT:\n{result.stdout[-500:]}")
            if result.stderr:
                print(f"⚠️  STDERR:\n{result.stderr[-500:]}")
        except FileNotFoundError:
            return {
                "status": "error",
                "error": (
                    "Robot Framework is not installed. "
                    "Install it with: pip install robotframework robotframework-seleniumlibrary"
                ),
                "robot_file": str(robot_file_path),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "report_path": None,
                "failed_tests": [],
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": "Robot Framework execution timed out after 120 seconds.",
                "robot_file": str(robot_file_path),
                "total": 0,
                "passed": 0,
                "failed": 0,
                "report_path": None,
                "failed_tests": [],
            }

    # Step 3: Parse output.xml
    output_xml = reports_dir / "output.xml"
    report_html = reports_dir / "report.html"

    total = 0
    passed = 0
    failed = 0
    failed_tests = []

    if output_xml.exists() and ET:
        try:
            tree = ET.parse(str(output_xml))
            root = tree.getroot()

            # Find all test elements
            for test_elem in root.iter("test"):
                total += 1
                test_name_attr = test_elem.get("name", "Unknown")

                # Find status element within test
                status_elem = test_elem.find("status")
                if status_elem is not None:
                    test_status = status_elem.get("status", "FAIL")
                    if test_status == "PASS":
                        passed += 1
                    else:
                        failed += 1
                        fail_msg = status_elem.text or ""
                        failed_tests.append(f"{test_name_attr}: {fail_msg}".strip())
                else:
                    failed += 1
                    failed_tests.append(test_name_attr)

        except ET.ParseError as e:
            print(f"⚠️  Failed to parse output.xml: {e}")
    else:
        print("⚠️  output.xml not found or XML parser unavailable.")

    # Step 4: Return results
    return {
        "status": "completed",
        "total": total,
        "passed": passed,
        "failed": failed,
        "report_path": str(report_html) if report_html.exists() else None,
        "log_path": str(reports_dir / "log.html") if (reports_dir / "log.html").exists() else None,
        "robot_file": str(robot_file_path),
        "failed_tests": failed_tests,
        "test_name": safe_name,
    }
