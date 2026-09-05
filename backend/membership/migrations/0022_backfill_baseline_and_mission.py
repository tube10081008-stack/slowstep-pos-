"""
이관 기준선 채우기 + '3회 방문' 미션을 '5회 방문 1,000P'로 변경.

왜 필요한가:
- 이관 회원은 방문수를 25회처럼 들고 들어온다. 미션이 그 누적치를 그대로
  읽으면 '3회 방문'이 처음부터 달성 상태가 되고, 보상은 0P로 막혀 있었다.
  오래 다닌 손님만 미션 보상을 못 받는 거꾸로 된 상황이라 기준선을 도입했다.

기준선 복원 방법:
  기준선 = 지금 값 − 우리 앱에서 쌓은 값
  우리 앱 방문수는 결제완료 거래 수와 같고(결제 시 +1, 취소 시 −1),
  누적결제도 같은 거래의 실결제액 합이다. 그래서 이관 이후에 이미 결제한
  회원이 있어도 이관 시점 값이 정확히 복원된다.
  이관하지 않은 회원은 두 값이 같아 기준선이 0이 된다 — 계산이 그대로다.

이미 '달성'으로 찍혀 있던 미션:
  기준선을 적용해 다시 계산했을 때 목표에 못 미치는 것만 지운다. 그건 예전
  규칙으로 막아뒀던 것이고 보상도 나가지 않았으므로, 지워야 앞으로 정상적으로
  달성하고 보상을 받을 수 있다. 앱에서 진짜로 달성한 건은 그대로 둔다.
"""

from django.db import migrations
from django.db.models import Sum


def _forward(apps, schema_editor):
    Member = apps.get_model("membership", "Member")
    Mission = apps.get_model("membership", "Mission")
    MemberMission = apps.get_model("membership", "MemberMission")
    Transaction = apps.get_model("membership", "Transaction")

    # ── 1) 사장님 요청: 3회 방문 → 5회 방문, 보상 1,000P ──
    Mission.objects.filter(
        condition_type="visit_count", target_value=3
    ).update(
        title="이번 시즌 5회 방문",
        description="5번 방문하고 1,000P 받기",
        target_value=5,
        reward_points=1000,
    )

    # ── 2) 기준선 복원 ──
    for member in Member.objects.all().iterator():
        paid = Transaction.objects.filter(member=member, status="paid")
        app_visits = paid.count()
        app_spent = paid.aggregate(s=Sum("net_amount"))["s"] or 0
        member.baseline_visit_count = max(0, member.visit_count - app_visits)
        member.baseline_total_spent = max(0, member.total_spent - app_spent)
        member.save(
            update_fields=["baseline_visit_count", "baseline_total_spent"]
        )

    # ── 3) 기준선으로 다시 재면 미달인 '달성' 기록 제거 ──
    # (보상이 나가지 않은 채 막혀 있던 것들 — 지워야 앞으로 받을 수 있다)
    stale = []
    for mm in MemberMission.objects.filter(
        is_completed=True
    ).select_related("member", "mission"):
        m, mission = mm.member, mm.mission
        if mission.condition_type == "visit_count":
            value = max(0, m.visit_count - m.baseline_visit_count)
        elif mission.condition_type == "total_spent":
            value = max(0, m.total_spent - m.baseline_total_spent)
        else:
            continue
        if value < mission.target_value:
            stale.append(mm.pk)
    MemberMission.objects.filter(pk__in=stale).delete()


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 기준선을 지우면 미션이 다시 잠긴다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0021_mission_baseline"),
    ]

    operations = [
        migrations.RunPython(_forward, _noop),
    ]
