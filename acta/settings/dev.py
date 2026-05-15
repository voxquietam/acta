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

# Force the template engine to read source from disk on every request.
# By default Django 5.x's app_dirs + filesystem stack ends up wrapped in
# ``cached.Loader`` (compiled templates pinned in memory for the process
# lifetime), which forces a container restart after every template edit.
# Listing the loaders explicitly skips that wrapper, so .html edits show
# up on the next request without restarting uvicorn. Django forbids
# ``APP_DIRS=True`` when ``loaders`` is given — ``app_directories.Loader``
# in the list does exactly what ``APP_DIRS=True`` would have done.
TEMPLATES[0]["APP_DIRS"] = False  # noqa: F405
TEMPLATES[0]["OPTIONS"]["loaders"] = [  # noqa: F405
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]
