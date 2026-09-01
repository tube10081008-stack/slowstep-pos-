"""
API 뷰. API 계약(docs/API-CONTRACT.md, Base: /api/v1)에 맞춰 구현.
"""
import os
from datetime import datetime

from django.conf import settings
from django.db import connection
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Member, MenuItem, Mission, OrderItem, PointEntry, Store, Transaction
from .payments import TossError
from .serializers import (
    CheckoutRequestSerializer,
    MemberCreateSerializer,
    MemberMissionSerializer,
    MemberSerializer,
    MenuItemSerializer,
    MenuItemWriteSerializer,
    MissionSerializer,
    PointEntrySerializer,
    QuoteRequestSerializer,
    StoreSerializer,
    TransactionSerializer,
)
from .auth import (
    PinRateLimited,
    StorePinPermission,
    check_pin,
    issue_token,
    request_authorized,
)
from .ai_order import OrderParseError, parse_order
from .imports import CsvImportError, import_members_csv
from .member_qr import QrUnavailable, member_url, qr_svg
from .margins import margin_summary, menu_item_margins, to_supply
from .profile import build_member_dashboard, hall_of_fame
from .services import (
    CheckoutError,
    CouponError,
    ReferralError,
    SpinError,
    apply_referral,
    build_quote,
    cancel_transaction,
    checkout,
)
from .services import spin as spin_service


def _query_date(request):
    """?date=YYYY-MM-DD → date. 없거나 형식이 틀리면 None(호출부가 오늘로 처리)."""
    raw = (request.query_params.get("date") or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


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

        # 미적용 마이그레이션 — 스키마가 코드보다 뒤처지면 새 컬럼 조회가 500이 된다.
        # (배포에서 실제로 겪은 문제라 눈에 보이게 노출한다)
        pending = None
        if db_ok:
            try:
                from django.db import connections
                from django.db.migrations.executor import MigrationExecutor

                executor = MigrationExecutor(connections["default"])
                pending = len(
                    executor.migration_plan(executor.loader.graph.leaf_nodes())
                )
            except Exception:
                pending = None

        body = {
            "status": "ok" if db_ok else "degraded",
            "db": {"ok": db_ok, "engine": engine, "persistent": persistent},
        }
        # 경고는 **모아서** 싣는다. 한 칸에 덮어쓰면 마지막 것만 남아,
        # 실제로 500을 냈던 '미적용 마이그레이션' 경고가 조용히 사라진다.
        warnings: list[str] = []

        if pending is not None:
            body["db"]["pending_migrations"] = pending
            if pending:
                warnings.append(
                    f"적용되지 않은 마이그레이션이 {pending}건 있습니다. "
                    "스키마가 코드보다 뒤처져 일부 API가 실패할 수 있습니다."
                )
        # 매장 PIN이 코드 기본값 그대로인지. **값은 절대 싣지 않는다** — 출처만 본다.
        # 저장소가 공개라 기본값을 쓰면 매출·고객 명단이 사실상 무방비다.
        # (환경변수를 넣었는데 반영이 안 된 경우를 눈으로 확인할 수단이기도 하다)
        pin_from_env = bool(os.environ.get("STORE_PIN"))
        body["config"] = {"store_pin": "env" if pin_from_env else "default"}
        if not pin_from_env and not settings.DEBUG:
            warnings.append(
                "매장 PIN이 코드 기본값입니다. 저장소가 공개라면 누구나 "
                "POS·대시보드에 들어올 수 있습니다. 배포 환경변수 STORE_PIN을 "
                "설정하고 재배포하세요."
            )
        if db_error:
            body["db"]["error"] = db_error
        if not persistent:
            warnings.append(
                "임시 저장소 모드: 주문·회원 데이터가 콜드스타트 시 초기화되고 "
                "동시 접속 간 불일치할 수 있습니다. DATABASE_URL(Neon 등)을 "
                "설정해 영구 저장으로 전환하세요."
            )

        if warnings:
            body["status"] = "degraded"
            body["warnings"] = warnings
            # 기존 클라이언트가 읽는 단일 문자열도 유지한다(하나로 합쳐서).
            body["warning"] = " / ".join(warnings)
        return Response(body, status=200 if db_ok else 503)


class PinLoginView(APIView):
    """
    매장 PIN 로그인 → 토큰 발급.

    POST {"pin": "0000"} → {"token": "..."} · 실패 401 · 시도 초과 429.
    GET → 현재 토큰이 유효한지 확인({"authorized": bool}).
    """

    def get(self, request):
        return Response({"authorized": request_authorized(request)})

    def post(self, request):
        pin = str(request.data.get("pin", "")).strip()
        if not pin:
            return Response({"detail": "PIN을 입력하세요."}, status=400)
        try:
            ok = check_pin(request, pin)
        except PinRateLimited:
            return Response(
                {"detail": "시도 횟수를 초과했습니다. 잠시 후 다시 시도하세요."},
                status=429,
            )
        if not ok:
            return Response({"detail": "PIN이 올바르지 않습니다."}, status=401)
        return Response({"token": issue_token()})


class StoreView(APIView):
    """기본 매장 설정 조회 (POS 초기화)."""

    def get(self, request):
        store = Store.objects.first()
        if store is None:
            return Response({"detail": "매장 설정이 없습니다."}, status=404)
        return Response(StoreSerializer(store).data)


class MenuView(APIView):
    """
    메뉴 목록(공개) + 등록(직원).

    GET  판매 중인 메뉴. `?all=1` 이면 판매중지분까지(직원 전용 — 관리 화면용).
    POST 새 메뉴 등록 🔒 — 디저트가 매일 바뀌므로 POS에서 바로 추가한다.
    """

    def get(self, request):
        qs = MenuItem.objects.all()
        if request.query_params.get("all") in ("1", "true", "yes"):
            if not request_authorized(request):
                return Response({"detail": "권한이 없습니다."}, status=403)
        else:
            qs = qs.filter(is_available=True)
        return Response(MenuItemSerializer(qs, many=True).data)

    def post(self, request):
        if not request_authorized(request):
            return Response({"detail": "권한이 없습니다."}, status=403)
        store = Store.objects.first()
        if store is None:
            return Response({"detail": "매장 설정이 없습니다."}, status=404)
        ser = MenuItemWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        item = ser.save(store=store)
        return Response(MenuItemSerializer(item).data, status=201)


class MenuReorderView(APIView):
    """
    메뉴 순서 저장 🔒 — `{"ids": [3, 1, 7, ...]}` 순서대로 sort_order 를 매긴다.

    한 건씩 PATCH 하면 드래그 한 번에 여러 요청이 나가고, 중간에 하나만
    실패하면 순서가 어긋난 채로 남는다. 한 번에 받아 통째로 저장한다.
    """

    permission_classes = [StorePinPermission]

    def post(self, request):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "ids 목록이 필요합니다."}, status=400)
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return Response({"detail": "ids 는 숫자 목록이어야 합니다."}, status=400)

        found = {m.id: m for m in MenuItem.objects.filter(id__in=ids)}
        missing = [i for i in ids if i not in found]
        if missing:
            return Response({"detail": f"없는 메뉴가 포함됐습니다: {missing}"}, status=400)

        for order, mid in enumerate(ids, start=1):
            item = found[mid]
            if item.sort_order != order:
                item.sort_order = order
                item.save(update_fields=["sort_order"])
        return Response({"updated": len(ids)})


class MenuDetailView(APIView):
    """메뉴 수정·삭제 🔒."""

    permission_classes = [StorePinPermission]

    def patch(self, request, pk):
        item = get_object_or_404(MenuItem, pk=pk)
        ser = MenuItemWriteSerializer(item, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        return Response(MenuItemSerializer(ser.save()).data)

    def delete(self, request, pk):
        """
        메뉴 삭제. 지난 주문 기록은 남는다 — OrderItem 이 이름·단가를
        스냅샷으로 갖고 있고 메뉴 참조는 SET_NULL 이라 매출이 사라지지 않는다.
        그래도 오늘 판 메뉴라면 지우는 대신 '판매중지'를 권한다.
        """
        item = get_object_or_404(MenuItem, pk=pk)
        today = timezone.localdate()
        sold_today = OrderItem.objects.filter(
            menu_item=item, transaction__paid_at__date=today
        ).exists()
        if sold_today and request.query_params.get("force") not in ("1", "true", "yes"):
            return Response(
                {"detail": "오늘 판매된 메뉴입니다. 판매중지로 내리거나 force=1 로 삭제하세요.",
                 "sold_today": True},
                status=409,
            )
        name = item.name
        item.delete()
        return Response({"deleted": name})


class StoreSessionView(APIView):
    """영업 시작/마감 토글."""

    permission_classes = [StorePinPermission]

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
    """오늘 정산 요약 + 최근 결제. (매출·마진 → 점주 전용)"""

    permission_classes = [StorePinPermission]

    def get(self, request):
        store = Store.objects.first()
        today = _query_date(request) or timezone.localdate()
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
        # 오늘 기여이익(공급가 − 재료원가 − 적립비용). 자세한 기준은 margins.py.
        net_today = agg["net"] or 0
        material_cost = OrderItem.objects.filter(transaction__in=today_qs).aggregate(
            s=Sum(F("unit_cost") * F("quantity"))
        )["s"] or 0
        reward_cost = PointEntry.objects.filter(
            transaction__in=today_qs,
            reason__in=(PointEntry.Reason.EARN, PointEntry.Reason.STAMP, PointEntry.Reason.MISSION),
            delta__gt=0,
        ).aggregate(s=Sum("delta"))["s"] or 0
        supply = to_supply(net_today)
        contribution = supply - material_cost - reward_cost
        return Response({
            "date": today.isoformat(),
            "is_open": store.is_open if store else False,
            "opened_at": store.opened_at.isoformat() if store and store.opened_at else None,
            "count": agg["n"] or 0,
            "gross": agg["gross"] or 0,
            "discount": agg["discount"] or 0,
            "net": net_today,
            "points": agg["points"] or 0,
            "by_method": methods,
            "margin": {
                "supply_revenue": supply,
                "material_cost": material_cost,
                "reward_cost": reward_cost,
                "contribution": contribution,
                "margin_rate": round(contribution / supply * 100, 1) if supply else 0.0,
            },
        })


class OrderParseView(APIView):
    """
    자연어 주문 → 장바구니 항목. POS 상단 입력창이 호출.

    POST {"text": "아아 두 잔이랑 라떼 하나 따뜻하게"}
    → {"items": [...], "source": "gemini"|"rule"}
    키(GEMINI_API_KEY) 미설정 시 규칙 기반으로 자동 폴백.
    """

    permission_classes = [StorePinPermission]

    def post(self, request):
        try:
            return Response(parse_order(request.data.get("text", "")))
        except OrderParseError as exc:
            return Response({"detail": str(exc)}, status=400)


class MemberQrView(APIView):
    """
    멤버십 페이지로 가는 QR(공개). 모든 손님에게 동일한 고정 주소만 담는다 —
    개인정보도 토큰도 들어가지 않으므로 인쇄해 붙여도 된다.
    """

    def get(self, request):
        url = member_url(request.build_absolute_uri("/"))
        try:
            svg = qr_svg(url)
        except QrUnavailable as exc:
            # QR만 못 그릴 뿐, 주소는 알려준다(직접 입력·인쇄로 대체 가능).
            return Response({"url": url, "detail": str(exc)}, status=503)
        return Response({"url": url, "svg": svg})


class HallOfFameView(APIView):
    """이달의 단골(공개) — 닉네임으로 표시하므로 매장 화면에 띄워도 된다."""

    def get(self, request):
        return Response(hall_of_fame())


class MarginView(APIView):
    """
    원가·마진 분석: 기간 기여이익 + 메뉴별 마진 순위.

    ?days=N (기본 30). 기준: 공급가 매출 − 재료원가 − 적립비용(적립 시점 인식).
    원가가 드러나므로 매장 PIN 필수.
    """

    permission_classes = [StorePinPermission]

    def get(self, request):
        try:
            days = max(1, min(365, int(request.query_params.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        summary = margin_summary(days)
        summary["menu"] = menu_item_margins(days)
        return Response(summary)


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related("store").all()
    http_method_names = ["get", "post"]

    # 고객이 본인 멤버십을 보는 경로만 공개. 명단 검색·가입·일괄등록은 매장 전용.
    PUBLIC_ACTIONS = {"lookup", "dashboard", "missions", "points", "referral", "spin"}

    def get_permissions(self):
        if self.action in self.PUBLIC_ACTIONS:
            return []
        return [StorePinPermission()]

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

    @action(detail=False, methods=["post"], url_path="import")
    def import_csv(self, request):
        """
        기존 고객 CSV 일괄 등록(payhere 등 이관).

        multipart `file`(엑셀 CP949·UTF-8 자동 판별) 또는 JSON `csv`(텍스트).
        `dry_run=true` 면 검증·집계만 하고 저장하지 않는다(미리보기).
        """
        upload = request.FILES.get("file")
        csv_text = (request.data.get("csv") or "").strip()
        dry_run = str(request.data.get("dry_run", "")).lower() in ("1", "true", "yes", "y")
        if upload is None and not csv_text:
            return Response(
                {"detail": "file(업로드) 또는 csv(텍스트)가 필요합니다."}, status=400
            )
        try:
            summary = import_members_csv(
                file_bytes=upload.read() if upload else None,
                csv_text=csv_text or None,
                dry_run=dry_run,
            )
        except CsvImportError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(summary)

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

    @action(detail=True, methods=["post"], url_path="referral")
    def referral(self, request, pk=None):
        """친구 초대 코드 적용(손님 폰에서 호출). 둘 다 포인트를 받는다."""
        member = self.get_object()
        try:
            result = apply_referral(member, request.data.get("code", ""))
        except ReferralError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(result)

    @action(detail=True, methods=["get"], url_path="coupons")
    def coupons(self, request, pk=None):
        """이 회원이 지금 쓸 수 있는 쿠폰(직원용). 결제 화면에서 붙여 쓴다."""
        from .profile import coupon_list

        return Response(coupon_list(self.get_object()))

    @action(detail=True, methods=["post"], url_path="spin")
    def spin(self, request, pk=None):
        """
        룰렛 1회(손님 폰에서 호출). 기회를 1 차감하고 쿠폰을 발행한다.

        **당첨 칸은 서버가 정해 내려준다** — 화면은 그 칸으로 멈추는 연출만 한다.
        """
        member = self.get_object()
        try:
            return Response(spin_service(member))
        except SpinError as exc:
            return Response({"detail": str(exc)}, status=400)

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
    permission_classes = [StorePinPermission]

    def get_queryset(self):
        # 목록은 최근 결제/취소 100건(대기 제외), 그 외 액션은 전체 대상.
        base = Transaction.objects.select_related("member").prefetch_related("items")
        if self.action != "list":
            return base.all()
        qs = base.exclude(status=Transaction.Status.PENDING)
        # ?date=YYYY-MM-DD 면 그날치만. 날짜를 안 주면 최근 100건(기존 동작).
        day = _query_date(self.request)
        if day:
            return qs.filter(paid_at__date=day)
        return qs[:100]

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
                coupon_id=data.get("coupon_id"),
                discount_pct=data.get("discount_pct") or 0,
            )
        except (CheckoutError, CouponError) as exc:
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
