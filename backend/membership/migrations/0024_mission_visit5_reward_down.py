"""
'이번 시즌 5회 방문' 보상을 1,000P → 500P 로 내린다.

왜 마이그레이션인가:
- 미션은 Store 아래 DB 행이라 코드 상수가 아니다. 서버리스에는 셸이 없고
  미션을 고치는 API도 없으니, 배포 때 자동으로 도는 마이그레이션이 유일한 길.

이미 받은 사람 것은 건드리지 않는다 — 지급된 1,000P 는 원장에 남고,
MemberMission 이 완료로 잠겨 있어 다시 계산되지 않는다. 회수는 하지 않는다.
"""

from django.db import migrations

OLD, NEW = 1000, 500
TITLE = "이번 시즌 5회 방문"


def _down(apps, schema_editor):
    Mission = apps.get_model("membership", "Mission")
    Mission.objects.filter(
        title=TITLE, condition_type="visit_count", target_value=5, reward_points=OLD
    ).update(reward_points=NEW, description=f"5번 방문하고 {NEW:,}P 받기")


def _up(apps, schema_editor):
    Mission = apps.get_model("membership", "Mission")
    Mission.objects.filter(
        title=TITLE, condition_type="visit_count", target_value=5, reward_points=NEW
    ).update(reward_points=OLD, description=f"5번 방문하고 {OLD:,}P 받기")


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0023_split_payment"),
    ]

    operations = [
        migrations.RunPython(_down, _up),
    ]
