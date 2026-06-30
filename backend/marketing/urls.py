"""marketing API 라우팅 (Base: /api/v1)."""
from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import CampaignViewSet, DashboardView, SegmentViewSet

router = DefaultRouter(trailing_slash=False)
router.register("segments", SegmentViewSet, basename="segment")
router.register("campaigns", CampaignViewSet, basename="campaign")

urlpatterns = [
    path("dashboard/stats", DashboardView.as_view(), name="dashboard-stats"),
]
urlpatterns += router.urls
