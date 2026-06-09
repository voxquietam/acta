"""PWA endpoints: the web app manifest and the service worker.

Both must be reachable without authentication (the login page links the
manifest and registers the worker) and carry the right content types +
headers for the browser to treat the app as installable. See ADR 0029.
"""

import json

from django.urls import reverse

import pytest


@pytest.mark.django_db
class TestPwaManifest:
    """``/manifest.webmanifest`` — public, valid manifest JSON."""

    def test_manifest_is_public_and_well_formed(self, client):
        """Anonymous GET returns a valid manifest with the install-critical keys."""
        resp = client.get(reverse("web:pwa_manifest"))
        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/manifest+json"
        data = json.loads(resp.content)
        assert data["name"]
        assert data["start_url"] == "/"
        assert data["display"] == "standalone"
        assert data["theme_color"] and data["background_color"]

    def test_manifest_declares_192_512_and_maskable_icons(self, client):
        """Installability needs 192 + 512 ``any`` icons plus a maskable one."""
        resp = client.get(reverse("web:pwa_manifest"))
        icons = json.loads(resp.content)["icons"]
        sizes = {i["sizes"] for i in icons}
        assert {"192x192", "512x512"} <= sizes
        purposes = {i["purpose"] for i in icons}
        assert "any" in purposes
        assert "maskable" in purposes
        # Icon URLs resolve through ``{% static %}`` (hash-aware in prod).
        assert all(i["src"].startswith("/static/") for i in icons)


@pytest.mark.django_db
class TestServiceWorker:
    """``/sw.js`` — public, root-scope, uncached, network-first worker."""

    def test_worker_is_public_with_root_scope_headers(self, client):
        """Anonymous GET returns JS with the root-scope + no-cache headers."""
        resp = client.get(reverse("web:service_worker"))
        assert resp.status_code == 200
        assert resp["Content-Type"] == "application/javascript"
        assert resp["Service-Worker-Allowed"] == "/"
        assert "no-cache" in resp["Cache-Control"]

    def test_worker_is_served_at_root_path(self):
        """Scope == served path's dir, so the worker must live at the root."""
        assert reverse("web:service_worker") == "/sw.js"

    def test_worker_has_a_fetch_handler(self, client):
        """A ``fetch`` listener is what makes the browser offer Install."""
        body = client.get(reverse("web:service_worker")).content.decode()
        assert 'addEventListener("fetch"' in body
        # Real-time + API paths must be passed straight through (not cached).
        assert "/events/" in body
        assert "/api/" in body
