"""
문자 문구 자동 생성 — 옵션 몇 개 고르면 문장을 만들어 준다.

프로젝트 컨벤션을 그대로 따른다(ai_order.py 와 같은 2단 구조):
1. **Gemini** (`GEMINI_API_KEY` 설정 시)
2. **규칙 기반 폴백** (키 없거나 호출 실패) — 키 없이도 기능이 동작해야 한다.

**모델 출력을 그대로 믿지 않는다.** 문자는 되돌릴 수 없고 글자수가 곧 요금이라,
돌아온 문장에서 금칙 요소를 걷어내고 길이를 우리가 다시 재서 자른다:

- `(광고)` 표기와 수신거부 안내는 **모델이 쓰지 못하게 하고 발송 직전에
  sender.py 가 붙인다.** 모델이 제멋대로 붙이면 두 번 들어가고, 번호를
  지어내면 법적 표기가 틀린 번호로 나간다.
- 치환 변수는 우리가 아는 것만 남긴다. `{이름}` 을 `{성함}` 으로 지어내면
  치환이 안 된 채 그대로 손님에게 간다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("slowstep")

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_REVISION = "2026-05-20"
GEMINI_TIMEOUT = 8

# 쓸 수 있는 치환 변수 — segments.render_message 와 반드시 같아야 한다.
VARS = ("{이름}", "{포인트}", "{등급}", "{스탬프}", "{방문}")

# 광고성은 "(광고) " (8바이트) + "\n\n무료수신거부 " + 번호가 먼저 먹는다.
AD_OVERHEAD = 34
SMS_BYTES = 90
LMS_BYTES = 1000            # 넉넉히. 실제 상한은 2,000바이트지만 문자로 쓸 길이가 아니다.

PURPOSES = {
    "comeback": "한동안 안 오신 손님께 다시 들러 달라고",
    "new_menu": "새로 나온 메뉴를 알리려고",
    "coupon": "쿠폰이나 혜택이 있다고 알리려고",
    "points": "쌓인 포인트를 쓰시라고",
    "thanks": "자주 와 주셔서 고맙다고",
    "notice": "휴무·영업시간 같은 매장 소식을 알리려고",
}
TONES = {
    "warm": "따뜻하고 정중하게, 과장 없이",
    "casual": "친근하고 가볍게, 반말은 쓰지 말고",
    "crisp": "군더더기 없이 짧고 담백하게",
}

PROMPT = """너는 동네 카페 '슬로우스텝'의 사장이다. 단골에게 보낼 문자 한 통을 쓴다.

목적: {purpose}
말투: {tone}
{benefit}

지켜야 할 것:
- 한국어로, **{budget}바이트 안에서**(한글 1자 = 2바이트) 끝낼 것. 이게 가장 중요하다.
- 쓸 수 있는 치환 변수는 {vars} 뿐이다. 다른 변수를 지어내지 마라. 안 써도 된다.
- "(광고)" 표기, 수신거부 안내, 전화번호, 링크는 **절대 쓰지 마라.** 시스템이 따로 붙인다.
- 이모지는 최대 1개. 느낌표 남발 금지. 없는 혜택을 지어내지 마라.
- 설명이나 따옴표 없이 **문자 본문만** 출력해라.

문자 본문:"""


def _bytes(text: str) -> int:
    """EUC-KR 기준 바이트 — 통신사가 재는 잣대."""
    return sum(2 if ord(ch) > 0x7F else 1 for ch in text)


def budget_for(is_ad: bool, long_form: bool) -> int:
    """이 조건에서 본문이 쓸 수 있는 바이트."""
    cap = LMS_BYTES if long_form else SMS_BYTES
    return cap - (AD_OVERHEAD if is_ad else 0)


def trim_to(text: str, budget: int) -> str:
    """예산을 넘으면 자른다. 문장 끝에서 자르고, 안 되면 글자에서 끊는다."""
    if _bytes(text) <= budget:
        return text
    # 문장 단위로 뒤에서부터 덜어낸다 — 말이 끊긴 문자는 안 보내느니만 못하다.
    parts = re.split(r"(?<=[.!?~다요])\s+", text)
    while len(parts) > 1:
        parts.pop()
        joined = " ".join(parts).strip()
        if _bytes(joined) <= budget:
            return joined
    out = ""
    for ch in text:
        if _bytes(out + ch) > budget:
            break
        out += ch
    return out.rstrip()


def sanitize(text: str, budget: int) -> str:
    """모델이 돌려준 문장에서 우리가 붙일 것·지어낸 것을 걷어낸다."""
    text = (text or "").strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    text = text.strip('"').strip("'").strip()

    # 우리가 발송 직전에 붙이는 것들 — 모델이 쓰면 두 번 들어간다.
    text = re.sub(r"\(\s*광고\s*\)", "", text)
    text = re.sub(r"무료\s*수신\s*거부.*$", "", text, flags=re.S)
    text = re.sub(r"\b\d{2,4}[-.\s]?\d{3,4}[-.\s]?\d{4}\b", "", text)   # 전화번호
    text = re.sub(r"https?://\S+", "", text)                            # 링크

    # 모르는 치환 변수는 통째로 지운다 — 그대로 손님에게 가면 사고다.
    text = re.sub(r"\{[^}]*\}", lambda m: m.group(0) if m.group(0) in VARS else "", text)

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return trim_to(text, budget)


def _fallback(purpose: str, benefit: str, tone: str, budget: int) -> str:
    """
    키가 없거나 호출이 실패해도 문장은 나와야 한다.

    화려할 필요는 없다. 사장님이 손볼 초안이면 충분하고, 무엇보다
    **AI가 없다고 기능이 멈추지 않는 것**이 중요하다.

    **적어 넣은 혜택을 먼저 세운다.** 예전에는 인사말을 앞에 깔고 혜택을
    뒤에 붙였는데, 광고성 SMS 는 본문 예산이 56바이트뿐이라 인사말이
    예산을 다 먹고 정작 사장님이 입력한 혜택이 잘려나갔다. 문자의 핵심은
    혜택이고, 인사는 자리가 남을 때 붙이는 것이다.
    """
    benefit = (benefit or "").strip()
    lead = "{이름}님, "
    closer = {
        "comeback": "오랜만에 들러 주세요.",
        "new_menu": "새 메뉴 나왔어요.",
        "coupon": "들르실 때 말씀해 주세요.",
        "points": "다음 방문 때 쓰실 수 있어요.",
        "thanks": "늘 찾아 주셔서 고맙습니다.",
        "notice": "슬로우스텝 안내 드립니다.",
    }.get(purpose, "슬로우스텝입니다.")

    if benefit:
        # 혜택 + 이름은 반드시 남긴다. 마무리 문장은 자리가 남으면 붙인다.
        core = f"{lead}{benefit}"
        with_closer = f"{core} {closer}"
        return with_closer if _bytes(with_closer) <= budget else trim_to(core, budget)

    full = {
        "comeback": "{이름}님, 오랜만이에요. 요즘 날씨에 어울리는 음료 준비해 뒀어요.",
        "new_menu": "{이름}님, 슬로우스텝에 새 메뉴가 나왔어요. 한번 맛보러 오세요.",
        "coupon": "{이름}님, 쓰실 수 있는 혜택이 있어요. 들르실 때 말씀해 주세요.",
        "points": "{이름}님, 모아둔 {포인트}P 가 있어요. 다음 방문 때 쓰실 수 있어요.",
        "thanks": "{이름}님, 늘 찾아 주셔서 고맙습니다. 오늘도 좋은 하루 보내세요.",
        "notice": "{이름}님, 슬로우스텝 매장 안내 드립니다.",
    }.get(purpose, "{이름}님, 슬로우스텝입니다.")
    if tone == "crisp":
        full = f"{lead}{closer}"
    return trim_to(full, budget)


def _gemini(purpose: str, benefit: str, tone: str, budget: int) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    prompt = PROMPT.format(
        purpose=PURPOSES.get(purpose, PURPOSES["notice"]),
        tone=TONES.get(tone, TONES["warm"]),
        benefit=(f"꼭 담을 내용: {benefit}" if benefit else "특별히 담을 혜택은 없다."),
        budget=budget,
        vars=", ".join(VARS),
    )
    body = json.dumps({"model": GEMINI_MODEL, "input": prompt}).encode()
    req = urllib.request.Request(
        GEMINI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "Api-Revision": GEMINI_API_REVISION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as res:
            payload = json.loads(res.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        log.warning("gemini 문구 생성 실패, 규칙 폴백: %s", exc)
        return None

    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "text" and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(payload)
    return "\n".join(out).strip() or None


def compose(purpose: str, benefit: str = "", tone: str = "warm",
            is_ad: bool = True, long_form: bool = False) -> dict:
    """옵션 → 문자 초안. `source` 로 AI가 썼는지 폴백인지 알려 준다."""
    budget = budget_for(is_ad, long_form)
    raw = _gemini(purpose, benefit, tone, budget)
    source = "gemini"
    if raw:
        text = sanitize(raw, budget)
        if not text:                       # 걷어내고 나니 남은 게 없다
            source, text = "rule", _fallback(purpose, benefit, tone, budget)
    else:
        source, text = "rule", _fallback(purpose, benefit, tone, budget)
    return {
        "text": text,
        "source": source,
        "bytes": _bytes(text),
        "budget": budget,
        "type": "SMS" if _bytes(text) + (AD_OVERHEAD if is_ad else 0) <= SMS_BYTES else "LMS",
    }
