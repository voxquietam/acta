from rest_framework.routers import DefaultRouter

from .views import ProjectUpdateViewSet, ProjectViewSet

router = DefaultRouter()
router.register(r"projects", ProjectViewSet, basename="project")
router.register(r"project-updates", ProjectUpdateViewSet, basename="project-update")

urlpatterns = router.urls
