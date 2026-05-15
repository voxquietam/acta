"""Root URL configuration for Acta."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

api_v1_patterns = [
    path("", include("apps.workspaces.urls")),
    path("", include("apps.projects.urls")),
    path("", include("apps.labels.urls")),
    path("", include("apps.tasks.urls")),
    path("", include("apps.comments.urls")),
    path("", include("apps.activity.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/v1/", include((api_v1_patterns, "api_v1"))),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
