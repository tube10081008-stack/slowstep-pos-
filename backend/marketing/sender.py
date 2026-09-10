"""
발송 채널 래퍼 — 캠페인이 대행사를 직접 알지 않게 한 겹 둔다.

`alimtalk.py` 를 대체한다. 실제로 나가는 건 알림톡이 아니라 **문자(SMS/LMS)**라
이름을 맞췄다. 알림톡을 붙일 땐 여기 채널을 하나 더 얹으면 된다.

여기가 책임지는 것:
- 광고성 표기((광고) + 무료수신거부)를 **본문에 강제로 붙인다**
- 광고성 야간 발송(21시~08시)을 **막는다** — 정보통신망법 제50조 제3항
- 키가 없으면 Mock 으로 떨어져 세그먼트·치환·로그 흐름을 그대로 검증한다
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .solapi import SolapiClient, SolapiError

# 광고성 메시지를 보낼 수 있는 시간(로컬 기준). 이 밖은 사전 별도 동의가
# 있어야 하는데, 우리는 그 동의를 따로 받지 않으므로 그냥 막는다.
AD_HOURS = (8, 21)          # 08:00 이상 21:00 미만


class SendError(Exception):
    """발송 요청 자체가 실패."""


@dataclass
class SendResult:
    success: bool
    phone: str
    reason: str = ""
    mocked: bool = False


def ad_window_open(now=None) -> bool:
    """지금 광고성 문자를 보내도 되는 시간인가."""
    hour = timezone.localtime(now or timezone.now()).hour
    return AD_HOURS[0] <= hour < AD_HOURS[1]


class MessageClient:
    """캠페인이 쓰는 발송기. 대행사 교체는 이 안에서 끝난다."""

    def __init__(self) -> None:
        self.solapi = SolapiClient()

    @property
    def is_live(self) -> bool:
        return self.solapi.is_live

    def decorate(self, body: str, is_ad: bool) -> str:
        """광고성이면 (광고) 표기와 수신거부 안내를 부가."""
        if not is_ad:
            return body
        return f"(광고) {body}\n\n무료수신거부 {settings.SMS_OPT_OUT_NUMBER}"

    def send_many(self, items: list[tuple[str, str]]) -> dict[str, SendResult]:
        """
        [(수신번호, 완성된 본문)] → {번호: 결과}.

        **키가 없으면 Mock 성공**을 돌려준다. 대행사 계약 전에도 세그먼트와
        치환·로그를 끝까지 돌려 볼 수 있어야 실제 발송 날 처음 보는 화면이
        없다.
        """
        if not items:
            return {}
        if not self.is_live:
            return {
                phone: SendResult(True, phone, "Mock 발송", mocked=True)
                for phone, _ in items
            }
        try:
            bulk = self.solapi.send_many(items)
        except SolapiError as exc:
            raise SendError(str(exc)) from exc
        return {
            o.phone: SendResult(o.success, o.phone, o.reason)
            for o in bulk.outcomes
        }

    def send_one(self, phone: str, body: str) -> SendResult:
        return self.send_many([(phone, body)]).get(
            phone, SendResult(False, phone, "결과 없음")
        )
