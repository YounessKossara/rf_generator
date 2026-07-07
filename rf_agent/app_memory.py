"""
app_memory — backward-compatibility shim.

All logic has moved into rf_agent/discovery/. This file re-exports everything
so existing callers (rf_generator.py, self_healer.py, etc.) continue to work
without changes during the migration.
"""

from rf_agent.discovery.cache import (
    MEMORY_FILE,
    _domain_key,
    cache_enabled,
    load_all,
    load_app,
    load_app_for_generation,
    save_app,
)
from rf_agent.discovery.utils import (
    rf_to_playwright,
    _extract_interactive_elements,
)
from rf_agent.discovery.recipe import (
    discover_login_recipe,
    build_login_context,
    _selector_grounded_in_html,
    _derive_recipe_via_regex,
    _static_html_lacks_form,
)
from rf_agent.discovery.page_structure import discover_page_structure
from rf_agent.discovery.modules import discover_modules_batch
from rf_agent.discovery.catalogs import discover_catalogs_batch

__all__ = [
    "MEMORY_FILE",
    "_domain_key",
    "cache_enabled",
    "load_all",
    "load_app",
    "load_app_for_generation",
    "save_app",
    "rf_to_playwright",
    "_extract_interactive_elements",
    "discover_login_recipe",
    "build_login_context",
    "_selector_grounded_in_html",
    "_derive_recipe_via_regex",
    "_static_html_lacks_form",
    "discover_page_structure",
    "discover_modules_batch",
    "discover_catalogs_batch",
]
