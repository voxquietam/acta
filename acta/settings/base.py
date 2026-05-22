"""
Base settings shared between dev and prod.

Environment-specific overrides live in dev.py and prod.py.
"""

import os
from pathlib import Path

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")

DEBUG = False

ALLOWED_HOSTS: list[str] = []

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ROOT_URLCONF = "acta.urls"

ASGI_APPLICATION = "acta.asgi.application"

# SSE storage — see ADR 0015. ``DjangoModelStorage`` persists events
# in the DB so they're visible across processes (manage.py shell,
# bulk-import scripts, etc.) and survive worker restarts. Without
# this django_eventstream defaults to in-memory-only pub/sub which
# is fine for in-process broadcasts but invisible from outside.
# Migrate to Redis backend if we ever run multiple Uvicorn workers.
EVENTSTREAM_STORAGE_CLASS = "django_eventstream.storage.DjangoModelStorage"

# SSE channel authorization — restricts ``workspace-<id>`` channels
# to that workspace's members. See ``apps.workspaces.sse``.
EVENTSTREAM_CHANNELMANAGER_CLASS = "apps.workspaces.sse.WorkspaceChannelManager"


# -----------------------------------------------------------------------------
# Applications
# -----------------------------------------------------------------------------

DJANGO_APPS = [
    # django-unfold replaces the look of django.contrib.admin and MUST be
    # listed before it so its templates take precedence.
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # ``naturaltime`` for relative timestamps (e.g. "25 days ago") in the
    # activity log / comment timeline; ships uk translations.
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "django_filters",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # SSE real-time updates — see ADR 0015. Requires ASGI runtime.
    "django_eventstream",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.workspaces",
    "apps.projects",
    "apps.tasks",
    "apps.labels",
    "apps.comments",
    "apps.activity",
    "apps.notifications",
    "apps.reactions",
    "apps.attachments",
    "apps.web",
    "apps.mcp",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# -----------------------------------------------------------------------------
# Middleware
# -----------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # i18n: LocaleMiddleware reads cookie / Accept-Language; the custom
    # UserLanguageMiddleware after it overrides with User.language if set.
    # Both must run after AuthenticationMiddleware. See ADR 0018.
    "django.middleware.locale.LocaleMiddleware",
    "apps.accounts.middleware.UserLanguageMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]


# -----------------------------------------------------------------------------
# Templates
# -----------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context.workspace_nav",
                "acta.context_processors.app_version",
            ],
        },
    },
]


# -----------------------------------------------------------------------------
# Database — populated per environment
# -----------------------------------------------------------------------------

DATABASES: dict = {}


# -----------------------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"

# Email — SMTP credentials supplied via env vars in prod (Gmail SMTP by
# default; swap for any provider by setting the four EMAIL_HOST_*
# variables). The dev settings module overrides to a console backend so
# local invite emails land in ``docker compose logs web`` instead of
# requiring SMTP credentials for every developer.
#
# Note the ``or`` pattern: ``os.environ.get("X", default)`` only honours
# the default when the var is *absent*, not when it's set to an empty
# string. Docker Compose's ``${EMAIL_PORT:-}`` pass-through passes ``""``
# when the .env doesn't define the var, so ``int("")`` would explode at
# import time. ``... or default`` collapses both cases into the same
# fallback.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND") or "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST") or "smtp.gmail.com"
EMAIL_PORT = int(os.environ.get("EMAIL_PORT") or "587")
EMAIL_USE_TLS = (os.environ.get("EMAIL_USE_TLS") or "true").lower() in {"1", "true", "yes"}
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER") or ""
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD") or ""
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or (EMAIL_HOST_USER or "no-reply@actaspace.com")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Public changelog link (sidebar version kicker). Override for forks.
CHANGELOG_URL = os.environ.get("ACTA_CHANGELOG_URL") or "https://github.com/voxquietam/acta/blob/master/CHANGELOG.md"

# allauth
ACCOUNT_LOGIN_METHODS = {"email", "username"}
# ``password1*`` is required — allauth 65 derives the LoginForm's
# password field from ``SIGNUP_FIELDS`` and silently drops it when
# ``password1`` isn't here. ``password2*`` keeps the confirm-on-signup.
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
# Signup is closed for v0.1.0 — admins create accounts via Django admin.
# See ``apps.accounts.adapters`` for the adapter implementation.
ACCOUNT_ADAPTER = "apps.accounts.adapters.NoSignupAccountAdapter"
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapters.NoSignupSocialAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
}


# -----------------------------------------------------------------------------
# DRF
# -----------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # ``ApiTokenAuthentication`` runs first so ``Authorization: Token
        # <secret>`` headers from programmatic clients (curl, scripts,
        # the planned MCP server) authenticate via the ``ApiToken``
        # model. SessionAuthentication remains the fallback for the
        # web UI (cookie-based) since it ignores Authorization headers
        # entirely.
        "apps.accounts.auth.ApiTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}


# -----------------------------------------------------------------------------
# Internationalization
# -----------------------------------------------------------------------------

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("en", _("English")),
    ("uk", _("Ukrainian")),
]
LOCALE_PATHS = [
    BASE_DIR / "locale",
]


# -----------------------------------------------------------------------------
# Static / media
# -----------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "static_collected"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Uploaded files — attachments, inline editor images, user avatars. Stored
# on the local filesystem under MEDIA_ROOT (a folder on the VM, mounted to
# the ``acta-media`` Docker volume in production so files survive container
# rebuilds). The storage backend is chosen via ``STORAGES["default"]``
# (FileSystemStorage now, S3-compatible later) — see ADR 0025.
#
# Media is NOT served publicly: there is no ``/media/`` route in dev or
# prod. Every file is streamed by the auth-gated download view in
# ``apps.attachments`` after a workspace-membership check, so dev and prod
# behave identically. MEDIA_URL is nominal (Django wants a value for
# ``FieldFile.url``) and is not routed.
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))
MEDIA_URL = "/media/"


# -----------------------------------------------------------------------------
# File attachments (see docs/decisions/0025-file-storage.md)
# -----------------------------------------------------------------------------

# Per-category raw-upload size caps, in bytes. Images are re-encoded on
# upload, so their cap guards the *raw* upload, not the stored size;
# documents and archives are stored as-is and need more headroom. This is
# policy, not a DB constraint — change it with an edit + restart, no
# migration, and already-stored files are unaffected.
ATTACHMENT_MAX_UPLOAD_BYTES = {
    "image": 5 * 1024 * 1024,
    "document": 15 * 1024 * 1024,
    "archive": 15 * 1024 * 1024,
    "avatar": 3 * 1024 * 1024,
}

# Allowed upload types, grouped by category. The category selects the size
# cap above and whether the file is image-normalized. Extensions and the
# browser-supplied content type are advisory; the upload path sniffs the
# real content type and rejects mismatches. Category keys are internal
# codes and are never translated.
ATTACHMENT_ALLOWED_TYPES = {
    "image": {
        "extensions": [
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "svg",
        ],
        "content_types": [
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "image/svg+xml",
        ],
    },
    "document": {
        "extensions": [
            "pdf",
            "txt",
            "md",
            "csv",
            "docx",
            "xlsx",
            "pptx",
        ],
        "content_types": [
            "application/pdf",
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ],
    },
    "archive": {
        "extensions": [
            "zip",
        ],
        "content_types": [
            "application/zip",
        ],
    },
}

# Image normalization bounds (Pillow). Uploaded raster images are
# downscaled so the long edge fits within the bound, re-encoded at
# ATTACHMENT_IMAGE_QUALITY, and stripped of EXIF (orientation applied
# first). SVG is vector and skips raster normalization. Avatars use the
# smaller square bound.
ATTACHMENT_IMAGE_MAX_EDGE = 2048
ATTACHMENT_AVATAR_MAX_EDGE = 512
ATTACHMENT_IMAGE_QUALITY = 85

# Serving offload backend for the auth-gated download view (ADR 0025):
#   "simple" — stream via Django's FileResponse. Correct on any proxy,
#              including the current Traefik-only prod stack and dev.
#   "nginx"  — return X-Accel-Redirect and let an nginx sidecar stream the
#              bytes (frees the ASGI worker). Requires that sidecar; the
#              admin's Traefik edge alone cannot do this.
# Switching backends never changes the view code — only this setting (and
# the deployment topology for "nginx").
ATTACHMENT_SENDFILE_BACKEND = os.environ.get("DJANGO_SENDFILE_BACKEND", "simple")
# Internal location prefix the nginx sidecar maps onto MEDIA_ROOT; only
# consulted when ATTACHMENT_SENDFILE_BACKEND == "nginx".
ATTACHMENT_SENDFILE_NGINX_LOCATION = "/media-internal/"


# -----------------------------------------------------------------------------
# django-unfold (admin theme)
# -----------------------------------------------------------------------------

UNFOLD = {
    "SITE_TITLE": "Acta",
    "SITE_HEADER": "Acta",
    "SITE_SUBHEADER": "task tracker · admin",
    "SITE_SYMBOL": "checklist",
    "SITE_URL": "/",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "SHOW_BACK_BUTTON": True,
    "COLORS": {
        "primary": {
            "50": "250 245 255",
            "100": "243 232 255",
            "200": "233 213 255",
            "300": "216 180 254",
            "400": "192 132 252",
            "500": "168 85 247",
            "600": "147 51 234",
            "700": "126 34 206",
            "800": "107 33 168",
            "900": "88 28 135",
            "950": "59 7 100",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {
                "title": _("Workspace"),
                "separator": True,
                "items": [
                    {
                        "title": _("Workspaces"),
                        "icon": "domain",
                        "link": reverse_lazy("admin:workspaces_workspace_changelist"),
                    },
                    {
                        "title": _("Members"),
                        "icon": "group",
                        "link": reverse_lazy("admin:workspaces_workspacemember_changelist"),
                    },
                    {
                        "title": _("Invites"),
                        "icon": "mail",
                        "link": reverse_lazy("admin:workspaces_workspaceinvite_changelist"),
                    },
                ],
            },
            {
                "title": _("Projects"),
                "separator": True,
                "items": [
                    {
                        "title": _("Projects"),
                        "icon": "folder",
                        "link": reverse_lazy("admin:projects_project_changelist"),
                    },
                    {
                        "title": _("Project updates"),
                        "icon": "campaign",
                        "link": reverse_lazy("admin:projects_projectupdate_changelist"),
                    },
                ],
            },
            {
                "title": _("Tasks"),
                "separator": True,
                "items": [
                    {
                        "title": _("Tasks"),
                        "icon": "task",
                        "link": reverse_lazy("admin:tasks_task_changelist"),
                    },
                    {
                        "title": _("Comments"),
                        "icon": "chat",
                        "link": reverse_lazy("admin:comments_comment_changelist"),
                    },
                    {
                        "title": _("Labels"),
                        "icon": "sell",
                        "link": reverse_lazy("admin:labels_label_changelist"),
                    },
                    {
                        "title": _("Label groups"),
                        "icon": "category",
                        "link": reverse_lazy("admin:labels_labelgroup_changelist"),
                    },
                ],
            },
            {
                "title": _("Activity"),
                "separator": True,
                "items": [
                    {
                        "title": _("Activity log"),
                        "icon": "timeline",
                        "link": reverse_lazy("admin:activity_activitylog_changelist"),
                    },
                ],
            },
            {
                "title": _("System"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Users"),
                        "icon": "person",
                        "link": reverse_lazy("admin:accounts_user_changelist"),
                    },
                    {
                        "title": _("Groups"),
                        "icon": "shield_person",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": _("Sites"),
                        "icon": "public",
                        "link": reverse_lazy("admin:sites_site_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": _("Email addresses"),
                        "icon": "alternate_email",
                        "link": reverse_lazy("admin:account_emailaddress_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": _("Social apps"),
                        "icon": "key",
                        "link": reverse_lazy("admin:socialaccount_socialapp_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": _("Social accounts"),
                        "icon": "account_circle",
                        "link": reverse_lazy("admin:socialaccount_socialaccount_changelist"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                ],
            },
        ],
    },
}
