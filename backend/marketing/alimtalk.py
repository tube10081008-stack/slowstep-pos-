"""
알림톡(카카오) 발송 클라이언트.

- ALIMTALK_API_KEY 설정 시 실제 발송 대행사(NHN Cloud/Solapi/Aligo 등) 연동 지점.
- 미설정 시 Mock 발송 성공 반환 → 키 없이 세그먼트·캠페인 전체 흐름 검증.

광고성 메시지는 수신 동의 + (광고) 표기 + 무료 수신거부가 법적 요구사항.
정보성/광고성 판단은 Campaign.is_ad로 분기하며, 본 모듈은 표기를 자동 부가한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


class AlimtalkError(Exception):
    """발송 실패."""


@dataclass
class SendResult:
    success: bool
    phone: str
    reason: str = ""
    mocked: bool = False


class AlimtalkClient:
    """알림톡 발송 래퍼."""

    def __init__(self) -> None:
        self.api_key = settings.ALIMTALK_API_KEY
        self.sender_key = settings.ALIMTALK_SENDER_KEY
        self.api_base = (settings.ALIMTALK_API_BASE or "").rstrip("/")

    @property
    def is_live(self) -> bool:
        return bool(self.api_key and self.sender_key and self.api_base)

    def decorate(self, body: str, is_ad: bool) -> str:
        """광고성이면 (광고) 표기와 수신거부 안내를 부가."""
        if not is_ad:
            return body
        return f"(광고) {body}\n\n무료수신거부 {settings.ALIMTALK_OPT_OUT_NUMBER}"

    def send(self, phone: str, body: str) -> SendResult:
        """
        단건 발송. 키 미설정 시 Mock 성공.
        실연동 시 이 지점에서 대행사 API 호출(템플릿 검수 필요).
        """
        if not self.is_live:
            return SendResult(success=True, phone=phone, mocked=True)

        # 실연동 자리표시자: 대행사별 페이로드/엔드포인트로 교체.
        # 예) NHN Cloud Alimtalk v2.3 /messages, Solapi /messages/v4/send 등.
        raise AlimtalkError(
            "실 발송 대행사 연동 미구현(키만 설정됨). alimtalk.py의 send()를 "
            "사용 대행사 API에 맞게 구현하세요."
        )
