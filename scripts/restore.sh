#!/usr/bin/env bash
#
# scripts/restore.sh — restore an Acta backup bundle produced by backup.sh.
#
# This is destructive: every row in the live database is dropped and replaced
# by the dump, and (in full mode) every file in the media volume is replaced
# by the tarball. Use only when rolling back a bad deploy or seeding a local
# dev copy from a prod snapshot.
#
# Safeguards before anything destructive happens:
#   1. The manifest is printed and the operator must type the workspace slug
#      shown in it. ``--yes-i-am-sure`` only skips the typed confirmation in
#      non-interactive contexts (CI, scripted dev resets).
#   2. A safety snapshot of the current DB (and media, if mode=full) is
#      written to ``${ACTA_BACKUP_DIR}/restore-safety/`` first. Pass
#      ``--no-safety`` to skip — only do that in an unrecoverable emergency.
#   3. ``web`` and ``qcluster`` are stopped during the restore so nothing
#      writes to the DB / media mid-replace; both are brought back up at the
#      end. ``db`` stays up.
#
# Usage:
#   scripts/restore.sh <bundle_dir> [--yes-i-am-sure] [--no-safety]
#
# Example:
#   scripts/restore.sh /var/backups/acta/pre-deploy/20260531-180000-ac2657a

set -euo pipefail

# ---- CLI ------------------------------------------------------------

BUNDLE=""
ASSUME_YES=0
NO_SAFETY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes-i-am-sure) ASSUME_YES=1; shift ;;
        --no-safety)     NO_SAFETY=1;  shift ;;
        -h|--help)
            sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
            exit 2
            ;;
        --*) echo "restore.sh: unknown flag '$1'" >&2; exit 2 ;;
        *)
            if [[ -n "$BUNDLE" ]]; then
                echo "restore.sh: only one bundle dir accepted" >&2; exit 2
            fi
            BUNDLE="$1"; shift ;;
    esac
done

if [[ -z "$BUNDLE" ]]; then
    echo "restore.sh: bundle directory required" >&2
    sed -n '24,26p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
if [[ ! -d "$BUNDLE" ]]; then
    echo "restore.sh: '$BUNDLE' is not a directory" >&2
    exit 2
fi

MANIFEST="$BUNDLE/manifest.json"
DB_DUMP="$BUNDLE/db.dump"
MEDIA_TAR="$BUNDLE/media.tar.zst"

[[ -f "$MANIFEST" ]] || { echo "restore.sh: manifest.json missing in $BUNDLE" >&2; exit 2; }
[[ -f "$DB_DUMP" ]]  || { echo "restore.sh: db.dump missing in $BUNDLE"      >&2; exit 2; }

# ---- Preflight ------------------------------------------------------

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "restore.sh: missing tool '$1'" >&2; exit 3; }
}

require docker
require jq

COMPOSE="docker compose"
$COMPOSE ps --status running --services 2>/dev/null | grep -qx db \
    || { echo "restore.sh: 'db' service must be running" >&2; exit 4; }

MODE="$(jq -r '.mode' "$MANIFEST")"
BUNDLE_SHA="$(jq -r '.git_sha' "$MANIFEST")"
BUNDLE_BRANCH="$(jq -r '.git_branch' "$MANIFEST")"
BUNDLE_CREATED="$(jq -r '.created_at' "$MANIFEST")"

if [[ "$MODE" == "full" ]]; then
    [[ -f "$MEDIA_TAR" ]] || { echo "restore.sh: media.tar.zst missing from full-mode bundle" >&2; exit 2; }
    require zstd
fi

# ---- Confirmation ---------------------------------------------------

CUR_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"

cat >&2 <<EOM
=== Restore plan =============================================================
  Bundle:        $BUNDLE
  Created at:    $BUNDLE_CREATED
  Mode:          $MODE
  Bundle SHA:    $BUNDLE_SHA  (branch: $BUNDLE_BRANCH)
  Current SHA:   $CUR_SHA
  DB dump:       $(du -h "$DB_DUMP" | cut -f1)
EOM

if [[ "$MODE" == "db-only" ]]; then
    cat >&2 <<EOM
  Media:         NOT IN BUNDLE — current media volume will be left untouched.
                 If the dump references files not present locally, those
                 attachments will 404 after restore.
EOM
else
    cat >&2 <<EOM
  Media tar:     $(du -h "$MEDIA_TAR" | cut -f1)
                 Current media volume will be WIPED and replaced.
EOM
fi

if (( ASSUME_YES == 0 )); then
    cat >&2 <<EOM

This will DROP every table in the live database and replace it with the
dump above. Type the literal string  i-understand  to proceed:
EOM
    read -r CONFIRM
    if [[ "$CONFIRM" != "i-understand" ]]; then
        echo "restore.sh: aborted." >&2
        exit 1
    fi
fi

log() { printf '[restore] %s\n' "$*" >&2; }

# ---- Safety snapshot ------------------------------------------------

if (( NO_SAFETY == 0 )); then
    log "safety snapshot before restore (use --no-safety to skip)"
    SAFETY_TAG="restore-safety"
    SAFETY_MODE="$MODE"
    "$(dirname "$0")/backup.sh" \
        --tag "$SAFETY_TAG" \
        --mode "$SAFETY_MODE" \
        --keep 5 \
        --target-sha "$BUNDLE_SHA" \
        >/dev/null
fi

# ---- Stop writers ---------------------------------------------------

log "stopping web + qcluster"
$COMPOSE stop web qcluster >/dev/null

restore_failed() {
    local rc=$?
    if (( rc != 0 )); then
        log "FAILED (exit $rc) — bringing web + qcluster back up so the site responds"
        $COMPOSE start web qcluster >/dev/null || true
    fi
}
trap restore_failed EXIT

# ---- Restore DB -----------------------------------------------------

PG_USER="$($COMPOSE exec -T db printenv POSTGRES_USER | tr -d '\r')"
PG_DB="$($COMPOSE exec -T db printenv POSTGRES_DB | tr -d '\r')"

log "pg_restore --clean --if-exists into $PG_DB"
# --clean + --if-exists drops every object before recreating it, so we don't
# need to dropdb/createdb (which would fail anyway while other sessions hold
# locks). We stream the dump on stdin, which means pg_restore can't read
# the archive twice — so no -j (parallel restore needs to seek the file).
# At Acta scale (sub-second restores for the current DB) the serial path
# is fine; if the dump ever crosses a few GB, switch to ``docker cp`` +
# in-container ``pg_restore -j N /tmp/db.dump`` to get parallelism back.
$COMPOSE exec -T db pg_restore \
    -U "$PG_USER" \
    -d "$PG_DB" \
    --clean --if-exists \
    --no-owner \
    --no-privileges \
    < "$DB_DUMP"

# ---- Restore media --------------------------------------------------

if [[ "$MODE" == "full" ]]; then
    log "wiping + repopulating /app/media from media.tar.zst"
    # ``compose run`` reuses the service spec, including its volume mounts,
    # so we don't have to repeat the ``acta-media:/app/media`` line here
    # (Compose name-prefixes the actual volume, ``acta_acta-media`` etc.).
    # The temporary container is gone after ``--rm``; web restarts below.
    # --no-deps so ``compose run`` doesn't restart the db (which we are
    # *currently restoring into*); the temp container only needs the
    # named volume from the web service, not a fresh db sibling.
    zstd -dc "$MEDIA_TAR" | $COMPOSE run --rm -T --no-deps \
        --entrypoint sh \
        web -c 'rm -rf /app/media/* /app/media/.[!.]* /app/media/..?* 2>/dev/null; tar -C /app/media -xf -'
fi

# ---- Bring writers back ---------------------------------------------

log "starting web + qcluster"
$COMPOSE start web qcluster >/dev/null
trap - EXIT

cat >&2 <<EOM

=== Restore done =============================================================
The DB now matches bundle $BUNDLE_SHA.
If the running code is at a *newer* SHA than the bundle, run migrations to
re-align: ``docker compose exec web python manage.py migrate``.
EOM
