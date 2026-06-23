"""
Tests unitaires pour rf_agent/step_renderer.py

Couvre :
- Plan valide → code Robot Framework correct
- selector_id inconnu → UnknownIdError
- Valeur vide dans un step 'input' → ${EMPTY} dans le RF généré
- Step open_app_and_login → bonne syntaxe
- Step go_to avec nav_id → URL correcte dans le RF
- Step click → Smart Click avec label, role, selector
- Screenshot automatique ajouté si absent du plan
"""

import pytest
from rf_agent.step_renderer import render_robot_test, UnknownIdError


# ── Fixtures ────────────────────────────────────────────────────────────────

CATALOG = {
    "url": "https://example.com",
    "title": "Test App",
    "elements": [
        {
            "id": "in_username",
            "role": "text_input",
            "label": "Username",
            "selector": "xpath://input[@placeholder='Username']"
        },
        {
            "id": "btn_login",
            "role": "button",
            "label": "Login",
            "selector": "xpath://button[@type='submit']"
        },
        {
            "id": "in_search",
            "role": "text_input",
            "label": "Search",
            "selector": "xpath://input[@placeholder='Search']"
        },
    ],
    "nav": [
        {
            "id": "lnk_admin",
            "label": "Admin",
            "href": "https://example.com/admin"
        }
    ]
}

VALID_PLAN = {
    "test_id": "TC-001",
    "title": "Login test",
    "steps": [
        {"keyword": "open_app_and_login", "username": "Admin", "password": "admin123"},
        {"keyword": "input", "selector_id": "in_username", "value": "testuser"},
        {"keyword": "click", "selector_id": "btn_login"},
    ]
}

PLAN_EMPTY_VALUE = {
    "test_id": "TC-002",
    "title": "Empty input test",
    "steps": [
        {"keyword": "open_app_and_login", "username": "Admin", "password": "admin123"},
        {"keyword": "input", "selector_id": "in_search", "value": ""},
        {"keyword": "click", "selector_id": "btn_login"},
    ]
}

PLAN_UNKNOWN_ID = {
    "test_id": "TC-003",
    "title": "Bad plan",
    "steps": [
        {"keyword": "open_app_and_login", "username": "Admin", "password": "admin123"},
        {"keyword": "click", "selector_id": "btn_inexistant"},
    ]
}

PLAN_WITH_GOTO = {
    "test_id": "TC-004",
    "title": "Navigation test",
    "steps": [
        {"keyword": "open_app_and_login", "username": "Admin", "password": "admin123"},
        {"keyword": "go_to", "nav_id": "lnk_admin"},
    ]
}

PLAN_WITH_SCREENSHOT = {
    "test_id": "TC-005",
    "title": "Plan with screenshot",
    "steps": [
        {"keyword": "open_app_and_login", "username": "Admin", "password": "admin123"},
        {"keyword": "screenshot", "name": "TC-005_result"},
    ]
}


# ── Tests ────────────────────────────────────────────────────────────────────

class TestRenderRobotTest:

    def test_valid_plan_returns_render_result(self):
        """Un plan valide doit retourner un RenderResult sans lever d'exception."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert result is not None
        assert result.body != ""

    def test_test_id_in_output(self):
        """L'ID du test doit apparaître dans le corps généré."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "TC-001" in result.body

    def test_open_app_and_login_rendered(self):
        """open_app_and_login doit générer la ligne Open App And Login avec les identifiants."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "Open App And Login" in result.body
        assert "Admin" in result.body
        assert "admin123" in result.body

    def test_click_generates_smart_click(self):
        """Un step 'click' doit générer 'Smart Click' avec label, role, selector."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "Smart Click" in result.body
        assert "xpath://button[@type='submit']" in result.body

    def test_input_generates_smart_input(self):
        """Un step 'input' doit générer 'Smart Input' avec la valeur."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "Smart Input" in result.body
        assert "testuser" in result.body

    def test_empty_value_renders_as_empty_variable(self):
        """Une valeur vide doit être rendue comme ${EMPTY} et non comme chaîne vide."""
        result = render_robot_test(CATALOG, PLAN_EMPTY_VALUE)
        assert "${EMPTY}" in result.body

    def test_unknown_selector_id_raises_error(self):
        """Un selector_id absent du catalogue doit lever UnknownIdError."""
        with pytest.raises(UnknownIdError):
            render_robot_test(CATALOG, PLAN_UNKNOWN_ID)

    def test_go_to_with_nav_id_renders_url(self):
        """go_to avec nav_id doit générer 'Go To' avec l'URL correspondante."""
        result = render_robot_test(CATALOG, PLAN_WITH_GOTO)
        assert "Go To" in result.body
        assert "https://example.com/admin" in result.body

    def test_screenshot_auto_added_when_absent(self):
        """Si le plan ne contient pas de screenshot, un Capture Page Screenshot doit être ajouté."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "Capture Page Screenshot" in result.body

    def test_screenshot_not_duplicated_when_present(self):
        """Si le plan contient déjà un screenshot, il ne doit pas être dupliqué."""
        result = render_robot_test(CATALOG, PLAN_WITH_SCREENSHOT)
        count = result.body.count("Capture Page Screenshot")
        assert count == 1

    def test_empty_plan_raises_error(self):
        """Un plan vide doit lever UnknownIdError."""
        with pytest.raises(UnknownIdError):
            render_robot_test(CATALOG, {})

    def test_plan_without_steps_raises_error(self):
        """Un plan sans steps doit lever UnknownIdError."""
        with pytest.raises(UnknownIdError):
            render_robot_test(CATALOG, {"test_id": "TC-X", "title": "X", "steps": []})

    def test_unknown_refs_empty_on_valid_plan(self):
        """Un plan valide ne doit générer aucune référence inconnue."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert result.unknown_refs == []

    def test_selector_from_catalog_used_in_output(self):
        """Le sélecteur exact du catalogue doit apparaître dans le RF généré."""
        result = render_robot_test(CATALOG, VALID_PLAN)
        assert "xpath://input[@placeholder='Username']" in result.body
