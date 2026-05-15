from django.urls import path

from .views import set_language

app_name = "accounts"

urlpatterns = [
    path("set-language/", set_language, name="set_language"),
]
