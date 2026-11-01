"""
2026-10 사장님 결정 두 가지를 운영 DB에 반영한다.

■ 미션 보상 전면 중단
  매장 미션(5회 방문·10회 방문·누적 5만원)을 **끈다(is_active=False).** 지우지
  않는다 — 이미 달성·지급된 기록(MemberMission·포인트 원장)이 이 행을 가리킨다.
  개인 퀘스트(다시 만나기·도장깨기·이번 달 N번 방문)는 코드에서 끈다(quests.ENABLED).

■ 커피 원두 선택(고소 기본 / 산미 +500원)
  커피 분류 메뉴에 원두 선택을 켠다. 다른 분류 메뉴는 메뉴판 수정 화면에서
  하나씩 켤 수 있다.
  예전에 산미를 받으려고 만든 **500원짜리 '산미' 메뉴는 판매중지로 내린다.**
  그대로 두면 옵션 +500 에 메뉴 +500 이 겹쳐 두 번 받게 된다. 지우지 않으므로
  지난 주문 기록은 그대로 남고, 필요하면 다시 올릴 수 있다.
  ('산미원두' 같은 원두 판매 메뉴는 이름이 달라 건드리지 않는다.)
"""

from django.db import migrations


def _forward(apps, schema_editor):
    Mission = apps.get_model("membership", "Mission")
    MenuItem = apps.get_model("membership", "MenuItem")

    Mission.objects.filter(is_active=True).update(is_active=False)

    MenuItem.objects.filter(category="coffee").update(bean_available=True)
    MenuItem.objects.filter(name="산미", price=500).update(
        is_available=False, show_on_board=False
    )


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 미션을 다시 켜면 포인트가 또 나간다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0034_bean_option"),
    ]

    operations = [
        migrations.RunPython(_forward, _noop),
    ]
