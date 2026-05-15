"""Root URL configuration for Acta."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    # API and page routes will be wired in apps as they are implemented.
]
