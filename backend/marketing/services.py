"""캠페인 발송 + 점주 대시보드 집계."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction as db_transaction
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from membership.models import Member, Transaction

from .alimtalk import AlimtalkClient, AlimtalkError
from .models import Campaign, MessageLog
from .segments import render_message, resolve_members


class CampaignError(Exception):
    pass


@db_transaction.atomic
def send_campaign(campaign: Campaign) -> Campaign:
    """
    캠페인 대상 회원에게 알림톡 발송. 회원별 MessageLog 기록.
    수신 미동의자(광고성)는 제외(skipped)로 안전 처리.
    """
    if campaign.status == Campaign.Status.SENT:
        raise CampaignError("이미 발송된 캠페인입니다.")
    if not campaign.segment:
        raise CampaignError("세그먼트가 지정되지 않았습니다.")

    members = list(resolve_members(campaign.segment))
    client = AlimtalkClient()
    sent = failed = skipped = 0

    for member in members:
        # 광고성인데 미동의면 발송 제외(법적 보호).
        if campaign.is_ad and not member.marketing_opt_in:
            MessageLog.objects.create(
                campaign=campaign, member=member, phone=member.phone,
                rendered_message="", status=MessageLog.Status.SKIPPED,
                reason="마케팅 수신 미동의",
            )
            skipped += 1
            continue

        body = client.decorate(
            render_message(campaign.message_template, member), campaign.is_ad
        )
        try:
            result = client.send(member.phone, body)
            status = (
                MessageLog.Status.SENT if result.success else MessageLog.Status.FAILED
            )
            reason = "Mock 발송" if result.mocked else result.reason
            if result.success:
                sent += 1
            else:
                failed += 1
        except AlimtalkError as exc:
            status, reason = MessageLog.Status.FAILED, str(exc)
            failed += 1

        MessageLog.objects.create(
            campaign=campaign, member=member, phone=member.phone,
            rendered_message=body, status=status, reason=reason,
        )

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
