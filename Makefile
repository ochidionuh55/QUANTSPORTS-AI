# QUANTSPORT AI - developer commands.
# Run `make help` for the list.

.DEFAULT_GOAL := help
COMPOSE := docker compose
PYTHON := python3

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Environment ------------------------------------------------------------
.PHONY: env
env: ## Create .env from the template if it does not exist
	@test -f .env || (cp .env.example .env && echo "Created .env - fill in your secrets.")

.PHONY: install
install: ## Install runtime and dev dependencies locally
	$(PYTHON) -m pip install -r requirements-dev.txt

# --- Stack ------------------------------------------------------------------
.PHONY: up
up: env ## Build and start the full stack
	$(COMPOSE) up --build -d
	@echo "API:  http://localhost:8000/health"
	@echo "Docs: http://localhost:8000/docs"

.PHONY: up-infra
up-infra: env ## Start only Postgres and Redis (for local, non-Docker app runs)
	$(COMPOSE) up -d postgres redis

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete volumes (DESTROYS LOCAL DATA)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs from all services
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: shell
shell: ## Open a shell in the api container
	$(COMPOSE) exec api /bin/bash

# --- Quality gate -----------------------------------------------------------
.PHONY: test
test: ## Run the test suite
	$(PYTHON) -m pytest

.PHONY: coverage
coverage: ## Run tests with a coverage report
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Run ruff checks
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

.PHONY: format
format: ## Auto-format and fix lint issues
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

.PHONY: typecheck
typecheck: ## Run mypy in strict mode
	$(PYTHON) -m mypy app

.PHONY: check
check: lint typecheck test ## Run the full quality gate (what CI runs)

# --- Database ---------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply all migrations
	$(COMPOSE) exec api alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

.PHONY: migrate-down
migrate-down: ## Roll back one migration
	$(COMPOSE) exec api alembic downgrade -1

.PHONY: db-shell
db-shell: ## Open psql against the dev database
	$(COMPOSE) exec postgres psql -U quantsport -d quantsport

# --- Health -----------------------------------------------------------------
.PHONY: health
health: ## Query the running API health endpoint
	@curl -fsS http://localhost:8000/health | $(PYTHON) -m json.tool || echo "API not reachable."
