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

If a deploy regresses something:

```bash
# 1. Backup the DB first.
docker compose exec -T db pg_dump -U acta acta \
  > backup-$(date +%Y%m%d-%H%M%S).sql

# 2. Roll any migration the bad release added back to the prior one
#    (Django can't reverse without the code that knew how — do this
#    before resetting).
docker compose exec web python manage.py migrate <app> <prev_migration>

# 3. Hard-reset to the last good commit and rebuild.
git reset --hard <previous-sha>
docker compose up -d --build
```

## Backups

Right now manual: `pg_dump` as above before risky deploys. A cron-based
off-site backup is on the TODO list.

## Troubleshooting

- **`make deploy` says "Your local branch has diverged"** — someone
  committed on the VM. Save those changes elsewhere, then re-run.
- **502 from Traefik after deploy** — check the container is up:
  `docker compose ps`. If it is, hit it directly on the VM with
  `curl -I http://localhost/accounts/login/`. If that 200's, the
  Traefik backend URL is wrong — ping admin.
- **`docker compose up` rebuilds the image but the page still shows
  the old CSS** — Cloudflare or browser cache. Purge or hard-reload.
