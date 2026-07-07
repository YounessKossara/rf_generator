"""
rf_generator — backward-compatibility shim.

All logic has moved into rf_agent/generation/. This file re-exports
`generate_rf_code` so existing callers (main.py) continue to work
without changes.
"""

from rf_agent.generation.orchestrator import generate_rf_code

__all__ = ["generate_rf_code"]
