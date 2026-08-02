"""
게이미피케이션 보상 규칙 — 룰렛 · 해피아워 · 초대.

**비용 설계:** 보상은 그대로 적립비용이 되고, 마진 분석에서 기여이익을 깎는다
(margins.py). 그래서 룰렛의 기댓값을 기존 고정 보상과 거의 같게 맞춰
"재미는 늘리되 비용은 그대로"가 되도록 했다.
"""
from __future__ import annotations

import random
from decimal import Decimal

# 스탬프 완성 룰렛: (보상 포인트, 가중치)
# 기댓값 = 1000*.30 + 2000*.25 + 3000*.25 + 5000*.15 + 10000*.05 = 2,800P
# 기존 고정 보상 3,000P와 비슷해 매장 부담은 사실상 그대로다.
ROULETTE = (
    (1000, 30),
    (2000, 25),
    (3000, 25),
    (5000, 15),
    (10000, 5),
)

# 친구 초대 보상 (초대한 사람 / 초대받은 사람)
REFERRAL_REWARD = 2000


def spin_roulette(rng: random.Random | None = None) -> tuple[int, int, list[int]]:
    """
    룰렛 1회. 반환: (당첨 포인트, 당첨 칸 index, 칸 목록)

    당첨은 **서버가 정한다** — 화면은 결과 칸으로 돌아가는 연출만 한다.
    (클라이언트가 결과를 고르면 조작될 수 있다)
    """
    rng = rng or random.Random()
    segments = [p for p, _ in ROULETTE]
    weights = [w for _, w in ROULETTE]
    idx = rng.choices(range(len(segments)), weights=weights, k=1)[0]
    return segments[idx], idx, segments


def earn_multiplier(store, when=None) -> Decimal:
    """지금 적용할 적립 배수(해피아워면 >1)."""
    if store is not None and store.is_happy_hour(when):
        return Decimal(store.happy_multiplier)
    return Decimal(1)
