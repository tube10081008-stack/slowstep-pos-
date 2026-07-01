"""membership API 라우팅 (Base: /api/v1)."""
from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import (
    MemberViewSet,
    MenuView,
    MissionViewSet,
    SalesSummaryView,
    StoreSessionView,
    StoreView,
    TransactionViewSet,
)

router = DefaultRouter(trailing_slash=False)
router.register("members", MemberViewSet, basename="member")
router.register("missions", MissionViewSet, basename="mission")
router.register("transactions", TransactionViewSet, basename="transaction")

urlpatterns = [
    path("store", StoreView.as_view(), name="store"),
    path("store/session", StoreSessionView.as_view(), name="store-session"),
    path("menu", MenuView.as_view(), name="menu"),
    path("sales/summary", SalesSummaryView.as_view(), name="sales-summary"),
]
urlpatterns += router.urls
