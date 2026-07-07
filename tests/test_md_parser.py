"""
Tests unitaires pour rf_agent/md_parser.py

Couvre :
- Extraction d'un TC avec liste numérotée (français)
- Extraction d'un TC avec liste à puces (anglais)
- Extraction de plusieurs TCs dans un même fichier
- Format tableau markdown
- Markdown vide → liste vide (pas de crash)
- TC sans section "étapes" → steps vide
"""

import pytest
from rf_agent.md_parser import parse_md


# ── Fixtures ────────────────────────────────────────────────────────────────

SIMPLE_FR = """
## TC-001 — Login avec identifiants valides

Préconditions :
- L'utilisateur est sur la page de login

Étapes :
1. Entrer le nom d'utilisateur "Admin"
2. Entrer le mot de passe "admin123"
3. Cliquer sur le bouton Connexion

Résultat attendu :
L'utilisateur est redirigé vers le tableau de bord
"""

SIMPLE_EN = """
## TC001 Login with valid credentials

Preconditions:
- User is on the login page

Steps:
1. Enter username "standard_user"
2. Enter password "secret_sauce"
3. Click Login button

Expected result:
User is redirected to the inventory page
"""

MULTI_TC = """
## TC-001 — Premier test

Étapes :
- Étape A
- Étape B

Résultat attendu :
Résultat A

## TC-002 — Deuxième test

Étapes :
- Étape C

Résultat attendu :
Résultat B
"""

TABLE_FORMAT = """
## TC-003 — Test avec tableau

Étapes :

| N° | Action | Résultat attendu |
|----|--------|-----------------|
| 1  | Cliquer sur Login | La page de login s'affiche |
| 2  | Entrer les identifiants | Les champs sont remplis |
"""

NO_STEPS = """
## TC-004 — Test sans étapes

Préconditions :
- L'application est accessible

Résultat attendu :
La page s'affiche correctement
"""


# ── Tests ────────────────────────────────────────────────────────────────────

class TestParseMd:

    def test_empty_markdown_returns_empty_list(self):
        """Un markdown vide ne doit pas crasher — retourner une liste vide."""
        result = parse_md("")
        assert result == []

    def test_markdown_without_tc_returns_empty_list(self):
        """Un markdown sans aucun TC ne doit rien retourner."""
        result = parse_md("# Titre\n\nDu texte quelconque sans identifiant de test.")
        assert result == []

    def test_simple_french_tc_extracted(self):
        """Un TC simple en français doit être extrait correctement."""
        result = parse_md(SIMPLE_FR)
        assert len(result) == 1
        tc = result[0]
        assert tc["id"] == "TC-001"
        assert "Login" in tc["title"]

    def test_french_steps_extracted(self):
        """Les étapes en liste numérotée doivent être extraites."""
        result = parse_md(SIMPLE_FR)
        tc = result[0]
        assert len(tc["steps"]) == 3
        # Les marqueurs de liste (1. 2. 3.) doivent être supprimés
        assert not tc["steps"][0].startswith("1.")

    def test_french_expected_extracted(self):
        """Le résultat attendu doit être extrait."""
        result = parse_md(SIMPLE_FR)
        tc = result[0]
        assert tc["expected"] != ""
        assert "tableau de bord" in tc["expected"]

    def test_french_preconditions_extracted(self):
        """Les préconditions doivent être extraites."""
        result = parse_md(SIMPLE_FR)
        tc = result[0]
        assert len(tc["preconditions"]) >= 1

    def test_english_tc_extracted(self):
        """Un TC en anglais avec keywords anglais doit être extrait."""
        result = parse_md(SIMPLE_EN)
        assert len(result) == 1
        tc = result[0]
        assert "TC001" in tc["id"].upper().replace("-", "")
        assert len(tc["steps"]) == 3

    def test_multiple_tcs_extracted(self):
        """Plusieurs TCs dans un même fichier doivent tous être extraits."""
        result = parse_md(MULTI_TC)
        assert len(result) == 2
        ids = [tc["id"] for tc in result]
        assert "TC-001" in ids
        assert "TC-002" in ids

    def test_multiple_tcs_independent(self):
        """Les étapes d'un TC ne doivent pas contaminer le suivant."""
        result = parse_md(MULTI_TC)
        tc1 = next(tc for tc in result if tc["id"] == "TC-001")
        tc2 = next(tc for tc in result if tc["id"] == "TC-002")
        assert len(tc1["steps"]) == 2
        assert len(tc2["steps"]) == 1

    def test_table_format_steps_extracted(self):
        """Le format tableau doit être reconnu et les actions extraites."""
        result = parse_md(TABLE_FORMAT)
        assert len(result) == 1
        tc = result[0]
        assert len(tc["steps"]) == 2

    def test_table_format_steps_are_dicts(self):
        """Les étapes en tableau doivent être des dicts avec 'action' et 'expected'."""
        result = parse_md(TABLE_FORMAT)
        tc = result[0]
        first_step = tc["steps"][0]
        assert isinstance(first_step, dict)
        assert "action" in first_step
        assert "expected" in first_step

    def test_tc_without_steps_has_empty_steps(self):
        """Un TC sans section étapes doit avoir steps = []."""
        result = parse_md(NO_STEPS)
        assert len(result) == 1
        tc = result[0]
        assert tc["steps"] == []

    def test_result_is_always_list_of_dicts(self):
        """Chaque élément retourné doit être un dict avec les clés attendues."""
        result = parse_md(SIMPLE_FR)
        for tc in result:
            assert isinstance(tc, dict)
            assert "id" in tc
            assert "title" in tc
            assert "preconditions" in tc
            assert "steps" in tc
            assert "expected" in tc

    def test_list_markers_stripped_from_steps(self):
        """Les marqueurs de liste (-, *, 1.) doivent être supprimés des étapes."""
        result = parse_md(SIMPLE_FR)
        for step in result[0]["steps"]:
            if isinstance(step, str):
                assert not step.startswith("-")
                assert not step.startswith("*")
                assert not step[0].isdigit() or "." not in step[:3]
