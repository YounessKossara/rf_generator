"""
RF Generator — Markdown Test Case Parser

Parses a markdown string and extracts structured test cases.
Supports both French and English keywords.
Supports both list-based and table-based step formats.
"""

import re


def parse_md(markdown_content: str) -> list[dict]:
    """
    Parse a markdown string and extract test cases.

    Each test case has:
      - id: str           (e.g. "TC001")
      - title: str        (e.g. "Login avec identifiants valides")
      - preconditions: list[str]
      - steps: list[str]  (or list[dict] when table format with sub-expected)
      - expected: str

    Parsing rules:
      - TC id detected by: "TC001", "## TC001", "### TC001"
      - Title = text after TC id on same line
      - Preconditions = lines after "Précondition" or "Precondition"
      - Steps = numbered list (1. 2. 3.) or bullet list (- )
      - Steps can also be in a markdown table with columns:
        | N° | Action | Résultat attendu |
      - Expected = lines after "Résultat attendu" or "Expected"
      - Handle both French and English keywords
    """
    test_cases = []
    lines = markdown_content.strip().split("\n")

    # Regex to detect a TC id line: optional markdown heading + TC + digits
    tc_pattern = re.compile(
        r"^(?:#{1,6}\s*)?(?:\*\*)?(?:TC[-_]?\d+)(?:\*\*)?",
        re.IGNORECASE,
    )
    tc_id_extract = re.compile(r"(TC[-_]?\d+)", re.IGNORECASE)

    # Section header patterns (French + English)
    precondition_pattern = re.compile(
        r"^(?:#{1,6}\s*)?(?:\*\*)?\s*(?:pr[ée]conditions?|preconditions?)\s*(?::)?\s*(?:\*\*)?",
        re.IGNORECASE,
    )
    steps_pattern = re.compile(
        r"^(?:#{1,6}\s*)?(?:\*\*)?\s*(?:[ée]tapes?|steps?|actions?)\s*(?::)?\s*(?:\*\*)?",
        re.IGNORECASE,
    )
    expected_pattern = re.compile(
        r"^(?:#{1,6}\s*)?(?:\*\*)?\s*(?:r[ée]sultat[s]?\s*attendu[s]?|expected\s*(?:result[s]?)?|r[ée]sultat[s]?\s*escompt[ée][s]?)\s*(?::)?\s*(?:\*\*)?",
        re.IGNORECASE,
    )

    # Table detection pattern: a line like | ... | ... | ... |
    table_separator_pattern = re.compile(r"^\|[\s:|-]+\|$")

    # State machine
    current_tc = None
    current_section = None  # 'preconditions', 'steps', 'expected', None
    table_columns = None    # dict mapping role -> column index when in table mode
    in_table = False        # True when currently parsing a table body

    def _save_current():
        nonlocal current_tc, in_table, table_columns
        if current_tc:
            # Clean up expected: join as single string
            if isinstance(current_tc["expected"], list):
                current_tc["expected"] = "\n".join(current_tc["expected"]).strip()
            test_cases.append(current_tc)
            current_tc = None
        in_table = False
        table_columns = None

    for line in lines:
        stripped = line.strip()

        # Skip empty lines — also exit table mode
        if not stripped:
            in_table = False
            table_columns = None
            continue

        # ── Check if this line starts a new TC ──
        tc_match = tc_id_extract.search(stripped)
        if tc_match and tc_pattern.match(stripped):
            _save_current()
            tc_id = tc_match.group(1).upper()
            # Title is everything after the TC id
            title = stripped[tc_match.end():].strip()
            # Remove leading separators like : — - |
            title = re.sub(r"^[\s:—\-–|]+", "", title).strip()
            # Remove trailing ** if present
            title = re.sub(r"\*\*$", "", title).strip()
            current_tc = {
                "id": tc_id,
                "title": title,
                "preconditions": [],
                "steps": [],
                "expected": [],
            }
            current_section = None
            in_table = False
            table_columns = None
            continue

        # If we are not inside a TC, skip
        if current_tc is None:
            continue

        # ── Table parsing ──
        # Detect table header row: | Col1 | Col2 | Col3 |
        if stripped.startswith("|") and stripped.endswith("|") and not in_table:
            cols = _detect_table_header(stripped)
            if cols:
                table_columns = cols
                # The next line should be the separator (|---|---|---|
                # which we'll skip below)
                current_section = "table"
                continue

        # Skip table separator row: |---|---|---|
        if current_section == "table" and table_separator_pattern.match(stripped):
            in_table = True
            continue

        # Parse table data rows
        if in_table and stripped.startswith("|") and stripped.endswith("|"):
            row_data = _parse_table_row(stripped, table_columns)
            if row_data:
                action = row_data.get("action", "").strip()
                sub_expected = row_data.get("expected", "").strip()
                if action:
                    if sub_expected:
                        current_tc["steps"].append({
                            "action": action,
                            "expected": sub_expected,
                        })
                    else:
                        current_tc["steps"].append(action)
            continue

        # If we hit a non-table line while in table mode, exit table
        if in_table:
            in_table = False
            table_columns = None
            current_section = None

        # ── Check for section headers ──
        if precondition_pattern.match(stripped):
            current_section = "preconditions"
            # Check if there's content on the same line after the header
            remainder = precondition_pattern.sub("", stripped).strip()
            if remainder:
                current_tc["preconditions"].append(_clean_list_item(remainder))
            continue

        if steps_pattern.match(stripped):
            current_section = "steps"
            remainder = steps_pattern.sub("", stripped).strip()
            if remainder:
                current_tc["steps"].append(_clean_list_item(remainder))
            continue

        if expected_pattern.match(stripped):
            current_section = "expected"
            remainder = expected_pattern.sub("", stripped).strip()
            if remainder:
                current_tc["expected"].append(remainder)
            continue

        # ── Collect content for current section ──
        if current_section == "preconditions":
            current_tc["preconditions"].append(_clean_list_item(stripped))
        elif current_section == "steps":
            current_tc["steps"].append(_clean_list_item(stripped))
        elif current_section == "expected":
            current_tc["expected"].append(stripped)

    # Save last TC
    _save_current()

    return test_cases


def _clean_list_item(text: str) -> str:
    """Remove leading list markers like '1.', '2.', '-', '*', '•'."""
    cleaned = re.sub(r"^\d+[\.\)]\s*", "", text)
    cleaned = re.sub(r"^[-\*•]\s*", "", cleaned)
    return cleaned.strip()


# ── Table helpers ──

# Column name patterns for header detection (French + English)
_ACTION_NAMES = re.compile(
    r"^(action[s]?|[ée]tape[s]?|step[s]?|description)$", re.IGNORECASE
)
_EXPECTED_NAMES = re.compile(
    r"^(r[ée]sultat[s]?\s*attendu[s]?|expected(\s*result[s]?)?|r[ée]sultat[s]?)$",
    re.IGNORECASE,
)
_NUMBER_NAMES = re.compile(
    r"^(n[°o]?|#|num[ée]ro|number|id|step)$", re.IGNORECASE
)


def _detect_table_header(header_line: str) -> dict | None:
    """
    Detect a markdown table header and return column role mapping.

    Returns a dict like {"action": 1, "expected": 2} (column indices)
    or None if this doesn't look like a steps table.
    """
    cells = [c.strip() for c in header_line.strip("|").split("|")]
    if len(cells) < 2:
        return None

    col_map = {}
    for i, cell in enumerate(cells):
        clean = cell.strip().strip("*").strip()
        if _ACTION_NAMES.match(clean):
            col_map["action"] = i
        elif _EXPECTED_NAMES.match(clean):
            col_map["expected"] = i
        elif _NUMBER_NAMES.match(clean):
            col_map["number"] = i

    # Must have at least the action column to be a valid steps table
    if "action" not in col_map:
        return None

    return col_map


def _parse_table_row(row_line: str, col_map: dict) -> dict | None:
    """
    Parse a single markdown table data row using the column mapping.

    Returns {"action": "...", "expected": "..."} or None.
    """
    if not col_map:
        return None

    cells = [c.strip() for c in row_line.strip("|").split("|")]

    result = {}
    action_idx = col_map.get("action")
    expected_idx = col_map.get("expected")

    if action_idx is not None and action_idx < len(cells):
        result["action"] = cells[action_idx].strip()
    if expected_idx is not None and expected_idx < len(cells):
        result["expected"] = cells[expected_idx].strip()

    return result if result.get("action") else None
