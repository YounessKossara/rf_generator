"""
RF Generator — Robot Framework Executor (Self-Healing)

Executes the .robot file and returns results with parsed output.
When tests fail with selector errors, automatically triggers
the self-healing agent to fix and re-execute failed tests.
"""

import os
import subprocess
import re
from pathlib import Path

try:
    from xml.etree import ElementTree as ET
except ImportError:
    ET = None

from rf_agent.self_healer import (
    is_healable_error,
    fetch_page_html,
    heal_test_case,
    extract_test_case_block,
    replace_test_case_block,
    extract_tc_name_from_error,
    extract_base_url_from_rf_code,
    _needs_login,
    _extract_credentials_from_rf,
    _extract_target_url,
)

MAX_HEAL_ATTEMPTS = 3


def _run_robot(robot_file_path: Path, reports_dir: Path, screenshots_dir: Path,
               test_filter: str = None) -> int:
    """Run Robot Framework and return the return code."""
    try:
        import robot  # noqa: F401
        use_module = True
    except ImportError:
        use_module = False

    if use_module:
        from robot import run as rf_run
        kwargs = {
            "outputdir": str(reports_dir),
            "output": "output.xml",
            "report": "report.html",
            "log": "log.html",
            "consolecolors": "off",
            "variable": [f"SCREENSHOT_ROOT:{str(screenshots_dir)}/"],
        }
        if test_filter:
            kwargs["test"] = test_filter
        rc = rf_run(str(robot_file_path), **kwargs)
        return rc
    else:
        cmd = [
            "python", "-m", "robot",
            "--outputdir", str(reports_dir),
            "--output", "output.xml",
            "--report", "report.html",
            "--log", "log.html",
            "--variable", f"SCREENSHOT_ROOT:{str(screenshots_dir)}/",
        ]
        if test_filter:
            cmd.extend(["--test", test_filter])
        cmd.append(str(robot_file_path))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=120, cwd=str(Path.cwd()),
            )
            if result.stdout:
                print(f"📋 STDOUT:\n{result.stdout[-500:]}")
            if result.stderr:
                print(f"⚠️  STDERR:\n{result.stderr[-500:]}")
            return result.returncode
        except FileNotFoundError:
            return -1
        except subprocess.TimeoutExpired:
            return -2

    return -99


def _parse_output_xml(output_xml: Path) -> dict:
    """Parse output.xml and return structured results."""
    total = 0
    passed = 0
    failed = 0
    failed_tests = []
    passed_tests = []

    if output_xml.exists() and ET:
        try:
            tree = ET.parse(str(output_xml))
            root = tree.getroot()
            for test_elem in root.iter("test"):
                total += 1
                test_name_attr = test_elem.get("name", "Unknown")
                status_elem = test_elem.find("status")
                if status_elem is not None:
                    test_status = status_elem.get("status", "FAIL")
                    if test_status == "PASS":
                        passed += 1
                        passed_tests.append(test_name_attr)
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

    return {
        "total": total, "passed": passed, "failed": failed,
        "failed_tests": failed_tests, "passed_tests": passed_tests,
    }


async def execute_rf(rf_code: str, test_name: str) -> dict:
    """
    Execute RF tests with self-healing. Merges results
    programmatically instead of re-running all tests.
    """
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in test_name)

    robot_files_dir = Path("output/robot_files")
    reports_dir = Path(f"output/rf_reports/{safe_name}")
    screenshots_dir = Path(f"output/rf_reports/{safe_name}/screenshots")
    robot_files_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    robot_file_path = robot_files_dir / f"{safe_name}.robot"
    robot_file_path.write_text(rf_code, encoding="utf-8")
    print(f"📄 Robot file saved: {robot_file_path}")

    # Step 1: First execution
    rc = _run_robot(robot_file_path, reports_dir, screenshots_dir)
    print(f"🤖 Robot Framework finished with return code: {rc}")

    if rc == -1:
        return {"status": "error", "error": "Robot Framework is not installed.",
                "robot_file": str(robot_file_path), "total": 0, "passed": 0, "failed": 0,
                "report_path": None, "failed_tests": [], "passed_tests": [],
                "healing_attempts": {}, "healed_tests": [], "still_failing": []}
    if rc == -2:
        return {"status": "error", "error": "Robot Framework timed out.",
                "robot_file": str(robot_file_path), "total": 0, "passed": 0, "failed": 0,
                "report_path": None, "failed_tests": [], "passed_tests": [],
                "healing_attempts": {}, "healed_tests": [], "still_failing": []}

    # Step 2: Parse initial results
    output_xml = reports_dir / "output.xml"
    results = _parse_output_xml(output_xml)

    # ══════════════════════════════════════════
    #  Self-Healing Loop
    # ══════════════════════════════════════════
    healing_attempts = {}
    healed_tests = []
    still_failing = []
    current_rf_code = rf_code

    # Separate healable vs non-healable failures
    healable_failures = [ft for ft in results["failed_tests"] if is_healable_error(ft)]
    non_healable_failures = [ft for ft in results["failed_tests"] if not is_healable_error(ft)]

    # Keep track of originally passed tests
    original_passed = list(results["passed_tests"])

    if healable_failures:
        print(f"\n{'═' * 50}")
        print(f"  🔧 SELF-HEALING AGENT ACTIVATED")
        print(f"  {len(healable_failures)} healable failure(s) detected")
        print(f"{'═' * 50}\n")

        base_url = extract_base_url_from_rf_code(current_rf_code)

        for failure in healable_failures:
            tc_name = extract_tc_name_from_error(failure)
            error_msg = failure

            tc_id_match = re.match(r'(TC-?\d+)', tc_name)
            tc_id = tc_id_match.group(1) if tc_id_match else tc_name[:10]

            healing_attempts[tc_id] = 0
            healed = False

            for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
                healing_attempts[tc_id] = attempt
                print(f"🔧 [HEALER] {tc_id} failed — attempt {attempt}/{MAX_HEAL_ATTEMPTS}")

                # 1. Extract TC block to check if it needs login
                tc_block = extract_test_case_block(current_rf_code, tc_name)
                if not tc_block:
                    print(f"   ⚠️  [HEALER] Could not extract TC block for '{tc_name}'")
                    break

                # 2. Smart page fetch — login if TC needs post-login DOM
                login_needed = _needs_login(tc_block)
                username, password = _extract_credentials_from_rf(tc_block)
                target_url = _extract_target_url(tc_block, base_url)
                
                print(f"🔍 [HEALER] Fetching DOM from {base_url} (login={'yes' if login_needed else 'no'})...")
                page_html = await fetch_page_html(
                    base_url, needs_login=login_needed,
                    username=username, password=password,
                    target_url=target_url
                )

                # 3. Ask LLM to heal
                print(f"🤖 [HEALER] LLM analyzing error and generating fix...")
                fixed_tc = heal_test_case(
                    tc_rf_code=tc_block,
                    error_message=error_msg,
                    page_html=page_html,
                    base_url=base_url,
                    attempt=attempt,
                )

                # 4. Replace TC in .robot file
                current_rf_code = replace_test_case_block(current_rf_code, tc_name, fixed_tc)
                robot_file_path.write_text(current_rf_code, encoding="utf-8")

                # 5. Re-execute ONLY the failed test
                heal_dir = Path(f"output/rf_reports/{safe_name}/heal_{tc_id}_{attempt}")
                heal_dir.mkdir(parents=True, exist_ok=True)
                test_filter = f"*{tc_id}*"
                print(f"   🚀 Re-executing ONLY: {test_filter}")
                _run_robot(robot_file_path, heal_dir, screenshots_dir, test_filter)

                # 6. Parse heal results
                heal_results = _parse_output_xml(heal_dir / "output.xml")

                if heal_results["passed"] > 0 and heal_results["failed"] == 0:
                    print(f"✅ [HEALER] {tc_id} healed on attempt {attempt}!")
                    healed_tests.append(tc_id)
                    healed = True
                    break
                else:
                    if heal_results["failed_tests"]:
                        error_msg = heal_results["failed_tests"][0]
                    print(f"   ⚠️  [HEALER] {tc_id} still failing on attempt {attempt}")

            if not healed:
                print(f"❌ [HEALER] {tc_id} still failing after {MAX_HEAL_ATTEMPTS} attempts.")
                still_failing.append(tc_id)

        # ── Merge results programmatically (no re-run) ──
        print(f"\n{'═' * 50}")
        print(f"  📊 MERGING RESULTS (no re-execution)")
        print(f"{'═' * 50}")

        # Start with originally passed tests
        final_passed = list(original_passed)
        final_failed = list(non_healable_failures)

        # Add healed tests to passed
        for ft in healable_failures:
            tc_name_check = extract_tc_name_from_error(ft)
            tc_id_match = re.match(r'(TC-?\d+)', tc_name_check)
            tc_id_check = tc_id_match.group(1) if tc_id_match else tc_name_check[:10]

            if tc_id_check in healed_tests:
                final_passed.append(tc_name_check)
            else:
                final_failed.append(ft)

        results = {
            "total": len(final_passed) + len(final_failed),
            "passed": len(final_passed),
            "failed": len(final_failed),
            "failed_tests": final_failed,
            "passed_tests": final_passed,
        }
        print(f"   📊 Final: {results['passed']}/{results['total']} passed, "
              f"{len(healed_tests)} healed, {len(still_failing)} still failing")

    # ── Build response ──
    report_html = reports_dir / "report.html"
    log_html = reports_dir / "log.html"

    return {
        "status": "completed",
        "total": results["total"],
        "passed": results["passed"],
        "failed": results["failed"],
        "report_path": str(report_html) if report_html.exists() else None,
        "log_path": str(log_html) if log_html.exists() else None,
        "robot_file": str(robot_file_path),
        "failed_tests": results["failed_tests"],
        "passed_tests": results.get("passed_tests", []),
        "test_name": safe_name,
        "healing_attempts": healing_attempts,
        "healed_tests": healed_tests,
        "still_failing": still_failing,
    }
