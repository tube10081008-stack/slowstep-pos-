"""루트 URL 설정."""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    # 루트(/) 접속 시 점주 대시보드로 이동.
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),
    path("admin/", admin.site.urls),
    path("api/v1/", include("membership.urls")),
    path("api/v1/", include("marketing.urls")),
]
