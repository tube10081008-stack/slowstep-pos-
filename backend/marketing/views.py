"""
마케팅 + 대시보드 API.
docs/API-CONTRACT.md(마케팅 절)에 맞춰 구현.
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from membership.serializers import MemberSerializer

from .models import Campaign, Segment
from .segments import resolve_members
from .serializers import (
    CampaignSerializer,
    MessageLogSerializer,
    SegmentPreviewSerializer,
    SegmentSerializer,
)
from .services import CampaignError, dashboard_stats, send_campaign


class DashboardView(APIView):
    """점주 대시보드 핵심 지표."""

    def get(self, request):
        return Response(dashboard_stats())


class SegmentViewSet(viewsets.ModelViewSet):
    queryset = Segment.objects.all()
    serializer_class = SegmentSerializer

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """저장 없이 필터로 대상 회원 수·샘플 미리보기."""
        ser = SegmentPreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # 임시 Segment 인스턴스로 쿼리 (저장 X).
        tmp = Segment(**ser.validated_data)
        qs = resolve_members(tmp)
        sample = qs[:10]
        return Response({
            "count": qs.count(),
            "sample": MemberSerializer(sample, many=True).data,
        })

    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        """세그먼트에 속한 회원 목록."""
        segment = self.get_object()
        qs = resolve_members(segment)
        return Response({
            "count": qs.count(),
            "members": MemberSerializer(qs[:200], many=True).data,
        })


class CampaignViewSet(viewsets.ModelViewSet):
    queryset = Campaign.objects.select_related("segment").all()
    serializer_class = CampaignSerializer

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """캠페인 발송(알림톡)."""
        campaign = self.get_object()
        try:
            send_campaign(campaign)
        except CampaignError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(CampaignSerializer(campaign).data)

    @action(detail=True, methods=["get"])
    def logs(self, request, pk=None):
        """발송 로그."""
        campaign = self.get_object()
        qs = campaign.logs.select_related("member")[:200]
        return Response(MessageLogSerializer(qs, many=True).data)
