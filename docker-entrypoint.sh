#!/bin/sh
# Production entrypoint for the Acta web container.
#
# Responsibilities (in order):
#   1. Wait for Postgres to accept TCP connections.
#   2. Apply Django migrations.
#   3. Compile gettext catalogues (.mo) — .po files are committed, .mo
#      files are built here at deploy time per docs/decisions/0018-i18n.md.
#   4. Collect static assets into STATIC_ROOT.
#   5. exec the container's CMD (uvicorn by default).
#
# Anything that should run on every container start belongs here.
# Anything that should run only once per release (data backfills, etc.)
# belongs in a separate one-shot job.

set -e

# --- Wait for Postgres ---------------------------------------------------
DB_HOST="${POSTGRES_HOST:-db}"
DB_PORT="${POSTGRES_PORT:-5432}"
echo "Waiting for Postgres at ${DB_HOST}:${DB_PORT}..."
for i in $(seq 1 30); do
  if python -c "import socket; socket.create_connection(('${DB_HOST}', int('${DB_PORT}')), timeout=1)" 2>/dev/null; then
    echo "Postgres is reachable."
    break
  fi
  if [ "$i" = "30" ]; then
    echo "Postgres did not become reachable within 30s — aborting." >&2
    exit 1
  fi
  sleep 1
done

# --- Django bootstrap ----------------------------------------------------
echo "Applying migrations..."
python manage.py migrate --noinput

echo "Compiling translations..."
python manage.py compilemessages --ignore=.venv 2>&1 | grep -vE "already compiled|^processing" || true

echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

# --- Hand off to the container CMD --------------------------------------
echo "Starting application: $*"
exec "$@"
