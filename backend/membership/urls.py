"""membership API 라우팅 (Base: /api/v1)."""
from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import (
    HealthView,
    MarginView,
    HallOfFameView,
    MemberQrView,
    OrderParseView,
    PinLoginView,
    MemberViewSet,
    MenuDetailView,
    MenuReorderView,
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
    path("health", HealthView.as_view(), name="health"),
    path("auth/pin", PinLoginView.as_view(), name="auth-pin"),
    path("store", StoreView.as_view(), name="store"),
    path("store/session", StoreSessionView.as_view(), name="store-session"),
    path("menu", MenuView.as_view(), name="menu"),
    path("menu/reorder", MenuReorderView.as_view(), name="menu-reorder"),
    path("menu/<int:pk>", MenuDetailView.as_view(), name="menu-detail"),
    path("member-qr", MemberQrView.as_view(), name="member-qr"),
    path("hall-of-fame", HallOfFameView.as_view(), name="hall-of-fame"),
    path("orders/parse", OrderParseView.as_view(), name="orders-parse"),
    path("sales/summary", SalesSummaryView.as_view(), name="sales-summary"),
    path("margins/summary", MarginView.as_view(), name="margins-summary"),
]
urlpatterns += router.urls
