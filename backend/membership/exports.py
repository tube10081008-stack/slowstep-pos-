"""
데이터 내보내기 — DB가 사라져도 사장님 손에 사본이 남게.

Neon에 문제가 생기면 이관해 온 회원 포인트도, 개업 이후 매출도 복구할
방법이 없다(페이히어 원본은 이미 정리했다). 관리형 DB의 백업을 믿되,
**믿는 것과 사본을 갖는 것은 다르다.**

엑셀에서 바로 열리도록 UTF-8 BOM을 붙인다(없으면 한글이 깨진다).
"""
from __future__ import annotations

import csv
import io

from django.utils import timezone

from .models import Coupon, Member, OrderItem, PointEntry, Transaction

KINDS = ("members", "transactions", "points", "coupons")


def _writer():
    buf = io.StringIO()
    buf.write("﻿")            # 엑셀용 BOM — 없으면 한글이 깨진다
    return buf, csv.writer(buf)


def _dt(value) -> str:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def export_members() -> str:
    buf, w = _writer()
    w.writerow([
        "회원ID", "이름", "연락처", "포인트", "등급", "누적결제액", "방문횟수",
        "스탬프", "룰렛기회", "마케팅동의", "초대코드", "가입일",
        "이관시점_누적결제", "이관시점_방문수",
    ])
    for m in Member.objects.order_by("id").iterator():
        w.writerow([
            m.id, m.name, m.phone, m.points, m.get_tier_display(), m.total_spent,
            m.visit_count, m.stamps, m.spins,
            "Y" if m.marketing_opt_in else "N", m.referral_code or "",
            _dt(m.joined_at), m.baseline_total_spent, m.baseline_visit_count,
        ])
    return buf.getvalue()


def export_transactions() -> str:
    """거래 1건 = 1행. 주문 항목은 한 칸에 모아 적는다(엑셀에서 읽기 쉽게)."""
    buf, w = _writer()
    w.writerow([
        "거래ID", "결제시각", "상태", "회원", "연락처", "주문내용",
        "주문총액", "할인", "사용포인트", "실결제액", "적립포인트",
        "결제수단", "분할수단", "분할금액", "승인번호", "주문번호",
    ])
    items_by_txn: dict[int, list[str]] = {}
    for oi in OrderItem.objects.select_related("transaction").iterator():
        label = f"{oi.name}"
        if oi.option_label:
            label += f"({oi.option_label})"
        items_by_txn.setdefault(oi.transaction_id, []).append(f"{label}×{oi.quantity}")

    qs = Transaction.objects.select_related("member").order_by("id")
    for t in qs.iterator():
        w.writerow([
            t.id, _dt(t.paid_at), t.get_status_display(),
            t.member.name if t.member else "비회원",
            t.member.phone if t.member else "",
            ", ".join(items_by_txn.get(t.id, [])),
            t.gross_amount, t.discount, t.points_used, t.net_amount, t.points_earned,
            t.get_payment_method_display(),
            t.get_split_method_display() if t.split_method else "",
            t.split_amount or "", t.approval_no, t.toss_order_id,
        ])
    return buf.getvalue()


def export_points() -> str:
    """포인트 원장 — 잔액의 진실 원천이라 이것만 있어도 복원할 수 있다."""
    buf, w = _writer()
    w.writerow(["원장ID", "시각", "회원", "연락처", "증감", "사유", "반영후잔액", "거래ID"])
    qs = PointEntry.objects.select_related("member").order_by("id")
    for e in qs.iterator():
        w.writerow([
            e.id, _dt(e.created_at), e.member.name, e.member.phone,
            e.delta, e.get_reason_display(), e.balance_after, e.transaction_id or "",
        ])
    return buf.getvalue()


def export_coupons() -> str:
    buf, w = _writer()
    w.writerow(["쿠폰ID", "회원", "연락처", "종류", "발행경로", "메모",
                "발행일", "만료일", "사용일", "사용거래ID"])
    qs = Coupon.objects.select_related("member").order_by("id")
    for c in qs.iterator():
        w.writerow([
            c.id, c.member.name, c.member.phone, c.get_kind_display(),
            c.get_source_display(), c.note,
            _dt(c.issued_at), _dt(c.expires_at), _dt(c.used_at),
            c.used_transaction_id or "",
        ])
    return buf.getvalue()


EXPORTERS = {
    "members": (export_members, "회원"),
    "transactions": (export_transactions, "거래"),
    "points": (export_points, "포인트원장"),
    "coupons": (export_coupons, "쿠폰"),
}


def export_csv(kind: str) -> tuple[str, str]:
    """(CSV 텍스트, 파일명) 반환. 모르는 kind 는 KeyError."""
    fn, label = EXPORTERS[kind]
    today = timezone.localdate().isoformat()
    return fn(), f"슬로우스텝_{label}_{today}.csv"
