# SmartTender AI

Plateforme de détection d'appels d'offres et de rapprochement de CV, pour
Inetum Tunisie.

**Module 1 — Détection intelligente.** Opérationnel. Deux portails (TUNEPS,
J360), déduplication, extraction documentaire avec OCR, scoring explicable,
notifications.

**Module 2 — Matching de CV.** Opérationnel. Découpage des dossiers en
passages, index vectoriel Qdrant, embeddings multilingues, score hybride avec
preuve par profil.

**Module 3 — Génération documentaire.** À venir.

L'architecture cible est décrite dans
[`parcours_smarttender.html`](parcours_smarttender.html) — à ouvrir dans un
navigateur.

---

## Ce que le dépôt ne contient pas — lis ceci en premier

Quatre choses manquent volontairement. Un clone frais **ne démarre pas** tant
qu'elles ne sont pas en place, et l'erreur que tu obtiendras sinon ne dira pas
laquelle manque.

| Ce qui manque | Pourquoi | Comment l'obtenir |
|---|---|---|
| `backend/.env` | contient des identifiants réels | `cp .env.example .env`, puis compléter |
| `backend/models/` | **536 Mo** de poids ONNX — des binaires de cette taille n'ont rien à faire dans un historique git | `python scripts/fetch_models.py` |
| `backend/certs/j360-session.json` | c'est un identifiant, au même titre qu'un mot de passe | `python -m app.cli capture-login j360` |
| la clé Mistral | secret par nature | à coller dans `.env` |

Le `.gitignore` les couvre. **Un `git add -f` passe outre** — ne le fais pas.

> Un piège déjà rencontré : la règle `models/` était écrite sans barre oblique
> initiale, et git exclut alors *tout* dossier de ce nom à n'importe quelle
> profondeur. Elle avalait `app/db/models/` — les neuf modèles ORM n'ont jamais
> été versionnés pendant des semaines, et personne ne s'en apercevait puisque
> chacun les avait en local. Elle est désormais ancrée en `/models/`. Si tu
> ajoutes une règle d'exclusion, ancre-la.

---

## Démarrage — 20 minutes

### 1. Prérequis

| Outil | Version | Pourquoi |
|---|---|---|
| **Docker Desktop** | récent | toute la pile tourne en conteneurs |
| **Python** | 3.10+ | tests, outils opérateur, capture de session |
| **Node** | 18+ | uniquement pour développer le frontend |
| **Git** | — | — |

Sous Windows, Docker Desktop doit utiliser le backend **WSL 2**. Prévois
**8 Go de RAM** disponibles : la pile en consomme environ 5, et le modèle
d'embedding 1,5 de plus.

### 2. Configuration

Depuis la racine du dépôt :

```bash
cp .env.example .env
```

Les valeurs par défaut suffisent pour tout faire tourner. Deux sont
facultatives et changent ce que la plateforme sait faire :

```ini
SMARTTENDER_CONNECTOR_J360_USERNAME=      # sans : J360 s'affiche indisponible
SMARTTENDER_LLM__MISTRAL_API_KEY=         # sans : le raffinement LLM est ignoré
```

Dans les deux cas, **tout le reste continue de fonctionner**. C'est l'invariant
central : une source absente ou un service tiers en panne dégradent une
capacité, jamais la disponibilité.

### 3. Télécharger les modèles d'embedding

```bash
cd backend
python scripts/fetch_models.py
cd ..
```

536 Mo, en deux modèles. Le script est idempotent — un fichier déjà présent est
laissé tel quel — et écrit dans un fichier temporaire avant de le renommer, de
sorte qu'un téléchargement interrompu ne laisse pas un modèle tronqué qui se
charge et produit n'importe quoi.

Les conteneurs les lisent par un montage `./models` en lecture seule : un seul
téléchargement sert toute la pile, et une reconstruction d'image ne déplace pas
un demi-gigaoctet pour une correction d'une ligne.

Sans eux, l'ingestion, le scoring et les notifications fonctionnent
normalement ; seul le rapprochement CV / appel d'offres est indisponible.

### 4. Lancer la pile

```bash
docker compose up -d
```

Quinze conteneurs démarrent :

| Rôle | Services |
|---|---|
| Données | PostgreSQL, Redis, MinIO, Qdrant |
| Application | API, frontend |
| Traitement | `worker-pipeline`, `worker-scraping`, `worker-support`, `worker-ai`, `beat` |
| Observation | Flower, Prometheus, Grafana, Mailpit |

Les migrations s'appliquent automatiquement.

`worker-ai` tourne délibérément à **concurrence 1**. Celery fonctionne par
préfork : chaque processus charge sa propre copie du modèle de 470 Mo. Routé
vers une file servie à concurrence 8, il a mis 3,7 Go de poids identiques en
mémoire et emporté la machine hôte. Ne remonte pas ce nombre en croyant
accélérer les choses.

### 5. Vérifier que tout répond

```bash
scripts\preflight          # Windows
bash scripts/preflight.sh  # Linux / macOS / Git Bash
```

**Ne saute pas cette étape.** Docker Desktop sous Windows perd par intermittence
la redirection d'un port publié : le conteneur reste « healthy » et le
navigateur reçoit une réponse vide. Le script teste chaque URL comme le ferait
un navigateur et redémarre ce qui ne répond pas.

Puis ouvre **<http://localhost:3000>**.

| Écran | URL | Identifiants |
|---|---|---|
| Interface | <http://localhost:3000> | — |
| API + documentation | <http://localhost:8000/docs> | — |
| Emails de notification | <http://localhost:8025> | — |
| Fichiers stockés (MinIO) | <http://localhost:9001> | `minioadmin` / `minioadmin` |
| Index vectoriel (Qdrant) | <http://localhost:6333/dashboard> | aucune |
| Files Celery (Flower) | <http://localhost:5555> | — |
| Métriques (Grafana) | <http://localhost:3001> | — |

> Les identifiants MinIO sont ceux par défaut et Qdrant n'a **aucune
> authentification**. Acceptable en développement local, inacceptable en
> production — à durcir avant tout déploiement.

### 6. Créer un profil de notification

```bash
docker compose exec api smarttender-admin seed --user operator --email <toi>@inetum.com
```

Sans profil actif, le pipeline tourne jusqu'au bout et **n'annonce rien** — ce
qui ressemble exactement à un notificateur cassé. L'identifiant doit être
`operator`, celui que l'interface envoie.

Les emails partent vers Mailpit, jamais vers de vraies boîtes.

---

## Les deux portails

### TUNEPS — fonctionne immédiatement

Le portail tunisien est public. Écran **Lancer un scraping** : coche `tuneps`,
mot-clé `logiciel`, lance.

Ou sans rien écrire en base :

```bash
cd backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # une fois
.venv/Scripts/python -m app.cli dry-run tuneps --keyword logiciel --show 3
```

Les avis détaillés demandent un **certificat électronique TUNTRUST**, qui n'est
pas encore disponible. Sans lui, TUNEPS remonte les listes mais pas les
dossiers.

### J360 — une session par personne

J360 est un abonnement payant dont le login est protégé contre les robots. La
session se capture **une fois, dans un vrai navigateur** :

```bash
cd backend
.venv/Scripts/pip install playwright && .venv/Scripts/python -m playwright install chromium
.venv/Scripts/python -m app.cli capture-login j360
```

Une fenêtre Chrome s'ouvre. Tu te connectes toi-même, tu navigues jusqu'à tes
résultats, tu reviens au terminal et tu appuies sur Entrée.

> **La session t'identifie à chaque requête.** Garde le connecteur
> mono-thread et lent, et n'insiste jamais après un 401 ou un 403. C'est le
> volume qui se remarque, pas l'usage.

---

## Travailler sur le code

### Les branches

```text
main     partagée      personne n'y travaille directement
ahmed    ·
farah    ·  une branche par personne
sahar    ·
```

On développe sur sa branche, on y intègre `main` régulièrement, et on ne
fusionne dans `main` qu'une fois les tests verts. Résoudre les conflits sur sa
propre branche laisse `main` intact tant que ce n'est pas propre.

```bash
git checkout -b <prenom>          # une fois
git add -A && git commit -m "…"
git push -u origin <prenom>

git checkout main && git pull --ff-only origin main
git checkout <prenom> && git merge main    # conflits résolus ICI
```

### Tests et qualité

```bash
cd backend
make install        # environnement virtuel + dépendances
make test           # 655 tests, aucune infrastructure requise, ~20 s
make check          # lint + tests — à lancer avant de pousser
make connectors     # quelles sources sont exécutables, et pourquoi pas
```

La suite ne demande **ni Docker, ni réseau, ni base de données, ni clé d'API**.
Une suite qui exige docker-compose est une suite que les gens cessent de
lancer ; une suite qui appelle une API payante est une suite qu'on désactive au
premier quota dépassé.

### Reconstruire après modification

Depuis la racine du dépôt (`docker-compose.yml` n'est pas dans `backend/` — il
orchestre toute la plateforme, frontend compris) :

```bash
docker compose build api worker-pipeline worker-scraping worker-support worker-ai
docker compose up -d
```

Le frontend :

```bash
cd frontend
npm install && npm run dev                      # développement, port 5173
docker build -t smarttender/frontend:local .    # pour la pile
```

---

## Quand quelque chose ne va pas

### Page blanche après une reconstruction du frontend

`Ctrl+Shift+R`. Le navigateur garde l'ancienne page, qui nomme un bundle dont
l'empreinte a changé, et réclame un fichier qui n'existe plus. Corrigé par un
`Cache-Control: no-cache` sur `index.html`, mais un onglet ouvert avant le
correctif garde le problème.

### Docker répond 500 sur toutes ses routes

La VM WSL est engorgée. Redémarrer Docker Desktop ne suffit pas :

```powershell
wsl --shutdown
```

Puis rouvrir Docker Desktop. Vérifie `vmmemWSL` dans le gestionnaire de tâches :
au-delà de 6 Go, c'est ce symptôme.

### Le matching renvoie « service indisponible »

`worker-ai` démarre, ou les modèles sont absents.

```bash
docker compose logs worker-ai --tail 30
docker compose exec worker-ai python -c "from app.services.embeddings import get_embedder; print(get_embedder().dimensions)"
```

Doit afficher `384`.

### Une source ne remonte rien

L'écran **Sources & santé** dit pourquoi : identifiants manquants, session
expirée, ou aucun résultat. Un run vide qui ne s'explique pas est un bug ; un
run vide qui s'explique est une information.

---

## Arrêter proprement

```bash
docker compose stop
```

Puis, pour libérer la RAM que WSL2 conserve : quitter Docker Desktop depuis la
zone de notification, **puis** `wsl --shutdown`. L'ordre compte — sinon Docker
relance la machine virtuelle aussitôt.

> ⚠️ **`docker compose down -v` détruit les données.** Le `-v` supprime les
> volumes, donc tous les appels d'offres collectés, les CV importés et l'index
> vectoriel. `stop` suffit : les conteneurs redémarrent sur les mêmes volumes.

---

## Mistral — ce qui part, et ce qui ne part pas

Le raffinement LLM sert à réparer un OCR bancal et à lire les exigences d'un
CCTP. Il est **facultatif** : sans clé, chaque étape est ignorée.

`SMARTTENDER_LLM__SCOPE` porte une décision, pas un réglage :

| Valeur | Ce qui peut partir |
|---|---|
| `off` | rien |
| `tenders` | les avis, qui sont des documents publics |
| `tenders_and_cvs` | les avis et les CV |

Dans **tous** les cas, le texte est anonymisé avant l'envoi : noms, courriels,
téléphones, adresses, profils sociaux, dates de naissance et numéros
d'identité sont remplacés par des marqueurs typés. Les technologies et les
montants sont préservés — une anonymisation qui mange les compétences produit
un CV que plus personne ne peut rapprocher de rien.

Le parcours pose comme garantie transverse que les CV ne quittent jamais le
serveur (RGPD / INPDP). Passer à `tenders_and_cvs` est un arbitrage à
documenter, pas un réglage à changer en passant.

---

## Documentation

- [`backend/README.md`](backend/README.md) — architecture détaillée, choix de
  conception, structure du projet
- [`parcours_smarttender.html`](parcours_smarttender.html) — l'architecture
  cible des trois modules
- <http://localhost:8000/docs> — l'API, testable directement
- Le code lui-même : les décisions non évidentes sont expliquées en commentaire
  à l'endroit où elles s'appliquent, avec la mesure qui les a motivées.
