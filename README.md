# AI QA Agent MVP

Ce projet est un agent autonome alimenté par l'IA (LLM) et Playwright pour tester automatiquement des pages web (ex: pages de login). Il suit un cycle **OBSERVE -> THINK -> ACT -> VERIFY -> REPORT**.

## 🚀 Installation & Démarrage

Suivez ces étapes pour installer et exécuter l'agent localement.

### 1. Prérequis
- Python 3.8+ installé.
- Git installé.

### 2. Cloner le projet
```bash
git clone <URL_DU_REPO>
cd agent_testing
```

### 3. Configurer l'environnement virtuel et les dépendances
Ouvrez votre terminal dans le dossier `agent_testing` :
```bash
# Créer l'environnement virtuel
python -m venv .venv

# Activer l'environnement virtuel (Windows)
.venv\Scripts\activate


# Installer les dépendances Python
pip install -r requirements.txt

# Installer les navigateurs Playwright
playwright install chromium
```

### 4. Configuration des variables d'environnement
Créez un fichier `.env` à la racine du projet (`agent_testing/.env`). Ce fichier ne doit JAMAIS être commité sur GitHub (il est déjà dans le `.gitignore`).
Ajoutez-y votre clé d'API Groq ou n'importe:
```env
GROQ_API_KEY=votre_cle_api_ici
```

### 5. Lancer l'application
Démarrez le serveur FastAPI :
```bash
python main.py
```
Ouvrez votre navigateur et allez sur **http://localhost:8000** pour utiliser l'interface de l'agent.

---

## 🛠️ Guide de Contribution pour l'équipe (Git Workflow)

Afin d'éviter les conflits et de garder le code propre, nous utilisons un système de **Branches**. Ne travaillez jamais directement sur la branche `main`.

### Étape 1 : Créer sa propre branche
Avant de commencer à coder une nouvelle fonctionnalité, assurez-vous d'être à jour et créez une branche :
```bash
# Se mettre à jour avec la version principale
git checkout main
git pull origin main

# Créer une branche (utilisez un nom descriptif, ex: feature/trello, fix/ui-bug)
git checkout -b feature/nom-de-ma-fonctionnalite
```

### Étape 2 : Coder et Tester
Développez votre fonctionnalité. Testez-la localement pour vous assurer que rien n'est cassé.

### Étape 3 : Sauvegarder et Envoyer
```bash
# Ajouter les fichiers modifiés
git add .

# Créer un commit descriptif
git commit -m "feat: ajout de l'intégration Trello"

# Envoyer votre branche sur le GitHub
git push origin feature/nom-de-ma-fonctionnalite
```

### Étape 4 : Créer une Pull Request (PR)
1. Allez sur la page GitHub du projet.
2. GitHub vous proposera de créer une **Pull Request** pour votre branche récemment poussée. Cliquez dessus.
3. Décrivez ce que vous avez fait.
4. L'encadrant ou un autre membre de l'équipe reverra le code et le fusionnera (Merge) dans `main`.

## 📁 Architecture du projet

- `main.py` : Point d'entrée de l'API FastAPI et du serveur web.
- `agent/` : Cœur de l'agent (Orchestrateur, Observer, Planner, Executor, Reporter).
- `tools/` : Wrappers pour les services externes (Playwright, Groq LLM, Trello).
- `frontend/` : Interface utilisateur web (HTML, CSS, JS).
- `output/` : ce dossier sera crée automatiquement apres les tests (screenshots + rapport .json)
