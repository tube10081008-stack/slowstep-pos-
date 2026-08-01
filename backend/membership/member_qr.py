"""
멤버십 QR — 결제 완료 화면에서 손님이 스캔해 본인 페이지로 가는 링크.

**연락처를 QR에 넣지 않는다.**
`/member/?phone=010...` 를 그대로 인코딩하면, 매장 화면을 옆에서 사진 찍는 것만으로
남의 연락처를 수집할 수 있다(계산대 화면은 다음 손님에게도 보인다).
그래서 서명된 **단기 토큰**을 담는다:

- 토큰은 SECRET_KEY로 서명(TimestampSigner) → 위조 불가, 서버에 저장 불필요
- 유효기간이 짧아(기본 1시간) 사진이 남아도 곧 무용지물이 된다
- 만료되면 멤버십 페이지에서 연락처로 조회하면 된다(기존 경로 유지)
- SECRET_KEY를 교체하면 발급된 모든 토큰이 즉시 무효화된다
"""
from __future__ import annotations

import io

import qrcode
import qrcode.image.svg
from django.core import signing

TOKEN_SALT = "slowstep.member.qr"
TOKEN_MAX_AGE = 60 * 60  # 1시간


class MemberTokenError(Exception):
    """토큰이 위조됐거나 만료됨."""


def _signer() -> signing.TimestampSigner:
    return signing.TimestampSigner(salt=TOKEN_SALT)


def issue_member_token(member) -> str:
    return _signer().sign(str(member.pk))


def resolve_member_token(token: str):
    """토큰 → Member. 실패 시 MemberTokenError."""
    from .models import Member

    if not token:
        raise MemberTokenError("링크가 올바르지 않습니다.")
    try:
        raw = _signer().unsign(token, max_age=TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        raise MemberTokenError("링크 유효시간이 지났습니다. 연락처로 조회해 주세요.")
    except signing.BadSignature:
        raise MemberTokenError("링크가 올바르지 않습니다.")
    member = Member.objects.filter(pk=raw).select_related("store").first()
    if member is None:
        raise MemberTokenError("회원을 찾을 수 없습니다.")
    return member


def member_url(member, base: str) -> str:
    """스캔했을 때 열릴 주소. base 예: https://slowstep-pos.vercel.app"""
    return f"{base.rstrip('/')}/member/?t={issue_member_token(member)}"


def qr_svg(data: str, box_size: int = 10, border: int = 2) -> str:
    """
    QR을 SVG 문자열로. 이미지 요청 없이 화면에 바로 넣을 수 있어,
    고객 화면이 서버를 호출하지 않아도 된다(POS가 받아 전달).
    """
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
    # XML 선언은 인라인 삽입 시 불필요하고, 크기는 CSS로 제어한다.
    svg = svg.split("?>", 1)[-1].strip()
    return svg.replace('width="', 'data-w="', 1).replace('height="', 'data-h="', 1)
