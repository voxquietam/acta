# Release playbook

A linear, copy-pasteable run of what it takes to ship a new Acta
release end-to-end — from a clean `dev` branch to a verified prod
container running the new tag. The first run of this playbook
(2026-05-31, release `v0.5.0` / hot-fix `v0.5.1`) surfaced a few
foot-guns that are documented inline below so the second run goes
straight through.

This is the **operator's** runbook; the underlying mechanics live in
`docs/deployment.md` (deploy target shape) and `docs/operations.md`
(env vars, recurring jobs, backups layout).

## Step 0 — Pre-release sanity

On the laptop, on the branch you're shipping (`dev` by default):

```bash
# Confirm dev is clean and pushed
git status -sb
git log --oneline origin/dev..dev   # should be empty after push
```

Decide the version. Acta uses semver:

- patch (`0.5.0` → `0.5.1`) — bug-fix only, no schema or API change
- minor (`0.4.0` → `0.5.0`) — new features, possibly migrations; the
  default for a "real release"
- major — reserved for `v1.0.0` (public launch)

Bump it in two places:

- `pyproject.toml` (`version = "X.Y.Z"`)
- `CHANGELOG.md` — close `## [Unreleased]` with a new `## [X.Y.Z] —
  YYYY-MM-DD` section and the Keep-a-Changelog buckets
  (Added / Changed / Fixed / Performance / Infrastructure / ⚠ Breaking).

Commit as two separate Conventional Commits:

```bash
git commit -m "chore(release): vX.Y.Z"   # pyproject + CHANGELOG
```

(Feature commits go in earlier as their own `feat(scope)` / `fix(scope)`
commits — never bundle them with the release commit.)

## Step 1 — CI-equivalent gate

```bash
source .venv/bin/activate          # pre-commit lives in the venv
make ci-check
```

This runs pre-commit, the full pytest suite, `npm ci` + both bundles,
and `manage.py check --deploy`. Takes ~3-5 minutes. Two warnings from
the deploy-check (`security.W008` SSL redirect, `security.W009`
SECRET_KEY) are **expected** on the laptop — they fire because the
local `.env` has a dev secret and no SSL redirect; on the prod VM
those env vars are real and the warnings disappear.

If anything else is red, fix it before tagging — once a tag is
pushed it's effectively immutable history.

## Step 2 — Merge to master and tag

```bash
git push origin dev

git checkout master
git pull origin master
git merge --ff-only dev            # fast-forward only; never a merge commit
git push origin master

git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

History stays linear (matches the repo convention — no merge bubbles,
see CLAUDE.md / feedback memory on rebase-not-merge). The annotated
tag carries the message so `git describe` and `gh release` see it.

## Step 3 — Pre-flight on the prod VM (first time only)

Skip this on every subsequent release; it only applies to the very
first deploy of the backup pipeline.

```bash
ssh prod
# As root (su / panel console / ssh root@):
mkdir -p /var/backups/acta
chown muser:muser /var/backups/acta
chmod 750 /var/backups/acta
exit

# As muser:
ls -ld /var/backups/acta            # drwxr-x--- muser muser
touch /var/backups/acta/.write-test && rm /var/backups/acta/.write-test && echo OK

# jq is required by scripts/restore.sh — install once (Debian):
# (as root)
apt-get install -y jq
# (as muser)
jq --version                        # jq-1.7+
```

Lessons from the first run, in case they come up again:

- `sudo` may not exist on a hardened VM. Switch to root the way the
  provider supports (su / ssh root / panel console), do the few root
  ops, then `exit` back to `muser`.
- `jq` not being installed silently degrades `applied_migrations` in
  the backup manifest to `[]` (the script falls back) **and** breaks
  `scripts/restore.sh` entirely (it requires `jq` to read the
  manifest). Always install `jq` before relying on the pipeline.

## Step 4 — Strap-in backup (first time only)

The Makefile target `make backup-prerelease` only exists from the
release that introduced the backup pipeline onward. On the very first
deploy that brings that release into prod, the running code still has
the **old** Makefile, so `make backup-prerelease` doesn't exist yet.
Workaround — pull the script from the tag without touching the working
tree:

```bash
cd /opt/acta
git fetch --tags origin master
git show vX.Y.Z:scripts/backup.sh > /tmp/acta-backup.sh
chmod +x /tmp/acta-backup.sh
/tmp/acta-backup.sh --tag pre-deploy --mode full --keep 14
```

Verify the bundle is good before continuing:

```bash
NEW=$(ls -dt /var/backups/acta/pre-deploy/*/ | head -1)
jq '.mode, .git_sha, (.applied_migrations | length)' "$NEW/manifest.json"
ls -lh "$NEW"
```

All three files (`db.dump`, `media.tar.zst`, `manifest.json`) should
exist and `.applied_migrations` should be a non-empty array.

From the **second** release onward this whole step disappears —
`make deploy` calls `make backup-prerelease` itself.

## Step 5 — Deploy

```bash
cd /opt/acta && make deploy
```

What happens:

1. `make backup-prerelease MODE=full KEEP=14` — auto-snapshot of the
   live DB + media volume. `BRANCH=dev` snapshots DB only.
2. `git fetch --tags origin master`
3. `git reset --hard origin/master` — code switches to the new tag.
4. `docker compose up -d --build` — pulls image cache, rebuilds the
   web + qcluster images, recreates containers.
5. The entrypoint inside the new web container runs `migrate` →
   `compilemessages` → `collectstatic` → `seed_telegram_templates`.
6. `setup_scheduled_jobs` — registers django-q schedules.
7. `telegram_set_webhook` — re-points the Telegram webhook (idempotent).
8. `docker compose ps` — prints final status.

Downtime: ~10-30 s.

Foot-guns from the first run:

- **Files referenced by `package.json` build scripts must be `COPY`-ed
  in `Dockerfile`.** Locally `make build-css` works via a bind-mount
  of the whole repo, but the in-image build only sees what `COPY`
  pulled in. The first try of v0.5.0 failed with *"Specified config
  file `/build/tailwind.prose.config.js` does not exist"* — the
  config file was in git, just not in the Dockerfile's COPY block.
  Hot-fix shipped as v0.5.1.
- A failed `docker compose up --build` leaves the **old** containers
  running, not a half-broken new one. So a broken release is safe to
  diagnose — the live site keeps serving the previous version. The
  fix flow is "patch the bug → tag a `vX.Y.Z+1` → `make deploy`
  again". Don't try to hand-patch the prod working tree; the next
  `git reset --hard` would wipe it.

## Step 6 — Smoke

Migrations didn't print in the `make deploy` output because containers
were started detached (`-d`). Read them from the logs:

```bash
# 1. Migration history (look for the new ones marked OK)
docker compose logs web | grep -iE 'applying|migrat|error|traceback' | head -40

# 2. Current schema position
docker compose exec -T web python manage.py showmigrations --plan | tail -25

# 3. HTTP healthcheck via the local proxy port (Traefik sits upstream
#    on the admin's infra, so don't try https://… from inside the VM)
curl -sI http://localhost/accounts/login/ | head -1   # 200 / 302

# 4. Django check on the running config
docker compose exec -T web python manage.py check    # "no issues"
```

Then open `https://actaspace.com/` in a browser and click around:
dashboard, kanban, inbox, a project page. Telegram bot — send a
`/start` if the bot username is reachable.

## Step 7 — Daily-backup cron (first time only)

```bash
crontab -e
```

Add:

```cron
0 4 * * *  cd /opt/acta && /usr/bin/make backup-daily >> /home/muser/acta-backup.log 2>&1
```

(Cron strips the shell's `$PATH` to a minimal set, so absolute paths to
`make` are safer than relying on `make` being on the cron PATH —
check `which make`, usually `/usr/bin/make` on Debian.)

Verify:

```bash
crontab -l
# Smoke-fire it once now to confirm the cron environment will work:
cd /opt/acta && make backup-daily
ls -lh /var/backups/acta/daily/
```

A bundle in `daily/<ts>-<sha>/` with `db.dump` + `manifest.json` (no
media — daily is db-only by design) means the cron will fire cleanly
at the next 04:00 UTC.

## Rollback

If a deploy regresses production:

```bash
# 1. Pick the pre-deploy snapshot taken just before the bad deploy.
ls -lt /var/backups/acta/pre-deploy/ | head

# 2. Restore. Stops web + qcluster, runs pg_restore --clean, replaces
#    the media volume (full bundles only), restarts services. Takes a
#    safety snapshot first under restore-safety/.
cd /opt/acta && make restore FROM=/var/backups/acta/pre-deploy/<ts>-<sha>

# 3. Point the running code at the matching SHA so schema and code line up.
git reset --hard <bundle_git_sha>
docker compose up -d --build
```

If the regression is *code-only* (no new migrations between bad and
good), you can skip the DB restore and just `git reset --hard` + rebuild.
Check first:

```bash
make show-pending-migrations
```

## Backups TODOs (not yet wired)

- **Off-site sync** — bundles live on the VM disk; if the VM dies
  they die with it. Wire an `rclone` or `rsync` job to push
  `/var/backups/acta/` to R2 / a second host. Tracked in
  memory `project_todo_offsite_backup` (to be opened).
- **Daily via django-q** — fold the daily cron into
  `setup_scheduled_jobs` so it shows up in
  `/admin/django_q/schedule/` like the other recurring jobs.
  Tracked in memory `project_todo_backup_pipeline_djangoq`.

## Release journal

| Date         | Tag    | Notes                                                                                         |
|--------------|--------|-----------------------------------------------------------------------------------------------|
| 2026-05-31   | v0.5.0 | First release after `0.4.0`; introduced the pre-deploy backup pipeline + `make restore`. The initial deploy failed in `npm run build:css` because `tailwind.prose.config.js` wasn't `COPY`-ed in the Dockerfile (existed in git, not in the build context). |
| 2026-05-31   | v0.5.1 | Hot-fix for the above. Added the missing `COPY` line. First deploy that auto-snapshotted via the new pipeline. |
