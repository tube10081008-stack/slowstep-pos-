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
- 퀘스트는 **한 테마(그룹)씩만 활성화**한다. 성격이 다른 목표 세 개를
  나란히 던지면 뭘 하라는 건지 안 잡힌다. 한 챕터를 다 깨면 보너스를 주고
  다음 챕터가 열린다.
- 보상에 상한을 둔다 — 적립비용이 마진에서 차감되므로(margins.py)
  예측 가능해야 한다. 결제 1건당 지급 총액도 묶어 둔다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import MenuItem, OrderItem, Transaction

MAX_REWARD = 2000               # 퀘스트 1개 보상 상한
MAX_REWARD_PER_CHECKOUT = 3000  # 결제 1건에서 나갈 수 있는 퀘스트 보상 총액
GROUP_SIZE = 3                  # 한 그룹에 담는 퀘스트 수 상한

TASTE_REWARD = 200      # '처음 만나기' 1건 — 문턱이 낮으니 보상도 낮게
GROUP_CLEAR_BONUS = 500  # 챕터 완주 보너스(공통)
COLLECTION_MIN_KINDS = 3  # 이 종류 수 미만인 갈래는 도장깨기로 치지 않는다

# 그룹(챕터) 정의 — 위에서부터 우선순위.
# (키, 제목, 한 줄 설명, 클리어 보너스 포인트, 클리어 시 룰렛 기회)
# 미달성 퀘스트가 남은 **첫 번째** 그룹 하나만 손님에게 보여 준다.
#
# 도장깨기는 포인트 대신 **룰렛 기회**로 준다 — 메뉴를 전부 도는 건 가장
# 품이 드는 챕터라, 숫자로 주는 것보다 한 번 돌리는 재미가 남는 게 낫다.
GROUPS = (
    ("comeback", "다시 만나기", "오랜만이에요. 발걸음만 해주시면 돼요", 0, 0),
    ("collection", "도장깨기", "거의 다 모으셨어요. 마무리만 남았습니다", 0, 1),
    ("taste", "취향 탐험대", "아직 안 드셔본 갈래를 하나씩", GROUP_CLEAR_BONUS, 0),
    ("rhythm", "나만의 리듬", "평소 오시던 박자를 이어가요", GROUP_CLEAR_BONUS, 0),
    ("option", "한 끗 다르게", "늘 마시던 잔을 조금만 바꿔서", GROUP_CLEAR_BONUS, 0),
    ("timeslot", "다른 시간의 슬로우스텝", "같은 자리도 시간에 따라 달라요", GROUP_CLEAR_BONUS, 0),
)
GROUP_META = {k: (t, d, b, s) for k, t, d, b, s in GROUPS}
GROUP_ORDER = [k for k, *_ in GROUPS]


@dataclass
class Quest:
    key: str            # 달성 기록용 안정 식별자
    kind: str           # taste / collection / rhythm / stretch / option / timeslot / comeback
    title: str
    description: str
    progress: int
    target: int
    reward: int
    group: str = ""     # 비면 kind 를 그룹으로 본다

    def __post_init__(self):
        if not self.group:
            self.group = self.kind

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

    # ── 2) 컬렉션 마무리: 거의 다 모은 카테고리(2종 이내로 남음) ──
    # **다 모은 것(left==0)도 후보에 남긴다.** 예전에는 1~2종 남은 것만 뽑았는데,
    # 마지막 한 잔을 마시는 순간 후보에서 사라져 보상을 줄 기회가 없었다
    # (취향 탐험대 쪽에 적어둔 것과 같은 함정). 후보는 두고 진행률로 판정한다.
    # 정렬은 아직 남은 것 먼저 — 다 깬 칸이 앞자리를 차지하면 화면에
    # 할 일이 안 보인다.
    picked = 0
    for cat, c in sorted(coll.items(),
                         key=lambda kv: (kv[1]["total"] == kv[1]["tried"],
                                         kv[1]["total"] - kv[1]["tried"])):
        left = c["total"] - c["tried"]
        # 종류가 3개는 돼야 '정복'이라 부를 만하다. 1~2종짜리 갈래(그날의
        # 디저트 등)까지 세면 한 잔 마시고 챕터가 끝나 룰렛이 공짜로 나간다.
        if c["total"] >= COLLECTION_MIN_KINDS and c["tried"] > 0 and 0 <= left <= 2:
            out.append(Quest(
                key=f"collection:{cat}", kind="collection",
                title=(f"{labels.get(cat, cat)} 정복까지 {left}종" if left
                       else f"{labels.get(cat, cat)} 정복"),
                description=(f"다음 도전: {c['next']}" if c["next"] else "마지막 한 잔!"),
                progress=c["tried"], target=c["total"], reward=1000,
            ))
            picked += 1
            if picked >= GROUP_SIZE:
                break

    # ── 3) 취향 확장: 선호 순서대로 '처음 만나기' ──
    # 후보 조건에 '아직 안 먹었을 것'을 넣으면 먹는 순간 후보에서 사라져
    # 보상을 지급할 기회가 없어진다. 후보는 고정하고 진행률로 판정한다.
    picked = 0
    for cat in ("dessert", "coldbrew", "tea", "ade", "noncoffee"):
        if cat not in coll:
            continue
        out.append(Quest(
            key=f"taste:{cat}", kind="taste",
            title=f"{labels.get(cat, cat)} 처음 만나기",
            description=(f"{coll[cat]['next']} 어떠세요?" if coll[cat]["next"]
                         else "새로운 맛을 만나요"),
            progress=min(1, cats.get(cat, 0)), target=1, reward=TASTE_REWARD,
        ))
        picked += 1
        if picked >= GROUP_SIZE:
            break

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
            key=f"stretch:{_month_key(now)}", kind="stretch", group="rhythm",
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


def group_key(group: str) -> str:
    """그룹 클리어 보너스를 기록하는 키."""
    return f"group:{group}"


def _grouped(member) -> dict[str, list[Quest]]:
    """후보를 그룹별로 모아 상한(GROUP_SIZE)까지 자른다."""
    out: dict[str, list[Quest]] = {}
    for q in build_candidates(member):
        q.reward = min(q.reward, MAX_REWARD)
        bucket = out.setdefault(q.group, [])
        if len(bucket) < GROUP_SIZE:
            bucket.append(q)
    return out


def _done_keys(member) -> set[str]:
    from .models import MemberQuest

    return set(MemberQuest.objects.filter(member=member).values_list("key", flat=True))


def active_group(member) -> dict | None:
    """
    지금 활성화된 **한 챕터**. 미달성 퀘스트가 남은 첫 그룹을 통째로 돌려준다.

    이미 깬 퀘스트도 목록에 남겨 둔다 — 빠지면 '2/3 달성'이 말이 안 되고,
    그룹으로 묶은 이유(완주감)가 사라진다.
    """
    buckets = _grouped(member)
    done = _done_keys(member)
    for key in GROUP_ORDER:
        items = buckets.get(key)
        if not items:
            continue
        if all(q.is_completed for q in items):
            continue                       # 다 깬 챕터 — 다음으로 넘어간다
        title, desc, bonus, bonus_spins = GROUP_META[key]
        return {
            "key": key,
            "title": title,
            "description": desc,
            "bonus": bonus,
            "bonus_spins": bonus_spins,
            "bonus_earned": group_key(key) in done,
            "done": sum(1 for q in items if q.is_completed),
            "total": len(items),
            "items": [q.to_dict() for q in items],
        }
    return None


def evaluate_and_award(member, txn, record_point) -> list[dict]:
    """
    결제 직후 호출 — 달성한 퀘스트에 보상을 1회 지급하고 rewards 항목을 돌려준다.
    (record_point 는 services._record_point 를 주입받는다 — 원장 기록을 한 곳으로 모으기 위함)
    """
    from .models import MemberQuest, PointEntry

    done = _done_keys(member)
    buckets = _grouped(member)
    awarded: list[dict] = []
    budget = MAX_REWARD_PER_CHECKOUT

    def _pay(key, kind, title, points, label, spins=0) -> bool:
        """
        예산 안에서 1회만 지급. 예산을 넘으면 기록하지 않아 다음 방문에 지급된다.

        spins 는 룰렛 기회 — 포인트가 아니라 예산에 잡히지 않는다(당첨 쿠폰은
        손님이 직접 돌려서 받고, 종류마다 부담이 달라 미리 세지 못한다).
        """
        nonlocal budget
        if key in done or points > budget:
            return False
        _, created = MemberQuest.objects.get_or_create(
            member=member, key=key,
            defaults={"kind": kind, "title": title, "reward_points": points},
        )
        if not created:                    # 이미 지급됨(동시 요청 등)
            return False
        done.add(key)
        budget -= points
        if points:
            record_point(member, txn, points, PointEntry.Reason.MISSION)
        item = {"type": label, "title": title, "points": points}
        if spins:
            member.spins += 1               # 저장은 호출부(_apply_stamp_and_tier 뒤)에서
            item["spins"] = spins
            item["description"] = "룰렛 기회 1번! 멤버십에서 돌려보세요"
        awarded.append(item)
        return True

    # 화면에 뜬 챕터가 아니라 **후보 전체**를 본다 — 달성하는 순간 다음 챕터로
    # 넘어가므로, 활성 그룹만 보면 방금 깬 퀘스트의 보상을 놓친다.
    for gkey in GROUP_ORDER:
        items = buckets.get(gkey) or []
        for q in items:
            if q.is_completed:
                _pay(q.key, q.kind, q.title, q.reward, "quest")
        # 챕터 완주 보너스 — 퀘스트가 전부 지급 완료된 뒤에만.
        title, _desc, bonus, spins = GROUP_META[gkey]
        if (bonus or spins) and items and all(q.key in done for q in items):
            _pay(group_key(gkey), "group", f"{title} 완주", bonus,
                 "quest_group", spins=spins)
    return awarded
