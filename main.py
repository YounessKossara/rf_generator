"""
main — backward-compatibility shim.

All logic has moved into rf_agent/api/. This file re-exports `app` so that
`uvicorn main:app --port 8001` continues to work without changes.

New canonical start command: uvicorn rf_agent.api.server:app --port 8001 --reload
"""

from rf_agent.api.server import app  # noqa: F401

__all__ = ["app"]
