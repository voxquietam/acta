from django.urls import path

from rest_framework.routers import DefaultRouter

from .bulk import TaskBulkView
from .views import TaskViewSet

router = DefaultRouter()
router.register(r"tasks", TaskViewSet, basename="task")

# Bulk path is registered explicitly before the router URLs so it wins
# over the ``tasks/<pk>/`` detail route — otherwise ``bulk`` would be
# interpreted as a primary key and yield a 404.
urlpatterns = [
    path("tasks/bulk/", TaskBulkView.as_view(), name="task-bulk"),
    *router.urls,
]
