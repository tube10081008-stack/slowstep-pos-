"""marketing API 라우팅 (Base: /api/v1)."""
from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import CampaignViewSet, ComposeView, DashboardView, InsightsView, SegmentViewSet, SmsStatusView, SmsTestView

router = DefaultRouter(trailing_slash=False)
router.register("segments", SegmentViewSet, basename="segment")
router.register("campaigns", CampaignViewSet, basename="campaign")

urlpatterns = [
    path("dashboard/stats", DashboardView.as_view(), name="dashboard-stats"),
    path("insights", InsightsView.as_view(), name="insights"),
    path("sms/test", SmsTestView.as_view(), name="sms-test"),
    path("sms/status", SmsStatusView.as_view(), name="sms-status"),
    path("campaigns/compose", ComposeView.as_view(), name="campaign-compose"),
]
urlpatterns += router.urls
