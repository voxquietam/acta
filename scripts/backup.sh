#!/usr/bin/env bash
#
# scripts/backup.sh — snapshot the Acta production database (and media volume)
# into a single timestamped bundle. Designed to be called from ``make deploy``
# *before* the new image rolls out, so the rollback path always has a fresh
# restore point.
#
# Layout (under ``${ACTA_BACKUP_DIR:-/var/backups/acta}``):
#
#   <tag>/<YYYYMMDD-HHMMSS>-<sha7>/
#       db.dump            -- pg_dump custom format, restorable via pg_restore -j
#       media.tar.zst      -- only when --mode=full
#       manifest.json      -- metadata + applied-migration list (rollback aid)
#
# Tags partition snapshots by purpose (pre-deploy, daily, manual…). Retention
# is enforced per tag, so daily snapshots never crowd out pre-deploy ones.
#
# Exits non-zero on any failure. ``make deploy`` invokes the script with
# ``set -e`` so a failed backup aborts the deploy itself — that's the whole
# point of putting it first in the chain.
#
# Usage:
#   scripts/backup.sh [--mode full|db-only] [--tag <name>] [--keep <N>]
#                     [--target-sha <sha>] [--out <dir>]
#
# Defaults: --mode full, --tag manual, --keep 14, --out $ACTA_BACKUP_DIR.

set -euo pipefail

# ---- CLI parsing ----------------------------------------------------

MODE="full"
TAG="manual"
KEEP=14
TARGET_SHA=""
OUT_DIR="${ACTA_BACKUP_DIR:-/var/backups/acta}"

usage() {
    sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)        MODE="${2:-}";        shift 2 ;;
        --tag)         TAG="${2:-}";         shift 2 ;;
        --keep)        KEEP="${2:-}";        shift 2 ;;
        --target-sha)  TARGET_SHA="${2:-}";  shift 2 ;;
        --out)         OUT_DIR="${2:-}";     shift 2 ;;
        -h|--help)     usage ;;
        *) echo "backup.sh: unknown arg '$1'" >&2; usage ;;
    esac
done

case "$MODE" in
    full|db-only) ;;
    *) echo "backup.sh: --mode must be 'full' or 'db-only', got '$MODE'" >&2; exit 2 ;;
esac

if ! [[ "$KEEP" =~ ^[0-9]+$ ]] || (( KEEP < 1 )); then
    echo "backup.sh: --keep must be a positive integer, got '$KEEP'" >&2
    exit 2
fi

# ---- Tool + container preflight -------------------------------------

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "backup.sh: missing tool '$1'" >&2; exit 3; }
}

require docker
require git
# zstd lives on the host (not in the container); needed when --mode=full so
# media tarballs land already compressed. db-only mode skips the requirement.
if [[ "$MODE" == "full" ]]; then
    require zstd
fi

COMPOSE="docker compose"
if ! $COMPOSE ps --status running --services 2>/dev/null | grep -qx db; then
    echo "backup.sh: 'db' service is not running — start the stack first" >&2
    exit 4
fi
if [[ "$MODE" == "full" ]] && ! $COMPOSE ps --status running --services 2>/dev/null | grep -qx web; then
    # Media lives in a Docker named volume; we read it through the web
    # container's mount. If web is down, a full bundle isn't possible.
    echo "backup.sh: 'web' service is not running — required for media tar" >&2
    exit 4
fi

# ---- Bundle paths ---------------------------------------------------

TS="$(date -u +%Y%m%d-%H%M%S)"
HEAD_SHA="$(git rev-parse HEAD)"
SHA7="${HEAD_SHA:0:7}"
BUNDLE_DIR="${OUT_DIR}/${TAG}/${TS}-${SHA7}"

mkdir -p "$BUNDLE_DIR"
trap 'rc=$?; if (( rc != 0 )); then echo "backup.sh: aborted, cleaning $BUNDLE_DIR" >&2; rm -rf "$BUNDLE_DIR"; fi' EXIT

log() { printf '[backup] %s\n' "$*" >&2; }

# ---- Postgres dump --------------------------------------------------

# POSTGRES_USER / _DB come from the same .env Compose reads, so we ask
# the running container for them rather than re-parsing the file.
PG_USER="$($COMPOSE exec -T db printenv POSTGRES_USER | tr -d '\r')"
PG_DB="$($COMPOSE exec -T db printenv POSTGRES_DB | tr -d '\r')"
if [[ -z "$PG_USER" || -z "$PG_DB" ]]; then
    echo "backup.sh: could not read POSTGRES_USER/DB from the db container" >&2
    exit 4
fi

log "pg_dump (custom format) → db.dump"
# -Fc = custom format (compressed, restorable with pg_restore -j for parallel
# restore). --no-owner / --no-privileges so the restore doesn't fail on a
# fresh cluster that doesn't have the same role names; ownership is rebuilt
# by Django's migrations anyway.
$COMPOSE exec -T db pg_dump \
    -U "$PG_USER" \
    -d "$PG_DB" \
    -Fc \
    --no-owner \
    --no-privileges \
    > "$BUNDLE_DIR/db.dump"

# Verify the dump is readable before we treat it as a real backup. Catches
# truncated output (disk full mid-write) and obviously corrupt files cheaply.
log "verify pg_restore --list"
docker run --rm -i postgres:16 pg_restore --list < "$BUNDLE_DIR/db.dump" > /dev/null

DB_BYTES="$(wc -c < "$BUNDLE_DIR/db.dump" | tr -d ' ')"

# ---- Media tar (full mode only) -------------------------------------

MEDIA_BYTES=0
if [[ "$MODE" == "full" ]]; then
    log "tar acta-media → media.tar.zst"
    # Streaming pipeline: tar inside the container → zstd on the host.
    # -T0 lets zstd parallelise across all cores; level 10 gives roughly the
    # same ratio as zstd default but a touch faster on mixed binary content.
    $COMPOSE exec -T web tar -C /app/media -cf - . \
        | zstd -T0 -10 -q -o "$BUNDLE_DIR/media.tar.zst"
    MEDIA_BYTES="$(wc -c < "$BUNDLE_DIR/media.tar.zst" | tr -d ' ')"
fi

# ---- Manifest -------------------------------------------------------

PG_VERSION="$($COMPOSE exec -T db pg_dump --version | tr -d '\r')"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Applied migrations: showmigrations --plan prints every entry; we keep only
# the ones marked [X] so the manifest is small and lists exactly what's in
# the DB at this moment. Rollback uses this to know which migrations the
# next release introduces vs. which were already there.
APPLIED_MIGRATIONS="$($COMPOSE exec -T web python manage.py showmigrations --plan 2>/dev/null \
    | grep -E '^\s*\[X\]' \
    | sed -E 's/^\s*\[X\]\s+//' \
    | jq -R . | jq -s . || echo '[]')"

# jq isn't strictly required — fall back to a compact JSON if it's missing,
# at the cost of skipping the migration list rather than failing the backup.
if ! command -v jq >/dev/null 2>&1; then
    APPLIED_MIGRATIONS='[]'
fi

cat > "$BUNDLE_DIR/manifest.json" <<JSON
{
  "version": 1,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "tag": "${TAG}",
  "mode": "${MODE}",
  "git_sha": "${HEAD_SHA}",
  "git_branch": "${GIT_BRANCH}",
  "target_sha": "${TARGET_SHA}",
  "pg_version": "${PG_VERSION}",
  "db_bytes": ${DB_BYTES},
  "media_bytes": ${MEDIA_BYTES},
  "applied_migrations": ${APPLIED_MIGRATIONS}
}
JSON

# ---- Retention ------------------------------------------------------

TAG_DIR="${OUT_DIR}/${TAG}"
# Sort by name (timestamp prefix is sortable), drop everything older than
# the newest $KEEP. Skip the current bundle implicitly because it's the
# newest. ``mapfile`` is bash 4+ only (missing from macOS' bundled bash 3);
# the while-read variant keeps the smoke-on-laptop story working too.
while IFS= read -r stale; do
    [[ -z "$stale" ]] && continue
    log "prune $TAG/$stale"
    rm -rf "${TAG_DIR:?}/${stale:?}"
done < <(ls -1 "$TAG_DIR" 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) || true)

# ---- Done -----------------------------------------------------------

trap - EXIT
log "ok: $BUNDLE_DIR ($(du -sh "$BUNDLE_DIR" | cut -f1))"
# stdout is reserved for the bundle path so Make / shell pipelines can
# capture it: ``BUNDLE=$(scripts/backup.sh --tag pre-deploy --mode full)``.
printf '%s\n' "$BUNDLE_DIR"
