"""Root URL configuration for Acta."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    # API and page routes will be wired in apps as they are implemented.
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
