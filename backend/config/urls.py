"""루트 URL 설정."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("membership.urls")),
    path("api/v1/", include("marketing.urls")),
]
