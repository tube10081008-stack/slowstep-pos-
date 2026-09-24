"""
가입용 무작위 닉네임 — 실명 대신 쓰는 표시 이름.

왜 실명을 받지 않나:
- 연락처만으로 회원을 식별할 수 있어 **이름은 수집하지 않아도 되는 정보**다.
  개인정보는 적게 가질수록 관리 부담도 유출 위험도 줄어든다(최소 수집 원칙).
- 매장 화면·랭킹·알림톡에 실명이 뜨는 것보다 닉네임이 부담이 적다.

'행동 + 동물' 조합(예: 느긋한 수달, 낮잠자는 판다)으로 만들며, 이미 쓰는
닉네임은 피한다. 조합이 모두 소진되면 뒤에 숫자를 붙인다.
"""
from __future__ import annotations

import random

ACTIONS = [
    "느긋한", "졸린", "부지런한", "산책하는", "춤추는", "노래하는", "사색하는",
    "여유로운", "콧노래하는", "기지개펴는", "하품하는", "폴짝뛰는", "뒹구는",
    "낮잠자는", "책읽는", "구름보는", "별세는", "미소짓는", "살금걷는",
    "두리번대는", "포근한", "나른한", "상냥한", "씩씩한", "조용한", "다정한",
    "엉뚱한", "새침한", "달리는", "휘파람부는", "간식먹는", "볕쬐는",
]

ANIMALS = [
    "수달", "고양이", "판다", "펭귄", "라쿤", "알파카", "코알라", "햄스터",
    "다람쥐", "토끼", "여우", "곰", "사슴", "고슴도치", "물개", "미어캣",
    "오리", "부엉이", "올빼미", "돌고래", "나무늘보", "두더지", "청설모",
    "너구리", "카피바라", "왈라비", "친칠라", "바다표범", "거북이", "레서판다",
]

MAX_LEN = 50  # Member.name 필드 길이


def _candidates(rng: random.Random) -> list[str]:
    pairs = [f"{a} {b}" for a in ACTIONS for b in ANIMALS]
    rng.shuffle(pairs)
    return pairs


def generate_nickname(existing: set[str] | None = None, rng=None) -> str:
    """
    사용 중이 아닌 '행동 동물' 닉네임 하나를 반환.

    existing 을 주지 않으면 DB에서 현재 이름들을 읽어 중복을 피한다.
    """
    if existing is None:
        from .models import Member

        existing = set(Member.objects.values_list("name", flat=True))
    rng = rng or random.Random()

    for name in _candidates(rng):
        if name not in existing:
            return name[:MAX_LEN]

    # 조합(약 960개)이 모두 쓰인 경우 — 숫자를 붙여 유일하게 만든다.
    base = rng.choice(ACTIONS) + " " + rng.choice(ANIMALS)
    n = 2
    while f"{base} {n}" in existing:
        n += 1
    return f"{base} {n}"[:MAX_LEN]
