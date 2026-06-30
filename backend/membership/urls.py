"""membership API 라우팅 (Base: /api/v1)."""
from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import (
    MemberViewSet,
    MissionViewSet,
    StoreView,
    TransactionViewSet,
)

router = DefaultRouter(trailing_slash=False)
router.register("members", MemberViewSet, basename="member")
router.register("missions", MissionViewSet, basename="mission")
router.register("transactions", TransactionViewSet, basename="transaction")

urlpatterns = [
    path("store", StoreView.as_view(), name="store"),
]
urlpatterns += router.urls
