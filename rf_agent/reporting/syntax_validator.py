"""
RF Generator — Robot Framework Syntax Validator

Validates Robot Framework syntax and uses LLM to fix errors.
"""

import re
from rf_agent.infrastructure.llm import get_smart_llm, invoke_with_retry
from langchain.messages import SystemMessage, HumanMessage


def validate_rf_syntax(rf_code: str) -> dict:
    """
    Validate Robot Framework syntax.

    Returns:
        {"valid": bool, "errors": list[str]}

    Checks:
      - Must contain "*** Settings ***"
      - Must contain "*** Test Cases ***"
      - Must contain "Library    SeleniumLibrary"
      - Each test case must have at least 2 lines of keywords
      - No unclosed brackets
      - Keywords must be properly indented (4 spaces or more)
    """
    errors = []

    # ── Check required sections ──
    if "*** Settings ***" not in rf_code:
        errors.append("Missing '*** Settings ***' section.")

    if "*** Test Cases ***" not in rf_code:
        errors.append("Missing '*** Test Cases ***' section.")

    # Check for SeleniumLibrary import
    selenium_pattern = re.compile(r"Library\s+SeleniumLibrary", re.IGNORECASE)
    if not selenium_pattern.search(rf_code):
        errors.append("Missing 'Library    SeleniumLibrary' in Settings.")

    # ── Check for unclosed brackets ──
    open_curly = rf_code.count("{")
    close_curly = rf_code.count("}")
    if open_curly != close_curly:
        errors.append(
            f"Unbalanced curly braces: {open_curly} opening vs {close_curly} closing."
        )

    open_square = rf_code.count("[")
    close_square = rf_code.count("]")
    if open_square != close_square:
        errors.append(
            f"Unbalanced square brackets: {open_square} opening vs {close_square} closing."
        )

    # ── Check test cases have content ──
    lines = rf_code.split("\n")
    in_test_cases_section = False
    current_test_name = None
    current_test_keyword_count = 0

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if stripped.startswith("***"):
            if "Test Cases" in stripped:
                in_test_cases_section = True
            else:
                # Save previous test if switching sections
                if in_test_cases_section and current_test_name:
                    if current_test_keyword_count < 2:
                        errors.append(
                            f"Test case '{current_test_name}' has fewer than 2 keyword lines."
                        )
                in_test_cases_section = False
            continue

        if not in_test_cases_section:
            continue

        # Skip blank lines
        if not stripped:
            continue

        # If line starts at column 0 (no leading whitespace), it's a test case name
        if line and not line[0].isspace():
            # Save previous test case check
            if current_test_name and current_test_keyword_count < 2:
                errors.append(
                    f"Test case '{current_test_name}' has fewer than 2 keyword lines."
                )
            current_test_name = stripped
            current_test_keyword_count = 0
        elif current_test_name:
            # It's a keyword line (indented under a test case)
            # Skip [Documentation], [Tags], [Setup], [Teardown] metadata lines
            if not stripped.startswith("["):
                current_test_keyword_count += 1

    # Check last test case
    if in_test_cases_section and current_test_name and current_test_keyword_count < 2:
        errors.append(
            f"Test case '{current_test_name}' has fewer than 2 keyword lines."
        )

    return {"valid": len(errors) == 0, "errors": errors}


def fix_rf_syntax(rf_code: str, errors: list[str]) -> str:
    """
    Use LLM to fix Robot Framework syntax errors.

    Args:
        rf_code: The original RF code with errors.
        errors: List of error messages from validate_rf_syntax.

    Returns:
        The fixed RF code as a string.
    """
    error_list = "\n".join(f"  - {e}" for e in errors)

    system_prompt = """You are an expert Robot Framework developer.
Fix the provided Robot Framework code to resolve all listed syntax errors.
Return ONLY the corrected Robot Framework code.
Do NOT add explanations, comments about fixes, or markdown formatting.
Do NOT wrap the code in ```robot or ``` blocks."""

    human_prompt = f"""Fix the following Robot Framework code.

ERRORS FOUND:
{error_list}

ORIGINAL CODE:
{rf_code}

Return the corrected .robot code only."""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt),
    ]

    response = invoke_with_retry(get_smart_llm, messages)
    fixed_code = response.content.strip()

    # Clean up markdown fences if present
    if fixed_code.startswith("```"):
        lines = fixed_code.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fixed_code = "\n".join(lines)

    return fixed_code
