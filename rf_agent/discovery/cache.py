"""
Persistence layer — reads and writes app_memory.json.
"""

import json
import os
import re
from pathlib import Path


MEMORY_FILE = Path("output/app_memory.json")


def _domain_key(url: str) -> str:
    m = re.match(r'https?://[^/]+', url)
    return m.group(0).rstrip('/') if m else url.rstrip('/')


def cache_enabled() -> bool:
    return os.environ.get("RF_USE_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


def load_all() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_app(base_url: str) -> dict:
    return load_all().get(_domain_key(base_url), {})


def load_app_for_generation(base_url: str) -> dict:
    if not cache_enabled():
        return {}
    return load_app(base_url)


def save_app(base_url: str, data: dict):
    all_data = load_all()
    key = _domain_key(base_url)
    all_data[key] = {**all_data.get(key, {}), **data}
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(all_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"   \U0001f4be [MEMORY] Saved recipe for {key}")
