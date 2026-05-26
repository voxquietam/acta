# Acta — common dev shortcuts.
#
# Usage: `make <target>`.  Most targets run inside the `web` service
# of docker-compose.  Don't add anything destructive without explicit
# user intent — see CLAUDE.md "Don't run on the user's behalf".

COMPOSE       := docker compose
COMPOSE_DEV   := docker compose -f docker-compose.yml -f docker-compose.dev.yml
EXEC          := $(COMPOSE) exec web
MANAGE        := $(EXEC) python manage.py

NODE_RUN      := docker run --rm -v "$(PWD):/work" -w /work node:20-alpine

.PHONY: help up down restart logs build rebuild ps shell dbshell migrate \
	makemigrations createsuperuser test test-fast format lint pre-commit \
	i18n-extract i18n-compile build-js watch-js install-js ci-check deploy

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

test-js: ## run vitest unit suite (static_src/js/lib/**/*.test.js)
	$(NODE_RUN) npm test

test-js-watch: ## vitest in watch mode for active TDD on frontend libs
	$(NODE_RUN) -it npm run test:watch

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

# ---- Frontend bundle (description editor) --------------------------
# Runs Node in a throwaway container so the host doesn't need Node
# installed. See docs/decisions/0014-frontend-architecture.md for the
# bundling rationale (the editor is the only piece of the app that
# needs a build step; the rest stays on CDN).

install-js: ## install npm dependencies for the editor bundle
	$(NODE_RUN) npm install --no-audit --no-fund

build-js: ## bundle static_src/js/description_editor.js -> static/js/*.bundle.js
	$(NODE_RUN) npm run build:js

watch-js: ## rebuild the bundle on every save (Ctrl-C to stop)
	$(NODE_RUN) -it npm run watch:js

# ---- Frontend bundle (Tailwind CSS) --------------------------------
# Compiles ``static_src/css/main.css`` (with @tailwind directives) into
# a static stylesheet. Replaces the Tailwind Play CDN — pre-compiled
# CSS loads instantly with no in-browser JIT pass, so the dashboard
# no longer flashes unstyled HTML for ~200-500ms on cold load.

build-css: ## compile static_src/css/main.css -> static/css/main.bundle.css
	$(NODE_RUN) npm run build:css

watch-css: ## rebuild the stylesheet on every template/CSS save
	$(NODE_RUN) -it npm run watch:css

build-front: build-js build-css ## compile both bundles in one go

# ---- Lucide icon library -------------------------------------------
# Snapshots ``lucide-static``'s SVG files into a JSON manifest that
# ``apps/web/templatetags/lucide.py`` reads at import. The manifest is
# committed so deploy doesn't need ``node_modules``. Re-run when
# bumping the lucide-static version.

extract-icons: ## rebuild apps/web/lucide_icons.json from node_modules/lucide-static
	$(NODE_RUN) node -e "process.exit(0)" >/dev/null  # ensure docker is warm
	python3 scripts/extract_lucide.py

# ---- CI-equivalent shortcuts ---------------------------------------
# One-command analogues of what a CI runner would do. Cheaper than
# spinning up Woodpecker / GitHub Actions for a self-hosted single-
# admin project. Run ``make ci-check`` before pushing, ``make deploy``
# on the prod VM after the push lands. See docs/deployment.md.

ci-check: ## lint + tests + frontend build + django check --deploy
	pre-commit run --all-files
	$(EXEC) pytest --create-db --tb=short -q
	$(NODE_RUN) npm ci --no-audit --no-fund
	$(NODE_RUN) npm run build:css
	$(NODE_RUN) npm run build:js
	$(EXEC) env DJANGO_SETTINGS_MODULE=acta.settings.prod \
		DJANGO_ALLOWED_HOSTS=actaspace.com \
		DJANGO_CSRF_TRUSTED_ORIGINS=https://actaspace.com \
		python manage.py check --deploy

# Branch to deploy. Defaults to ``master`` (the prod release branch);
# override with ``make deploy BRANCH=dev`` while gating a feature on
# the prod VM before it's merged. ``git reset --hard`` makes the call
# idempotent and tolerant of force-pushes.
BRANCH ?= master

deploy: ## (on prod VM) fetch + reset to BRANCH (default master) + rebuild
	git fetch --tags origin $(BRANCH)
	git reset --hard origin/$(BRANCH)
	$(COMPOSE) up -d --build
	$(COMPOSE) exec -T web python manage.py setup_scheduled_jobs
	$(COMPOSE) exec -T web python manage.py telegram_set_webhook || true
	$(COMPOSE) ps
