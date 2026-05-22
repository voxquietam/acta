# ADR 0025: File Storage & Attachments

**Status:** accepted
**Date:** 2026-05-22

## Context

[0006](0006-mvp-scope.md) put "File attachments" out of MVP. We are now
pulling them in (amended in 0006), because the daily workflow needs:

- **Task attachments** — screenshots and documents on a task, like the
  Links panel in the rail.
- **Comment attachments** — files on a comment (comments are polymorphic,
  see [0022](0022-polymorphic-comments.md)).
- **Inline editor images** — paste/drag images into the TipTap editor used
  by *both* task descriptions and project descriptions
  (`project.description` is Markdown via `render_markdown`).
- **User avatars** — replace the username-hash colour circle with an
  uploaded image; see also the settings/account work.

Deployment is a **single VM** (Ivano-Frankivsk, ~20 users, ≈60 GB disk
shared between Postgres, Docker and now media) with a **slow uplink**.
Production runs Uvicorn (ASGI, see [0015](0015-real-time.md)); the reverse
proxy lives in external infra shared with `ksu24.back`, *not* in this
repo's compose. We are explicitly **not** using Cloudflare R2 / object
storage yet (see the `file-storage-r2` planning note).

## Decision

### Backend now: Django `FileSystemStorage`, swappable by config

Files live in a folder under `MEDIA_ROOT` on the VM, written through
Django's `FileField` / `ImageField`. The storage backend is selected via
`STORAGES["default"]["BACKEND"]` so moving to MinIO/R2 later is a settings
swap plus a one-time `rclone copy` of the media folder — no model or code
rewrite. We chose this over MinIO/S3 because:

- Auth-gated serving (below) means we don't need S3 presigned URLs, and
  presigned URLs would actually *weaken* privacy (a signed link is
  forwardable for its TTL).
- MinIO is another stateful service storing its objects in a VM folder
  anyway — same disk, more moving parts, no win at this scale.
- `FileField` keeps the migration path open without paying S3's config
  and dependency cost today.

S3/MinIO earns its place only when one of these lands: attachment volume
makes app-served downloads contend with SSE/request workers beyond what
`X-Accel-Redirect` (below) fixes; a second app instance needs a shared
network filesystem; the VM disk tightens (then go straight to R2, free
10 GB); or we need versioning/lifecycle/presigned for an integration.

### Access control: auth-gated Django view, never public

Media is **not** served publicly (no `/media/` in Caddy/whitenoise, no
public bucket). Every download goes through a Django view that checks
workspace membership for the requesting user, then returns the file.
Filenames are stored as opaque UUIDs, but security does not rest on
unguessability — the membership check is the gate. When we later move to
R2/MinIO, this view issues short-TTL signed URLs instead of streaming.

### Serving: `FileResponse` now; `X-Accel-Redirect` via an nginx sidecar as the path

Streaming bytes through an ASGI worker (`FileResponse`) is fine for small
images and docs but ties up a worker for the whole transfer of a large
file — workers we also need for SSE and normal requests. The optimization
is to let a **file-serving proxy** stream the file after Django has
authorized it: Django returns an empty response carrying an
`X-Accel-Redirect` (nginx) header pointing at an *internal* location; the
proxy serves the bytes and frees the worker. This keeps full privacy
(Django still runs the membership check) without presigned URLs.

The catch is the prod topology. The edge proxy is **Traefik**
(admin-managed, `actaspace.com` → VM `:80` → uvicorn `:8000`), and Traefik
is a pure L7 router — it cannot serve a file from disk, so it has **no**
`X-Accel-Redirect` / `X-Sendfile` support. `X-Accel-Redirect` is
nginx-only; `X-Sendfile` is Apache. So enabling the offload is **not a
config flip** — it requires putting a file server we control between
Traefik and uvicorn:

- Add an **nginx sidecar to Acta's own compose stack**: Traefik → nginx →
  uvicorn. nginx owns an `internal` `location /media-internal/` reading
  the shared media volume; everything else proxies to uvicorn.
- The download view returns `X-Accel-Redirect: /media-internal/<path>`;
  nginx streams the bytes, the worker is freed.

Until that sidecar exists we **default to `FileResponse`**, which is
correct on the current Traefik-only stack and in dev. The code is wired
through a single `sendfile()` helper (e.g. `django-sendfile2`) with a
backend chosen by setting (`simple` now, `nginx` once the sidecar lands),
so the view never changes — only the deployment topology and one setting.

Note this offload saves *workers*, not user-facing download speed: on the
slow Ivano-Frankivsk uplink the byte transfer to the user is the
bottleneck regardless of who streams it, and only a CDN/edge near users
(R2) addresses that. So the sidecar earns its keep only if app-served
downloads start contending with SSE/request workers — not before.

### Image normalization on upload

User-supplied images are re-encoded server-side so a macOS screenshot
that weighs like a DSLR photo doesn't sit at full size on a tight disk and
a slow uplink. On upload, for image content types (via Pillow):

- Downscale to a max bound on the long edge (originals beyond it are
  resized; smaller ones are left alone).
- Re-encode at a sensible quality (JPEG/WebP) to shrink byte size.
- Strip EXIF metadata (privacy + size; also fixes orientation by applying
  it before stripping).

Avatars get their own smaller bound and a square crop. Exact dimensions
and quality live in settings, not hard-coded. Non-image files (pdf, txt,
md, docx, …) are stored as-is.

### Storage layout & validation

- Path: `attachments/<workspace_id>/<owner_type>/<owner_id>/<uuid>.<ext>`;
  avatars under `avatars/<user_id>/<uuid>.<ext>`. Scoping by workspace
  keeps the membership check and any future per-workspace quota simple.
- Validate on upload: a **per-category** size cap, a MIME/extension
  whitelist, filename sanitised, content type **sniffed not trusted**
  (the browser-supplied type and extension are advisory).
- Size caps live in a settings dict keyed by category, not a single
  constant, because images don't need the headroom documents do — and
  images are re-encoded anyway so their cap is just a sanity guard on the
  raw upload, not the stored size:

  | Category        | Raw-upload cap | Stored |
  |-----------------|----------------|--------|
  | image           | 10 MB          | re-encoded, typically ≪ cap |
  | document        | 25 MB          | as-is  |
  | archive (zip)   | 25 MB          | as-is  |
  | avatar          | 8 MB           | cropped + resized |

- Whitelist: images (`png jpg jpeg gif webp svg`), documents
  (`pdf txt md csv docx xlsx pptx`), archive (`zip`). No video/audio for
  now.
- Caps and whitelist are **policy in settings, never a DB constraint** —
  so they change with an edit + restart (env-overridable), no migration,
  and already-stored files are unaffected. Only integrity (FK + the
  "exactly one owner" check) lives in the database.

### Attachment model: explicit nullable FKs + a check constraint

A single `Attachment` model carries `workspace` (denormalized for
access-control filtering and path scoping), `uploader`, `file`,
`original_name`, `size`, `content_type`, `kind` (`file` / `inline_image`),
`created_at`, plus the owner. The owner is **explicit nullable FKs** —
`task`, `comment`, `project` — guarded by a DB `CheckConstraint` that
exactly one is set, **not** a `GenericForeignKey`.

This deliberately matches the codebase's established pattern for
polymorphic ownership: both `apps.comments.Comment` and
`apps.reactions.Reaction` use nullable FKs + a check constraint and
explicitly avoid content types (see [0022](0022-polymorphic-comments.md)).
For attachments the owner set is small and bounded (task / comment /
project, maybe project-update later), so a generic relation's open-ended
flexibility goes unused, while explicit FKs buy what we actually want:

- **DB referential integrity + cascade.** `on_delete=CASCADE` removes an
  owner's attachment rows automatically; a `post_delete` signal removes
  the file blob — no dangling rows or orphaned files on the tight VM disk.
  A `GenericForeignKey` gives none of this without extra `GenericRelation`
  + signal wiring.
- **No N+1 by construction.** `owner.attachments` reverse accessor +
  `prefetch_related` work directly, honoring the project's hard no-N+1
  rule without the per-row `content_type` resolution a GFK invites.
- **One mental model.** Third model in line with Comment and Reaction;
  same migrations, admin, serializer shape.

The cost — a migration to add a nullable FK + extend the check constraint
when a new owner type appears — is rare and cheap, and accepted.

The `project` FK exists to own **inline editor images** in project
descriptions (task-description inline images hang off `task`); panel
attachments use `kind=file`, embedded editor images use
`kind=inline_image`.

## Why

- Local `FileSystemStorage` matches the single-VM reality and stays
  reversible via `FileField` — we get attachments now without standing up
  and operating object storage we don't yet need.
- Auth-gated serving is the privacy posture Vox asked for; presigned URLs
  would trade that away, so we keep the proxy-offload benefit instead via
  `X-Accel-Redirect` rather than signed links.
- Defaulting to `FileResponse` means the feature is correct on the current
  Traefik-only stack today; the `X-Accel-Redirect` fast path stays
  reachable behind the `sendfile()` abstraction, gated on adding an nginx
  sidecar — a deployment change, not a view rewrite.
- Image normalization is the cheapest guard against the disk and uplink
  being eaten by oversized screenshots.

## Consequences

- New dependencies: `Pillow` (image processing) and a sendfile helper
  (`django-sendfile2` or equivalent). `boto3` / `django-storages` are
  deliberately *not* added yet.
- `MEDIA_ROOT` + an `acta-media` Docker volume + a media backup procedure
  separate from the Postgres dump — operational note for deployment docs.
- Media is per-VM until/unless we move it off; a VM rebuild must restore
  the media volume alongside the database. Two known offload paths when
  the 60 GB VM disk tightens: the admin's **NAS** mounted into the media
  volume (solves disk, not user download speed — still via the slow
  uplink), or **R2** (solves both via a CDN edge near users). Picking
  either is a future decision; ask the admin whether the NAS exposes an
  S3-compatible API first.
- A storage migration to R2/MinIO later touches settings + a data copy
  only, by design.
- Originals are not preserved for images (we re-encode in place). If a
  use case ever needs the untouched original, that's a new decision.
