# Deploying Acta

Acta is self-hosted only. The production instance is one Docker Compose
stack on a single VM. No CI runner — deploy is a single `make` target
the operator runs by hand after pushing to `master`.

## Prerequisites

- A Linux VM with Docker + Docker Compose plugin installed.
- A reverse proxy in front of it that terminates TLS and forwards HTTP
  to port `80` on the VM. The production setup uses Traefik on the
  admin's infrastructure; nginx / Caddy / Cloudflare Tunnel all work.
- The repo cloned to `/opt/acta` (or anywhere — the Makefile uses
  relative paths).
- A `.env` file alongside `docker-compose.yml` with the secrets
  `docker-compose.yml` references (`DJANGO_SECRET_KEY`,
  `POSTGRES_PASSWORD`, `DJANGO_ALLOWED_HOSTS`,
  `DJANGO_CSRF_TRUSTED_ORIGINS`). `.env.example` is the template.

For the full step-by-step of cutting a new release (version bump,
tag, prod-side commands, smoke, rollback) see
[`release-playbook.md`](release-playbook.md). This file documents the
underlying mechanics; the playbook is the linear runbook.

## Day-to-day deploy

After a push to `master`, on the prod VM:

```bash
cd /opt/acta
make deploy
```

That's it. Behind the target:

1. `git fetch --tags origin master`
2. `git reset --hard origin/master` — idempotent, survives force-push
3. `docker compose up -d --build` — rebuilds the image, recreates the
   container; the entrypoint runs migrations, `compilemessages`, and
   `collectstatic`
4. `docker compose ps` — prints the status so you can eyeball it

Downtime: ~10–30 seconds while the container restarts. For 20 users
that's fine.

### Deploying a non-master branch

To gate a feature on the prod VM before merging it to `master`, pass
the branch name via `BRANCH=`:

```bash
make deploy BRANCH=dev
```

Same flow, just resets to `origin/dev` instead. Run `make deploy` (no
override) once the branch is merged back to bring prod onto master.

## Pre-push gate (optional but recommended)

Before pushing to `master`, run the full check matrix locally:

```bash
make ci-check
```

This runs everything a CI pipeline would:

- `pre-commit run --all-files` — black / isort / flake8 / template
  comment lint
- `pytest --create-db` — full test suite against a fresh schema
- `npm ci && npm run build:css && npm run build:js` — frontend bundles
  compile cleanly
- `python manage.py check --deploy` under prod settings — catches
  `SECURE_*` misconfigs, debug-true leaks, weak `SECRET_KEY`

If `ci-check` is green and you trust the diff, push and `make deploy`
on the VM.

## First deploy after a `container_name` change

Acta pinned its container names to `acta.web` / `acta.db`. If you're
upgrading from an older deploy where Compose auto-named them
(`acta-web-1` / `acta-db-1`), do **one** clean stop on the VM before
the first `make deploy`:

```bash
docker compose down
# data volume ``acta-pgdata`` is preserved — DB is safe
make deploy
```

After that, Compose uses the new names and `make deploy` works
normally.

## Rolling back

Every `make deploy` writes a pre-deploy snapshot to
`/var/backups/acta/pre-deploy/<ts>-<sha>/` before touching anything,
so the rollback target is already on disk. The fastest path is to
restore that bundle:

```bash
# 1. Find the snapshot taken just before the bad deploy. Each bundle
#    directory's manifest.json records the git_sha the snapshot
#    matches plus the target_sha the deploy was rolling toward.
ls -lt /var/backups/acta/pre-deploy/ | head

# 2. Restore. Takes a safety snapshot of the current (broken) state
#    first, stops web + qcluster, runs pg_restore, optionally replaces
#    the media volume, restarts services.
make restore FROM=/var/backups/acta/pre-deploy/<ts>-<sha>

# 3. Point the code at the matching commit and rebuild so running
#    code, schema, and media all line up again. The manifest in the
#    bundle tells you which SHA to reset to.
git reset --hard <previous-sha>
docker compose up -d --build
```

If the regression is small enough that the schema didn't change (no
new migrations between the broken deploy and the previous good one),
you can skip the DB restore and just reset the code. Check with
`make show-pending-migrations` before deciding.

## Backups

Local backups are automatic:

- **Pre-deploy** — `make deploy` runs `make backup-prerelease` before
  every fetch/reset. `BRANCH=master` snapshots DB + media (retention
  14); any other branch snapshots DB only (retention 7). A failed
  backup aborts the deploy.
- **Daily** — install the cron line in `docs/operations.md` "Backups"
  to schedule `make backup-daily` (DB only, retention 7) at 04:00 UTC.

Bundles land under `$ACTA_BACKUP_DIR` (default `/var/backups/acta`).
See `docs/operations.md` "Backups" for layout, manifest contents,
restore drill procedure, and the still-open off-site sync task.

## Troubleshooting

- **`make deploy` says "Your local branch has diverged"** — someone
  committed on the VM. Save those changes elsewhere, then re-run.
- **502 from Traefik after deploy** — check the container is up:
  `docker compose ps`. If it is, hit it directly on the VM with
  `curl -I http://localhost/accounts/login/`. If that 200's, the
  Traefik backend URL is wrong — ping admin.
- **`docker compose up` rebuilds the image but the page still shows
  the old CSS** — Cloudflare or browser cache. Purge or hard-reload.
