from rest_framework.routers import DefaultRouter

from .views import WorkspaceMemberViewSet, WorkspaceViewSet

router = DefaultRouter()
router.register(r"workspaces", WorkspaceViewSet, basename="workspace")
router.register(r"workspace-members", WorkspaceMemberViewSet, basename="workspace-member")

urlpatterns = router.urls
