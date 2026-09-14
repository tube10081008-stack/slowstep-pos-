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


class SmsStatusView(APIView):
    """
    문자 연결 진단 🔒 — `GET`. **문자를 보내지 않는다.**

    '문자가 안 온다'는 원인이 여러 갈래다: 환경변수 미반영 / 키 오류 /
    잔액 0 / 발신번호 미등록. 발송 버튼만 눌러서는 어느 쪽인지 알 수 없고,
    운영에는 셸이 없어 로그도 못 본다. 잔액 조회 API 를 두드려 **인증까지는
    되는지**를 먼저 갈라 준다.
    """

    permission_classes = [StorePinPermission]

    def get(self, request):
        from django.conf import settings
        from .solapi import SolapiClient, SolapiError

        c = SolapiClient()
        env = {
            "SOLAPI_API_KEY": bool(settings.SOLAPI_API_KEY),
            "SOLAPI_API_SECRET": bool(settings.SOLAPI_API_SECRET),
            "SMS_SENDER_PHONE": bool(settings.SMS_SENDER_PHONE),
        }
        out = {
            "live": c.is_live,
            "env": env,
            # 값 자체는 싣지 않되, 발신번호는 '등록한 번호와 같은지' 눈으로
            # 대조해야 해서 예외로 보여 준다(공개해도 무방한 매장 대표번호).
            "sender": c.sender,
        }
        if not c.is_live:
            out["auth"] = "미설정"
            out["hint"] = (
                "Vercel 환경변수 " +
                ", ".join(k for k, v in env.items() if not v) +
                " 이(가) 비어 있습니다. 값을 넣고 재배포해야 반영됩니다."
            )
            return Response(out)

        try:
            body = c.balance()
        except SolapiError as exc:
            # 갈래마다 손이 가야 할 곳이 다르다. 연결 실패를 '키를 확인하세요'로
            # 안내하면 멀쩡한 키를 몇 번이나 다시 넣게 된다.
            hints = {
                "auth": "API Key/Secret 이 거부됐습니다. 솔라피 콘솔 → "
                        "개발/연동 → API Key 관리에서 값을 다시 확인하세요.",
                "network": "솔라피 서버에 닿지 못했습니다. 키 문제가 아니라 "
                           "네트워크 문제입니다. 잠시 뒤 다시 눌러 보세요.",
                "timeout": "솔라피 응답이 늦습니다. 키는 맞을 수 있습니다. "
                           "잠시 뒤 다시 확인하세요.",
            }
            out["auth"] = "실패"
            out["kind"] = exc.kind
            out["error"] = str(exc)
            out["hint"] = hints.get(
                exc.kind, f"솔라피가 오류를 돌려줬습니다: {exc}"
            )
            return Response(out, status=502)

        point = body.get("point") or body.get("balance") or 0
        out["auth"] = "성공"
        out["balance"] = body
        if not point:
            out["hint"] = (
                "인증은 됐지만 잔액이 0입니다. 솔라피 콘솔에서 충전하세요 — "
                "잔액이 없으면 발송이 접수되지 않습니다."
            )
        else:
            out["hint"] = (
                f"인증·잔액 정상(잔액 {point}). 그래도 안 오면 발신번호가 "
                f"솔라피에 등록한 번호({c.sender})와 같은지, 수신 폰이 "
                "번호를 차단하지 않았는지 확인하세요."
            )
        return Response(out)


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
