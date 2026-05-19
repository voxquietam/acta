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
