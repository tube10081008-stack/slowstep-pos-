"""
손님 분석 — 쌓인 거래로 **다음에 무엇을 할지** 정하는 숫자들.

매출·회원 수 같은 '현황'은 대시보드가 이미 보여 준다. 여기는 그 다음 질문이다.

- 처음 온 손님이 다시 오나?            → retention
- 단골인데 요즘 안 보이는 사람은 누구?  → churn_risk (바로 문자 대상으로 넘긴다)
- 문자 보낸 게 매출로 돌아왔나?        → campaign_effect
- 쿠폰이 다시 오게 만드나?             → coupon_effect
- 언제 붐비나?                         → heatmap
- 무엇이 같이 팔리나?                  → pairs

원칙
- **원본에서 매번 계산한다.** 집계 결과를 따로 저장하지 않으므로 과거 데이터에도
  그대로 소급되고, 계산식을 고쳐도 옛 숫자와 어긋날 일이 없다.
- 회원별 방문은 **방문한 날** 단위로 본다. 같은 날 두 번 결제한 건 한 번 온 것이다.
- 쿼리는 섹션마다 한두 번으로 끝낸다. 회원 수만큼 도는 쿼리는 쓰지 않는다
  (회원이 늘면 화면이 먼저 멈춘다).
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, timedelta
from itertools import combinations

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate
from django.utils import timezone

from membership.models import Coupon, Member, OrderItem, Transaction

from .models import Campaign, MessageLog
from .solapi import message_type

# 이탈 판단 — 평소 방문 간격의 2배를 넘기면 '요즘 안 보이는' 것으로 본다.
# 너무 짧은 간격(매일 오는 손님)이 사흘 빠졌다고 경보를 울리지 않도록 최소 14일.
CHURN_FACTOR = 2
CHURN_MIN_DAYS = 14
# 이만큼 지나면 '떠난' 손님 — 위험 명단이 아니라 따로 센다.
CHURN_LOST_DAYS = 180
CHURN_MIN_VISITS = 3

RETURN_WINDOW = 30      # 첫 방문 후 재방문을 보는 기간(일)
COHORT_MONTHS = 6       # 코호트 표에 보일 달 수
CAMPAIGN_WINDOW = 7     # 문자 발송 후 효과를 보는 기간(일)
COUPON_RETURN_DAYS = 30
# 문자 1건 요금 — 화면의 '약 20원 / 약 60원' 안내와 같은 값(대략치).
SMS_COST = {"SMS": 20, "LMS": 60}


def _paid():
    return Transaction.objects.filter(
        status=Transaction.Status.PAID, paid_at__isnull=False
    )


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _has_phone(phone: str) -> bool:
    return len([c for c in (phone or "") if c.isdigit()]) >= 10


def visit_days() -> dict[int, list[date]]:
    """회원별 방문한 날(매장 현지 날짜) — 오름차순. 쿼리 1번."""
    rows = (
        _paid().filter(member__isnull=False)
        .annotate(d=TruncDate("paid_at"))
        .values_list("member_id", "d")
        # 기본 정렬(-created_at)이 남아 있으면 그 열까지 DISTINCT 에 끼어
        # 같은 날 두 번 결제가 두 번 방문으로 센다. 정렬을 비운다.
        .order_by()
        .distinct()
    )
    out: dict[int, list[date]] = defaultdict(list)
    for mid, d in rows:
        out[mid].append(d)
    for days in out.values():
        days.sort()
    return out


def _month_add(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


# ───────────────────────── 재방문 ─────────────────────────

def retention(days_map: dict[int, list[date]], today: date) -> dict:
    """
    처음 온 손님이 다시 오는가.

    **신규만 센다.** payhere 에서 옮겨온 회원(이관 방문수 > 0)은 새 POS 첫 결제가
    진짜 첫 방문이 아니라서, 섞으면 재방문율이 부풀려진다.
    """
    migrated = set(
        Member.objects.filter(baseline_visit_count__gt=0).values_list("id", flat=True)
    )
    new = {mid: ds for mid, ds in days_map.items() if mid not in migrated}

    # ① 첫 방문 후 30일 안에 다시 왔나 — 30일이 다 지난 손님만 분모에 넣는다.
    #    (어제 처음 온 손님을 '안 돌아왔다'로 세면 숫자가 늘 낮게 나온다)
    #    최근 120일 안에 처음 온 손님으로 한정해 '요즘' 숫자를 본다.
    elig = ret = 0
    for ds in new.values():
        first = ds[0]
        if not (today - timedelta(days=120) <= first <= today - timedelta(days=RETURN_WINDOW)):
            continue
        elig += 1
        if any(first < d <= first + timedelta(days=RETURN_WINDOW) for d in ds[1:]):
            ret += 1

    # ② 월별 코호트 — 첫 방문한 달 기준, k개월 뒤에도 한 번이라도 왔나.
    cy, cm = today.year, today.month
    months = [_month_add(cy, cm, -i) for i in range(COHORT_MONTHS - 1, -1, -1)]
    groups: dict[tuple[int, int], list[set]] = {ym: [] for ym in months}
    for ds in new.values():
        ym = (ds[0].year, ds[0].month)
        if ym in groups:
            groups[ym].append({(d.year, d.month) for d in ds})
    cohorts = []
    for y, m in months:
        members = groups[(y, m)]
        cells = []
        for k in range(1, COHORT_MONTHS):
            ty, tm = _month_add(y, m, k)
            if (ty, tm) > (cy, cm):
                break                      # 아직 오지 않은 달
            n = sum(1 for s in members if (ty, tm) in s)
            cells.append({
                "k": k, "count": n, "rate": _pct(n, len(members)),
                "partial": (ty, tm) == (cy, cm),   # 이번 달은 진행 중
            })
        cohorts.append({"month": f"{y}-{m:02d}", "size": len(members), "cells": cells})

    return {
        "return_30d": {"eligible": elig, "returned": ret, "rate": _pct(ret, elig)},
        "cohorts": cohorts,
        "new_total": len(new),
        "migrated_excluded": len(migrated),
    }


# ───────────────────────── 이탈 위험 ─────────────────────────

def churn_risk(days_map: dict[int, list[date]], today: date, limit: int = 50) -> dict:
    """
    **평소보다 오래 안 온 단골.** 새 POS 에서 3번 이상 온 손님만 본다 — 두 번으로는
    '평소 간격'을 말할 수 없다.

    누적 결제가 큰 순서로 준다. 다 붙잡을 수 없다면 잃으면 아픈 손님부터다.
    """
    risk: dict[int, dict] = {}
    lost = 0
    for mid, ds in days_map.items():
        if len(ds) < CHURN_MIN_VISITS:
            continue
        interval = (ds[-1] - ds[0]).days / (len(ds) - 1)
        since = (today - ds[-1]).days
        if since < max(CHURN_MIN_DAYS, CHURN_FACTOR * interval):
            continue
        if since > CHURN_LOST_DAYS:
            lost += 1
            continue
        risk[mid] = {
            "visits": len(ds), "interval_days": round(interval, 1),
            "days_since": since, "last_visit": ds[-1].isoformat(),
        }

    rows = []
    for m in Member.objects.filter(id__in=risk).order_by("-total_spent", "name"):
        rows.append({
            "id": m.id, "name": m.name, "phone": m.phone,
            "tier": m.tier, "tier_display": m.get_tier_display(),
            "visit_count": m.visit_count, "total_spent": m.total_spent,
            "points": m.points, "stamps": m.stamps,
            "marketing_opt_in": m.marketing_opt_in, "has_phone": _has_phone(m.phone),
            **risk[m.id],
        })
    return {"count": len(rows), "members": rows[:limit], "lost": lost}


# ───────────────────────── 문자 효과 ─────────────────────────

def campaign_effect(now, limit: int = 10) -> dict:
    """
    문자를 받은 손님이 **7일 안에** 다시 왔나, 얼마를 썼나.

    비교 기준으로 '같은 7일 동안 문자를 안 받은 회원의 방문율'을 같이 준다.
    완벽한 대조군은 아니다 — 보통 오래 안 온 손님을 골라 보내므로 받은 쪽이
    불리한 비교다. 그래도 받은 쪽이 높다면 효과가 있었다는 강한 신호다.
    """
    camps = list(
        Campaign.objects.filter(status=Campaign.Status.SENT, sent_at__isnull=False)
        .order_by("-sent_at")[:limit]
    )
    if not camps:
        return {"window_days": CAMPAIGN_WINDOW, "campaigns": []}

    recips: dict[int, set] = defaultdict(set)
    cost: Counter = Counter()
    for cid, mid, body in MessageLog.objects.filter(
        campaign__in=camps, status=MessageLog.Status.SENT
    ).values_list("campaign_id", "member_id", "rendered_message"):
        if mid:
            recips[cid].add(mid)
        cost[cid] += SMS_COST[message_type(body or "")]

    # 발송 시점에 이미 회원이던 사람 수 — 비교군 분모.
    joined = sorted(Member.objects.values_list("joined_at", flat=True))

    out = []
    for c in camps:
        end = c.sent_at + timedelta(days=CAMPAIGN_WINDOW)
        got = recips.get(c.id, set())
        visited, others, revenue, orders = set(), set(), 0, 0
        for mid, amount in _paid().filter(
            member__isnull=False, paid_at__gt=c.sent_at, paid_at__lte=end
        ).values_list("member_id", "net_amount"):
            if mid in got:
                visited.add(mid)
                revenue += amount
                orders += 1
            else:
                others.add(mid)
        base = bisect_left(joined, c.sent_at) - len(got)
        out.append({
            "id": c.id, "name": c.name, "is_ad": c.is_ad,
            "sent_at": timezone.localtime(c.sent_at).isoformat(),
            "done": end <= now,            # 7일이 다 지났나(아니면 진행 중)
            "sent": len(got), "visited": len(visited),
            "rate": _pct(len(visited), len(got)),
            "baseline_rate": _pct(len(others), base) if base > 0 else None,
            "orders": orders, "revenue": revenue,
            "cost": cost.get(c.id, 0),
        })
    return {"window_days": CAMPAIGN_WINDOW, "campaigns": out}


# ───────────────────────── 쿠폰 효과 ─────────────────────────

def coupon_effect(days_map: dict[int, list[date]], now) -> dict:
    """
    쿠폰 종류·발행 사유별로 **쓰였나, 쓰고 나서 다시 왔나.**

    '다시 왔나'는 쿠폰을 쓴 날 이후 30일 안의 방문이다. 30일이 안 지난 건
    아직 모르므로 분모에서 뺀다.
    """
    today = timezone.localdate(now)
    agg = Coupon.objects.values("kind", "source").annotate(
        issued=Count("id"),
        used=Count("id", filter=Q(used_at__isnull=False)),
        expired=Count("id", filter=Q(used_at__isnull=True, expires_at__lt=now)),
        revenue=Sum("used_transaction__net_amount"),
    )
    kinds, sources = dict(Coupon.Kind.choices), dict(Coupon.Source.choices)
    rows = {
        (r["kind"], r["source"]): {
            "kind": r["kind"], "source": r["source"],
            "label": f"{kinds.get(r['kind'], r['kind'])} · {sources.get(r['source'], r['source'])}",
            "issued": r["issued"], "used": r["used"], "expired": r["expired"],
            "use_rate": _pct(r["used"], r["issued"]),
            "revenue": r["revenue"] or 0,
            "_days": [], "_elig": 0, "_back": 0,
        }
        for r in agg
    }
    for kind, source, issued, used, mid in Coupon.objects.filter(
        used_at__isnull=False
    ).values_list("kind", "source", "issued_at", "used_at", "member_id"):
        row = rows.get((kind, source))
        if row is None:
            continue
        row["_days"].append((used - issued).total_seconds() / 86400)
        used_day = timezone.localtime(used).date()
        if used_day > today - timedelta(days=COUPON_RETURN_DAYS):
            continue
        row["_elig"] += 1
        limit = used_day + timedelta(days=COUPON_RETURN_DAYS)
        if any(used_day < d <= limit for d in days_map.get(mid, ())):
            row["_back"] += 1

    out = []
    for row in rows.values():
        ds, elig, back = row.pop("_days"), row.pop("_elig"), row.pop("_back")
        row["avg_days_to_use"] = round(sum(ds) / len(ds), 1) if ds else None
        row["return_rate"] = _pct(back, elig)
        row["return_eligible"] = elig
        out.append(row)
    out.sort(key=lambda r: -r["issued"])
    return {"return_days": COUPON_RETURN_DAYS, "rows": out}


# ───────────────────────── 요일 × 시간 ─────────────────────────

def heatmap(since) -> dict:
    """요일(월=1…일=7) × 시간대별 결제 건수·매출. 쿼리 1번."""
    rows = (
        _paid().filter(paid_at__gte=since)
        .annotate(wd=ExtractIsoWeekDay("paid_at"), h=ExtractHour("paid_at"))
        .values("wd", "h")
        .annotate(n=Count("id"), revenue=Sum("net_amount"))
    )
    cells = [
        {"wd": r["wd"], "h": r["h"], "count": r["n"], "revenue": r["revenue"] or 0}
        for r in rows
    ]
    peak = max(cells, key=lambda c: c["count"], default=None)
    return {"cells": cells, "peak": peak}


# ───────────────────────── 같이 팔리는 메뉴 ─────────────────────────

def pairs(since, top: int = 10, min_count: int = 3) -> dict:
    """
    한 주문에 같이 담긴 메뉴 짝.

    '같이 산 횟수'만 보면 제일 잘 팔리는 메뉴(아메리카노)가 모든 짝에 끼어 든다.
    그래서 **우연보다 몇 배**(lift)를 같이 준다 — 1보다 크면 서로 끌어당기는 짝,
    세트 구성·추천 멘트의 근거가 된다.
    """
    baskets: dict[int, set] = defaultdict(set)
    for tid, name in OrderItem.objects.filter(
        transaction__status=Transaction.Status.PAID,
        transaction__paid_at__gte=since,
    ).values_list("transaction_id", "name"):
        baskets[tid].add(name)

    total = len(baskets)
    single: Counter = Counter()
    together: Counter = Counter()
    multi = 0
    for names in baskets.values():
        single.update(names)
        if len(names) >= 2:
            multi += 1
            together.update(combinations(sorted(names), 2))

    rows = []
    for (a, b), n in together.items():
        if n < min_count:
            continue
        rows.append({
            "a": a, "b": b, "count": n,
            "a_share": _pct(n, single[a]),        # A 주문 중 B도 담긴 비율
            "b_share": _pct(n, single[b]),
            "lift": round(n * total / (single[a] * single[b]), 2),
        })
    rows.sort(key=lambda r: (-r["count"], -r["lift"]))
    return {
        "orders": total, "multi_orders": multi,
        "multi_rate": _pct(multi, total), "rows": rows[:top],
    }


def build(days: int = 90) -> dict:
    """대시보드 '손님 분석' 한 번에. 섹션 하나가 실패해도 나머지는 보여 준다."""
    import logging

    log = logging.getLogger(__name__)
    now = timezone.now()
    today = timezone.localdate(now)
    since = now - timedelta(days=days)
    days_map = visit_days()

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # 한 섹션 오류로 분석 화면 전체가 비면 안 된다
            log.exception("insights section failed: %s", fn.__name__)
            return {"error": f"{type(exc).__name__}: 계산하지 못했습니다."}

    return {
        "days": days,
        "generated_at": timezone.localtime(now).isoformat(),
        "retention": safe(retention, days_map, today),
        "churn": safe(churn_risk, days_map, today),
        "campaigns": safe(campaign_effect, now),
        "coupons": safe(coupon_effect, days_map, now),
        "heatmap": safe(heatmap, since),
        "pairs": safe(pairs, since),
    }
