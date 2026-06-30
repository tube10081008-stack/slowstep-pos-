"""세그먼트 → 회원 쿼리 변환 + 메시지 템플릿 렌더링."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Max, Q
from django.utils import timezone

from membership.models import Member, Transaction


def resolve_members(segment):
    """세그먼트 필터를 적용한 회원 쿼리셋 반환."""
    qs = Member.objects.all()

    if segment.tier:
        qs = qs.filter(tier=segment.tier)
    if segment.min_visits:
        qs = qs.filter(visit_count__gte=segment.min_visits)
    if segment.min_spent:
        qs = qs.filter(total_spent__gte=segment.min_spent)
    if segment.require_opt_in:
        qs = qs.filter(marketing_opt_in=True)

    if segment.inactive_days and segment.inactive_days > 0:
        cutoff = timezone.now() - timedelta(days=segment.inactive_days)
        # 마지막 결제완료 시각이 cutoff 이전이거나, 결제 이력이 없는 회원.
        qs = qs.annotate(
            last_paid=Max(
                "transactions__paid_at",
                filter=Q(transactions__status=Transaction.Status.PAID),
            )
        ).filter(Q(last_paid__lt=cutoff) | Q(last_paid__isnull=True))

    return qs.order_by("-total_spent")


def render_message(template: str, member: Member) -> str:
    """{이름}{포인트}{등급}{스탬프}{방문} 치환."""
    mapping = {
        "{이름}": member.name,
        "{포인트}": f"{member.points:,}",
        "{등급}": member.get_tier_display(),
        "{스탬프}": str(member.stamps),
        "{방문}": str(member.visit_count),
    }
    out = template
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out
