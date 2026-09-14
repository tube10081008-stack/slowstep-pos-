"""
마케팅 + 대시보드 API.
docs/API-CONTRACT.md(마케팅 절)에 맞춰 구현.
"""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from membership.auth import StorePinPermission
from membership.serializers import MemberSerializer

from .models import Campaign, Segment
from .segments import resolve_members
from .serializers import (
    CampaignSerializer,
    MessageLogSerializer,
    SegmentPreviewSerializer,
    SegmentSerializer,
)
from .sender import MessageClient, SendError
from .services import CampaignError, dashboard_stats, send_campaign


class SmsTestView(APIView):
    """
    테스트 발송 🔒 — `POST {"phone": "01012345678", "text": "..."}`.

    실전 첫 발송이 곧바로 42명에게 나가면 되돌릴 수 없다. 발신번호 등록,
    잔액, 키가 다 맞는지 **본인 폰 한 대로** 먼저 확인하는 통로다.

    - 항상 정보성으로 나간다((광고) 표기 없음). 광고성 테스트는 야간 제한과
      동의 확인에 걸리므로 여기서 다루지 않는다.
    - MessageLog 를 남기지 않는다 — 캠페인 발송 통계가 테스트로 오염된다.
    """

    permission_classes = [StorePinPermission]

    def post(self, request):
        phone = (request.data.get("phone") or "").strip()
        if len([c for c in phone if c.isdigit()]) < 10:
            return Response({"detail": "받을 번호를 정확히 입력해 주세요."}, status=400)
        text = (request.data.get("text") or "").strip()
        if not text:
            return Response({"detail": "보낼 내용을 입력해 주세요."}, status=400)
        if len(text) > 1000:
            return Response({"detail": "내용이 너무 깁니다(1,000자 이내)."}, status=400)

        client = MessageClient()
        try:
            res = client.send_one(phone, text)
        except SendError as exc:
            return Response({"detail": str(exc)}, status=502)
        if not res.success:
            return Response({"detail": res.reason or "발송에 실패했습니다."}, status=502)
        return Response({
            "success": True,
            "phone": phone,
            "mocked": res.mocked,
            "live": client.is_live,
        })


class DashboardView(APIView):
    """점주 대시보드 핵심 지표. (매출·고객 지표 → 매장 PIN 필수)"""

    permission_classes = [StorePinPermission]

    def get(self, request):
        return Response(dashboard_stats())


class SegmentViewSet(viewsets.ModelViewSet):
    queryset = Segment.objects.all()
    serializer_class = SegmentSerializer
    permission_classes = [StorePinPermission]

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
    permission_classes = [StorePinPermission]

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
