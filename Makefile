COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm --no-deps

.DEFAULT_GOAL := help
.PHONY: help up down logs migrate revision psql shell test test-fast lint fmt typecheck check reset

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

up: ## Start the full local stack and apply migrations
	@test -f .env || cp .env.example .env
	$(COMPOSE) up -d db redis minio
	$(COMPOSE) up minio-init
	$(COMPOSE) run --rm migrate
	$(COMPOSE) up -d api worker web
	@echo "api  -> http://localhost:8000/docs"
	@echo "web  -> http://localhost:3000"
	@echo "minio-> http://localhost:9001"

down: ## Stop the stack (volumes preserved)
	$(COMPOSE) down

logs: ## Tail application logs
	$(COMPOSE) logs -f api worker

migrate: ## Apply migrations to head
	$(COMPOSE) run --rm migrate

revision: ## Autogenerate a migration: make revision m="add foo"
	$(COMPOSE) run --rm migrate alembic revision --autogenerate -m "$(m)"

psql: ## Open psql as the schema owner
	$(COMPOSE) exec db sh -c 'psql -U $$POSTGRES_USER -d $$POSTGRES_DB'

shell: ## Python shell inside the api image
	$(COMPOSE) run --rm api python

test: ## Run the full test suite against a live database
	$(COMPOSE) up -d db redis
	$(COMPOSE) run --rm migrate
	$(COMPOSE) run --rm api pytest

test-fast: ## Run only tests that do not need a database
	$(RUN) api pytest -m "not requires_db"

lint: ## ruff check + format check
	$(RUN) api ruff check .
	$(RUN) api ruff format --check .

fmt: ## Apply ruff formatting and import fixes
	$(RUN) api ruff check --fix .
	$(RUN) api ruff format .

typecheck: ## mypy strict
	$(RUN) api mypy api worker tests

check: lint typecheck test ## Everything CI runs

reset: ## DESTRUCTIVE: drop all local data volumes and rebuild
	$(COMPOSE) down -v
	rm -rf .data
	$(MAKE) up
