"""
멤버십 핵심 비즈니스 로직: 포인트 적립/사용, 결제 확정, 게이미피케이션.

docs/DATA-MODEL.md '핵심 트랜잭션 로직' 절을 구현.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction as db_transaction
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

        decaf = bool(raw.get("decaf")) and mi.decaf_available
        oatmilk = bool(raw.get("oatmilk")) and mi.oatmilk_available
        temperature = (raw.get("temperature") or "").lower()
        if mi.temp_option == MenuItem.Temp.HOTICE:
            if temperature not in ("hot", "ice"):
                temperature = "ice"  # 기본 아이스
        elif mi.temp_option == MenuItem.Temp.ICE:
            temperature = "ice"
        else:
            temperature = ""

        unit_price = mi.price + (opt if decaf else 0) + (opt if oatmilk else 0)
        lines.append(OrderLine(mi, qty, temperature, decaf, oatmilk, unit_price))
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


@db_transaction.atomic
def checkout(
    *,
    member: Member | None,
    gross_amount: int,
    points_to_use: int,
    payment_method: str,
    items: list | None = None,
    toss_payment_key: str = "",
    toss_order_id: str = "",
) -> CheckoutResult:
    """
    결제 확정 전체 플로우(원자적):
    (메뉴 항목→옵션 단가·총액·세트할인) → 견적 → Toss 승인 →
    포인트 사용/적립 → 스탬프·등급·미션.
    items가 주어지면 서버가 총액·할인을 계산한다(gross_amount 대체).
    """
    store = member.store if member else Store.objects.first()
    if store is None:
        raise CheckoutError("매장 설정이 없습니다. seed_demo를 실행하세요.")

    resolved = resolve_order(items, store)
    discount = 0
    if resolved:
        gross_amount = resolved.gross
        discount = resolved.discount

    quote = build_quote(member, gross_amount, points_to_use, discount)

    # 멱등: 동일 order_id가 이미 완료됐으면 거절(409 매핑).
    if toss_order_id and Transaction.objects.filter(
        toss_order_id=toss_order_id, status=Transaction.Status.PAID
    ).exists():
        raise CheckoutError("이미 처리된 주문입니다.")

    txn = Transaction.objects.create(
        store=store,
        member=member,
        gross_amount=quote.gross_amount,
        discount=quote.discount,
        points_used=quote.points_used,
        net_amount=quote.net_amount,
        points_earned=quote.points_earned,
        payment_method=payment_method,
        toss_order_id=toss_order_id,
        status=Transaction.Status.PENDING,
    )

    # ── 결제 승인 (Toss / 현금) ──
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
    txn.save()

    # ── 주문 항목 기록(메뉴·옵션 스냅샷) ──
    if resolved:
        for l in resolved.lines:
            OrderItem.objects.create(
                transaction=txn, menu_item=l.menu_item, name=l.menu_item.name,
                unit_price=l.unit_price, quantity=l.quantity,
                temperature=l.temperature, decaf=l.decaf, oatmilk=l.oatmilk,
            )

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
