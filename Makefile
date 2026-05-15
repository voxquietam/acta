# Acta — common dev shortcuts.
#
# Usage: `make <target>`.  Most targets run inside the `web` service
# of docker-compose.  Don't add anything destructive without explicit
# user intent — see CLAUDE.md "Don't run on the user's behalf".

COMPOSE       := docker compose
COMPOSE_DEV   := docker compose -f docker-compose.yml -f docker-compose.dev.yml
EXEC          := $(COMPOSE) exec web
MANAGE        := $(EXEC) python manage.py

.PHONY: help up down restart logs build rebuild ps shell dbshell migrate \
	makemigrations createsuperuser test test-fast format lint pre-commit \
	i18n-extract i18n-compile

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---- Docker lifecycle ----------------------------------------------

up: ## start dev stack in background
	$(COMPOSE_DEV) up -d

down: ## stop stack (keep data volume)
	$(COMPOSE) down

restart: ## restart web only (templates pick up; venv state intact)
	$(COMPOSE) restart web

build: ## rebuild image without starting
	$(COMPOSE_DEV) build

rebuild: ## rebuild + start (after requirements/Dockerfile changes)
	$(COMPOSE_DEV) up -d --build

ps: ## show running containers
	$(COMPOSE) ps

logs: ## tail web logs
	$(COMPOSE) logs -f web

# ---- Django shortcuts ----------------------------------------------

shell: ## open a Django shell (IPython)
	$(MANAGE) shell

dbshell: ## open psql attached to the dev DB
	$(MANAGE) dbshell

migrate: ## apply pending migrations
	$(MANAGE) migrate

makemigrations: ## auto-generate migrations from model changes
	$(MANAGE) makemigrations

createsuperuser: ## create an admin user
	$(MANAGE) createsuperuser

# ---- Tests + formatting --------------------------------------------

test: ## run full pytest suite
	$(EXEC) pytest

test-fast: ## skip slow markers (when we have them)
	$(EXEC) pytest -m "not slow"

format: ## run black + isort over the repo
	$(EXEC) black .
	$(EXEC) isort .

lint: ## run flake8
	$(EXEC) flake8

pre-commit: ## run pre-commit on every file
	pre-commit run --all-files

# ---- i18n ----------------------------------------------------------

i18n-extract: ## scan code/templates for translatable strings
	$(MANAGE) makemessages -l uk

i18n-compile: ## compile .po into .mo for runtime
	$(MANAGE) compilemessages
