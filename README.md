# SmartTender AI

Plateforme de détection d'appels d'offres pour Inetum Tunisie.

**Module 1 — Détection intelligente** est opérationnel : deux portails
(TUNEPS, J360), déduplication, scoring explicable, notifications.
Les modules 2 (matching de CV) et 3 (génération documentaire) restent à venir.

L'architecture cible complète est décrite dans [`parcours_smarttender.html`](parcours_smarttender.html)
— à ouvrir dans un navigateur.

---

## Démarrage — 15 minutes

### 1. Prérequis

| Outil | Version | Pourquoi |
|---|---|---|
| **Docker Desktop** | récent | Toute la pile tourne en conteneurs |
| **Python** | 3.10+ | Tests, outils opérateur, capture de session |
| **Git** | — | — |

Sous Windows, Docker Desktop doit utiliser le backend **WSL 2**.

### 2. Configuration

```bash
cd backend
cp .env.example .env
```

Le fichier `.env` **n'est pas dans le dépôt** — il contient des identifiants.
Les valeurs par défaut suffisent pour tout faire tourner ; seul J360 demande un
compte (voir l'étape 5).

### 3. Lancer la pile

```bash
docker compose up -d
```

Treize conteneurs démarrent : PostgreSQL, Redis, MinIO, l'API, trois workers
Celery, l'ordonnanceur, et les outils d'observation. Les migrations
s'appliquent automatiquement.

### 4. Vérifier que tout répond

```bash
scripts\preflight          # Windows
bash scripts/preflight.sh  # Linux / macOS / Git Bash
```

**Ne saute pas cette étape.** Docker Desktop sous Windows perd par intermittence
la redirection d'un port publié : le conteneur reste « healthy » et le
navigateur reçoit une réponse vide. Le script teste chaque URL comme le ferait
un navigateur et redémarre ce qui ne répond pas.

Puis ouvre **<http://localhost:3000>**.

| Écran | URL |
|---|---|
| Interface | <http://localhost:3000> |
| API + documentation | <http://localhost:8000/docs> |
| Emails de notification | <http://localhost:8025> |
| Fichiers stockés (MinIO) | <http://localhost:9001> |
| Files Celery (Flower) | <http://localhost:5555> |
| Métriques (Grafana) | <http://localhost:3001> |

### 5. Créer un profil de notification

```bash
docker compose exec api smarttender-admin seed --user operator --email <ton.email>@inetum.com
```

Sans profil actif, le pipeline tourne jusqu'au bout et **n'annonce rien** — ce
qui ressemble exactement à un notificateur cassé. L'identifiant doit être
`operator`, celui que l'interface envoie.

Les emails partent vers Mailpit (<http://localhost:8025>), jamais vers de
vraies boîtes.

---

## TUNEPS fonctionne immédiatement

Le portail tunisien est public. Depuis l'interface, écran **Lancer un
scraping** : coche `tuneps`, mot-clé `logiciel`, lance.

Ou en ligne de commande, sans rien écrire en base :

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # une fois
.venv/Scripts/python -m app.cli dry-run tuneps --keyword logiciel --show 3
```

---

## J360 demande une session — par personne

J360 est un abonnement payant. Son login est protégé contre les robots, donc il
se capture **une fois dans un vrai navigateur** :

```bash
cd backend
.venv/Scripts/pip install playwright && .venv/Scripts/python -m playwright install chromium
.venv/Scripts/python -m app.cli capture-login j360
```

Une fenêtre Chrome s'ouvre. Tu te connectes **toi-même**, tu navigues jusqu'à
tes résultats, puis tu reviens au terminal et tu appuies sur Entrée.

Les cookies sont écrits dans `backend/certs/j360-session.json` — **exclu du
dépôt, et à ne jamais partager** : c'est un identifiant, au même titre qu'un
mot de passe.

Renseigne aussi les identifiants dans `.env` (ils servent à re-capturer la
session, pas à se connecter automatiquement) :

```
SMARTTENDER_CONNECTOR_J360_USERNAME=...
SMARTTENDER_CONNECTOR_J360_PASSWORD=...
```

Sans session, J360 s'affiche simplement comme indisponible et **tout le reste
fonctionne normalement** — c'est l'invariant central de la plateforme : une
source en panne n'arrête jamais les autres.

---

## Travailler sur le code

```bash
cd backend
make install        # environnement virtuel + dépendances
make test           # 475 tests, aucune infrastructure requise, ~10 s
make check          # lint + tests — à lancer avant de pousser
make connectors     # quelles sources sont exécutables, et pourquoi pas
```

La suite de tests ne demande **ni Docker, ni réseau, ni base de données**. Une
suite qui exige docker-compose est une suite que les gens cessent de lancer.

Après une modification du code backend :

```bash
docker build --target runtime -t smarttender/ingestion:local .
docker build --target runtime-browser -t smarttender/ingestion:browser .
docker compose up -d
```

Le frontend :

```bash
cd frontend
npm install && npm run dev      # développement, port 5173
docker build -t smarttender/frontend:local .   # pour la pile
```

---

## Arrêter proprement

```bash
docker compose stop
```

Puis, pour libérer la RAM que WSL2 conserve : quitter Docker Desktop depuis la
zone de notification, **puis** `wsl --shutdown`. L'ordre compte — sinon Docker
relance la machine virtuelle aussitôt.

> ⚠️ **`docker compose down -v` détruit les données.** Le `-v` supprime les
> volumes, donc tous les appels d'offres collectés. `stop` suffit : les
> conteneurs redémarrent sur les mêmes volumes.

---

## Ce qui n'est pas dans le dépôt

Volontairement, parce que ce sont des secrets :

| Fichier | Comment l'obtenir |
|---|---|
| `backend/.env` | `cp .env.example .env` puis compléter |
| `backend/certs/j360-session.json` | `smarttender-admin capture-login j360` |

Ne les commite jamais. Le `.gitignore` les couvre, mais un `git add -f` passe
outre.

---

## Documentation

- [`backend/README.md`](backend/README.md) — architecture détaillée, choix de
  conception, structure du projet
- [`parcours_smarttender.html`](parcours_smarttender.html) — l'architecture
  cible des trois modules
- <http://localhost:8000/docs> — l'API, testable directement
