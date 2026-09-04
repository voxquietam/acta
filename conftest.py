"""Project-wide pytest fixtures.

Currently one job: make the default ``client`` follow the canonical-URL
redirects introduced in ADR 0031.
"""

from django.test import Client

import pytest


class FollowingClient(Client):
    """Test client whose ``get()`` follows redirects unless told otherwise.

    Acta's URLs gained a workspace segment, and the legacy workspace-less
    paths now answer ``301`` towards the canonical form. Test suites are
    full of ``client.get(reverse("web:all_tasks"))`` — every one of those
    asks "is this page reachable and what does it render", not "which
    status code does the transport use to get there". Following by default
    keeps them expressing that, instead of adding ``follow=True`` to a
    hundred and thirty call sites.

    Tests that care about the redirect itself — that it happens at all,
    where it points, that POST endpoints are exempt — instantiate
    ``django.test.Client`` directly, so this default cannot hide a
    regression there. See ``apps/web/tests/test_workspace_scoped_urls.py``.
    """

    def get(self, path, *args, **kwargs):
        """Follow only the canonical-URL redirect, nothing else.

        Scoped to ``301`` on purpose. That is what the legacy-path
        redirect answers, while the login gate and every other
        ``LoginRequiredMixin`` bounce use ``302`` — following those too
        would quietly break the suites that assert an anonymous visitor
        gets redirected.
        """
        if "follow" in kwargs:
            return super().get(path, *args, **kwargs)
        response = super().get(path, *args, **kwargs)
        if response.status_code == 301:
            return super().get(path, *args, follow=True, **kwargs)
        return response


@pytest.fixture
def client():
    """Override pytest-django's client with the redirect-following one."""
    return FollowingClient()
