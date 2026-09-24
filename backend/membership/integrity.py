"""
데이터 정합성 점검 — 돈과 관련된 불변식이 깨졌는지 본다.

여기 있는 검사는 **버그를 잡기 위한 것이 아니라 손해를 잡기 위한 것**이다.
포인트 잔액이 원장과 어긋나면 손님에게 줄 돈이 틀어지고, 거래 금액이
어긋나면 정산이 틀어진다. 조용히 틀어진 채로 굴러가는 게 가장 위험하다.

서버리스에는 셸이 없어 `manage.py`를 못 돌리므로, 같은 함수를
`GET /api/v1/integrity`(점주 전용)로도 노출한다.
"""
from __future__ import annotations

from django.db.models import Count, F, Q, Sum

from .models import Coupon, Member, Mission, OrderItem, PointEntry, Store, Transaction

# 미션은 코드 상수가 아니라 DB 행이라, 시드를 고쳐도 이미 만들어진 매장의
# 행은 옛 금액을 그대로 들고 있는다(2026-08-08 이후 운영에 5,000P 짜리 미션이
# 남아 있던 이유). 코드가 의도한 금액을 여기 적어 두고 실제 행과 대조한다.
# (조건, 목표값) → (제목, 보상)
INTENDED_MISSIONS = {
    ("visit_count", 5): ("이번 시즌 5회 방문", 500),
    ("visit_count", 10): ("단골 인증 10회 방문", 500),
    ("total_spent", 50000): ("누적 5만원 달성", 500),
}


def _rows(qs, limit, fmt):
    """검사 결과를 보기 좋게. 너무 많으면 앞부분만 — 화면을 덮지 않게."""
    out = [fmt(o) for o in qs[:limit]]
    return out


def check_point_ledger(limit=20) -> dict:
    """
    회원 잔액 == 원장 합계.

    원장이 진실의 원천이므로, 어긋나면 **잔액 쪽이 틀린 것**이다.
    """
    bad = []
    agg = {
        r["member_id"]: r["s"] or 0
        for r in PointEntry.objects.values("member_id").annotate(s=Sum("delta"))
    }
    for m in Member.objects.only("id", "name", "phone", "points").iterator():
        ledger = agg.get(m.id, 0)
        if m.points != ledger:
            bad.append({
                "member_id": m.id, "name": m.name, "phone": m.phone,
                "balance": m.points, "ledger": ledger, "diff": m.points - ledger,
            })
    return {
        "name": "포인트 잔액 vs 원장",
        "bad": len(bad),
        "sample": bad[:limit],
        "hint": "원장이 진실의 원천 — 잔액을 원장 합계로 맞춰야 한다.",
    }


def check_transaction_amounts(limit=20) -> dict:
    """실결제액 == 총액 − 할인 − 사용포인트."""
    qs = Transaction.objects.exclude(
        net_amount=F("gross_amount") - F("discount") - F("points_used")
    )
    return {
        "name": "거래 금액 계산",
        "bad": qs.count(),
        "sample": _rows(qs, limit, lambda t: {
            "id": t.id, "gross": t.gross_amount, "discount": t.discount,
            "points_used": t.points_used, "net": t.net_amount,
            "expected": t.gross_amount - t.discount - t.points_used,
        }),
        "hint": "net = gross − discount − points_used 여야 한다.",
    }


def check_split_payments(limit=20) -> dict:
    """분할 금액은 실결제액보다 작아야 하고, 주 수단과 달라야 한다."""
    qs = Transaction.objects.filter(split_amount__gt=0).filter(
        Q(split_amount__gte=F("net_amount"))
        | Q(split_method="")
        | Q(split_method=F("payment_method"))
    )
    return {
        "name": "분할 결제",
        "bad": qs.count(),
        "sample": _rows(qs, limit, lambda t: {
            "id": t.id, "net": t.net_amount,
            "method": t.payment_method,
            "split_method": t.split_method, "split_amount": t.split_amount,
        }),
        "hint": "수단별 집계가 실제 입금과 어긋난다.",
    }


def check_order_totals(limit=20) -> dict:
    """주문 항목 합계 == 거래 총액. (항목이 있는 거래만)"""
    bad = []
    sums = {
        r["transaction_id"]: r["s"] or 0
        for r in OrderItem.objects.values("transaction_id").annotate(
            s=Sum(F("unit_price") * F("quantity"))
        )
    }
    qs = Transaction.objects.filter(id__in=sums.keys()).only("id", "gross_amount")
    for t in qs.iterator():
        if sums[t.id] != t.gross_amount:
            bad.append({"id": t.id, "gross": t.gross_amount, "items_sum": sums[t.id]})
    return {
        "name": "주문 항목 합계",
        "bad": len(bad),
        "sample": bad[:limit],
        "hint": "메뉴 가격을 바꿔도 과거 거래는 변하면 안 된다(단가 스냅샷).",
    }


def check_member_totals(limit=20) -> dict:
    """
    누적 결제액·방문수 == 이관 기준선 + 우리 앱 결제완료분.

    등급이 이 값으로 정해지므로 어긋나면 등급·쿠폰이 잘못 나간다.
    """
    bad = []
    agg = {
        r["member_id"]: r
        for r in Transaction.objects.filter(status=Transaction.Status.PAID)
        .values("member_id")
        .annotate(n=Count("id"), s=Sum("net_amount"))
    }
    for m in Member.objects.only(
        "id", "name", "total_spent", "visit_count",
        "baseline_total_spent", "baseline_visit_count",
    ).iterator():
        a = agg.get(m.id) or {"n": 0, "s": 0}
        spent = m.baseline_total_spent + (a["s"] or 0)
        visits = m.baseline_visit_count + a["n"]
        if m.total_spent != spent or m.visit_count != visits:
            bad.append({
                "member_id": m.id, "name": m.name,
                "total_spent": m.total_spent, "expected_spent": spent,
                "visit_count": m.visit_count, "expected_visits": visits,
            })
    return {
        "name": "누적 결제액 · 방문수",
        "bad": len(bad),
        "sample": bad[:limit],
        "hint": "등급이 이 값으로 정해진다 — 어긋나면 등급·쿠폰이 잘못 나간다.",
    }


def check_coupons(limit=20) -> dict:
    """사용 표시와 사용 거래는 함께 있거나 함께 없어야 한다."""
    qs = Coupon.objects.filter(
        Q(used_at__isnull=False, used_transaction__isnull=True)
        | Q(used_at__isnull=True, used_transaction__isnull=False)
    )
    return {
        "name": "쿠폰 사용 표시",
        "bad": qs.count(),
        "sample": _rows(qs, limit, lambda c: {
            "id": c.id, "kind": c.kind,
            "used_at": c.used_at.isoformat() if c.used_at else None,
            "used_transaction": c.used_transaction_id,
        }),
        "hint": "한쪽만 남으면 쿠폰이 되살아나거나 영영 잠긴다.",
    }


def check_paid_without_time(limit=20) -> dict:
    """결제완료인데 결제 시각이 없는 건 — 날짜별 집계에서 통째로 샌다."""
    qs = Transaction.objects.filter(
        status=Transaction.Status.PAID, paid_at__isnull=True
    )
    return {
        "name": "결제완료 · 시각 없음",
        "bad": qs.count(),
        "sample": _rows(qs, limit, lambda t: {"id": t.id, "net": t.net_amount}),
        "hint": "paid_at 이 없으면 일자별 매출에서 빠진다.",
    }


def check_duplicate_orders(limit=20) -> dict:
    """같은 주문번호로 결제완료가 둘 이상 — 중복 결제."""
    dups = (
        Transaction.objects.filter(status=Transaction.Status.PAID)
        .exclude(toss_order_id="")
        .values("toss_order_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    return {
        "name": "중복 결제(주문번호)",
        "bad": dups.count(),
        "sample": list(dups[:limit]),
        "hint": "DB 유니크 제약이 걸려 있어야 정상적으로 0이다.",
    }


def check_negative_points(limit=20) -> dict:
    """잔액이 음수인 회원 — 어딘가에서 과다 차감됐다."""
    qs = Member.objects.filter(points__lt=0)
    return {
        "name": "포인트 잔액 음수",
        "bad": qs.count(),
        "sample": _rows(qs, limit, lambda m: {
            "member_id": m.id, "name": m.name, "points": m.points,
        }),
        "hint": "사용 한도 검사가 새고 있다는 뜻.",
    }


def check_reward_config(limit=20) -> dict:
    """
    포인트를 주는 **설정값**이 코드가 의도한 값과 같은가.

    다른 검사들과 성격이 다르다 — 데이터가 깨진 게 아니라 **설정이 조용히
    옛날 값으로 남아 있는** 경우를 잡는다. 운영에는 셸이 없어 `manage.py`로
    확인할 수 없고, 개발 DB 는 다시 시드하면 최신값이 되어 차이가 안 보인다.
    실제로 이 구멍으로 5,000P 짜리 미션이 한참 살아 있었다.
    """
    bad = []
    seen = set()
    for m in Mission.objects.all():
        key = (m.condition_type, m.target_value)
        seen.add(key)
        want = INTENDED_MISSIONS.get(key)
        if want is None:
            bad.append({"mission_id": m.id, "title": m.title,
                        "issue": "코드에 없는 미션", "reward": m.reward_points})
        elif m.reward_points != want[1]:
            bad.append({"mission_id": m.id, "title": m.title,
                        "issue": "보상 금액 불일치",
                        "reward": m.reward_points, "expected": want[1]})
    # 미션이 하나도 없는 DB(테스트·시드 전)까지 '빠졌다'고 잡으면 소음만 된다.
    # 한 줄이라도 있으면 세 줄이 다 있어야 맞다.
    if seen:
        for key, (title, reward) in INTENDED_MISSIONS.items():
            if key not in seen:
                bad.append({"mission_id": None, "title": title,
                            "issue": "DB 에 없는 미션", "expected": reward})

    for s in Store.objects.all():
        if s.signup_bonus_points != 1000:
            bad.append({"store": s.name, "issue": "가입 축하 포인트",
                        "reward": s.signup_bonus_points, "expected": 1000})
        if s.happy_start or s.happy_end:
            bad.append({"store": s.name, "issue": "해피아워가 켜져 있다",
                        "reward": f"{s.happy_start}~{s.happy_end}시"})

    return {
        "name": "포인트 지급 설정",
        "bad": len(bad),
        "sample": bad[:limit],
        "hint": "DB 행이 옛 값으로 남은 것 — 마이그레이션으로 덮어써야 한다.",
    }


CHECKS = (
    check_point_ledger,
    check_transaction_amounts,
    check_split_payments,
    check_order_totals,
    check_member_totals,
    check_coupons,
    check_paid_without_time,
    check_duplicate_orders,
    check_negative_points,
    check_reward_config,
)


def run_all(limit=20) -> dict:
    results = [fn(limit=limit) for fn in CHECKS]
    total = sum(r["bad"] for r in results)
    return {
        "ok": total == 0,
        "total_problems": total,
        "counts": {
            "members": Member.objects.count(),
            "transactions": Transaction.objects.count(),
            "point_entries": PointEntry.objects.count(),
            "coupons": Coupon.objects.count(),
        },
        "checks": results,
    }
