# RF Generator — Architecture

## Vue d'ensemble

RF Generator est un agent IA qui génère automatiquement des fichiers de tests
Robot Framework à partir d'un plan de test en markdown et d'une URL cible.
Il s'intègre dans OmniPlatform en tant que nœud de tests de non-régression.

## Composants principaux

```
Markdown (.md)  +  URL
        │
        ▼
┌─────────────────┐
│   md_parser     │  Extrait les cas de test structurés (id, titre, étapes, résultat)
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│              app_memory + dom_catalog        │
│                                             │
│  1. Découverte de la recette de login        │  ← LLM + Playwright
│  2. Capture du DOM post-login               │  ← Playwright headless
│  3. Construction du catalogue d'éléments    │  ← Playwright + règles déterministes
└────────────────────┬────────────────────────┘
                     │  catalog : { id → selector, label, role }
                     ▼
┌─────────────────────────────────────────────┐
│                rf_generator                  │
│                                             │
│  Mode A (catalog path) :                    │
│    LLM reçoit catalogue → plan JSON         │  ← LLM contraint aux IDs du catalogue
│    step_renderer traduit JSON → RF keywords │  ← Python pur, déterministe
│                                             │
│  Mode B (legacy path, fallback) :           │
│    LLM reçoit HTML brut → RF keywords       │  ← LLM, moins fiable
└────────────────────┬────────────────────────┘
                     │  fichier .robot
                     ▼
┌─────────────────────────────────────────────┐
│               rf_executor                   │
│                                             │
│  Lance Robot Framework                      │
│  Parse output.xml                           │
│  Boucle de guérison (jusqu'à 3 tentatives)  │
│    ├─ healer_runtime : JS dans le navigateur│  ← Pas de LLM, rapide
│    └─ self_healer    : LLM + DOM réel       │  ← LLM, si JS échoue
└────────────────────┬────────────────────────┘
                     │  résultats + rapport
                     ▼
┌─────────────────────────────────────────────┐
│   rf_docx_reporter  +  trello  +  mission_control │
│                                             │
│  Rapport Word .docx                         │
│  Cartes Trello pour les échecs              │
│  Résultats envoyés à Mission Control        │
└─────────────────────────────────────────────┘
```

## Principe anti-hallucination

Le LLM ne peut jamais inventer un sélecteur. Il reçoit une liste d'IDs extraits
du DOM réel (ex: `btn_login`, `in_username`) et ne peut référencer que ces IDs.
Si un ID est absent du catalogue, la validation rejette le plan et bascule en Mode B.

## Mémoire par domaine

`app_memory.json` persiste la recette de login et les catalogues par domaine.
Une deuxième génération pour la même application ne re-crawle pas — elle relit le cache.

## Intégration OmniPlatform

`mission_control.py` gère :
- Enregistrement de l'agent au démarrage
- Heartbeat toutes les 30 secondes
- Réception des tâches (md_content + base_url)
- Soumission des résultats (passed/failed/healed + URL du rapport)
