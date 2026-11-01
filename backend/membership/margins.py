"""
원가·마진 분석.

합의된 1차 기준:
- **공급가 기준**: 매출은 부가세 제외(공급가 = 매출 ÷ (1+vat_rate))로 인식.
- **적립 시점 인식**: 포인트/스탬프/미션 적립을 "적립된 거래" 시점의 마케팅 비용으로 본다.
  (사용 시점엔 이미 인식한 부채를 정산하는 것이라 마진에서 다시 빼지 않는다 → 이중계상 방지)
- **재료비만**: 인건비·임대료 등 고정비는 이번 범위 밖. 변동비(재료비 + 적립비용)로
  기여이익(contribution margin)을 낸다.

두 가지 관점:
- **기간 기여이익**(margin_summary): 실제 매출(net_amount)의 공급가 − 재료원가 − 적립비용.
  포인트 사용은 이미 net_amount에 반영돼 있고 비용은 적립 시점에 잡으므로 여기선 빼지 않는다.
- **메뉴별 마진**(menu_item_margins): 각 메뉴의 정가(옵션 포함 단가) 공급가 − 재료원가.
  세트할인·포인트는 거래 단위라 개별 메뉴에 배분하지 않는다(상품 자체 수익성 관점).

원가(cost)는 점주가 입력한 매입원가 그대로 사용한다(부가세 미분리) — 1차 단순화.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F, Sum
from django.utils import timezone

from .models import OrderItem, PointEntry, Store, Transaction

# 마진 비용으로 인식하는 포인트 적립 사유(적립 시점 인식).
REWARD_REASONS = (
    PointEntry.Reason.EARN,
    PointEntry.Reason.STAMP,
    PointEntry.Reason.MISSION,
)


def _vat_rate() -> Decimal:
    store = Store.objects.first()
    return Decimal(store.vat_rate) if store else Decimal("0.10")


def to_supply(amount_incl_vat: int, vat: Decimal | None = None) -> int:
    """부가세 포함 금액 → 공급가액(반올림)."""
    if vat is None:
        vat = _vat_rate()
    if amount_incl_vat <= 0:
        return 0
    return int(
        (Decimal(amount_incl_vat) / (Decimal(1) + vat)).quantize(
            Decimal("1"), ROUND_HALF_UP
        )
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def margin_summary(days: int = 30) -> dict:
    """
    기간 기여이익(공급가 매출 − 재료원가 − 적립비용).

    적립비용은 해당 기간에 '결제완료된 거래'에 귀속된 포인트 적립(earn/stamp/mission)의
    합. 취소 거래는 매출·원가·비용 모두에서 제외한다.
    """
    vat = _vat_rate()
    since = timezone.now() - timedelta(days=days)
    paid = Transaction.objects.filter(
        status=Transaction.Status.PAID, paid_at__gte=since
    )

    revenue_incl = paid.aggregate(s=Sum("net_amount"))["s"] or 0
    tx_count = paid.count()

    # 재료원가: 해당 거래들의 주문 항목 원가 합(수량 반영).
    material_cost = (
        OrderItem.objects.filter(transaction__in=paid).aggregate(
            s=Sum(F("unit_cost") * F("quantity"))
        )["s"]
        or 0
    )

    # 적립비용: 해당 거래들에 귀속된 포인트 적립(적립 시점 인식).
    reward_cost = (
        PointEntry.objects.filter(
            transaction__in=paid, reason__in=REWARD_REASONS, delta__gt=0
        ).aggregate(s=Sum("delta"))["s"]
        or 0
    )

    supply = to_supply(revenue_incl, vat)
    contribution = supply - material_cost - reward_cost

    return {
        "days": days,
        "vat_rate": float(vat),
        "tx_count": tx_count,
        "revenue_incl_vat": revenue_incl,
        "supply_revenue": supply,
        "material_cost": material_cost,
        "reward_cost": reward_cost,
        "contribution": contribution,
        "margin_rate": _rate(contribution, supply),
        "cost_rate": _rate(material_cost, supply),
    }


def menu_item_margins(days: int = 30, limit: int | None = None) -> list[dict]:
    """
    메뉴별 마진(정가 공급가 − 재료원가). 판매량 많은 순.

    원가 미입력(unit_cost=0) 항목은 원가율 0%·마진율 100%로 나오므로 UI에서
    '원가 미입력'으로 구분해 표시할 수 있게 has_cost 플래그를 함께 준다.
    """
    vat = _vat_rate()
    since = timezone.now() - timedelta(days=days)
    rows = (
        OrderItem.objects.filter(
            transaction__status=Transaction.Status.PAID,
            transaction__paid_at__gte=since,
        )
        .values("name")
        .annotate(
            qty=Sum("quantity"),
            revenue_incl=Sum(F("unit_price") * F("quantity")),
            cost=Sum(F("unit_cost") * F("quantity")),
        )
        .order_by("-qty")
    )

    result = []
    for r in rows:
        supply = to_supply(r["revenue_incl"] or 0, vat)
        cost = r["cost"] or 0
        margin = supply - cost
        result.append(
            {
                "name": r["name"],
                "qty": r["qty"] or 0,
                "revenue_incl_vat": r["revenue_incl"] or 0,
                "supply_revenue": supply,
                "material_cost": cost,
                "margin": margin,
                "margin_rate": _rate(margin, supply),
                "cost_rate": _rate(cost, supply),
                "has_cost": cost > 0,
            }
        )
    if limit is not None:
        result = result[:limit]
    return result
