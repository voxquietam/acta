from django.urls import path

from .views import create_api_token, invite_accept, revoke_api_token, set_language, user_settings

app_name = "accounts"

urlpatterns = [
    path("set-language/", set_language, name="set_language"),
    path("settings/", user_settings, name="settings"),
    path("settings/api-tokens/", create_api_token, name="create_api_token"),
    path("settings/api-tokens/<int:token_id>/revoke/", revoke_api_token, name="revoke_api_token"),
    path("invite/<str:token>/", invite_accept, name="invite_accept"),
]
