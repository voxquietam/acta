from django.urls import path

from .views import set_language, user_settings

app_name = "accounts"

urlpatterns = [
    path("set-language/", set_language, name="set_language"),
    path("settings/", user_settings, name="settings"),
]
