from rest_framework.routers import DefaultRouter

from .views import LabelGroupViewSet, LabelViewSet

router = DefaultRouter()
router.register(r"labels", LabelViewSet, basename="label")
router.register(r"label-groups", LabelGroupViewSet, basename="label-group")

urlpatterns = router.urls
