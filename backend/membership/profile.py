"""
고객(회원) 대시보드 데이터 조립 — 9.81 Park식 UX 참고.

성취 배지 · 방문 기록 타임라인 · 단골 랭킹(익명) · 다음 등급 진행률을
한 번의 응답으로 제공한다.
"""
from __future__ import annotations

from .models import Member, Mission, Transaction

# 등급 임계값 (누적 결제액 기준)
_SILVER_AT = 50_000
_GOLD_AT = 200_000


def _mask_name(name: str) -> str:
    """개인정보 보호용 이름 마스킹. 김슬로우 → 김**."""
    if not name:
        return "익명"
    return name[0] + "*" * max(1, len(name) - 1)


def _badges(member: Member, completed_missions: int) -> list[dict]:
    """카페 멤버십용 성취 배지 목록(획득 여부 포함)."""
    v = member.visit_count
    spent = member.total_spent
    defs = [
        ("first", "🌱", "첫 걸음", "첫 방문 완료", v >= 1),
        ("regular", "☕", "단골", "5회 방문", v >= 5),
        ("club10", "🔟", "10잔 클럽", "10회 방문", v >= 10),
        ("club20", "🏆", "20잔 마스터", "20회 방문", v >= 20),
        ("silver", "🥈", "실버 등급", "누적 5만원", member.tier in (Member.Tier.SILVER, Member.Tier.GOLD)),
        ("gold", "🥇", "골드 등급", "누적 20만원", member.tier == Member.Tier.GOLD),
        ("bigspender", "💎", "큰손", "누적 10만원", spent >= 100_000),
        ("mission", "🎯", "미션 클리어", "미션 1개 달성", completed_missions >= 1),
    ]
    return [
        {"key": k, "icon": icon, "title": t, "desc": d, "earned": bool(e)}
        for (k, icon, t, d, e) in defs
    ]


def _next_tier(member: Member) -> dict:
    """다음 등급까지 진행률."""
    spent = member.total_spent
    if member.tier == Member.Tier.BRONZE:
        lower, upper, nxt = 0, _SILVER_AT, "실버"
    elif member.tier == Member.Tier.SILVER:
        lower, upper, nxt = _SILVER_AT, _GOLD_AT, "골드"
    else:  # GOLD — 최고 등급
        return {
            "current": "골드", "next": None, "progress_pct": 100,
            "remaining": 0, "is_max": True,
        }
    span = upper - lower
    pct = int(min(100, max(0, (spent - lower) / span * 100))) if span else 100
    return {
        "current": member.get_tier_display(),
        "next": nxt,
        "progress_pct": pct,
        "remaining": max(0, upper - spent),
        "is_max": False,
    }


def _ranking(member: Member) -> dict:
    """누적 방문 기준 단골 랭킹(익명). 상위 %, TOP5, 내 순위."""
    ranked = list(
        Member.objects.order_by("-visit_count", "-total_spent").values(
            "id", "name", "visit_count"
        )
    )
    total = len(ranked)
    my_rank = next((i + 1 for i, r in enumerate(ranked) if r["id"] == member.id), total)
    leaderboard = [
        {
            "rank": i + 1,
            "nickname": _mask_name(r["name"]),
            "visits": r["visit_count"],
            "is_me": r["id"] == member.id,
        }
        for i, r in enumerate(ranked[:5])
    ]
    percentile = int(round(my_rank / total * 100)) if total else 100
    return {
        "rank": my_rank,
        "total": total,
        "percentile": max(1, percentile),
        "metric": "누적 방문",
        "leaderboard": leaderboard,
    }


def _timeline(member: Member) -> list[dict]:
    """최근 방문(결제) 기록 타임라인."""
    txns = (
        member.transactions.filter(status=Transaction.Status.PAID)
        .order_by("-paid_at")[:10]
    )
    return [
        {
            "net_amount": t.net_amount,
            "points_earned": t.points_earned,
            "payment_method": t.payment_method,
            "paid_at": t.paid_at.isoformat() if t.paid_at else None,
        }
        for t in txns
    ]


def _missions(member: Member) -> list[dict]:
    """활성 미션 진행률."""
    out = []
    for m in Mission.objects.filter(store=member.store, is_active=True):
        progress = m.member_value(member)
        out.append({
            "title": m.title,
            "description": m.description,
            "progress": progress,
            "target": m.target_value,
            "reward_points": m.reward_points,
            "is_completed": progress >= m.target_value,
        })
    return out


def build_member_dashboard(member: Member) -> dict:
    """고객 대시보드 전체 데이터."""
    missions = _missions(member)
    completed = sum(1 for m in missions if m["is_completed"])
    return {
        "badges": _badges(member, completed),
        "next_tier": _next_tier(member),
        "ranking": _ranking(member),
        "timeline": _timeline(member),
        "missions": missions,
    }
