"""
고객(회원) 대시보드 데이터 조립 — 9.81 Park식 UX 참고.

성취 배지 · 방문 기록 타임라인 · 단골 랭킹(익명) · 다음 등급 진행률을
한 번의 응답으로 제공한다.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import Member, MenuItem, Mission, OrderItem, Transaction
from .rewards import REFERRAL_REWARD

# 등급 임계값 (누적 결제액 기준)
_SILVER_AT = 50_000
_GOLD_AT = 200_000


def _display_name(name: str) -> str:
    """
    랭킹·명예의 전당에 쓰는 표시 이름.

    가입 시 '느긋한 수달' 같은 닉네임을 부여하므로 가릴 필요가 없다.
    다만 이관된 실명(예: 김슬로우)은 그대로 노출하면 안 되므로 마스킹한다.
    → 공백이 있으면 닉네임(행동+동물)으로 보고 그대로 쓴다.
    """
    if not name:
        return "익명"
    if " " in name.strip():
        return name
    return name[0] + "*" * max(1, len(name) - 1)


def _behavior_flags(member: Member) -> dict:
    """주문·시간 데이터에서 행동 특성을 뽑는다(배지 판정용)."""
    items = _paid_items(member)
    opt = items.aggregate(
        decaf=Count("id", filter=Q(decaf=True)),
        oat=Count("id", filter=Q(oatmilk=True)),
        shot=Count("id", filter=Q(shot=True)),
        hot=Count("id", filter=Q(temperature="hot")),
        ice=Count("id", filter=Q(temperature="ice")),
    )
    times = [
        timezone.localtime(t)
        for t in member.transactions.filter(
            status=Transaction.Status.PAID, paid_at__isnull=False
        ).values_list("paid_at", flat=True)
    ]
    morning = sum(1 for t in times if t.hour < 11)
    evening = sum(1 for t in times if t.hour >= 18)
    weekend = sum(1 for t in times if t.weekday() >= 5)
    return {**opt, "morning": morning, "evening": evening, "weekend": weekend}


def _badges(member: Member, completed_missions: int, collection: dict,
            streak: dict, flags: dict) -> list[dict]:
    """카페 멤버십용 성취 배지 목록(획득 여부 포함)."""
    v = member.visit_count
    spent = member.total_spent
    defs = [
        ("first", "✦", "첫 걸음", "첫 방문 완료", v >= 1),
        ("regular", "❖", "단골", "5회 방문", v >= 5),
        ("club10", "X", "10잔 클럽", "10회 방문", v >= 10),
        ("club20", "XX", "20잔 마스터", "20회 방문", v >= 20),
        ("silver", "◆", "실버 등급", "누적 5만원", member.tier in (Member.Tier.SILVER, Member.Tier.GOLD)),
        ("gold", "♛", "골드 등급", "누적 20만원", member.tier == Member.Tier.GOLD),
        ("bigspender", "◈", "큰손", "누적 10만원", spent >= 100_000),
        ("mission", "✧", "미션 클리어", "미션 1개 달성", completed_missions >= 1),
        # ── 시간대·요일 ──
        ("morning", "☀", "아침형 인간", "오전 방문 5회", flags["morning"] >= 5),
        ("evening", "☾", "저녁의 여유", "저녁 방문 5회", flags["evening"] >= 5),
        ("weekend", "◐", "주말 단골", "주말 방문 5회", flags["weekend"] >= 5),
        # ── 취향(옵션) ──
        ("oat", "◍", "오트밀크 애호가", "오트밀크 3잔", flags["oat"] >= 3),
        ("decaf", "◌", "디카페인파", "디카페인 3잔", flags["decaf"] >= 3),
        ("shot", "▲", "샷 추가러", "샷 추가 3잔", flags["shot"] >= 3),
        ("hotice", "◑", "양손잡이", "핫·아이스 모두 3잔 이상",
         flags["hot"] >= 3 and flags["ice"] >= 3),
        # ── 컬렉션·스트릭 ──
        ("explorer", "❉", "메뉴 탐험가", "메뉴 10종 도장깨기", collection["tried"] >= 10),
        ("collector", "✺", "컬렉터", "메뉴 절반 정복", collection["pct"] >= 50),
        ("streak3", "🔥", "3주 연속", "3주 연속 방문", streak["weeks"] >= 3),
        ("streak8", "🔥", "두 달 개근", "8주 연속 방문", streak["weeks"] >= 8),
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
            "nickname": _display_name(r["name"]),
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


def _paid_items(member: Member):
    return OrderItem.objects.filter(
        transaction__member=member,
        transaction__status=Transaction.Status.PAID,
    )


def _taste(member: Member) -> dict:
    """취향 프로필 — 최애 메뉴와 카테고리 분포. 이미 쌓인 주문 데이터를 쓴다."""
    items = _paid_items(member)
    top = list(
        items.values("name").annotate(qty=Sum("quantity")).order_by("-qty")[:3]
    )
    total = sum(r["qty"] for r in top) if top else 0
    cats = list(
        items.values("menu_item__category")
        .annotate(qty=Sum("quantity"))
        .order_by("-qty")
    )
    labels = dict(MenuItem.Category.choices)
    cat_total = sum(c["qty"] or 0 for c in cats) or 1
    return {
        "favorite": top[0]["name"] if top else None,
        "favorite_qty": top[0]["qty"] if top else 0,
        "top_items": [{"name": r["name"], "qty": r["qty"]} for r in top],
        "categories": [
            {
                "key": c["menu_item__category"] or "etc",
                "label": labels.get(c["menu_item__category"], "기타"),
                "qty": c["qty"] or 0,
                "pct": round((c["qty"] or 0) / cat_total * 100),
            }
            for c in cats if c["qty"]
        ][:6],
        "total_cups": total,
    }


def _collection(member: Member) -> dict:
    """메뉴 도장깨기 — 카테고리별로 몇 종을 맛봤는지."""
    tried = set(
        _paid_items(member).values_list("menu_item_id", flat=True)
    )
    tried.discard(None)
    menus = MenuItem.objects.filter(store=member.store, is_available=True)
    labels = dict(MenuItem.Category.choices)
    by_cat: dict[str, dict] = {}
    for m in menus:
        c = by_cat.setdefault(
            m.category,
            {"key": m.category, "label": labels.get(m.category, m.category),
             "total": 0, "tried": 0, "names": []},
        )
        c["total"] += 1
        if m.id in tried:
            c["tried"] += 1
        else:
            c["names"].append(m.name)     # 아직 안 먹어본 메뉴 → 추천에 쓸 수 있다
    cats = sorted(by_cat.values(), key=lambda c: -c["total"])
    total = sum(c["total"] for c in cats)
    done = sum(c["tried"] for c in cats)
    return {
        "tried": done,
        "total": total,
        "pct": round(done / total * 100) if total else 0,
        "categories": [
            {**c, "next": c["names"][0] if c["names"] else None, "names": None}
            for c in cats
        ],
    }


def _streak(member: Member) -> dict:
    """
    연속 방문 스트릭(주 단위).

    카페 방문 주기에 맞춰 '일'이 아니라 '주'로 센다. 이번 주에 왔으면 이어지는
    중이고, 안 왔어도 지난 주까지 이어졌다면 아직 끊긴 게 아니다(이번 주에 오면 연장).
    """
    days = list(
        member.transactions.filter(status=Transaction.Status.PAID, paid_at__isnull=False)
        .values_list("paid_at", flat=True)
    )
    if not days:
        return {"weeks": 0, "alive": False, "visited_this_week": False}

    def week_key(dt):
        d = timezone.localtime(dt).date()
        monday = d - timedelta(days=d.weekday())
        return monday

    weeks = sorted({week_key(d) for d in days}, reverse=True)
    this_week = week_key(timezone.now())
    last_week = this_week - timedelta(days=7)

    if weeks[0] not in (this_week, last_week):
        return {"weeks": 0, "alive": False, "visited_this_week": False}

    streak, cursor = 0, weeks[0]
    for w in weeks:
        if w == cursor:
            streak += 1
            cursor = cursor - timedelta(days=7)
        elif w < cursor:
            break
    return {
        "weeks": streak,
        "alive": True,
        "visited_this_week": weeks[0] == this_week,
    }


def hall_of_fame(store=None, when=None) -> dict:
    """이달의 단골 — 이번 달 방문 횟수 1위(닉네임으로 표시)."""
    now = timezone.localtime(when or timezone.now())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = (
        Transaction.objects.filter(
            status=Transaction.Status.PAID, paid_at__gte=start, member__isnull=False
        )
        .values("member__id", "member__name")
        .annotate(visits=Count("id"))
        .order_by("-visits")[:3]
    )
    top = [
        {
            "rank": i + 1,
            "nickname": _display_name(r["member__name"]),
            "visits": r["visits"],
            "member_id": r["member__id"],
        }
        for i, r in enumerate(rows)
    ]
    return {"month": now.strftime("%Y-%m"), "top": top}


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
    collection = _collection(member)
    streak = _streak(member)
    flags = _behavior_flags(member)
    return {
        "badges": _badges(member, completed, collection, streak, flags),
        "next_tier": _next_tier(member),
        "ranking": _ranking(member),
        "timeline": _timeline(member),
        "missions": missions,
        "taste": _taste(member),
        "collection": collection,
        "streak": streak,
        "hall_of_fame": hall_of_fame(member.store),
        "referral": {
            "code": member.ensure_referral_code(),
            "reward": REFERRAL_REWARD,
            "invited": member.referrals.count(),
            "referred": member.referred_by_id is not None,
        },
    }
