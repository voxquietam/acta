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

"Sign in with Google" is **not** configured via env vars — the Client ID
and Secret are stored in a `SocialApp` row in Django admin. See "One-time"
§6 below.

### One-time per environment

1. `migrate`
2. `compilemessages` (builds the `uk` `.mo`)
3. `collectstatic`
4. `createsuperuser`
5. `setup_scheduled_jobs` (seeds the recurring-job schedules — see below)
6. Create the **Telegram message templates** in `/admin/` (see "One-time" §5)
7. Set **`ACTA_PUBLIC_BASE_URL`** in the prod env (e.g. `https://actaspace.com`).
   `make deploy` then registers the Telegram webhook automatically
   (`telegram_set_webhook`, run on every deploy — see below); no manual step.
   To register by hand: `telegram_set_webhook --base-url https://actaspace.com`.
8. Set up **Google OAuth** — create the `SocialApp` in `/admin/` (see "One-time" §6)

### Recurring jobs

Run by the **`qcluster`** service (django-q2) — it comes up with the stack;
no crontab. Three daily jobs (auto-archive, attachment GC, cycle
notifications), seeded by `setup_scheduled_jobs` and editable in `/admin/`
→ Django Q. **Without the `qcluster` service running, done tasks never
auto-archive, orphan files pile up, and cycle start/ending notifications
never fire.** See "Recurring jobs (admin-managed scheduler)" below.

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

### 6. Set up Google OAuth ("Sign in with Google")

The "Continue with Google" button only appears once a Google `SocialApp`
row exists, so this step is what turns the feature on. The OAuth
credentials are **DB-held in Django admin**, not in env/settings — there
are no `GOOGLE_OAUTH_*` env vars to set despite the historical
placeholders in `.env.example`.

There are two halves: the Google Cloud side (once per Google project) and
the Acta admin side (once per Acta environment).

#### A. Google Cloud — create the OAuth client

Billing is **not** required for OAuth — skip / dismiss any "Start free
trial / add a card" prompt. OAuth clients are free.

1. <https://console.cloud.google.com/> → create a project (e.g. `Acta`).
   Don't go through the `/freetrial/` billing flow.
2. **APIs & Services → OAuth consent screen** (newer console labels this
   **Google Auth Platform**). Configure it once:
   - **App name:** `Acta`, support + developer contact emails.
   - **Audience / User type:** **External**.
3. **Audience** tab → note the **Publishing status**:
   - **Testing** (default): only emails listed under **Test users** can
     sign in. Add every Google account that needs access via
     **+ Add users**, or click **Publish app** to open it to anyone.
     A testing/unverified app shows a Google interstitial — the user
     clicks *Advanced → Go to Acta* to proceed.
   - **In production**: any Google account is allowed.
4. **Clients → Create OAuth client**:
   - **Application type:** Web application.
   - **Authorized redirect URIs** → add one per environment:
     - `https://actaspace.com/accounts/google/login/callback/`
     - `http://localhost:8001/accounts/google/login/callback/` (dev)
     - The trailing slash and the exact `/accounts/google/login/callback/`
       path matter — that's the route allauth exposes.
   - **Authorized JavaScript origins:** leave empty. We use the
     server-side redirect flow, not the browser JS flow.
5. After **Create**, open the client under **Clients** to read its
   **Client ID** and **Client secret** (the "Download JSON" button is
   optional and sometimes a no-op — the two values are all we need).

#### B. Acta admin — create the SocialApp

6. Admin → **Sites** (`django.contrib.sites`): set the `SITE_ID = 1` row's
   domain to the real host (`actaspace.com` on prod). allauth filters
   `SocialApp`s by the current Site, so this must match.
7. Admin → **Social applications → Add**:
   - **Provider:** Google
   - **Name:** `Google` (any label)
   - **Client id:** from step 5
   - **Secret key:** from step 5 (leave **Key** empty)
   - **Sites:** move the `SITE_ID = 1` site into *Chosen sites* — without
     this the button stays hidden.
8. Save. Reload `/accounts/login/` — the "Continue with Google" button
   now renders.

Behaviour (see ADR 0002 update): an existing account logs in when its
verified Google email matches; a brand-new account is created via Google
only when an active workspace invite for that exact email is in flight.

## Recurring jobs (admin-managed scheduler)

Recurring maintenance runs through **django-q2**, not host crontab. A
single **`qcluster`** process (its own compose service) polls the database
— which doubles as the broker, so there's no Redis — and runs each
schedule. Schedules are **editable in the admin** (`/admin/` → *Django Q*
→ *Scheduled tasks*): change the time, disable a job, or run it now,
without SSH.

Seed the three default daily schedules once per environment:

```bash
docker compose exec -T web python manage.py setup_scheduled_jobs
```

It's idempotent and only creates *missing* schedules, so re-running it on
each deploy never overwrites a time you've since edited in the admin. The
`qcluster` service comes up with the stack (`docker compose up -d`); on a
fresh deploy it restarts until `web` has applied migrations.

Each job is a callable in `apps/common/scheduled.py` wrapping a management
command — you can still run any of them by hand (e.g. with `--dry-run`).

### archive stale done tasks (~03:30 daily)

Archives every `done` task whose `updated_at` is older than the
per-workspace `Workspace.auto_archive_done_after_days` threshold (default
30 days; NULL per-workspace disables it). Idempotent. Emits
`system.task.archived` activity events with `actor=None`. The first run
processes the whole backlog of stale done rows — `--dry-run` first to see
the batch size; `--workspace <slug>` scopes to one workspace.

### gc orphan attachments (~04:00 daily)

Inline editor images are uploaded the instant they're pasted — before the
text is saved — so they linger if the user removes the image or abandons
the modal. This deletes inline images older than a grace window (default
24h) whose serve URL no longer appears in any description/comment body
(the `post_delete` signal drops the blob too). File attachments
(`kind=file`) are never touched. Flags: `--dry-run`, `--older-than-hours N`.

### notify cycle events (~06:00 daily)

For every cadence-running workspace, materializes the rolling cycle windows
(same `ensure_cycles` the web uses — so it also auto-rolls) and fans out
"Cycle N started" / "Cycle N ends tomorrow — M open" notifications.
Idempotent (the `Cycle` row stamps `start_notified_at` /
`end_notified_at`). Flags: `--dry-run`, `--workspace <slug>`.

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
