"""
매장 PIN 인증 — 점주/직원 화면(POS·대시보드) 보호.

설계:
- PIN을 맞히면 **서명된 토큰**을 발급하고, 이후 요청은 `X-Store-Token` 헤더로 인증한다.
  (클라이언트에서만 검사하면 API를 직접 호출해 우회할 수 있으므로 서버에서 강제)
- 토큰은 Django SECRET_KEY로 서명(TimestampSigner) → 서버에 세션 저장 없이 검증 가능
  (서버리스 환경에 적합). SECRET_KEY를 바꾸면 모든 토큰이 즉시 무효화된다.
- 무차별 대입 방어: IP당 시도 횟수를 제한한다. 4자리 PIN은 조합이 1만 개뿐이라
  이 제한이 실질적인 방어선이다.
- 고객용 화면(멤버십 조회)은 공개 유지 → 아래 permission을 붙이지 않는다.

PIN은 환경변수 `STORE_PIN`으로 바꾼다(미설정 시 기본값). 저장소가 공개라면
반드시 배포 환경변수로 재정의할 것.
"""
from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from rest_framework.permissions import BasePermission

TOKEN_SALT = "slowstep.store.pin"
# 매장 기기에 한 번 입력하면 오래 유지(재입력 부담 최소화).
TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30일

HEADER = "HTTP_X_STORE_TOKEN"

# 무차별 대입 제한: 창(초)당 최대 시도.
ATTEMPT_WINDOW = 300  # 5분
ATTEMPT_LIMIT = 7


class PinRateLimited(Exception):
    """시도 횟수 초과."""


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=TOKEN_SALT)


def store_pin() -> str:
    return str(getattr(settings, "STORE_PIN", "") or "")


def issue_token() -> str:
    """PIN 검증 통과 시 발급하는 서명 토큰."""
    return _signer().sign("store")


def token_valid(token: str) -> bool:
    if not token:
        return False
    try:
        _signer().unsign(token, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return False
    return True


def _attempt_key(ip: str) -> str:
    return f"pin-attempts:{ip}"


def client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def check_pin(request, pin: str) -> bool:
    """
    PIN 검증(+시도 제한). 초과 시 PinRateLimited.

    성공하면 시도 카운터를 초기화한다.
    """
    ip = client_ip(request)
    key = _attempt_key(ip)
    attempts = cache.get(key, 0)
    if attempts >= ATTEMPT_LIMIT:
        raise PinRateLimited()

    expected = store_pin()
    # 길이가 달라도 상수시간 비교 유지
    ok = bool(expected) and signing.constant_time_compare(str(pin or ""), expected)
    if ok:
        cache.delete(key)
        return True

    # 실패만 카운트. 창 만료 시 자동 해제.
    cache.set(key, attempts + 1, ATTEMPT_WINDOW)
    return False


def request_authorized(request) -> bool:
    return token_valid(request.META.get(HEADER, ""))


class StorePinPermission(BasePermission):
    """매장 PIN 토큰이 있어야 접근 가능(점주·직원 전용 API)."""

    message = "매장 PIN 인증이 필요합니다."

    def has_permission(self, request, view) -> bool:
        return request_authorized(request)
