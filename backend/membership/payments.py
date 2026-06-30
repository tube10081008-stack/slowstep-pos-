"""
Toss페이먼츠 결제 승인 클라이언트.

설계 상세는 docs/TOSS-INTEGRATION.md.
- TOSS_SECRET_KEY가 설정되면 실제 Toss confirm API 호출.
- 미설정 시 Mock 승인 성공을 반환(스캐폴드/데모에서 키 없이 전체 로직 검증).
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib import error, request

from django.conf import settings


class TossError(Exception):
    """Toss 승인 실패."""


@dataclass
class TossResult:
    approved: bool
    payment_key: str
    order_id: str
    amount: int
    raw: dict
    mocked: bool = False


class TossClient:
    """Toss 결제 승인 래퍼."""

    def __init__(self) -> None:
        self.secret_key = settings.TOSS_SECRET_KEY
        self.api_base = settings.TOSS_API_BASE.rstrip("/")

    @property
    def is_live(self) -> bool:
        return bool(self.secret_key)

    def confirm(self, payment_key: str, order_id: str, amount: int) -> TossResult:
        """
        결제 승인. 서버가 보관한 금액(amount)으로 최종 확정.
        키 미설정 시 Mock 성공 반환.
        """
        if not self.is_live:
            return TossResult(
                approved=True,
                payment_key=payment_key or f"mock_{order_id}",
                order_id=order_id,
                amount=amount,
                raw={"mock": True, "status": "DONE"},
                mocked=True,
            )

        url = f"{self.api_base}/v1/payments/confirm"
        body = json.dumps(
            {"paymentKey": payment_key, "orderId": order_id, "amount": amount}
        ).encode("utf-8")
        token = base64.b64encode(f"{self.secret_key}:".encode("utf-8")).decode()
        req = request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise TossError(f"Toss 승인 실패({exc.code}): {detail}") from exc
        except error.URLError as exc:
            raise TossError(f"Toss 연결 실패: {exc.reason}") from exc

        return TossResult(
            approved=payload.get("status") == "DONE",
            payment_key=payload.get("paymentKey", payment_key),
            order_id=payload.get("orderId", order_id),
            amount=payload.get("totalAmount", amount),
            raw=payload,
        )
