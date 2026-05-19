# syntax=docker/dockerfile:1.7

# --- Stage 1: frontend bundle ---------------------------------------------
# Compiles Tailwind (``static/css/main.bundle.css``) and bundles TipTap
# (``static/js/description_editor.bundle.js``). Runs in node:20-alpine so
# the final python image stays slim. Outputs are COPY'd into the python
# stage below, overwriting anything pre-built in the repo — CI is the
# source of truth for shipped assets.
FROM node:20-alpine AS frontend
WORKDIR /build

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# Sources Tailwind's content scanner needs to find used classes.
# Keep this list aligned with ``content:`` in tailwind.config.js.
COPY tailwind.config.js ./
COPY static_src/ ./static_src/
COPY templates/ ./templates/
COPY apps/ ./apps/
COPY static/ ./static/

RUN npm run build:css && npm run build:js

# --- Stage 2: python runtime ----------------------------------------------
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl build-essential libpq-dev gettext \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

COPY requirements/ /app/requirements/
ARG REQUIREMENTS=requirements/prod.txt
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --system -r ${REQUIREMENTS}

COPY . /app/

# Overwrite repo-committed bundles with the freshly built ones from the
# frontend stage. The committed copies in git stay as a fallback for
# dev environments that skip the docker build.
COPY --from=frontend /build/static/css/main.bundle.css /app/static/css/main.bundle.css
COPY --from=frontend /build/static/js/description_editor.bundle.js /app/static/js/description_editor.bundle.js

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

# The entrypoint runs migrations, compiles translations, and collects static
# assets before exec'ing CMD. ``docker-compose.dev.yml`` overrides CMD with
# ``runserver`` for local development — the entrypoint still runs first so
# dev containers also get a fresh migrate / compilemessages / collectstatic.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "acta.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
