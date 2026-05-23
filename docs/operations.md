# Operations runbook

Things to do when deploying Acta to a real server (staging / prod) —
not needed for local dev. Living document; append as new ops surface
lands.

The deploy target shape is set by ADR 0015 ("real-time"): ASGI app
under **uvicorn** behind **Caddy** (or nginx). Postgres is a managed
service or a sibling container. No Gunicorn — sync workers break SSE.

## Deploy checklist (TL;DR)

Everything needed for a fresh prod, in order. Details in the sections
below.

### Environment variables — required (the app breaks without these)

| Variable | Notes |
|----------|-------|
| `DJANGO_SETTINGS_MODULE=acta.settings.prod` | else it runs the **dev** config |
| `DJANGO_SECRET_KEY` | a real secret (not the dev placeholder) — prod refuses to boot without it |
| `DJANGO_ALLOWED_HOSTS=actaspace.com` | else every request is rejected |
| `DJANGO_CSRF_TRUSTED_ORIGINS=https://actaspace.com` | else POST/forms over HTTPS fail the CSRF check |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` (+ `POSTGRES_PORT`) | database |
| `DJANGO_MEDIA_ROOT=/…/media` on a **persistent** volume | else avatars + attachments vanish on container rebuild |

### Environment variables — feature toggles (the feature silently won't work)

| Variable | Enables |
|----------|---------|
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_USE_TLS` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `DEFAULT_FROM_EMAIL` | sending **workspace invites** by email |
| `ACTA_PUBLIC_BASE_URL=https://actaspace.com` | absolute links in invite emails + task links in Telegram |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` / `TELEGRAM_WEBHOOK_SECRET` | the Telegram notification bot |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | "Sign in with Google" |

### One-time per environment

1. `migrate`
2. `compilemessages` (builds the `uk` `.mo`)
3. `collectstatic`
4. `createsuperuser`
5. Create the **Telegram message templates** in `/admin/` (see §5 below)
6. `telegram_set_webhook --base-url https://actaspace.com` (prod uses a webhook, not polling)
7. Create the Google **SocialApp** in `/admin/` (once OAuth is wired)

### Recurring jobs

Three daily jobs (auto-archive, attachment GC, cycle notifications). See
"Recurring jobs" below for the cron lines — or run them from an in-app
scheduler. **Without these, done tasks never auto-archive, orphan files
pile up, and cycle start/ending notifications never fire.**

## One-time per environment

These run once when the environment is first provisioned.

### 1. Apply migrations

Every deploy that ships a new migration must run before the new code
serves traffic:

```bash
docker compose exec web python manage.py migrate
```

Migrations are committed with a `!` marker in the subject so they're
easy to spot in `git log` (see `CLAUDE.md` → commit style).

### 2. Compile translations (`.mo` files)

Per ADR 0018, `.po` files are committed but `.mo` files are built at
deploy time:

```bash
docker compose exec web python manage.py compilemessages
```

Re-run after every change to `locale/*/LC_MESSAGES/django.po`.

### 3. Collect static files

If the prod image serves static assets via the app container (or via
the reverse proxy reading from a shared volume):

```bash
docker compose exec web python manage.py collectstatic --noinput
```

### 4. Create the first superuser

```bash
docker compose exec web python manage.py createsuperuser
```

Subsequent users sign in via Google OAuth (ADR 0002) and get added to
workspaces from the admin or via the workspace owner flow.

### 5. Create the Telegram message templates (admin)

The per-kind Telegram DM wording lives in **`TelegramMessageTemplate`**
rows (Django admin → *Telegram message templates*), **not** in code — they
are data, so a fresh environment starts with the built-in English defaults
until you create them. Recreate the agreed templates after deploy:

| Kind | Body |
|------|------|
| Mention | `💬 <b>{actor}</b> mentioned you\n{task} — {title}\n{quote}` |
| Assigned | `📌 <b>{actor}</b> assigned you a task\n{task} — {title}\n{quote}\n{meta}` |
| Comment | `🗨️ <b>{actor}</b> commented\n{task} — {title}\n{quote}` |
| Status change | `🔄 <b>{actor}</b> moved {status_from} → {status_to}\n{task} — {title}` |
| Priority change | `🎚️ <b>{actor}</b> changed priority {priority_from} → {priority_to}\n{task} — {title}` |
| Due soon | `⏰ <b>{actor}</b> changed the due date\n{task} — {title}\n{due_change}` |
| Project update | `📊 <b>{actor}</b> posted an update · {project}\n{health}\n{quote}` |
| Cycle | `🔁 <b>{cycle}</b>\n{preview}` |

(Use real newlines in the admin textarea, not the literal `\n`.) No template
for *Announcement* is needed — it falls back to a sensible `📣 {title}`
default. Without any rows the bot still works, just in English defaults.

## Recurring jobs (cron / scheduler)

### Daily: auto-archive stale done tasks

Archives every `done` task whose `updated_at` is older than the
per-workspace `Workspace.auto_archive_done_after_days` threshold.
Defaults to 30 days; can be set to NULL per-workspace to disable.

Suggested cron (host crontab on the deploy node):

```cron
# At 03:30 every day — quiet hours for the team.
30 3 * * * cd /opt/acta && docker compose exec -T web python manage.py archive_stale_done_tasks >> /var/log/acta/archive.log 2>&1
```

Or as a separate systemd timer / k8s CronJob if that fits the deploy
shape better. The command is idempotent — running it twice the same
hour is a no-op once the first run has stamped the rows.

Flags:
- `--dry-run` — count rows that *would* be archived without writing.
  Run this manually before the very first scheduled run to see the
  initial batch size.
- `--workspace <slug>` — scope to a single workspace (handy for
  one-off cleanups or per-tenant rollouts).

The job emits `system.task.archived` activity events with
`actor=None`. The first scheduled run after the field ships will
process the entire backlog of stale done rows — review the dry-run
output first if the workspace has been around for a while.

### Daily: GC orphaned inline images

Inline editor images (pasted/dropped into a description or comment) are
uploaded the instant they're pasted — before the text is saved — so they
linger if the user removes the image or abandons the create-task modal.
This deletes inline images, older than a grace window (default 24h), whose
serve URL no longer appears in any task/project description or comment
body. The `post_delete` signal removes the file blob too. File attachments
(`kind=file`) are never touched.

```cron
# At 04:00 every day.
0 4 * * * cd /opt/acta && docker compose exec -T web python manage.py gc_orphan_attachments >> /var/log/acta/gc-attachments.log 2>&1
```

Flags:
- `--dry-run` — report the count that *would* be deleted without deleting.
- `--older-than-hours N` — grace window (default 24); raise it if users
  routinely take longer than a day between pasting and saving.

### Daily: cycle start / ending-soon notifications

For every workspace running cadence (cycles), this materializes the
rolling windows (same `ensure_cycles` the web pages call — so it also
performs auto roll-over) and fans out inbox notifications: once when a
cycle becomes active ("Cycle N started") and once when an active cycle is
within a day of its end ("Cycle N ends tomorrow — M tasks open").
Idempotent — re-runs the same day send nothing (the `Cycle` row stamps
`start_notified_at` / `end_notified_at`). Cadence-off workspaces are
skipped.

```cron
# At 06:00 every day (before the team starts — cycle starts/ends are dated).
0 6 * * * cd /opt/acta && docker compose exec -T web python manage.py notify_cycle_events >> /var/log/acta/cycle-notify.log 2>&1
```

Flags:
- `--dry-run` — report what would be sent without writing notifications.
- `--workspace <slug>` — limit to one workspace.

> Note: these recurring jobs are slated to move off raw crontab into an
> admin-manageable scheduler (so schedules are editable in Django admin
> without SSH). The management commands stay; only the trigger changes.

### Future hooks

When these subsystems land they will need their own scheduled jobs —
listed here so the runbook covers the full picture:

- **Project Updates digest** (ADR 0009) — weekly summary email.
- **Inactive-workspace cleanup** — none planned yet.
- **Backups** — Postgres `pg_dump` to off-host storage. Schedule and
  retention TBD.

## Per-release checklist

For each deploy:

1. `git pull` / image rebuild on the host.
2. `make build-front` (or `make build-js && make build-css`) — rebuild
   the description-editor bundle and the Tailwind stylesheet from
   their sources. The committed artefacts (`static/js/*.bundle.js` and
   `static/css/main.bundle.css`) are required by the templates; skip
   this and the page renders unstyled.
3. `docker compose exec web python manage.py migrate` (no-op if no
   new migrations).
4. `docker compose exec web python manage.py compilemessages` (if any
   `.po` changed).
5. `docker compose exec web python manage.py collectstatic --noinput`
   (if static files changed).
6. Restart the `web` container so uvicorn picks up the new code.
7. Tail logs for the first minute to confirm SSE streams reconnect
   cleanly and no migration deadlock occurred.

## Health checks

- **App**: `GET /healthz/` (TBD — not implemented yet).
- **SSE**: `curl -N https://<host>/sse/workspace/<id>/` should keep
  the connection open. ADR 0015 forbids Gunicorn precisely because
  sync workers fail this check.
- **DB**: standard Postgres health (managed-service dashboard or
  `pg_isready` on a sibling container).
