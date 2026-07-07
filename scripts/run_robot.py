"""
Run a generated .robot file with the same self-healing executor used by
the /api/execute-rf endpoint — without needing the FastAPI server.

Usage:
    python run_robot.py output\\robot_files\\rf_gen_<id>.robot

Selector failures trigger up to 3 healing attempts per test, the .robot
file is updated in place with healed test cases, and a final pass/fail
summary is printed.
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from rf_agent.execution.executor import execute_rf


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python run_robot.py <path-to-.robot>")
        return 1

    rf_path = Path(sys.argv[1])
    if not rf_path.exists():
        print(f"❌ File not found: {rf_path}")
        return 1

    rf_code = rf_path.read_text(encoding="utf-8")
    test_name = rf_path.stem

    print(f"🚀 Running {rf_path} with self-healing...")
    result = asyncio.run(execute_rf(rf_code, test_name))

    total = result.get("total", 0)
    passed = result.get("passed", 0)
    healed = result.get("healed_tests", []) or []
    still_failing = result.get("still_failing", []) or []

    print()
    print("=" * 60)
    print(f"  📊 Final: {passed}/{total} passed, "
          f"{len(healed)} healed, {len(still_failing)} still failing")
    if healed:
        print(f"  ✅ Healed: {', '.join(healed)}")
    if still_failing:
        print(f"  ❌ Still failing: {', '.join(still_failing)}")
    if result.get("report_path"):
        print(f"  📄 Report: {result['report_path']}")
    if result.get("robot_file"):
        print(f"  📝 Robot file (healed): {result['robot_file']}")
    print("=" * 60)

    return 0 if total > 0 and not still_failing else (0 if total == passed else 2)


if __name__ == "__main__":
    sys.exit(main())
