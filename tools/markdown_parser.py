"""
Markdown Parser — Extracts test context from .md files.

Parses markdown content to extract:
- URLs to test
- User flow descriptions / acceptance criteria
- Expected behaviors

This module is pure Python (no external dependencies).
"""

import re


def parse_markdown(content: str) -> dict:
    """
    Parse markdown content into structured test context.
    
    Returns:
        {
            "urls": [list of URLs found],
            "instructions": [list of test instruction lines],
            "expected_flows": [list of expected flow descriptions],
            "raw": original markdown content
        }
    """
    import time
    if content:
        random_val = str(int(time.time()))[-5:]
        content = content.replace("{{random}}", random_val)

    result = {
        "urls": [],
        "instructions": [],
        "expected_flows": [],
        "raw": content
    }

    if not content or not content.strip():
        return result

    lines = content.strip().split("\n")

    # Extract all URLs
    url_pattern = re.compile(r'https?://[^\s\)\]\>\"\']+')
    seen_urls = set()
    for line in lines:
        for url in url_pattern.findall(line):
            clean_url = url.rstrip(".,;:!?")
            if clean_url not in seen_urls:
                result["urls"].append(clean_url)
                seen_urls.add(clean_url)

    # Extract instructions (lines starting with - [ ], *, -, or numbered lists)
    instruction_pattern = re.compile(r'^\s*(?:[-*]|\d+[.)]\s*|- \[[ x]\])\s*(.+)', re.IGNORECASE)
    for line in lines:
        match = instruction_pattern.match(line)
        if match:
            text = match.group(1).strip()
            if text and len(text) > 5:  # Skip very short items
                result["instructions"].append(text)

    # Extract expected flows (lines/sections containing keywords)
    flow_keywords = [
        "should", "must", "expect", "verify", "check", "assert",
        "doit", "vérifier", "attendu", "valider", "confirmer",
        "when", "then", "given", "quand", "alors"
    ]
    for line in lines:
        line_lower = line.strip().lower()
        if any(kw in line_lower for kw in flow_keywords):
            clean = line.strip().lstrip("#-*> ").strip()
            if clean and len(clean) > 10:
                result["expected_flows"].append(clean)

    return result


def extract_test_context_summary(parsed: dict) -> str:
    """
    Return the full raw markdown context for the LLM.
    Since modern LLMs have large context windows, providing the full 
    markdown ensures no credentials or instructions are accidentally filtered out.
    """
    return parsed.get("raw", "").strip()
