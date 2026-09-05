"""
고객(회원) 대시보드 데이터 조립 — 9.81 Park식 UX 참고.

성취 배지 · 방문 기록 타임라인 · 단골 랭킹(익명) · 다음 등급 진행률을
한 번의 응답으로 제공한다.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import streaks
from .models import Member, MenuItem, Mission, OrderItem, PointEntry, Transaction
from .rewards import REFERRAL_DAILY_LIMIT, REFERRAL_REWARD, wheel_segments

# 등급 임계값 — 모델을 진실 원천으로 삼는다(두 곳에 적으면 반드시 어긋난다)
_TIERS = dict((t, amt) for amt, t in Member.TIER_THRESHOLDS)
_SILVER_AT = _TIERS[Member.Tier.SILVER]
_GOLD_AT = _TIERS[Member.Tier.GOLD]


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


def _hidden_flags(member: Member) -> dict:
    """히든 배지 판정 — 조건을 미리 알려주지 않는 '발견의 재미'용."""
    paid = member.transactions.filter(
        status=Transaction.Status.PAID, paid_at__isnull=False
    )
    # 그날의 첫 손님: 같은 날 나보다 앞선 결제가 없는 거래가 있는가
    first_of_day = False
    for t in paid.values_list("paid_at", flat=True):
        day = timezone.localtime(t).date()
        earlier = Transaction.objects.filter(
            status=Transaction.Status.PAID, paid_at__date=day, paid_at__lt=t
        ).exists()
        if not earlier:
            first_of_day = True
            break
    late = any(timezone.localtime(t).hour >= 21 for t in paid.values_list("paid_at", flat=True))
    from .models import Coupon
    jackpot = member.coupons.filter(kind=Coupon.Kind.FREE_DRINK).exists()
    set_master = paid.filter(discount__gt=0).count()
    return {
        "first_of_day": first_of_day,
        "late": late,
        "jackpot": jackpot,
        "set_master": set_master,
    }


# 단계형 배지: (키, 아이콘, 이름, 단계 임계값)
LEVELED = (
    ("cups", "☕", "커피 애호가", (10, 50, 100)),
    ("spender", "◈", "든든한 단골", (50_000, 200_000, 500_000)),
)
ROMAN = ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ")


def _leveled_badges(member: Member) -> list[dict]:
    """한 번 따면 끝나지 않고 단계가 오르는 배지."""
    values = {"cups": member.visit_count, "spender": member.total_spent}
    out = []
    for key, icon, title, steps in LEVELED:
        v = values[key]
        level = sum(1 for s in steps if v >= s)
        nxt = steps[level] if level < len(steps) else None
        out.append({
            "key": key, "icon": icon,
            "title": f"{title} {ROMAN[level - 1]}" if level else title,
            "desc": (f"다음 단계까지 {nxt - v:,}" + ("잔" if key == "cups" else "원"))
                    if nxt else "최고 단계 달성",
            "earned": level > 0, "level": level, "max_level": len(steps),
        })
    return out


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
        ("silver", "◆", "실버 등급", f"누적 {_SILVER_AT // 10000}만원",
         member.tier in (Member.Tier.SILVER, Member.Tier.GOLD)),
        ("gold", "♛", "골드 등급", f"누적 {_GOLD_AT // 10000}만원",
         member.tier == Member.Tier.GOLD),
        ("bigspender", "◈", "큰손", "누적 20만원", spent >= 200_000),
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
    # ── 히든 배지: 얻기 전에는 목록에 보이지 않는다 ──
    hidden = _hidden_flags(member)
    for key, icon, title, desc, earned in (
        ("first_guest", "✷", "오늘의 첫 손님", "그날 첫 번째로 방문", hidden["first_of_day"]),
        ("night", "☾", "늦은 밤의 위로", "밤 9시 이후 방문", hidden["late"]),
        ("jackpot", "★", "룰렛 잭팟", "룰렛에서 무료 음료 당첨", hidden["jackpot"]),
        ("setlover", "✚", "세트 마스터", "세트 할인 5회", hidden["set_master"] >= 5),
    ):
        if earned:
            defs.append((key, icon, title, desc, True))
    out = [
        {"key": k, "icon": icon, "title": t, "desc": d, "earned": bool(e)}
        for (k, icon, t, d, e) in defs
    ]
    out.extend(_leveled_badges(member))
    return out


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


# 월간 랭킹 시상 — 금액·횟수 두 부문 각각 지급
RANKING_PRIZES = (
    "아메리카노 + 플레인 휘낭시에",
    "아메리카노",
    "플레인 휘낭시에",
)


def _rank_rows(start, key: str) -> list[dict]:
    """이번 달 순위 TOP3. key='spent' 금액 / key='visits' 횟수."""
    qs = (
        Transaction.objects.filter(
            status=Transaction.Status.PAID, paid_at__gte=start, member__isnull=False
        )
        .values("member__id", "member__name")
    )
    if key == "spent":
        qs = qs.annotate(value=Sum("net_amount")).order_by("-value", "member__id")
    else:
        qs = qs.annotate(value=Count("id")).order_by("-value", "member__id")
    return [
        {
            "rank": i + 1,
            "nickname": _display_name(r["member__name"]),
            "value": r["value"] or 0,
            "member_id": r["member__id"],
            "prize": RANKING_PRIZES[i],
        }
        for i, r in enumerate(qs[:3])
    ]


def hall_of_fame(store=None, when=None) -> dict:
    """
    이달의 단골 — **금액 부문·횟수 부문**을 따로 매긴다.

    한 줄 세우기로는 객단가 높은 손님과 자주 오는 손님 중 한쪽이 늘 진다.
    부문을 나누면 각자 자기 방식으로 1등을 노릴 수 있다.
    """
    now = timezone.localtime(when or timezone.now())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    visits = _rank_rows(start, "visits")
    return {
        "month": now.strftime("%Y-%m"),
        "prizes": list(RANKING_PRIZES),
        "boards": [
            {"key": "spent", "label": "금액", "unit": "원", "top": _rank_rows(start, "spent")},
            {"key": "visits", "label": "횟수", "unit": "회", "top": visits},
        ],
        # 이전 화면과의 호환 — 횟수 기준 TOP3
        "top": [{**r, "visits": r["value"]} for r in visits],
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


def badge_rarity() -> dict:
    """
    배지별 보유 비율(%) — "이 배지를 가진 분은 3%뿐" 같은 희소성 표시용.

    회원 전체를 도는 비용이 있으므로 짧게 캐시한다(매장 단위 값이라
    사람마다 다시 계산할 이유가 없다).
    """
    from django.core.cache import cache

    cached = cache.get(_RARITY_KEY)
    if cached is not None:
        return cached

    members = list(Member.objects.all().only("id", "visit_count", "total_spent", "tier"))
    total = len(members)
    if not total:
        return {}

    counts: dict[str, int] = {}
    for m in members:
        try:
            for b in build_badges_only(m):
                if b.get("earned"):
                    counts[b["key"]] = counts.get(b["key"], 0) + 1
        except Exception:      # 한 명의 데이터 문제로 전체가 죽지 않게
            continue
    rarity = {k: round(v / total * 100, 1) for k, v in counts.items()}
    cache.set(_RARITY_KEY, rarity, 600)   # 10분
    return rarity


_RARITY_KEY = "slowstep.badge.rarity"


def build_badges_only(member: Member) -> list[dict]:
    """배지 목록만 계산(희귀도 집계에서 재사용)."""
    missions = _missions(member)
    completed = sum(1 for m in missions if m["is_completed"])
    return _badges(
        member, completed, _collection(member), streaks.build(member), _behavior_flags(member)
    )


def _title_of(badges: list[dict], rarity: dict) -> dict | None:
    """
    대표 칭호 — 획득한 배지 중 **가장 희귀한 것**을 뽑는다.
    닉네임 앞에 붙어 그 사람의 정체성이 된다.
    """
    earned = [b for b in badges if b.get("earned")]
    if not earned:
        return None
    ranked = sorted(earned, key=lambda b: rarity.get(b["key"], 100.0))
    top = ranked[0]
    return {
        "key": top["key"], "icon": top["icon"], "title": top["title"],
        "rarity": rarity.get(top["key"]),
    }


def build_member_dashboard(member: Member) -> dict:
    """고객 대시보드 전체 데이터."""
    missions = _missions(member)
    completed = sum(1 for m in missions if m["is_completed"])
    collection = _collection(member)
    streak = streaks.build(member)
    flags = _behavior_flags(member)
    badges = _badges(member, completed, collection, streak, flags)
    rarity = badge_rarity()
    for b in badges:
        b["rarity"] = rarity.get(b["key"])
    from .quests import active_group

    return {
        "badges": badges,
        "title": _title_of(badges, rarity),
        "quests": active_group(member),   # 한 번에 한 챕터만 활성화된다
        "next_tier": _next_tier(member),
        "ranking": _ranking(member),
        "timeline": _timeline(member),
        "missions": missions,
        "taste": _taste(member),
        "collection": collection,
        "streak": streak,
        "hall_of_fame": hall_of_fame(member.store),
        "coupons": coupon_list(member),
        "roulette": {
            "spins": member.spins,
            "segments": wheel_segments(),
        },
        "referral": {
            "code": member.ensure_referral_code(),
            "reward": REFERRAL_REWARD,
            "invited": member.referrals.count(),
            "referred": member.referred_by_id is not None,
            "daily_limit": REFERRAL_DAILY_LIMIT,
        },
    }


def coupon_list(member: Member) -> list[dict]:
    """보유 쿠폰 — 쓸 수 있는 것 먼저, 만료 임박 순."""
    now = timezone.now()
    out = []
    for c in member.coupons.filter(used_at__isnull=True, expires_at__gt=now).order_by(
        "expires_at"
    ):
        left = (c.expires_at - now).days
        out.append({
            "id": c.id,
            "kind": c.kind,
            "name": c.get_kind_display(),
            "source": c.get_source_display(),
            "note": c.note,
            "discount_pct": c.discount_pct,
            "expires_at": c.expires_at,
            "days_left": left,
        })
    return out
