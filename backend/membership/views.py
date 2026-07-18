"""
API 뷰. API 계약(docs/API-CONTRACT.md, Base: /api/v1)에 맞춰 구현.
"""
from django.conf import settings
from django.db import connection
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
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
from .services import CheckoutError, build_quote, cancel_transaction, checkout


def _resolve_member(member_id):
    if member_id is None:
        return None
    return get_object_or_404(Member, pk=member_id)


class HealthView(APIView):
    """
    서비스 상태 점검: DB 연결 + 저장 영속성 보고.

    POS·대시보드가 부팅 시 호출해 임시 저장소 모드(서버리스 + 무DB)면
    경고 배너를 띄운다. 모니터링/헬스체크 경로로도 사용.
    """

    def get(self, request):
        db_ok = True
        db_error = ""
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception as exc:  # 연결 실패도 200으로 보고(상태가 본문)
            db_ok = False
            db_error = str(exc)[:200]
        engine = connection.settings_dict.get("ENGINE", "").rsplit(".", 1)[-1]
        persistent = getattr(settings, "STORAGE_PERSISTENT", True)
        body = {
            "status": "ok" if db_ok else "degraded",
            "db": {"ok": db_ok, "engine": engine, "persistent": persistent},
        }
        if db_error:
            body["db"]["error"] = db_error
        if not persistent:
            body["warning"] = (
                "임시 저장소 모드: 주문·회원 데이터가 콜드스타트 시 초기화되고 "
                "동시 접속 간 불일치할 수 있습니다. DATABASE_URL(Neon 등)을 "
                "설정해 영구 저장으로 전환하세요."
            )
        return Response(body, status=200 if db_ok else 503)


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


class StoreSessionView(APIView):
    """영업 시작/마감 토글."""

    def post(self, request):
        store = Store.objects.first()
        if store is None:
            return Response({"detail": "매장 설정이 없습니다."}, status=404)
        action = request.data.get("action")
        if action == "open":
            store.is_open = True
            store.opened_at = timezone.now()
        elif action == "close":
            store.is_open = False
        else:
            return Response({"detail": "action은 open/close."}, status=400)
        store.save(update_fields=["is_open", "opened_at"])
        return Response(StoreSerializer(store).data)


class SalesSummaryView(APIView):
    """오늘 정산 요약 + 최근 결제."""

    def get(self, request):
        store = Store.objects.first()
        today = timezone.localdate()
        paid = Transaction.objects.filter(status=Transaction.Status.PAID)
        today_qs = paid.filter(paid_at__date=today)
        agg = today_qs.aggregate(
            n=Count("id"), gross=Sum("gross_amount"),
            discount=Sum("discount"), net=Sum("net_amount"),
            points=Sum("points_earned"),
        )
        methods = {
            row["payment_method"]: row["s"]
            for row in today_qs.values("payment_method").annotate(s=Sum("net_amount"))
        }
        return Response({
            "date": today.isoformat(),
            "is_open": store.is_open if store else False,
            "opened_at": store.opened_at.isoformat() if store and store.opened_at else None,
            "count": agg["n"] or 0,
            "gross": agg["gross"] or 0,
            "discount": agg["discount"] or 0,
            "net": agg["net"] or 0,
            "points": agg["points"] or 0,
            "by_method": methods,
        })


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related("store").all()
    http_method_names = ["get", "post"]

    def get_queryset(self):
        qs = Member.objects.select_related("store").all()
        q = self.request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        return qs.order_by("-total_spent")

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

    def get_queryset(self):
        # 목록은 최근 결제/취소 100건(대기 제외), 그 외 액션은 전체 대상.
        base = Transaction.objects.select_related("member").prefetch_related("items")
        if self.action == "list":
            return base.exclude(status=Transaction.Status.PENDING)[:100]
        return base.all()

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """결제 취소/환불(포인트·누적·재고 원복)."""
        txn = self.get_object()
        try:
            cancel_transaction(txn)
        except CheckoutError as exc:
            return Response({"detail": str(exc)}, status=400)
        txn.refresh_from_db()
        body = TransactionSerializer(txn).data
        if txn.member:
            txn.member.refresh_from_db()
            body["member"] = MemberSerializer(txn.member).data
        return Response(body)

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
                approval_no=data.get("approval_no", ""),
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
        if result.idempotent_replay:
            # 재전송된 요청 — 새로 결제된 게 아니라 기존 거래를 돌려줌.
            body["idempotent_replay"] = True
            return Response(body, status=status.HTTP_200_OK)
        return Response(body, status=status.HTTP_201_CREATED)
