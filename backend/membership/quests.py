"""
개인 맞춤 미션(퀘스트) — 그 사람 데이터에서 자동 생성한다.

매장 공통 미션(Mission 모델)과 달리 **DB에 미리 만들어 두지 않는다.**
조회할 때 회원 데이터로 후보를 뽑아 진행률을 계산하고, **달성했을 때만**
MemberQuest로 기록해 보상을 1회 지급한다.
→ 220명 × 퀘스트 수만큼 레코드가 불어나지 않고, 규칙을 바꿔도 과거 기록을
  건드릴 필요가 없다.

설계 원칙:
- **진행이 되돌아가지 않는 지표만 쓴다**(누적 카운트). 목표가 흔들리면
  손님이 손해 보는 느낌을 받는다.
- 회원당 **진행 중 3개**까지만 노출하고 보상에 상한을 둔다 — 적립비용이
  마진에서 차감되므로(margins.py) 예측 가능해야 한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import MenuItem, OrderItem, Transaction

MAX_ACTIVE = 3          # 한 번에 보여줄 퀘스트 수
MAX_REWARD = 2000       # 퀘스트 1개 보상 상한


@dataclass
class Quest:
    key: str            # 달성 기록용 안정 식별자
    kind: str           # taste / collection / rhythm / stretch / option / timeslot / comeback
    title: str
    description: str
    progress: int
    target: int
    reward: int

    @property
    def is_completed(self) -> bool:
        return self.progress >= self.target

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_completed"] = self.is_completed
        return d


def _paid(member):
    return member.transactions.filter(
        status=Transaction.Status.PAID, paid_at__isnull=False
    )


def _visit_dates(member) -> list:
    return sorted(
        timezone.localtime(t).date()
        for t in _paid(member).values_list("paid_at", flat=True)
    )


def avg_interval_days(member) -> float | None:
    """평균 방문 간격(일). 방문이 3회 미만이면 판단하지 않는다."""
    days = _visit_dates(member)
    if len(days) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    if not gaps:
        return None
    return sum(gaps) / len(gaps)


def _cat_counts(member) -> dict:
    rows = (
        OrderItem.objects.filter(
            transaction__member=member, transaction__status=Transaction.Status.PAID
        )
        .values("menu_item__category")
        .annotate(qty=Sum("quantity"))
    )
    return {r["menu_item__category"]: r["qty"] or 0 for r in rows if r["menu_item__category"]}


def _option_counts(member) -> dict:
    return OrderItem.objects.filter(
        transaction__member=member, transaction__status=Transaction.Status.PAID
    ).aggregate(
        oat=Count("id", filter=Q(oatmilk=True)),
        decaf=Count("id", filter=Q(decaf=True)),
        shot=Count("id", filter=Q(shot=True)),
    )


def _slot_counts(member) -> dict:
    times = [timezone.localtime(t) for t in _paid(member).values_list("paid_at", flat=True)]
    return {
        "morning": sum(1 for t in times if t.hour < 11),
        "afternoon": sum(1 for t in times if 11 <= t.hour < 18),
        "evening": sum(1 for t in times if t.hour >= 18),
    }


def _collection(member) -> dict:
    """카테고리별 (맛본 종수, 전체 종수, 다음 도전 메뉴)."""
    tried = set(
        OrderItem.objects.filter(
            transaction__member=member, transaction__status=Transaction.Status.PAID
        ).values_list("menu_item_id", flat=True)
    )
    tried.discard(None)
    out: dict[str, dict] = {}
    for m in MenuItem.objects.filter(store=member.store, is_available=True):
        c = out.setdefault(m.category, {"tried": 0, "total": 0, "next": None})
        c["total"] += 1
        if m.id in tried:
            c["tried"] += 1
        elif c["next"] is None:
            c["next"] = m.name
    return out


def _month_key(now=None) -> str:
    return timezone.localtime(now or timezone.now()).strftime("%Y-%m")


def _week_key(now=None) -> str:
    d = timezone.localtime(now or timezone.now()).date()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def _visits_since(member, start_date) -> int:
    return sum(1 for d in _visit_dates(member) if d >= start_date)


def build_candidates(member) -> list[Quest]:
    """이 회원에게 어울리는 퀘스트 후보를 우선순위 순으로 만든다."""
    labels = dict(MenuItem.Category.choices)
    cats = _cat_counts(member)
    coll = _collection(member)
    opts = _option_counts(member)
    slots = _slot_counts(member)
    now = timezone.localtime(timezone.now())
    out: list[Quest] = []

    # ── 1) 복귀: 평소 주기의 1.5배 넘게 안 오셨다 ──
    avg = avg_interval_days(member)
    days = _visit_dates(member)
    if avg and len(days) >= 2:
        last_gap = (days[-1] - days[-2]).days
        away = (now.date() - days[-1]).days
        threshold = max(7, round(avg * 1.5))
        if last_gap >= threshold or away >= threshold:
            out.append(Quest(
                key=f"comeback:{_month_key(now)}", kind="comeback",
                title="오랜만이에요",
                description=f"평소 {round(avg)}일마다 오셨어요. 이번에 오시면 보너스!",
                progress=1 if last_gap >= threshold else 0, target=1, reward=1000,
            ))

    # ── 2) 컬렉션 마무리: 거의 다 모은 카테고리(1~2종 남음) ──
    for cat, c in sorted(coll.items(), key=lambda kv: kv[1]["total"] - kv[1]["tried"]):
        left = c["total"] - c["tried"]
        if c["tried"] > 0 and 1 <= left <= 2:
            out.append(Quest(
                key=f"collection:{cat}", kind="collection",
                title=f"{labels.get(cat, cat)} 정복까지 {left}종",
                description=(f"다음 도전: {c['next']}" if c["next"] else "마지막 한 잔!"),
                progress=c["tried"], target=c["total"], reward=1000,
            ))
            break

    # ── 3) 취향 확장: 선호 순서대로 '처음 만나기' ──
    # 후보 조건에 '아직 안 먹었을 것'을 넣으면 먹는 순간 후보에서 사라져
    # 보상을 지급할 기회가 없어진다. 후보는 고정하고 진행률로 판정한다.
    for cat in ("dessert", "coldbrew", "tea", "ade", "noncoffee"):
        if cat in coll:
            out.append(Quest(
                key=f"taste:{cat}", kind="taste",
                title=f"{labels.get(cat, cat)} 처음 만나기",
                description=(f"{coll[cat]['next']} 어떠세요?" if coll[cat]["next"]
                             else "새로운 맛을 만나요"),
                progress=min(1, cats.get(cat, 0)), target=1, reward=500,
            ))

    # ── 4) 개인 주기: 이번 주에도 오시면 ──
    if avg and avg <= 12:
        monday = now.date() - timedelta(days=now.weekday())
        out.append(Quest(
            key=f"rhythm:{_week_key(now)}", kind="rhythm",
            title="이번 주도 만나요",
            description=f"평소 {round(avg)}일마다 오시는 리듬을 이어가요",
            progress=_visits_since(member, monday), target=1, reward=300,
        ))

    # ── 5) 월간 도전(난이도 개인화): 평소보다 조금만 더 ──
    first = now.date().replace(day=1)
    this_month = _visits_since(member, first)
    if member.visit_count >= 3:
        base = _personal_monthly_baseline(member)
        target = max(2, math.ceil(base * 1.2))
        out.append(Quest(
            key=f"stretch:{_month_key(now)}", kind="stretch",
            title=f"이번 달 {target}번 방문",
            description="평소보다 한 걸음만 더",
            progress=this_month, target=target,
            reward=min(MAX_REWARD, 300 * target),
        ))

    # ── 6) 옵션 탐험 (같은 이유로 후보는 고정) ──
    for key, label, price_hint in (
        ("oat", "오트밀크", "고소하게"), ("decaf", "디카페인", "부담 없이"),
        ("shot", "샷 추가", "진하게"),
    ):
        out.append(Quest(
            key=f"option:{key}", kind="option",
            title=f"{label} 한 번 바꿔보기",
            description=f"{price_hint} 즐기는 방법이에요",
            progress=min(1, opts.get(key, 0)), target=1, reward=300,
        ))

    # ── 7) 시간대 전환 ──
    if member.visit_count >= 3:
        for slot, label in (("morning", "오전"), ("evening", "저녁")):
            out.append(Quest(
                key=f"timeslot:{slot}", kind="timeslot",
                title=f"{label}에 한 번 들르기",
                description=f"{label}의 슬로우스텝은 또 다른 분위기예요",
                progress=min(1, slots.get(slot, 0)), target=1, reward=500,
            ))

    return out


def _personal_monthly_baseline(member) -> float:
    """그 사람의 평소 월 방문 수(최근 3개월 평균). 개인화된 난이도의 기준."""
    now = timezone.localtime(timezone.now())
    since = (now - timedelta(days=90)).date()
    recent = [d for d in _visit_dates(member) if d >= since]
    if not recent:
        return 1.0
    months = max(1, len({(d.year, d.month) for d in recent}))
    return len(recent) / months


def active_quests(member) -> list[Quest]:
    """
    지금 진행 중인 퀘스트(최대 3개). 이미 보상을 받은 키는 제외한다.
    """
    from .models import MemberQuest

    done = set(
        MemberQuest.objects.filter(member=member).values_list("key", flat=True)
    )
    out = []
    for q in build_candidates(member):
        if q.key in done or q.is_completed:
            continue                      # 이미 받았거나 이미 채운 건 도전 대상이 아니다
        q.reward = min(q.reward, MAX_REWARD)
        out.append(q)
        if len(out) >= MAX_ACTIVE:
            break
    return out


def evaluate_and_award(member, txn, record_point) -> list[dict]:
    """
    결제 직후 호출 — 달성한 퀘스트에 보상을 1회 지급하고 rewards 항목을 돌려준다.
    (record_point 는 services._record_point 를 주입받는다 — 원장 기록을 한 곳으로 모으기 위함)
    """
    from .models import MemberQuest, PointEntry

    done = set(
        MemberQuest.objects.filter(member=member).values_list("key", flat=True)
    )
    awarded: list[dict] = []
    # 표시 목록(active_quests)이 아니라 **후보 전체**를 본다 — 달성하는 순간
    # 표시 목록에서 빠지므로, 그것만 보면 보상을 놓친다.
    for q in build_candidates(member):
        if q.key in done or not q.is_completed:
            continue
        q.reward = min(q.reward, MAX_REWARD)
        _, created = MemberQuest.objects.get_or_create(
            member=member, key=q.key,
            defaults={"kind": q.kind, "title": q.title, "reward_points": q.reward},
        )
        if not created:      # 이미 지급됨(동시 요청 등)
            continue
        record_point(member, txn, q.reward, PointEntry.Reason.MISSION)
        awarded.append({"type": "quest", "title": q.title, "points": q.reward})
    return awarded
