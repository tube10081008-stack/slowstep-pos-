"""
솔라피(Solapi) 문자 발송 클라이언트.

토스 연동(membership/payments.py)과 같은 방식으로 **표준 라이브러리 urllib**만
쓴다. 서버리스에 올라가는 코드라 의존성을 하나 늘리면 콜드스타트가 그만큼
느려지고, 배포에서만 터지는 실패 지점이 하나 늘어난다.

인증은 HMAC-SHA256:
    signature = HMAC-SHA256(date + salt, API_SECRET)
    Authorization: HMAC-SHA256 apiKey=..., date=..., salt=..., signature=...

**API_SECRET 은 서명에만 쓰고 어디에도 싣지 않는다.** 로그·예외 메시지에
섞이면 그대로 유출이라, 오류를 만들 때도 응답 본문만 잘라 넣는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from urllib import error, request

from django.conf import settings

API_BASE = "https://api.solapi.com"
SEND_MANY = "/messages/v4/send-many/detail"

# 한 번에 보낼 건수. 솔라피 한도는 훨씬 크지만 서버리스 응답 시간이
# 먼저 걸린다 — 220명이면 3번 나눠 보내는 정도가 안전하다.
CHUNK = 100
TIMEOUT = 15

# SMS 는 EUC-KR 기준 90바이트까지. 넘으면 LMS(장문)로 자동 승격한다.
# 한글 1자 = 2바이트라, 45자 남짓이 경계다.
SMS_MAX_BYTES = 90


class SolapiError(Exception):
    """발송 요청 자체가 실패(인증·네트워크·형식). 개별 수신 실패와 다르다."""


@dataclass
class SendOutcome:
    """수신번호별 결과. 성공/실패를 회원 단위로 되짚어야 로그가 남는다."""

    phone: str
    success: bool
    reason: str = ""


@dataclass
class BulkResult:
    outcomes: list[SendOutcome] = field(default_factory=list)
    group_id: str = ""

    def by_phone(self) -> dict[str, SendOutcome]:
        return {o.phone: o for o in self.outcomes}


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def message_type(text: str) -> str:
    """SMS 냐 LMS 냐. 길이를 잘못 보면 발송이 통째로 거부된다."""
    return "SMS" if len(text.encode("euc-kr", "ignore")) <= SMS_MAX_BYTES else "LMS"


class SolapiClient:
    def __init__(self) -> None:
        self.api_key = settings.SOLAPI_API_KEY
        self.api_secret = settings.SOLAPI_API_SECRET
        self.sender = _digits(settings.SMS_SENDER_PHONE)

    @property
    def is_live(self) -> bool:
        return bool(self.api_key and self.api_secret and self.sender)

    def _auth_header(self) -> str:
        date = datetime.now(dt_timezone.utc).isoformat()
        salt = secrets.token_hex(16)
        signature = hmac.new(
            self.api_secret.encode(), (date + salt).encode(), hashlib.sha256
        ).hexdigest()
        return (
            f"HMAC-SHA256 apiKey={self.api_key}, date={date}, "
            f"salt={salt}, signature={signature}"
        )

    def _post(self, path: str, payload: dict) -> dict:
        req = request.Request(
            API_BASE + path,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header(),
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode() or "{}")
        except error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise SolapiError(f"솔라피 응답 {exc.code}: {body}") from exc
        except TimeoutError as exc:
            # 접수까지 됐는지 알 수 없는 유일한 경우다. 그냥 '실패'라고 하면
            # 사장님이 다시 누르고, 같은 손님이 두 통 받는다.
            raise SolapiError(
                "솔라피 응답이 시간 안에 오지 않았습니다. **이미 발송됐을 수 "
                "있으니** 솔라피 콘솔에서 발송 내역을 확인한 뒤 다시 보내세요."
            ) from exc
        except error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise SolapiError(
                    "솔라피 응답이 시간 안에 오지 않았습니다. **이미 발송됐을 수 "
                    "있으니** 솔라피 콘솔에서 발송 내역을 확인한 뒤 다시 보내세요."
                ) from exc
            raise SolapiError(f"솔라피에 연결하지 못했습니다: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise SolapiError("솔라피 응답을 해석하지 못했습니다.") from exc

    def send_many(self, items: list[tuple[str, str]]) -> BulkResult:
        """
        [(수신번호, 본문)] 을 나눠 보낸다.

        솔라피는 **실패한 건만** `failedMessageList` 로 돌려준다. 그래서
        기본을 성공으로 두고 실패한 번호만 덮어쓴다 — 반대로 하면
        응답 형식이 조금만 달라져도 보낸 문자가 전부 '실패'로 기록된다.
        """
        result = BulkResult()
        if not self.is_live:
            raise SolapiError("솔라피 키가 설정되지 않았습니다.")

        for i in range(0, len(items), CHUNK):
            chunk = items[i:i + CHUNK]
            payload = {
                "messages": [
                    {
                        "to": _digits(to),
                        "from": self.sender,
                        "text": text,
                        "type": message_type(text),
                    }
                    for to, text in chunk
                ]
            }
            body = self._post(SEND_MANY, payload)
            result.group_id = (body.get("groupInfo") or {}).get("groupId", "") or result.group_id

            failed = {}
            for row in body.get("failedMessageList") or []:
                failed[_digits(row.get("to", ""))] = (
                    f"{row.get('statusMessage') or ''}"
                    f"({row.get('statusCode') or ''})".strip()
                )
            for to, _text in chunk:
                key = _digits(to)
                if key in failed:
                    result.outcomes.append(SendOutcome(to, False, failed[key]))
                else:
                    result.outcomes.append(SendOutcome(to, True))
        return result

    def send_one(self, phone: str, text: str) -> SendOutcome:
        """단건 발송(쿠폰 발행 알림 등). 실패해도 예외 대신 결과로 돌려준다."""
        return self.send_many([(phone, text)]).outcomes[0]
