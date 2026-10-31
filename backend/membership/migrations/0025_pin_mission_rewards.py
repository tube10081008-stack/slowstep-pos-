"""
미션 보상 금액을 코드가 정한 값으로 못 박는다.

왜 또 하는가 (0024 에 이어서):
- 미션은 코드 상수가 아니라 **DB 행**이다. `seed_demo` 는 매장이 없을 때만
  도는 부팅 시드라, 2026-08-08(bc696c3)에 시드의 금액을 내렸어도 **그 전에
  만들어진 운영 매장의 행은 옛날 값 그대로 남았다.**
  → 운영에는 '단골 인증 10회 방문 5,000P', '누적 5만원 달성 2,000P' 가
    살아 있었다. 개발 DB 는 그 뒤에 다시 시드해서 500P 라 눈에 안 띄었다.
- 0024 는 '5회 방문' 한 줄만 손봤다. 나머지 둘도 같은 병이었다.

그래서 이번엔 **현재 값을 조건에 걸지 않는다.** 조건은 목표(무엇을 달성해야
하는가)뿐이고, 보상은 무조건 덮어쓴다. 운영이 어떤 값을 들고 있든 배포 후엔
코드가 적어 둔 금액이 된다.

이미 받아간 포인트는 회수하지 않는다 — 원장에 남고 MemberMission 이 완료로
잠겨 다시 계산되지 않는다.
"""

from django.db import migrations

# (조건, 목표값) → (제목, 설명, 보상)
INTENDED = {
    ("visit_count", 5): ("이번 시즌 5회 방문", "5번 방문하고 500P 받기", 500),
    ("visit_count", 10): ("단골 인증 10회 방문", "10번 방문하면 500P", 500),
    ("total_spent", 50000): ("누적 5만원 달성", "누적 결제 50,000원 달성 시 500P", 500),
}


def _pin(apps, schema_editor):
    Mission = apps.get_model("membership", "Mission")
    for (cond, target), (title, desc, reward) in INTENDED.items():
        Mission.objects.filter(
            condition_type=cond, target_value=target
        ).update(title=title, description=desc, reward_points=reward)


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 옛 금액으로 되살릴 이유가 없다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0024_mission_visit5_reward_down"),
    ]

    operations = [
        migrations.RunPython(_pin, _noop),
    ]
