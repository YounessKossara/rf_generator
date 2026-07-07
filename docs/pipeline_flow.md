# RF Generator — Flux de traitement détaillé

## Déclenchement

Le pipeline démarre de deux façons :
- **Via l'interface web** : POST `/api/generate-rf` avec `markdown_content` + `base_url`
- **Via Mission Control** : heartbeat reçoit une tâche assignée → `handle_incoming_task()`

---

## Étape 1 — Parsing du markdown

**Module :** `rf_agent/md_parser.py` → `parse_md(markdown_content)`

**Entrée :** chaîne de caractères markdown brute

**Sortie :** liste de dicts
```python
[
  {
    "id": "TC-001",
    "title": "Login avec identifiants valides",
    "preconditions": ["L'utilisateur est sur la page de login"],
    "steps": ["Entrer le nom d'utilisateur", "Entrer le mot de passe", "Cliquer sur Connexion"],
    "expected": "L'utilisateur arrive sur le tableau de bord"
  },
  ...
]
```

---

## Étape 2 — Extraction des identifiants par défaut

**Module :** `rf_agent/rf_generator.py` → `_extract_default_credentials()`

**Entrée :** liste de cas de test + markdown brut

**Sortie :** `(default_user, default_pass)` — chaînes de caractères

---

## Étape 3 — Chargement / découverte de la recette de login

**Module :** `rf_agent/app_memory.py` → `load_app_for_generation()` + `discover_login_recipe()`

**Entrée :** `base_url`

**Processus :**
1. Vérifier si une recette existe dans `output/app_memory.json`
2. Si absente ou incomplète : ouvrir la page de login avec Playwright, envoyer le HTML au LLM
3. LLM retourne les sélecteurs : champ username, champ password, bouton submit
4. Sauvegarder dans le cache

**Sortie :** dict `{ username_selector, password_selector, submit_selector, success_indicator, app_type }`

---

## Étape 4 — Découverte du DOM post-login

**Module :** `rf_agent/app_memory.py` → `discover_page_structure()`

**Entrée :** `base_url`, recette de login, identifiants

**Processus :**
1. Playwright ouvre le navigateur, se connecte avec les identifiants
2. Attend `networkidle` (important pour les SPAs Vue/React)
3. Extrait les éléments interactifs du DOM (boutons, champs, liens)
4. Retourne un HTML compact (~9000 caractères max)

**Sortie :** chaîne HTML compacte du tableau de bord

---

## Étape 5 — Classification des tests par module

**Module :** `rf_agent/rf_generator.py` → `_extract_nav_links()` + `_classify_test_to_module()`

**Entrée :** HTML du tableau de bord, liste de cas de test

**Sortie :** `{ "TC-001": "https://app.com/admin", "TC-005": "https://app.com/users", ... }`

---

## Étape 6 — Extraction des catalogues (Phase A)

**Module :** `rf_agent/app_memory.py` → `discover_catalogs_batch()`
**Module :** `rf_agent/dom_catalog.py` → `extract_catalog()`

**Entrée :** liste d'URLs de modules, identifiants, recette

**Processus :**
1. Une session Playwright : connexion une seule fois
2. Visite chaque URL de module
3. Pour chaque page : extrait tous les éléments interactifs avec des IDs synthétiques stables
4. Vérifie que chaque sélecteur est réellement présent dans le DOM

**Sortie :** `{ "https://app.com/admin": { elements: [...], nav: [...] }, ... }`

---

## Étape 7 — Construction de l'en-tête du fichier .robot

**Module :** `rf_agent/rf_generator.py` → `_build_header()`

**Sortie :** sections `*** Settings ***`, `*** Variables ***`, `*** Keywords ***`
contenant les imports de librairies et les mots-clés Smart (Smart Click, Smart Input, etc.)

---

## Étape 8A — Planification par catalogue (Mode A)

**Module :** `rf_agent/rf_generator.py` → `_try_catalog_plan()`
**Module :** `rf_agent/step_renderer.py` → `render_robot_test()`

**Entrée :** cas de test + catalogue fusionné (login + dashboard + module)

**Processus :**
1. LLM reçoit le catalogue (IDs seulement, pas de sélecteurs bruts)
2. LLM produit un plan JSON avec uniquement des références aux IDs du catalogue
3. Validation : tous les IDs existent dans le catalogue ?
4. Si oui → `step_renderer` traduit chaque step en mot-clé Robot Framework
5. Si non → retourne `""` → bascule en Mode B pour ce test

**Sortie :** corps du cas de test Robot Framework (avec Smart wrappers)

---

## Étape 8B — Génération LLM par lots (Mode B, fallback)

**Module :** `rf_agent/rf_generator.py` → génération par batch de 5 tests

**Entrée :** texte du cas de test + HTML brut de la page

**Processus :**
1. LLM génère les lignes Robot Framework directement depuis le HTML
2. `_validate_selectors_against_dom()` vérifie chaque sélecteur
3. `_downgrade_to_text_locator()` remplace les sélecteurs invalides par des XPath textuels

**Sortie :** corps du cas de test Robot Framework (sans Smart wrappers)

---

## Étape 9 — Validation syntaxique

**Module :** `rf_agent/rf_validator.py` → `validate_rf_syntax()`

**Entrée :** fichier .robot complet

**Processus :** vérifie la structure Robot Framework. Si erreur → LLM corrige.

---

## Étape 10 — Exécution Robot Framework

**Module :** `rf_agent/rf_executor.py` → `execute_rf()`

**Processus :**
1. Lance `python -m robot` avec le fichier .robot
2. Robot Framework ouvre Chrome, exécute chaque test
3. Produit `output.xml`, `report.html`, `log.html`

---

## Étape 11 — Parsing des résultats

**Module :** `rf_agent/rf_executor.py` → `_parse_output_xml()`

**Sortie :** `{ total, passed, failed, failed_tests: [...], passed_tests: [...] }`

---

## Étape 12 — Boucle de guérison (pour les tests échoués)

Pour chaque test échoué avec une erreur de sélecteur :

**Niveau 1 — Runtime (pendant l'exécution)**
- `healer_runtime.py` : JavaScript dans le navigateur cherche l'élément par label + rôle
- Si trouvé → nouvelle tentative avec le sélecteur réparé
- Aucun LLM, < 1 seconde

**Niveau 2 — LLM (après échec)**
- `self_healer.py` : ouvre Playwright, se reconnecte, navigue jusqu'à la page de l'échec
- Capture le DOM réel
- LLM reçoit (code du test échoué + message d'erreur + DOM) → propose une correction
- Réexécute uniquement ce test
- Jusqu'à 3 tentatives

---

## Étape 13 — Reporting et notification

- `rf_docx_reporter.py` → génère le rapport Word .docx
- `tools/trello.py` → crée des cartes Trello pour les tests encore échoués
- `mission_control.py` → soumet les résultats à Mission Control (passed/failed/healed + URL rapport)
