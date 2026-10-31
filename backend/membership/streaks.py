"""
연속 방문(스트릭) 계산과 룰렛 기회 지급.

두 가지를 따로 센다 — 성격이 다르기 때문이다.
- **일 연속**: 매일 오시는 분. 5일이면 룰렛 1회.
- **주 연속**: 주에 한 번씩 꾸준한 분. 4주면 룰렛 1회.

매일 오는 손님만 보상하면 주 1회 단골이 소외되고, 주 단위만 보면 매일 오는
손님이 심심하다. 둘 다 열어 두고 각자의 리듬으로 도전하게 한다.

지급 기록은 MemberQuest 에 남긴다(키가 안정적이고 중복 지급을 막아 준다).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import Transaction
from .rewards import DAILY_STREAK_GOAL, WEEKLY_STREAK_GOAL


def _visit_days(member) -> list:
    """방문한 날짜(중복 제거, 최신순)."""
    return sorted(
        {
            timezone.localtime(t).date()
            for t in member.transactions.filter(
                status=Transaction.Status.PAID, paid_at__isnull=False
            ).values_list("paid_at", flat=True)
        },
        reverse=True,
    )


def daily_streak(member, today=None) -> dict:
    """
    며칠 연속으로 오셨는가.

    오늘 아직 안 오셨어도 어제까지 이어졌다면 살아 있다 — 오늘 오시면 연장된다.
    """
    days = _visit_days(member)
    today = today or timezone.localtime(timezone.now()).date()
    if not days or days[0] < today - timedelta(days=1):
        return {"days": 0, "alive": False, "visited_today": False}

    streak, cursor = 0, days[0]
    for d in days:
        if d == cursor:
            streak += 1
            cursor -= timedelta(days=1)
        elif d < cursor:
            break
    return {"days": streak, "alive": True, "visited_today": days[0] == today}


def weekly_streak(member, today=None) -> dict:
    """몇 주 연속으로 오셨는가(주 1회 이상이면 그 주는 채운 것)."""
    today = today or timezone.localtime(timezone.now()).date()

    def monday(d):
        return d - timedelta(days=d.weekday())

    weeks = sorted({monday(d) for d in _visit_days(member)}, reverse=True)
    if not weeks:
        return {"weeks": 0, "alive": False, "visited_this_week": False}
    this_week = monday(today)
    if weeks[0] not in (this_week, this_week - timedelta(days=7)):
        return {"weeks": 0, "alive": False, "visited_this_week": False}

    streak, cursor = 0, weeks[0]
    for w in weeks:
        if w == cursor:
            streak += 1
            cursor -= timedelta(days=7)
        elif w < cursor:
            break
    return {"weeks": streak, "alive": True, "visited_this_week": weeks[0] == this_week}


def build(member, today=None) -> dict:
    """멤버십 상단에 띄울 스트릭 요약."""
    d = daily_streak(member, today)
    w = weekly_streak(member, today)
    return {
        "daily": {**d, "goal": DAILY_STREAK_GOAL,
                  "left": max(0, DAILY_STREAK_GOAL - d["days"])},
        "weekly": {**w, "goal": WEEKLY_STREAK_GOAL,
                   "left": max(0, WEEKLY_STREAK_GOAL - w["weeks"])},
        # 이전 화면과의 호환 — 배지 판정이 streak["weeks"] 를 쓴다
        "weeks": w["weeks"],
        "alive": w["alive"],
        "visited_this_week": w["visited_this_week"],
    }


def _cycle_key(kind: str, count: int, goal: int) -> str:
    """
    n번째 달성마다 한 번씩 지급하기 위한 키.

    5일 연속에서 멈추지 않고 10일, 15일에도 다시 받게 한다 — 한 번 받고 나면
    이어갈 이유가 없어지는 게 연속 방문 보상에서 가장 흔한 실패다.
    """
    return f"streak:{kind}:{(count // goal)}"


def grant_spins(member, today=None) -> list[dict]:
    """
    연속 방문 조건을 채웠으면 룰렛 기회를 준다. 지급한 보상 목록을 돌려준다.
    (결제 직후 호출 — member.spins 를 올리고 저장은 호출자가 한다)
    """
    from .models import MemberQuest

    out: list[dict] = []
    checks = (
        ("daily", daily_streak(member, today)["days"], DAILY_STREAK_GOAL, "일 연속 방문"),
        ("weekly", weekly_streak(member, today)["weeks"], WEEKLY_STREAK_GOAL, "주 연속 방문"),
    )
    for kind, count, goal, label in checks:
        if count < goal:
            continue
        key = _cycle_key(kind, count, goal)
        title = f"{goal}{'일' if kind == 'daily' else '주'} 연속 방문"
        _, created = MemberQuest.objects.get_or_create(
            member=member, key=key,
            defaults={"kind": f"streak_{kind}", "title": title, "reward_points": 0},
        )
        if not created:
            continue
        member.spins += 1
        out.append({"type": "spin", "title": title,
                    "description": f"{label} {count}회 — 룰렛 기회 1번!", "points": 0})
    return out
