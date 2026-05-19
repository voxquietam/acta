"""
ASGI entrypoint for Acta.

Production deployment uses this with Uvicorn. SSE real-time updates require
ASGI (see docs/decisions/0015-real-time.md); WSGI Gunicorn is not supported.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acta.settings.prod")

application = get_asgi_application()
