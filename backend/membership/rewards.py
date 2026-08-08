"""
게이미피케이션 보상 규칙 — 룰렛 · 해피아워 · 초대 · 등급.

**룰렛 보상은 포인트가 아니라 쿠폰이다.** 포인트로 주면 그대로 현금성 할인이
되어 매출의 몇 %가 고정으로 빠지지만, 쿠폰은 재방문해야 쓸 수 있고 종류마다
원가가 달라 매장이 부담을 조절할 수 있다.
"""
from __future__ import annotations

import random
from decimal import Decimal

from .models import Coupon

# 룰렛 칸: (쿠폰 종류, 확률 %)
#
# 사장님 지정: **할인쿠폰(5%·10%) 합쳐서 60%**, 1+1과 무료음료 각 20%,
# 원두 200g은 0%. 할인 60%를 두 장으로 나눌 때 싼 쪽(5%)에 더 실었다 —
# 40/20 이 아니라 30/30 을 원하시면 이 두 줄만 바꾸면 된다.
#
# 원두 200g은 **확률 0** — 화면에는 보이지만 당첨되지 않는 '보여주기용' 칸이다.
# 눈에 보이는 큰 상품이 있어야 돌릴 맛이 나고, 실제 지급 부담은 지지 않는다.
ROULETTE = (
    (Coupon.Kind.DISCOUNT_5, 40),
    (Coupon.Kind.DISCOUNT_10, 20),
    (Coupon.Kind.BOGO, 20),
    (Coupon.Kind.FREE_DRINK, 20),
    (Coupon.Kind.BEANS_200, 0),
)

# 룰렛 칸 표시용 (라벨, 부제)
WHEEL_LABELS = {
    Coupon.Kind.DISCOUNT_5: ("5%", "할인 쿠폰"),
    Coupon.Kind.DISCOUNT_10: ("10%", "할인 쿠폰"),
    Coupon.Kind.BOGO: ("1+1", "음료 쿠폰"),
    Coupon.Kind.FREE_DRINK: ("FREE", "무료 음료"),
    Coupon.Kind.BEANS_200: ("원두", "200g"),
}

# 친구 초대 보상 (초대한 사람 / 초대받은 사람 각각)
REFERRAL_REWARD = 1000
# 초대는 하루에 한 명까지만 — 지인끼리 코드를 돌려쓰는 걸 막는다
REFERRAL_DAILY_LIMIT = 1

# 룰렛 기회를 주는 연속 방문 조건
DAILY_STREAK_GOAL = 5     # 5일 연속 방문
WEEKLY_STREAK_GOAL = 4    # 4주 연속(주 1회 이상) 방문


def wheel_segments() -> list[dict]:
    """화면에 그릴 룰렛 칸 목록(순서 고정). 확률은 내려보내지 않는다."""
    labels = dict(Coupon.Kind.choices)
    out = []
    for kind, _weight in ROULETTE:
        title, sub = WHEEL_LABELS[kind]
        out.append({"kind": kind, "label": title, "sub": sub, "name": labels[kind]})
    return out


def spin_roulette(rng: random.Random | None = None) -> tuple[str, int]:
    """
    룰렛 1회. 반환: (당첨 쿠폰 종류, 당첨 칸 index)

    당첨은 **서버가 정한다** — 화면은 결과 칸으로 돌아가는 연출만 한다.
    (클라이언트가 결과를 고르면 조작될 수 있다)
    """
    rng = rng or random.Random()
    kinds = [k for k, _ in ROULETTE]
    weights = [w for _, w in ROULETTE]
    idx = rng.choices(range(len(kinds)), weights=weights, k=1)[0]
    return kinds[idx], idx


def issue_coupon(member, kind, source=Coupon.Source.ROULETTE, note="") -> Coupon:
    return Coupon.objects.create(member=member, kind=kind, source=source, note=note)


def earn_multiplier(store, when=None) -> Decimal:
    """지금 적용할 적립 배수(해피아워면 >1)."""
    if store is not None and store.is_happy_hour(when):
        return Decimal(store.happy_multiplier)
    return Decimal(1)
