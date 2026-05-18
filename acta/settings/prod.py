"""Production settings."""

import os

from .base import *  # noqa: F401,F403

DEBUG = False

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()]

# Origins trusted for unsafe (POST/PUT/PATCH/DELETE) requests behind the
# Traefik edge proxy. Required because Django 4+ checks ``Origin`` /
# ``Referer`` against this list when the request comes over HTTPS through
# a reverse proxy. Configure via ``DJANGO_CSRF_TRUSTED_ORIGINS`` as a
# comma-separated list of full scheme+host (e.g. ``https://actaspace.com``).
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ["POSTGRES_HOST"],
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

# Security hardening — to be reviewed before first prod deploy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# WhiteNoise — production only. Sits right after SecurityMiddleware so
# static-file responses bypass the rest of the chain. In dev runserver
# handles static itself, so this layer would only add scan-on-boot
# overhead without value.
MIDDLEWARE.insert(  # noqa: F405 — MIDDLEWARE comes from ``base.py``
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,  # noqa: F405
    "whitenoise.middleware.WhiteNoiseMiddleware",
)

# Compressed manifest storage:
#   - Gzips at ``collectstatic`` time → responses ship pre-compressed.
#   - Hashes filenames so we can serve them ``immutable`` with
#     ``Cache-Control: max-age=31536000``.
# Skipped in dev because ``runserver`` doesn't run ``collectstatic`` on
# every reload, so a manifest entry for every referenced ``{% static %}``
# would be missing and pages would 500.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
