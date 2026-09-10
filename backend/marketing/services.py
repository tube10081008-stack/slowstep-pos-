"""캠페인 발송 + 점주 대시보드 집계."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Sum
from django.utils import timezone

from membership.models import Member, Transaction

from .models import Campaign, MessageLog
from .segments import render_message, resolve_members
from .sender import MessageClient, SendError, ad_window_open


class CampaignError(Exception):
    pass


def send_campaign(campaign: Campaign) -> Campaign:
    """
    캠페인 대상 회원에게 문자 발송. 회원별 MessageLog 기록.

    **트랜잭션으로 감싸지 않는다.** 문자는 한 번 나가면 못 되돌리는데,
    실패 시 롤백하면 '보냈을지도 모르는' 기록까지 사라진다. 특히 타임아웃은
    솔라피가 이미 접수했을 수도 있는 경우라, 기록 없이 되돌리면 다시 눌렀을 때
    같은 손님이 두 번 받는다. 로그는 진행하면서 남기고 캠페인 상태만 마지막에
    정리한다.

    **한 번에 묶어 보낸다.** 예전에는 회원마다 API를 한 번씩 호출했는데,
    220명이면 요청이 220번이라 서버리스 응답 시간(수십 초)에 먼저 걸린다.
    중간에 끊기면 절반만 나가고 로그도 절반만 남아 누가 받았는지 모른다.

    제외(skipped) 처리하는 경우:
    - 광고성인데 수신 미동의 — 정보통신망법
    - 연락처가 없거나 형식이 이상함 — 보낼 곳이 없다
    """
    if campaign.status == Campaign.Status.SENT:
        raise CampaignError("이미 발송된 캠페인입니다.")
    if not campaign.segment:
        raise CampaignError("세그먼트가 지정되지 않았습니다.")
    if campaign.is_ad and not ad_window_open():
        # 발송하고 나서 사과할 수 없는 종류의 실수라 아예 막는다.
        raise CampaignError(
            "광고성 문자는 08시~21시에만 보낼 수 있습니다(정보통신망법). "
            "정보성이면 캠페인의 '광고성' 체크를 해제하세요."
        )

    members = list(resolve_members(campaign.segment))
    client = MessageClient()
    sent = failed = skipped = 0
    pending: list[tuple[str, str]] = []       # (번호, 본문)
    bodies: dict[str, str] = {}
    by_phone: dict[str, list] = {}

    for member in members:
        phone = (member.phone or "").strip()
        if len("".join(c for c in phone if c.isdigit())) < 10:
            MessageLog.objects.create(
                campaign=campaign, member=member, phone=phone,
                rendered_message="", status=MessageLog.Status.SKIPPED,
                reason="연락처 없음",
            )
            skipped += 1
            continue
        if campaign.is_ad and not member.marketing_opt_in:
            MessageLog.objects.create(
                campaign=campaign, member=member, phone=phone,
                rendered_message="", status=MessageLog.Status.SKIPPED,
                reason="마케팅 수신 미동의",
            )
            skipped += 1
            continue

        body = client.decorate(
            render_message(campaign.message_template, member), campaign.is_ad
        )
        pending.append((phone, body))
        bodies[phone] = body
        by_phone.setdefault(phone, []).append(member)

    try:
        results = client.send_many(pending)
    except SendError as exc:
        # 요청 자체가 실패(인증·네트워크). 전부 실패로 남기고 캠페인은
        # **작성중으로 되돌린다** — 발송완료로 잠기면 다시 못 보낸다.
        for phone, _body in pending:
            for member in by_phone.get(phone, []):
                MessageLog.objects.create(
                    campaign=campaign, member=member, phone=phone,
                    rendered_message=bodies.get(phone, ""),
                    status=MessageLog.Status.FAILED, reason=str(exc)[:200],
                )
        campaign.recipient_count = len(members)
        campaign.sent_count = 0
        campaign.failed_count = len(pending)
        campaign.skipped_count = skipped
        campaign.save()
        raise CampaignError(str(exc)) from exc

    for phone, _body in pending:
        res = results.get(phone)
        ok = bool(res and res.success)
        reason = "" if res is None else (res.reason or "")
        for member in by_phone.get(phone, []):
            MessageLog.objects.create(
                campaign=campaign, member=member, phone=phone,
                rendered_message=bodies.get(phone, ""),
                status=MessageLog.Status.SENT if ok else MessageLog.Status.FAILED,
                reason=reason if ok else (reason or "발송 실패"),
            )
        if ok:
            sent += 1
        else:
            failed += 1

    campaign.recipient_count = len(members)
    campaign.sent_count = sent
    campaign.failed_count = failed
    campaign.skipped_count = skipped
    campaign.status = Campaign.Status.SENT
    campaign.sent_at = timezone.now()
    campaign.save()
    return campaign


def dashboard_stats() -> dict:
    """점주 대시보드 핵심 지표 집계."""
    now = timezone.now()
    d30 = now - timedelta(days=30)

    members = Member.objects.all()
    total_members = members.count()
    opt_in = members.filter(marketing_opt_in=True).count()
    points_outstanding = members.aggregate(s=Sum("points"))["s"] or 0
    new_30d = members.filter(joined_at__gte=d30).count()

    tier_rows = members.values("tier").annotate(c=Count("id"))
    tier_breakdown = {r["tier"]: r["c"] for r in tier_rows}

    paid = Transaction.objects.filter(status=Transaction.Status.PAID)
    rev = paid.aggregate(total=Sum("net_amount"), n=Count("id"), avg=Avg("net_amount"))
    revenue_total = rev["total"] or 0
    tx_count = rev["n"] or 0
    avg_basket = int(rev["avg"] or 0)

    paid_30d = paid.filter(paid_at__gte=d30)
    revenue_30d = paid_30d.aggregate(s=Sum("net_amount"))["s"] or 0
    active_30d = paid_30d.values("member").distinct().count()

    # 일별 매출 추세(최근 14일)
    trend = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).date()
        day_rev = paid.filter(paid_at__date=day).aggregate(s=Sum("net_amount"))["s"] or 0
        trend.append({"date": day.isoformat()[5:], "revenue": day_rev})

    top_members = list(
        members.order_by("-total_spent")[:5].values(
            "id", "name", "phone", "tier", "total_spent", "visit_count", "points"
        )
    )

    recent_tx = list(
        paid.select_related("member")[:8].values(
            "id", "member__name", "net_amount", "points_earned",
            "payment_method", "paid_at"
        )
    )

    return {
        "members": {
            "total": total_members,
            "opt_in": opt_in,
            "opt_in_rate": round(opt_in / total_members * 100, 1) if total_members else 0,
            "new_30d": new_30d,
            "active_30d": active_30d,
            "tier_breakdown": tier_breakdown,
        },
        "revenue": {
            "total": revenue_total,
            "tx_count": tx_count,
            "avg_basket": avg_basket,
            "revenue_30d": revenue_30d,
        },
        "points_outstanding": points_outstanding,
        "trend_14d": trend,
        "top_members": top_members,
        "recent_transactions": recent_tx,
    }
