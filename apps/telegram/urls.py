from django.urls import path

from .views import telegram_disconnect, telegram_status, telegram_toggle, telegram_webhook

app_name = "telegram"

urlpatterns = [
    path("webhook/<str:secret>/", telegram_webhook, name="webhook"),
    path("status/", telegram_status, name="status"),
    path("disconnect/", telegram_disconnect, name="disconnect"),
    path("toggle/", telegram_toggle, name="toggle"),
]
