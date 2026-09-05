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
    Coupon,
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
from .quests import evaluate_and_award
from .rewards import (
    REFERRAL_DAILY_LIMIT,
    REFERRAL_REWARD,
    earn_multiplier,
    issue_coupon,
    spin_roulette,
)
from .streaks import grant_spins

# 매장 공통 미션을 전부 달성했을 때 한 번 주는 보너스
MISSION_CLEAR_BONUS = 1000


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
    size_up: bool
    unit_price: int  # 옵션 포함
    unit_cost: int = 0  # 재료원가(옵션 원가 포함) 스냅샷

    @property
    def line_total(self) -> int:
        return self.unit_price * self.quantity


@dataclass
class ResolvedOrder:
    lines: list  # list[OrderLine]
    gross: int
    discount: int
    set_pairs: int = 0   # 세트 할인이 가능한 쌍 수(눌렀을 때만 discount 에 반영)


@dataclass
class CheckoutResult:
    transaction: Transaction
    rewards: list[dict] = field(default_factory=list)
    # 동일 order_id 재시도(네트워크 재전송 등)로 기존 거래를 그대로 돌려준 경우 True.
    idempotent_replay: bool = False


def calc_points_earned(net_amount: int, store: Store, when=None) -> int:
    """실결제액 기준 적립 포인트(반올림). 해피아워면 배수를 적용한다."""
    rate = Decimal(store.point_earn_rate) * earn_multiplier(store, when)
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


def resolve_order(
    items: list | None, store: Store, set_discount: bool = False
) -> ResolvedOrder | None:
    """
    주문 항목([{menu_item_id, quantity, temperature, decaf, oatmilk}])을 검증하고
    옵션 포함 단가·총액을 계산. 총액은 서버가 계산(위변조 방지).

    세트 할인은 **직원이 눌렀을 때만**(set_discount=True) 붙는다.
    자동으로 먹이면 손님이 모르는 채 할인이 나가고, 안 깎아도 될 주문까지
    깎인다. 금액은 min(음료수, 디저트수) × store.set_discount_amount.
    """
    if not items:
        return None
    opt = store.option_price
    opt_cost = store.option_cost
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
        # 사이즈업 추가금이 0인 메뉴는 사이즈업을 팔지 않는다
        size_up = bool(raw.get("size_up")) and mi.size_up_price > 0
        temperature = (raw.get("temperature") or "").lower()
        if mi.temp_option == MenuItem.Temp.HOTICE:
            if temperature not in ("hot", "ice"):
                temperature = "ice"  # 기본 아이스
        elif mi.temp_option == MenuItem.Temp.ICE:
            temperature = "ice"
        else:
            temperature = ""

        n_opts = int(decaf) + int(oatmilk) + int(shot)
        unit_price = mi.price + opt * n_opts + (mi.size_up_price if size_up else 0)
        # 재료원가 스냅샷: 메뉴 원가 + 옵션당 추가 원가.
        # 사이즈업 원가는 아직 따로 두지 않았다 — 재료 마스터를 붙일 때 함께 정리한다.
        unit_cost = mi.cost + opt_cost * n_opts
        lines.append(OrderLine(mi, qty, temperature, decaf, oatmilk, shot, size_up,
                               unit_price, unit_cost))
        if mi.category == MenuItem.Category.DESSERT:
            dessert_qty += qty
        else:
            drink_qty += qty

    if not lines:
        raise CheckoutError("주문 항목이 비어 있습니다.")

    gross = sum(l.line_total for l in lines)
    pairs = min(drink_qty, dessert_qty)
    discount = pairs * store.set_discount_amount if set_discount else 0
    return ResolvedOrder(
        lines=lines, gross=gross, discount=discount, set_pairs=pairs
    )


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
    """활성 미션 진행 갱신, 달성 시 보너스 적립. 전부 깨면 추가 보너스."""
    missions = list(Mission.objects.filter(store=member.store, is_active=True))
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
    _award_mission_clear(member, txn, missions, rewards)


def _award_mission_clear(member, txn, missions, rewards) -> None:
    """미션을 전부 달성하면 한 번만 완주 보너스 — 낱개 보상엔 없는 매듭."""
    from .models import MemberQuest

    if not missions:
        return
    done = MemberMission.objects.filter(
        member=member, mission__in=missions, is_completed=True
    ).count()
    if done < len(missions):
        return
    _, created = MemberQuest.objects.get_or_create(
        member=member, key="mission:clear",
        defaults={"kind": "mission_clear", "title": "미션 전부 달성",
                  "reward_points": MISSION_CLEAR_BONUS},
    )
    if not created:
        return
    _record_point(member, txn, MISSION_CLEAR_BONUS, PointEntry.Reason.MISSION)
    rewards.append({"type": "mission_clear", "title": "미션 전부 달성",
                    "points": MISSION_CLEAR_BONUS})


def _apply_stamp_and_tier(member: Member, txn: Transaction, rewards: list[dict]) -> None:
    """스탬프 +1, 목표 도달 시 룰렛 기회. 등급 재계산 + 승급 쿠폰."""
    store = member.store
    member.stamps += 1
    if store.stamp_goal and member.stamps >= store.stamp_goal:
        member.stamps = 0
        # 여기서 바로 돌리지 않는다 — 손님이 자기 폰에서 직접 돌려야 재미가 산다.
        member.spins += 1
        rewards.append({
            "type": "spin",
            "title": f"스탬프 {store.stamp_goal}개 완성",
            "description": "룰렛 기회 1번! 멤버십에서 돌려보세요",
            "points": 0,
        })
    rewards.extend(grant_spins(member))
    _apply_tier(member, rewards)


def _apply_tier(member: Member, rewards: list[dict]) -> None:
    """등급 재계산. 처음 올라간 등급이면 1+1 쿠폰을 지급한다."""
    order = [Member.Tier.BRONZE, Member.Tier.SILVER, Member.Tier.GOLD]
    member.tier = member.compute_tier()
    if order.index(member.tier) <= order.index(member.tier_rewarded):
        return                                  # 이미 받은 등급(강등 후 재승급 포함)
    for tier in order[order.index(member.tier_rewarded) + 1: order.index(member.tier) + 1]:
        count = Member.TIER_COUPONS.get(tier, 0)
        for _ in range(count):
            issue_coupon(member, Coupon.Kind.BOGO, Coupon.Source.TIER,
                         note=f"{Member.Tier(tier).label} 승급")
        if count:
            rewards.append({
                "type": "tier",
                "title": f"{Member.Tier(tier).label} 등급 달성",
                "description": f"음료 1+1 쿠폰 {count}장",
                "points": 0,
            })
    member.tier_rewarded = member.tier


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
    coupon_id=None,
    discount_pct: int = 0,
    set_discount: bool = False,
    split_method: str = "",
    split_amount: int = 0,
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
            coupon_id=coupon_id,
            discount_pct=discount_pct,
            set_discount=set_discount,
            split_method=split_method,
            split_amount=split_amount,
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
    coupon_id=None,
    discount_pct: int = 0,
    set_discount: bool = False,
    split_method: str = "",
    split_amount: int = 0,
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

    resolved = resolve_order(items, store, set_discount=set_discount)
    discount = 0
    if resolved:
        gross_amount = resolved.gross
        discount = resolved.discount
    # 쿠폰 할인은 세트 할인 위에 더한다(포인트 사용보다는 먼저 — 적립은 실결제액 기준)
    coupon, coupon_amount = resolve_coupon(member, coupon_id, resolved.lines if resolved else [])
    # 직원이 결제 화면에서 누른 수기 할인. 쿠폰과 같은 기준(주문 총액)으로 계산해
    # "5%가 왜 금액마다 다르지?"가 생기지 않게 한다.
    manual_pct = _valid_discount_pct(store, discount_pct)
    manual_amount = gross_amount * manual_pct // 100
    discount = min(gross_amount, discount + coupon_amount + manual_amount)

    quote = build_quote(member, gross_amount, points_to_use, discount)

    sp_method, sp_amount = _valid_split(
        payment_method, split_method, split_amount, quote.net_amount
    )
    txn = Transaction.objects.create(
        store=store,
        member=member,
        gross_amount=quote.gross_amount,
        discount=quote.discount,
        points_used=quote.points_used,
        net_amount=quote.net_amount,
        points_earned=quote.points_earned,
        manual_discount_pct=manual_pct,
        payment_method=payment_method,
        split_method=sp_method,
        split_amount=sp_amount,
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

    if coupon is not None:
        # 결제가 확정되는 순간에만 소진한다. 같은 거래 안이므로 승인 실패 시 함께 되돌아간다.
        used = Coupon.objects.filter(pk=coupon.pk, used_at__isnull=True).update(
            used_at=timezone.now(), used_transaction=txn
        )
        if not used:
            raise CouponError("이미 사용한 쿠폰입니다.")   # 동시 요청

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
                unit_price=l.unit_price, unit_cost=l.unit_cost, quantity=l.quantity,
                temperature=l.temperature, decaf=l.decaf, oatmilk=l.oatmilk, shot=l.shot,
                size_up=l.size_up,
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

    # ── 개인 맞춤 퀘스트 (그 사람 데이터에서 생성된 도전) ──
    member.save()          # 방문·누적을 먼저 반영해야 퀘스트 진행률이 맞다
    rewards.extend(evaluate_and_award(member, txn, _record_point))

    member.save()
    return CheckoutResult(transaction=txn, rewards=rewards)


def _valid_split(payment_method, split_method, split_amount, net_amount):
    """
    분할 결제 검증. 어긋나면 **거절이 아니라 분할 없음**으로 떨어뜨린다.

    수기 할인과 같은 판단이다 — 계산대에서는 결제가 실패하는 것보다
    "어? 분할이 안 걸렸네"를 직원이 바로 알아채는 쪽이 덜 위험하다.
    다만 금액이 어긋난 채로 남으면 정산이 틀어지므로, 애매하면 버린다.
    """
    try:
        split_amount = int(split_amount or 0)
    except (TypeError, ValueError):
        return "", 0
    if not split_method or split_amount <= 0:
        return "", 0
    if split_method == payment_method:
        return "", 0                       # 같은 수단으로 쪼갤 이유가 없다
    if split_method not in dict(Transaction.Method.choices):
        return "", 0
    if split_amount >= net_amount:
        return "", 0                       # 전액이면 그냥 그 수단으로 결제한 것
    return split_method, split_amount


def _valid_discount_pct(store: Store, pct) -> int:
    """
    매장이 허용한 할인율만 받는다.

    클라이언트가 보낸 숫자를 그대로 믿으면 화면을 조작해 100% 할인을 넣을 수
    있다. 허용 목록에 없는 값은 **거절이 아니라 0** — 결제가 실패하는 것보다
    할인이 안 먹는 쪽이 계산대에서 덜 위험하다(직원이 바로 알아챈다).
    """
    try:
        pct = int(pct or 0)
    except (TypeError, ValueError):
        return 0
    return pct if pct in store.discount_rate_list else 0


class CouponError(Exception):
    """쿠폰을 쓸 수 없는 상태(만료·사용됨·다른 회원)."""


def coupon_discount(coupon: Coupon, lines) -> int:
    """
    쿠폰이 깎아 주는 금액. 주문 내용에 따라 달라지므로 결제 시점에 계산한다.

    - 5%·10% 할인: 주문 총액에서 그 비율(원 단위 버림)
    - 음료 1+1: 음료 2잔 이상일 때 **싼 쪽 1잔** 값
    - 무료 음료: 음료 중 **가장 비싼 1잔** 값
    - 원두 200g: 물건으로 나가므로 금액 할인은 0
    """
    drinks = sorted(
        (l.unit_price for l in lines
         for _ in range(l.quantity)
         if l.menu_item.category != MenuItem.Category.DESSERT)
    )
    if coupon.discount_pct:
        gross = sum(l.line_total for l in lines)
        return gross * coupon.discount_pct // 100
    if coupon.kind == Coupon.Kind.BOGO:
        return drinks[0] if len(drinks) >= 2 else 0
    if coupon.kind == Coupon.Kind.FREE_DRINK:
        return drinks[-1] if drinks else 0
    return 0


def resolve_coupon(member: Member | None, coupon_id, lines) -> tuple[Coupon | None, int]:
    """쿠폰을 검증하고 (쿠폰, 할인액)을 돌려준다. 쓸 수 없으면 CouponError."""
    if not coupon_id:
        return None, 0
    if member is None:
        raise CouponError("쿠폰은 회원 주문에만 사용할 수 있어요.")
    coupon = Coupon.objects.filter(pk=coupon_id, member=member).first()
    if coupon is None:
        raise CouponError("이 회원의 쿠폰이 아닙니다.")
    if coupon.used_at is not None:
        raise CouponError("이미 사용한 쿠폰입니다.")
    if coupon.is_expired:
        raise CouponError("사용 기한이 지난 쿠폰입니다.")
    amount = coupon_discount(coupon, lines or [])
    if amount <= 0:
        if coupon.kind == Coupon.Kind.BOGO:
            raise CouponError("1+1 쿠폰은 음료 2잔 이상일 때 사용할 수 있어요.")
        if coupon.kind == Coupon.Kind.FREE_DRINK:
            raise CouponError("무료 음료 쿠폰은 음료가 있어야 사용할 수 있어요.")
        raise CouponError("이 주문에는 적용할 수 없는 쿠폰입니다.")
    return coupon, amount


class SpinError(Exception):
    """룰렛을 돌릴 수 없는 상태."""


@db_transaction.atomic
def spin(member: Member) -> dict:
    """
    룰렛 1회 — 기회를 1 차감하고 쿠폰을 발행한다.

    잠금을 걸고 기회를 확인하는 이유: 손님이 버튼을 두 번 누르거나 두 기기에서
    동시에 열면 기회 1번으로 쿠폰이 두 장 나올 수 있다.
    """
    me = Member.objects.select_for_update().get(pk=member.pk)
    if me.spins <= 0:
        raise SpinError("룰렛 기회가 없어요.")

    kind, index = spin_roulette()
    coupon = issue_coupon(me, kind, Coupon.Source.ROULETTE)
    me.spins -= 1
    me.save(update_fields=["spins"])
    member.spins = me.spins
    return {
        "index": index,
        "coupon": {
            "id": coupon.id,
            "kind": coupon.kind,
            "name": coupon.get_kind_display(),
            "expires_at": coupon.expires_at,
        },
        "spins_left": me.spins,
    }


class ReferralError(Exception):
    """초대 코드 적용 실패."""


@db_transaction.atomic
def apply_referral(member: Member, code: str) -> dict:
    """
    친구 초대 코드 적용 — 초대한 사람과 받은 사람 모두에게 포인트.

    받는 쪽은 한 번만 쓸 수 있고, 자기 코드는 쓸 수 없다. 이미 방문이 많은
    회원이 뒤늦게 쓰는 걸 막기 위해 **첫 방문 전후(방문 3회 이하)** 로 제한한다.
    초대하는 쪽은 **하루 1명** 까지만 — 코드를 뿌려 하루에 수십 명을 넣는 걸 막는다.
    """
    code = (code or "").strip().upper()
    if not code:
        raise ReferralError("초대 코드를 입력해 주세요.")

    me = Member.objects.select_for_update().get(pk=member.pk)
    if me.referred_by_id is not None:
        raise ReferralError("이미 초대 코드를 사용하셨어요.")
    if me.visit_count > 3:
        raise ReferralError("초대 코드는 가입 초기에만 사용할 수 있어요.")

    host = Member.objects.select_for_update().filter(referral_code=code).first()
    if host is None:
        raise ReferralError("없는 초대 코드예요.")
    if host.pk == me.pk:
        raise ReferralError("자신의 코드는 사용할 수 없어요.")

    today = timezone.localtime(timezone.now()).date()
    today_count = Member.objects.filter(
        referred_by=host, referral_used_at__date=today
    ).count()
    if today_count >= REFERRAL_DAILY_LIMIT:
        raise ReferralError(
            f"이 코드는 오늘 몫을 다 썼어요. 초대는 하루 {REFERRAL_DAILY_LIMIT}명까지예요."
        )

    me.referred_by = host
    me.referral_used_at = timezone.now()
    _record_point(me, None, REFERRAL_REWARD, PointEntry.Reason.REFERRAL)
    me.save(update_fields=["referred_by", "referral_used_at", "points"])
    _record_point(host, None, REFERRAL_REWARD, PointEntry.Reason.REFERRAL)
    host.save(update_fields=["points"])
    return {
        "reward": REFERRAL_REWARD,
        "host": host.name,
        "points": me.points,
    }


class PointGrantError(Exception):
    """수기 포인트 지급/회수 실패."""


@db_transaction.atomic
def grant_points(member: Member, delta: int, note: str = "") -> Member:
    """
    수기 포인트 지급·회수. **잔액과 원장을 함께 옮긴다.**

    잔액만 만지면 손님 화면의 '적립 내역'과 잔액이 서로 다른 말을 하게 된다
    (정합성 점검기가 잡는 바로 그 어긋남). 지급 경로는 여기 하나로 모은다.
    """
    delta = int(delta or 0)
    if delta == 0:
        raise PointGrantError("0P는 지급할 수 없습니다.")
    me = Member.objects.select_for_update().get(pk=member.pk)
    if me.points + delta < 0:
        raise PointGrantError(
            f"보유 포인트({me.points:,}P)보다 많이 회수할 수 없습니다."
        )
    _record_point(me, None, delta, PointEntry.Reason.ADJUST)
    me.save(update_fields=["points"])
    return me


@db_transaction.atomic
def prepaid_charge(
    member: Member, amount: int, payment_method: str, *,
    approval_no: str = "", toss_order_id: str = "",
) -> tuple[Transaction, Member]:
    """
    선결제(충전) — 낸 금액을 그대로 포인트로 넣어 준다.

    **3% 적립은 붙이지 않는다.** 충전 자체가 포인트를 주는 행위라
    여기에 적립까지 얹으면 이중이다(3만원 충전에 30,900P가 나간다).
    매출은 정상적으로 잡히고, 손님이 그 포인트를 쓸 때 다시 깎이지 않는다.
    """
    amount = int(amount or 0)
    if amount <= 0:
        raise CheckoutError("선결제 금액은 0보다 커야 합니다.")
    if member is None:
        raise CheckoutError("선결제는 회원에게만 가능합니다.")
    if payment_method not in dict(Transaction.Method.choices):
        raise CheckoutError("결제수단이 올바르지 않습니다.")

    me = Member.objects.select_for_update().get(pk=member.pk)
    txn = Transaction.objects.create(
        store=me.store, member=me,
        gross_amount=amount, discount=0, points_used=0,
        net_amount=amount, points_earned=0,      # 적립 없음 — 위 설명 참고
        payment_method=payment_method,
        approval_no=approval_no, toss_order_id=toss_order_id,
        status=Transaction.Status.PAID, paid_at=timezone.now(),
    )
    _record_point(me, txn, amount, PointEntry.Reason.ADJUST)
    # 누적 결제액·방문은 올린다 — 실제로 결제한 돈이고 등급 근거가 된다.
    me.total_spent += amount
    me.visit_count += 1
    me.tier = me.compute_tier()
    me.save(update_fields=["points", "total_spent", "visit_count", "tier"])
    return txn, me


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

    # 쿠폰 원복 — 취소했는데 쿠폰만 사라지면 손님이 손해를 본다
    Coupon.objects.filter(used_transaction=txn).update(used_at=None, used_transaction=None)

    # 재고 원복 (F식으로 원자적 증가 — 동시 판매와 충돌해도 유실 없음)
    for it in txn.items.select_related("menu_item").all():
        if it.menu_item and it.menu_item.stock is not None:
            MenuItem.objects.filter(
                pk=it.menu_item.pk, stock__isnull=False
            ).update(stock=F("stock") + it.quantity)

    txn.status = Transaction.Status.CANCELED
    txn.save(update_fields=["status"])
    return txn
