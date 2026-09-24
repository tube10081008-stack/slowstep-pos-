"""
메뉴판 확정본이 한 번 더 바뀌어 POS 를 다시 맞춘다 (2026-09 2차).

■ 골든애플커피를 되살린다.
  0031 에서 내렸는데 이번 확정본에 다시 들어왔다. 내가 내린 메뉴라
  내가 되돌린다. 가격도 5,200 → **5,500**, 아이스 전용으로 바뀌었다.
  (지우지 않고 판매중지로 내려둔 덕분에 주문 기록이 그대로 붙어 있다.)

■ 딥초코 이름을 메뉴판과 맞춘다.
  POS '딥초코멜로우 (기라델리)' → 확정본 표기 '딥초코 멜로우'.
  손님이 메뉴판에 적힌 이름으로 부르는데 POS 에서 못 찾으면 곤란하다.
  **이름만 바꾼다** — 같은 행이라 지난 주문·통계가 그대로 이어진다.
"""

from django.db import migrations

OLD_CHOCO, NEW_CHOCO = "딥초코멜로우 (기라델리)", "딥초코 멜로우"


def _forward(apps, schema_editor):
    MenuItem = apps.get_model("membership", "MenuItem")

    MenuItem.objects.filter(name="골든애플커피").update(
        price=5500, temp_option="ice", is_available=True, show_on_board=True
    )

    # 새 이름이 이미 따로 만들어져 있으면 건드리지 않는다(중복 생성 방지).
    if not MenuItem.objects.filter(name=NEW_CHOCO).exists():
        MenuItem.objects.filter(name=OLD_CHOCO).update(name=NEW_CHOCO)


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 메뉴판과 또 어긋난다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0031_menu_sync_poster"),
    ]

    operations = [
        migrations.RunPython(_forward, _noop),
    ]
