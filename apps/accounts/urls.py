from django.urls import path

from .views import (
    change_password,
    create_api_token,
    delete_api_token,
    invite_accept,
    remove_avatar,
    revoke_api_token,
    serve_avatar,
    set_language,
    upload_avatar,
    user_settings,
)

app_name = "accounts"

urlpatterns = [
    path("set-language/", set_language, name="set_language"),
    path("settings/", user_settings, name="settings"),
    path("settings/password/", change_password, name="change_password"),
    path("settings/avatar/", upload_avatar, name="upload_avatar"),
    path("settings/avatar/remove/", remove_avatar, name="remove_avatar"),
    path("settings/api-tokens/", create_api_token, name="create_api_token"),
    path("settings/api-tokens/<int:token_id>/revoke/", revoke_api_token, name="revoke_api_token"),
    path("settings/api-tokens/<int:token_id>/delete/", delete_api_token, name="delete_api_token"),
    path("avatar/<int:user_id>/", serve_avatar, name="serve_avatar"),
    path("invite/<str:token>/", invite_accept, name="invite_accept"),
]
