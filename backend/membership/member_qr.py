"""
멤버십 QR — 결제 완료 화면에서 손님이 스캔해 멤버십 페이지로 가는 링크.

**QR에는 개인정보도 토큰도 담지 않는다.** 모든 손님에게 동일한 고정 주소
(`/member/`)만 인코딩하고, 조회는 손님이 자기 폰에서 자기 번호를 입력해 한다.

- 계산대 화면을 촬영해도 얻을 게 없다(고정 주소뿐).
- 만료가 없으므로 인쇄해서 카운터에 붙여도 그대로 동작한다.
- 서버가 토큰을 발급·검증할 일이 없어 구조가 단순하다.
"""
from __future__ import annotations

import io

# QR은 부가 기능이다. 이 라이브러리가 없다고 해서 주문·결제까지 멈추면 안 되므로
# 임포트 실패를 앱 전체로 번지게 하지 않는다(실제로 배포 의존성 목록 누락으로
# API 전체가 500이 된 적이 있다). 없으면 QR 요청만 503으로 답한다.
try:
    import qrcode
    import qrcode.image.svg

    QR_AVAILABLE = True
except ImportError:  # pragma: no cover - 의존성 누락 시에만
    qrcode = None
    QR_AVAILABLE = False


class QrUnavailable(Exception):
    """QR 라이브러리(qrcode) 미설치."""


def member_url(base: str) -> str:
    """스캔했을 때 열릴 주소. base 예: https://slowstep-pos.vercel.app"""
    return f"{base.rstrip('/')}/member/"


def qr_svg(data: str, box_size: int = 10, border: int = 2) -> str:
    """
    QR을 SVG 문자열로. 이미지 요청 없이 화면에 바로 넣을 수 있어,
    고객 화면이 서버를 호출하지 않아도 된다(POS가 받아 전달).
    """
    if not QR_AVAILABLE:
        raise QrUnavailable("QR 생성 라이브러리(qrcode)가 설치되지 않았습니다.")
    q = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    q.add_data(data)
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    svg = buf.getvalue().decode()
    svg = svg.split("?>", 1)[-1].strip()
    return svg.replace('width="', 'data-w="', 1).replace('height="', 'data-h="', 1)
