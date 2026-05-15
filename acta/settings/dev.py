"""Development settings."""

import os

from .base import *  # noqa: F401,F403

DEBUG = True

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "acta"),
        "USER": os.environ.get("POSTGRES_USER", "acta"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "acta"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Force-disable browser caching in dev so template / static edits show up
# on the first reload. Never use this in production.
MIDDLEWARE = MIDDLEWARE + ["apps.web.middleware.NoBrowserCacheMiddleware"]  # noqa: F405
