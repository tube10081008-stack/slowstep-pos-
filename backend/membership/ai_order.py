"""
자연어 주문 파서 — "아아 두 잔이랑 라떼 하나 따뜻하게" → 장바구니 항목.

2단 구조(프로젝트 컨벤션: 키 미설정 시 폴백):
1. **Gemini** (`GEMINI_API_KEY` 설정 시) — 말장난·줄임말·복합 주문에 강함
2. **규칙 기반 폴백** (키 없거나 호출 실패) — 메뉴명·별칭·수량·옵션 키워드 매칭.
   결정적이고 빠르며 무료. 키 없이도 기능 전체가 동작한다.

**안전 규칙:** 모델 출력은 절대 그대로 믿지 않는다. 반환된 menu_item_id가 실제
판매 중인 메뉴인지, 옵션이 그 메뉴에 허용되는지, 수량이 상식 범위인지 서버가
전부 재검증한다. 금액은 여기서 계산하지 않는다(결제 시 services.py가 계산).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from .models import MenuItem

log = logging.getLogger("slowstep")

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_REVISION = "2026-05-20"
GEMINI_TIMEOUT = 8  # 초 — POS는 대기가 길면 쓸모없다

MAX_QTY = 20  # 한 줄 최대 수량(오인식 폭주 방지)
MAX_INPUT = 200  # 입력 길이 제한

# ── 한국어 수량 ──
NUM_WORDS = {
    "한": 1, "하나": 1, "일": 1, "두": 2, "둘": 2, "이": 2, "세": 3, "셋": 3,
    "삼": 3, "네": 4, "넷": 4, "사": 4, "다섯": 5, "오": 5, "여섯": 6, "육": 6,
    "일곱": 7, "칠": 7, "여덟": 8, "팔": 8, "아홉": 9, "구": 9, "열": 10, "십": 10,
}
COUNTERS = "잔|개|컵|병|조각|판"

# ── 옵션 키워드 ──
ICE_WORDS = ("아이스", "차가운", "차갑게", "냉", "ice", "시원한", "시원하게")
HOT_WORDS = ("핫", "따뜻한", "따뜻하게", "뜨거운", "뜨겁게", "온", "hot", "따듯한", "따듯하게")
DECAF_WORDS = ("디카페인", "디카페", "디카", "decaf")
OAT_WORDS = ("오트밀크", "오트", "귀리", "oat")
SHOT_WORDS = ("샷추가", "샷 추가", "샷", "연하게말고", "shot")

# 자주 쓰는 줄임말 → 메뉴명 일부
ALIASES = {
    "아아": ("아메리카노", "ice"),
    "아이스아메": ("아메리카노", "ice"),
    "아메": ("아메리카노", None),
    "따아": ("아메리카노", "hot"),
    "뜨아": ("아메리카노", "hot"),
    "카라": ("카페 라떼", None),
    "라떼": ("카페 라떼", None),
    "바닐라떼": ("바닐라 라떼", None),
    "바라": ("바닐라 라떼", None),
    "콜브": ("콜드브루", None),
    "아샷추": ("아이스티", "ice"),
}


class OrderParseError(Exception):
    """파싱 실패(사용자에게 보여줄 메시지)."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _menu_payload(items) -> list[dict]:
    """모델에 넘길 최소 메뉴 정보."""
    return [
        {
            "id": m.id,
            "name": m.name,
            "category": m.category,
            "temp": m.temp_option,          # hotice | ice | none
            "decaf": m.decaf_available,
            "oat": m.oatmilk_available,
            "shot": m.shot_available,
        }
        for m in items
    ]


# ────────────────────────── 규칙 기반 폴백 ──────────────────────────

def _find_menu(chunk: str, items) -> "MenuItem | None":
    """
    문장 조각에서 메뉴를 찾는다.

    매칭 우선순위: **전체 이름 → 별칭 → 이름의 단어(토큰)**.

    전체 이름이 별칭보다 먼저다. 별칭 '라떼'(→카페 라떼)가 '바닐라 라떼'·
    '콜드브루 라떼' 같은 정식 메뉴명을 가로채면 안 되기 때문.
    토큰 매칭도 필요하다 — 직원은 "플레인 휘낭시에"를 그냥 "휘낭시에"라고 부른다.
    같은 점수면 **이름이 짧은 쪽**(가장 기본 메뉴)을 고른다.
    """
    c = _norm(chunk)
    if not c:
        return None

    # 1) 전체 이름이 통째로 들어있으면 최우선 (가장 긴 이름이 이김)
    full = [m for m in items if _norm(m.name) in c]
    if full:
        return max(full, key=lambda m: len(_norm(m.name)))

    # 2) 별칭 후보 (아아·따아·라떼 …)
    alias_hit, alias_score = None, 0
    for alias, (name_part, _) in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in c:
            exact = [m for m in items if _norm(name_part) == _norm(m.name)]
            partial = [m for m in items if _norm(name_part) in _norm(m.name)]
            hit = exact[0] if exact else (
                min(partial, key=lambda m: len(_norm(m.name))) if partial else None
            )
            if hit is not None:
                alias_hit, alias_score = hit, len(alias)
                break

    # 3) 이름의 단어 단위 매칭 — 가장 긴 일치 토큰이 이김
    best, best_score = None, 0
    for m in items:
        tokens = [t for t in re.split(r"[\s()·]+", m.name) if len(_norm(t)) >= 2]
        score = max((len(_norm(t)) for t in tokens if _norm(t) in c), default=0)
        if score > best_score or (
            score == best_score and score > 0
            and best is not None and len(_norm(m.name)) < len(_norm(best.name))
        ):
            best, best_score = m, score

    # 더 길게 일치한 쪽을 채택. 동점이면 별칭(직접 지정한 규칙)을 우선.
    # → "미숫가루 크림 라떼"는 토큰 '미숫가루'(4)가 별칭 '라떼'(2)를 이긴다.
    if alias_hit is not None and alias_score >= best_score:
        return alias_hit
    return best or alias_hit


def _find_qty(chunk: str) -> int:
    """수량 추출: '2잔', '두 잔', '3개' → 숫자. 없으면 1."""
    c = chunk.replace(" ", "")
    m = re.search(rf"(\d+)\s*(?:{COUNTERS})", c)
    if m:
        return max(1, min(MAX_QTY, int(m.group(1))))
    for word, n in sorted(NUM_WORDS.items(), key=lambda x: -len(x[0])):
        if re.search(rf"{word}\s*(?:{COUNTERS})", c):
            return n
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", c)
    if m:
        v = int(m.group(1))
        if 1 <= v <= MAX_QTY:
            return v
    return 1


def _find_alias_temp(chunk: str) -> str | None:
    c = _norm(chunk)
    for alias, (_, temp) in sorted(ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in c and temp:
            return temp
    return None


def rule_parse(text: str, items) -> list[dict]:
    """규칙 기반 파싱. 쉼표·'랑'·'하고' 등으로 나눠 각 조각을 해석."""
    parts = re.split(r"[,\n]|그리고|하고|이랑|랑(?![떼])|플러스|\+", text)
    lines: list[dict] = []
    for part in parts:
        if not part.strip():
            continue
        m = _find_menu(part, items)
        if m is None:
            continue
        c = _norm(part)
        temp = ""
        if m.temp_option == MenuItem.Temp.HOTICE:
            if any(w in c for w in HOT_WORDS):
                temp = "hot"
            elif any(w in c for w in ICE_WORDS):
                temp = "ice"
            else:
                temp = _find_alias_temp(part) or "ice"
        elif m.temp_option == MenuItem.Temp.ICE:
            temp = "ice"

        lines.append(
            {
                "menu_item_id": m.id,
                "quantity": _find_qty(part),
                "temperature": temp,
                "decaf": m.decaf_available and any(w in c for w in DECAF_WORDS),
                "oatmilk": m.oatmilk_available and any(w in c for w in OAT_WORDS),
                "shot": m.shot_available and any(w in c for w in SHOT_WORDS),
            }
        )
    return lines


# ────────────────────────── Gemini ──────────────────────────

PROMPT = """당신은 한국 카페 POS의 주문 해석기입니다.
직원이 말한 주문을 아래 메뉴판에서 골라 JSON 배열로만 답하세요.

메뉴판(JSON):
{menu}

규칙:
- 반드시 메뉴판에 있는 id만 사용
- temperature: 메뉴 temp가 "hotice"면 "hot" 또는 "ice", "ice"면 "ice", "none"이면 ""
- 명시가 없으면 아이스로 간주. "따뜻하게/핫/뜨거운"이면 hot
- decaf/oat/shot은 해당 메뉴에서 허용될 때만 true
- 줄임말: 아아=아이스 아메리카노, 따아/뜨아=따뜻한 아메리카노, 라떼=카페 라떼
- 수량이 없으면 1

출력 형식(다른 텍스트 없이 JSON 배열만):
[{{"menu_item_id": 1, "quantity": 2, "temperature": "ice", "decaf": false, "oatmilk": false, "shot": false}}]

주문: {text}"""


def _collect_text(node) -> str:
    """응답 JSON 어디에 텍스트가 있든 긁어모은다(스키마 변화에 견디도록)."""
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

    walk(node)
    return "\n".join(out)


def _extract_json_array(text: str):
    """```json 펜스나 잡담이 섞여도 JSON 배열만 뽑아낸다."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def gemini_parse(text: str, items) -> list[dict] | None:
    """Gemini 호출. 키 없음·실패 시 None(→ 규칙 폴백)."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None

    body = json.dumps(
        {
            "model": GEMINI_MODEL,
            # 이 모델부터 temperature/top_p/top_k는 폐기됨 — 보내지 않는다.
            "input": PROMPT.format(
                menu=json.dumps(_menu_payload(items), ensure_ascii=False), text=text
            ),
        }
    ).encode()

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
        log.warning("gemini 주문 파싱 실패, 규칙 폴백: %s", exc)
        return None

    parsed = _extract_json_array(_collect_text(payload))
    if not isinstance(parsed, list):
        log.warning("gemini 응답에서 JSON 배열을 찾지 못함 — 규칙 폴백")
        return None
    return parsed


# ────────────────────────── 검증 + 진입점 ──────────────────────────

def _sanitize(raw_lines, items) -> list[dict]:
    """모델/규칙 결과를 실제 메뉴 기준으로 재검증(신뢰하지 않음)."""
    by_id = {m.id: m for m in items}
    clean: list[dict] = []
    for row in raw_lines or []:
        if not isinstance(row, dict):
            continue
        try:
            mid = int(row.get("menu_item_id"))
        except (TypeError, ValueError):
            continue
        m = by_id.get(mid)
        if m is None or m.sold_out:
            continue
        try:
            qty = int(row.get("quantity", 1))
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(MAX_QTY, qty))

        temp = str(row.get("temperature", "") or "").lower()
        if m.temp_option == MenuItem.Temp.HOTICE:
            temp = temp if temp in ("hot", "ice") else "ice"
        elif m.temp_option == MenuItem.Temp.ICE:
            temp = "ice"
        else:
            temp = ""

        clean.append(
            {
                "menu_item_id": m.id,
                "name": m.name,
                "quantity": qty,
                "temperature": temp,
                # 옵션은 그 메뉴에 허용될 때만
                "decaf": bool(row.get("decaf")) and m.decaf_available,
                "oatmilk": bool(row.get("oatmilk")) and m.oatmilk_available,
                "shot": bool(row.get("shot")) and m.shot_available,
            }
        )
    return clean


def parse_order(text: str) -> dict:
    """
    자연어 → 검증된 주문 항목.
    반환: {"items": [...], "source": "gemini"|"rule", "text": 원문}
    """
    text = (text or "").strip()
    if not text:
        raise OrderParseError("주문 내용을 입력하세요.")
    if len(text) > MAX_INPUT:
        raise OrderParseError(f"주문이 너무 깁니다(최대 {MAX_INPUT}자).")

    items = list(MenuItem.objects.filter(is_available=True))
    if not items:
        raise OrderParseError("판매 중인 메뉴가 없습니다.")

    source = "gemini"
    lines = gemini_parse(text, items)
    if lines is None:
        source = "rule"
        lines = rule_parse(text, items)
    else:
        # 모델이 빈 배열을 주면 규칙으로 한 번 더 시도
        checked = _sanitize(lines, items)
        if not checked:
            source = "rule"
            lines = rule_parse(text, items)
        else:
            return {"items": checked, "source": source, "text": text}

    clean = _sanitize(lines, items)
    if not clean:
        raise OrderParseError("주문에서 메뉴를 찾지 못했습니다. 메뉴 이름을 포함해 말해주세요.")
    return {"items": clean, "source": source, "text": text}
