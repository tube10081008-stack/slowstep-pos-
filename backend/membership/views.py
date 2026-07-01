"""
API 뷰. API 계약(docs/API-CONTRACT.md, Base: /api/v1)에 맞춰 구현.
"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Member, MenuItem, Mission, Store, Transaction
from .payments import TossError
from .serializers import (
    CheckoutRequestSerializer,
    MemberCreateSerializer,
    MemberMissionSerializer,
    MemberSerializer,
    MenuItemSerializer,
    MissionSerializer,
    PointEntrySerializer,
    QuoteRequestSerializer,
    StoreSerializer,
    TransactionSerializer,
)
from .profile import build_member_dashboard
from .services import CheckoutError, build_quote, checkout


def _resolve_member(member_id):
    if member_id is None:
        return None
    return get_object_or_404(Member, pk=member_id)


class StoreView(APIView):
    """기본 매장 설정 조회 (POS 초기화)."""

    def get(self, request):
        store = Store.objects.first()
        if store is None:
            return Response({"detail": "매장 설정이 없습니다."}, status=404)
        return Response(StoreSerializer(store).data)


class MenuView(APIView):
    """판매 중인 메뉴 목록(POS 주문 화면용)."""

    def get(self, request):
        qs = MenuItem.objects.filter(is_available=True)
        return Response(MenuItemSerializer(qs, many=True).data)


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related("store").all()
    http_method_names = ["get", "post"]

    def get_serializer_class(self):
        if self.action == "create":
            return MemberCreateSerializer
        return MemberSerializer

    def create(self, request, *args, **kwargs):
        serializer = MemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member = serializer.save()
        return Response(MemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def lookup(self, request):
        """?phone=01012345678 으로 회원번호 조회 (QR 스캔 결과)."""
        phone = request.query_params.get("phone", "").strip()
        if not phone:
            return Response({"detail": "phone 파라미터가 필요합니다."}, status=400)
        member = Member.objects.filter(phone=phone).select_related("store").first()
        if member is None:
            return Response({"detail": "회원을 찾을 수 없습니다."}, status=404)
        return Response(MemberSerializer(member).data)

    @action(detail=True, methods=["get"])
    def missions(self, request, pk=None):
        member = self.get_object()
        qs = member.member_missions.select_related("mission").all()
        return Response(MemberMissionSerializer(qs, many=True).data)

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        """고객 대시보드(배지·타임라인·랭킹·등급진행·미션) 한 번에."""
        member = self.get_object()
        data = {"member": MemberSerializer(member).data}
        data.update(build_member_dashboard(member))
        return Response(data)

    @action(detail=True, methods=["get"])
    def points(self, request, pk=None):
        """최근 포인트 내역."""
        member = self.get_object()
        qs = member.point_entries.all()[:50]
        return Response(PointEntrySerializer(qs, many=True).data)


class MissionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MissionSerializer

    def get_queryset(self):
        return Mission.objects.filter(is_active=True)


class TransactionViewSet(viewsets.ModelViewSet):
    queryset = Transaction.objects.select_related("member").all()
    serializer_class = TransactionSerializer
    http_method_names = ["get", "post"]

    @action(detail=False, methods=["post"])
    def quote(self, request):
        """결제 전 견적(사용 가능 포인트·적립 예상)."""
        req = QuoteRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        member = _resolve_member(data.get("member_id"))
        try:
            q = build_quote(member, data["gross_amount"], data["points_to_use"])
        except CheckoutError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(
            {
                "gross_amount": q.gross_amount,
                "points_used": q.points_used,
                "net_amount": q.net_amount,
                "points_earned": q.points_earned,
                "available_points": q.available_points,
            }
        )

    def create(self, request, *args, **kwargs):
        """거래 생성 + 결제 확정."""
        req = CheckoutRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        member = _resolve_member(data.get("member_id"))
        try:
            result = checkout(
                member=member,
                gross_amount=data.get("gross_amount") or 0,
                points_to_use=data["points_to_use"],
                payment_method=data["payment_method"],
                items=data.get("items"),
                toss_payment_key=data.get("toss_payment_key", ""),
                toss_order_id=data.get("toss_order_id", ""),
            )
        except CheckoutError as exc:
            return Response({"detail": str(exc)}, status=400)
        except TossError as exc:
            return Response({"detail": str(exc)}, status=502)

        txn = result.transaction
        body = TransactionSerializer(txn).data
        if txn.member:
            txn.member.refresh_from_db()
            body["member"] = MemberSerializer(txn.member).data
        body["rewards"] = result.rewards
        return Response(body, status=status.HTTP_201_CREATED)
