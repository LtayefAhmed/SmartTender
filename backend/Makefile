# =============================================================================
# Developer entry points. `make help` lists everything.
# =============================================================================
.DEFAULT_GOAL := help
PY := python
VENV := .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
endif

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------
.PHONY: venv
venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip setuptools wheel

.PHONY: install
install: venv ## Install the package with dev extras
	$(BIN)/python -m pip install -e ".[dev]"

.PHONY: install-browser
install-browser: ## Install Playwright + Chromium (only for dynamic connectors)
	$(BIN)/python -m pip install playwright
	$(BIN)/playwright install chromium

# --- quality -----------------------------------------------------------------
.PHONY: lint
lint: ## Lint
	$(BIN)/ruff check app tests

.PHONY: format
format: ## Auto-fix lint and format
	$(BIN)/ruff check --fix app tests
	$(BIN)/ruff format app tests

.PHONY: typecheck
typecheck: ## Static type check
	$(BIN)/mypy app

.PHONY: test
test: ## Run the test suite (no infrastructure required)
	$(BIN)/python -m pytest -q

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	$(BIN)/python -m pytest --cov=app --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint test ## Lint + test — run this before pushing

# --- database ----------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply migrations
	$(BIN)/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	$(BIN)/alembic downgrade -1

.PHONY: seed
seed: ## Create the default schedules
	$(BIN)/smarttender-admin seed

# --- run ---------------------------------------------------------------------
.PHONY: api
api: ## Run the API with reload
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: worker
worker: ## Run one worker across every queue (development only)
	$(BIN)/celery -A app.workers.celery_app:celery_app worker \
		--queues scraping,parsing,scoring,notifications,maintenance,ocr,ai,default \
		--concurrency 4 --loglevel INFO

.PHONY: beat
beat: ## Run the scheduler
	$(BIN)/celery -A app.workers.celery_app:celery_app beat --loglevel INFO

.PHONY: flower
flower: ## Run the Celery dashboard
	$(BIN)/celery -A app.workers.celery_app:celery_app flower --port=5555

# --- diagnostics -------------------------------------------------------------
.PHONY: connectors
connectors: ## List connectors and why each is or is not runnable
	$(BIN)/smarttender-admin connectors

.PHONY: dry-run
dry-run: ## Run a connector without writing: make dry-run c=tuneps
	$(BIN)/smarttender-admin dry-run $(c) --pages 1 --limit 10

.PHONY: health
health: ## Probe infrastructure dependencies
	$(BIN)/smarttender-admin health

# --- docker ------------------------------------------------------------------
# docker-compose.yml lives at the repo root (one level up), not here — it
# orchestrates the whole platform, frontend included, not just this package.
COMPOSE := docker compose -f ../docker-compose.yml

.PHONY: up
up: ## Start the full stack
	$(COMPOSE) up -d --build

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean-volumes
clean-volumes: ## Stop the stack AND DELETE ALL DATA
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail the stack logs
	$(COMPOSE) logs -f --tail=100
