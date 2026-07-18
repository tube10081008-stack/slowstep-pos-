"""
멤버십 핵심 비즈니스 로직: 포인트 적립/사용, 결제 확정, 게이미피케이션.

docs/DATA-MODEL.md '핵심 트랜잭션 로직' 절을 구현.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction as db_transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    Member,
    MemberMission,
    MenuItem,
    Mission,
    OrderItem,
    PointEntry,
    Store,
    Transaction,
)
from .payments import TossClient, TossError


class CheckoutError(Exception):
    """결제 검증 실패(잘못된 요청)."""


@dataclass
class Quote:
    gross_amount: int
    discount: int
    points_used: int
    net_amount: int
    points_earned: int
    available_points: int


@dataclass
class OrderLine:
    menu_item: "MenuItem"
    quantity: int
    temperature: str
    decaf: bool
    oatmilk: bool
    shot: bool
    unit_price: int  # 옵션 포함

    @property
    def line_total(self) -> int:
        return self.unit_price * self.quantity


@dataclass
class ResolvedOrder:
    lines: list  # list[OrderLine]
    gross: int
    discount: int


@dataclass
class CheckoutResult:
    transaction: Transaction
    rewards: list[dict] = field(default_factory=list)
    # 동일 order_id 재시도(네트워크 재전송 등)로 기존 거래를 그대로 돌려준 경우 True.
    idempotent_replay: bool = False


def calc_points_earned(net_amount: int, store: Store) -> int:
    """실결제액 기준 적립 포인트(반올림)."""
    rate = Decimal(store.point_earn_rate)
    return int((Decimal(net_amount) * rate).quantize(Decimal("1"), ROUND_HALF_UP))


def build_quote(
    member: Member | None, gross_amount: int, points_to_use: int, discount: int = 0
) -> Quote:
    """결제 전 견적 계산(승인 없음). subtotal = 총액 − 세트할인."""
    if gross_amount <= 0:
        raise CheckoutError("주문 금액은 0보다 커야 합니다.")
    points_to_use = max(0, points_to_use)
    discount = max(0, min(discount, gross_amount))
    subtotal = gross_amount - discount

    available = member.points if member else 0
    if points_to_use > available:
        raise CheckoutError("사용 포인트가 보유 포인트를 초과합니다.")
    if points_to_use > subtotal:
        raise CheckoutError("사용 포인트가 결제 금액을 초과합니다.")

    net = subtotal - points_to_use
    store = member.store if member else Store.objects.first()
    earned = calc_points_earned(net, store) if member else 0
    return Quote(
        gross_amount=gross_amount,
        discount=discount,
        points_used=points_to_use,
        net_amount=net,
        points_earned=earned,
        available_points=available,
    )


def resolve_order(items: list | None, store: Store) -> ResolvedOrder | None:
    """
    주문 항목([{menu_item_id, quantity, temperature, decaf, oatmilk}])을 검증하고
    옵션 포함 단가·총액·세트할인을 계산. 총액은 서버가 계산(위변조 방지).
    세트 할인: 커피(음료)+디저트 동시 주문 시 min(음료수, 디저트수)만큼 건당 할인.
    """
    if not items:
        return None
    opt = store.option_price
    lines: list[OrderLine] = []
    drink_qty = dessert_qty = 0
    for raw in items:
        qty = int(raw.get("quantity", 0))
        if qty <= 0:
            continue
        try:
            mi = MenuItem.objects.get(pk=raw.get("menu_item_id"), is_available=True)
        except MenuItem.DoesNotExist:
            raise CheckoutError(f"판매 중이 아닌 메뉴가 포함됐습니다(id={raw.get('menu_item_id')}).")

        # 재고 확인 (null=무제한)
        if mi.stock is not None and mi.stock < qty:
            raise CheckoutError(f"'{mi.name}' 재고가 부족합니다(남은 {mi.stock}개).")

        decaf = bool(raw.get("decaf")) and mi.decaf_available
        oatmilk = bool(raw.get("oatmilk")) and mi.oatmilk_available
        shot = bool(raw.get("shot")) and mi.shot_available
        temperature = (raw.get("temperature") or "").lower()
        if mi.temp_option == MenuItem.Temp.HOTICE:
            if temperature not in ("hot", "ice"):
                temperature = "ice"  # 기본 아이스
        elif mi.temp_option == MenuItem.Temp.ICE:
            temperature = "ice"
        else:
            temperature = ""

        unit_price = mi.price + (opt if decaf else 0) + (opt if oatmilk else 0) + (opt if shot else 0)
        lines.append(OrderLine(mi, qty, temperature, decaf, oatmilk, shot, unit_price))
        if mi.category == MenuItem.Category.DESSERT:
            dessert_qty += qty
        else:
            drink_qty += qty

    if not lines:
        raise CheckoutError("주문 항목이 비어 있습니다.")

    gross = sum(l.line_total for l in lines)
    discount = min(drink_qty, dessert_qty) * store.set_discount_amount
    return ResolvedOrder(lines=lines, gross=gross, discount=discount)


def _record_point(member, txn, delta, reason) -> int:
    """원장 기록 후 새 잔액 반환."""
    new_balance = member.points + delta
    PointEntry.objects.create(
        member=member,
        transaction=txn,
        delta=delta,
        reason=reason,
        balance_after=new_balance,
    )
    member.points = new_balance
    return new_balance


def _update_missions(member: Member, txn: Transaction, rewards: list[dict]) -> None:
    """활성 미션 진행 갱신, 달성 시 보너스 적립."""
    missions = Mission.objects.filter(store=member.store, is_active=True)
    for mission in missions:
        mm, _ = MemberMission.objects.get_or_create(member=member, mission=mission)
        if mm.is_completed:
            continue
        mm.progress = mission.member_value(member)
        if mm.progress >= mission.target_value:
            mm.mark_completed()
            _record_point(member, txn, mission.reward_points, PointEntry.Reason.MISSION)
            rewards.append(
                {
                    "type": "mission",
                    "title": mission.title,
                    "points": mission.reward_points,
                }
            )
        mm.save()


def _apply_stamp_and_tier(member: Member, txn: Transaction, rewards: list[dict]) -> None:
    """스탬프 +1, 목표 도달 시 리워드·리셋. 등급 재계산."""
    store = member.store
    member.stamps += 1
    if store.stamp_goal and member.stamps >= store.stamp_goal:
        member.stamps = 0
        _record_point(member, txn, store.stamp_reward_points, PointEntry.Reason.STAMP)
        rewards.append(
            {
                "type": "stamp",
                "title": f"스탬프 {store.stamp_goal}개 적립 완료",
                "points": store.stamp_reward_points,
            }
        )
    member.tier = member.compute_tier()


class _DuplicateOrder(Exception):
    """동시 요청이 같은 order_id로 먼저 결제를 완료함(유니크 제약 충돌)."""

    def __init__(self, order_id: str):
        self.order_id = order_id


def checkout(
    *,
    member: Member | None,
    gross_amount: int,
    points_to_use: int,
    payment_method: str,
    items: list | None = None,
    approval_no: str = "",
    toss_payment_key: str = "",
    toss_order_id: str = "",
) -> CheckoutResult:
    """결제 확정(멱등 래퍼). 동일 order_id 중복 요청은 기존 거래를 반환한다.

    유니크 제약 충돌(동시 중복)은 트랜잭션 전체가 롤백된 뒤 여기서
    승자의 거래를 조회해 재생으로 돌려준다.
    """
    try:
        return _checkout_atomic(
            member=member,
            gross_amount=gross_amount,
            points_to_use=points_to_use,
            payment_method=payment_method,
            items=items,
            approval_no=approval_no,
            toss_payment_key=toss_payment_key,
            toss_order_id=toss_order_id,
        )
    except _DuplicateOrder as dup:
        existing = Transaction.objects.filter(
            toss_order_id=dup.order_id, status=Transaction.Status.PAID
        ).first()
        if existing is not None:
            return CheckoutResult(transaction=existing, idempotent_replay=True)
        raise CheckoutError("이미 처리된 주문입니다.")


@db_transaction.atomic
def _checkout_atomic(
    *,
    member: Member | None,
    gross_amount: int,
    points_to_use: int,
    payment_method: str,
    items: list | None = None,
    approval_no: str = "",
    toss_payment_key: str = "",
    toss_order_id: str = "",
) -> CheckoutResult:
    """
    결제 확정 전체 플로우(원자적):
    (메뉴 항목→옵션 단가·총액·세트할인) → 견적 → 결제 승인 →
    포인트 사용/적립 → 스탬프·등급·미션.
    결제는 외부 단말(네이버 커넥트 등)에서 처리되고 앱은 기록만 한다.
    단, TOSS_* 결제수단은 Toss PG 승인 API를 호출한다(옵션).

    동시성/멱등성:
    - 회원 행을 select_for_update로 잠가 동시 결제의 포인트 이중사용을 막는다.
    - 재고는 조건부 UPDATE(stock >= qty)로 차감해 초과판매(TOCTOU)를 막는다.
    - 동일 order_id의 완료 거래가 있으면 그 거래를 그대로 반환한다(멱등 재생)
      → POS가 네트워크 오류로 재전송해도 중복 결제가 생기지 않는다.
    """
    # 멱등 재생: 이미 완료된 동일 주문이면 기존 거래 반환(중복 기록 방지).
    if toss_order_id:
        existing = Transaction.objects.filter(
            toss_order_id=toss_order_id, status=Transaction.Status.PAID
        ).first()
        if existing is not None:
            return CheckoutResult(transaction=existing, idempotent_replay=True)

    # 회원 행 잠금(동시 결제 직렬화). SQLite는 no-op이나 쓰기 자체가 직렬화됨.
    if member is not None:
        member = Member.objects.select_for_update().get(pk=member.pk)

    store = member.store if member else Store.objects.first()
    if store is None:
        raise CheckoutError("매장 설정이 없습니다. seed_demo를 실행하세요.")

    resolved = resolve_order(items, store)
    discount = 0
    if resolved:
        gross_amount = resolved.gross
        discount = resolved.discount

    quote = build_quote(member, gross_amount, points_to_use, discount)

    txn = Transaction.objects.create(
        store=store,
        member=member,
        gross_amount=quote.gross_amount,
        discount=quote.discount,
        points_used=quote.points_used,
        net_amount=quote.net_amount,
        points_earned=quote.points_earned,
        payment_method=payment_method,
        approval_no=approval_no,
        toss_order_id=toss_order_id,
        status=Transaction.Status.PENDING,
    )

    # ── 결제 승인 ──
    # 외부 단말(CARD/NAVERPAY/EASYPAY/CASH)은 단말에서 이미 승인됨 → 기록만.
    # TOSS_* 만 서버가 PG 승인 API 호출.
    if payment_method in (Transaction.Method.TOSS_CARD, Transaction.Method.TOSS_EASY):
        client = TossClient()
        try:
            result = client.confirm(toss_payment_key, txn.toss_order_id or str(txn.pk), quote.net_amount)
        except TossError as exc:
            # 거래는 pending 유지, 502로 매핑됨.
            raise exc
        if not result.approved:
            raise CheckoutError("결제가 승인되지 않았습니다.")
        txn.toss_payment_key = result.payment_key
        txn.toss_order_id = result.order_id

    txn.status = Transaction.Status.PAID
    txn.paid_at = timezone.now()
    try:
        txn.save()
    except IntegrityError:
        # 부분 유니크 제약(uniq_paid_toss_order_id) 충돌 = 동시 중복 주문.
        if txn.toss_order_id:
            raise _DuplicateOrder(txn.toss_order_id)
        raise

    # ── 주문 항목 기록(메뉴·옵션 스냅샷) ──
    if resolved:
        for l in resolved.lines:
            OrderItem.objects.create(
                transaction=txn, menu_item=l.menu_item, name=l.menu_item.name,
                unit_price=l.unit_price, quantity=l.quantity,
                temperature=l.temperature, decaf=l.decaf, oatmilk=l.oatmilk, shot=l.shot,
            )
            # 재고 차감: 조건부 UPDATE로 확인→차감 사이 초과판매(TOCTOU) 방지.
            # (null=무제한은 차감 없음)
            if l.menu_item.stock is not None:
                updated = MenuItem.objects.filter(
                    pk=l.menu_item.pk, stock__gte=l.quantity
                ).update(stock=F("stock") - l.quantity)
                if not updated:
                    raise CheckoutError(f"'{l.menu_item.name}' 재고가 부족합니다.")

    rewards: list[dict] = []

    if member is None:
        # 비회원: 적립/게이미피케이션 없음.
        return CheckoutResult(transaction=txn, rewards=rewards)

    # ── 포인트 사용 ──
    if quote.points_used > 0:
        _record_point(member, txn, -quote.points_used, PointEntry.Reason.REDEEM)

    # ── 포인트 적립 ──
    if quote.points_earned > 0:
        _record_point(member, txn, quote.points_earned, PointEntry.Reason.EARN)

    # ── 누적/방문 갱신 ──
    member.total_spent += quote.net_amount
    member.visit_count += 1

    # ── 스탬프·등급 ──
    _apply_stamp_and_tier(member, txn, rewards)

    # ── 미션 (방문/누적 갱신 후 평가) ──
    _update_missions(member, txn, rewards)

    member.save()
    return CheckoutResult(transaction=txn, rewards=rewards)


@db_transaction.atomic
def cancel_transaction(txn: Transaction) -> Transaction:
    """
    결제 취소/환불(원자적): 상태 전환 + 포인트 원복(사용분 환급·적립분 회수) +
    누적/방문/스탬프 되돌림 + 재고 원복. 실 Toss 연동 시 환불 API 호출 지점.

    거래·회원 행을 잠근 뒤 상태를 재확인해 동시 이중 취소를 막는다.
    """
    txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if txn.status != Transaction.Status.PAID:
        raise CheckoutError("결제완료 건만 취소할 수 있습니다.")

    member = txn.member
    if member is not None:
        member = Member.objects.select_for_update().get(pk=member.pk)
        # 순 포인트 변동 = 사용분 환급(+) − 적립분 회수(−)
        delta = txn.points_used - txn.points_earned
        if delta != 0:
            _record_point(member, txn, delta, PointEntry.Reason.CANCEL)
        member.total_spent = max(0, member.total_spent - txn.net_amount)
        member.visit_count = max(0, member.visit_count - 1)
        member.stamps = max(0, member.stamps - 1)
        member.tier = member.compute_tier()
        member.save()

    # 재고 원복 (F식으로 원자적 증가 — 동시 판매와 충돌해도 유실 없음)
    for it in txn.items.select_related("menu_item").all():
        if it.menu_item and it.menu_item.stock is not None:
            MenuItem.objects.filter(
                pk=it.menu_item.pk, stock__isnull=False
            ).update(stock=F("stock") + it.quantity)

    txn.status = Transaction.Status.CANCELED
    txn.save(update_fields=["status"])
    return txn
