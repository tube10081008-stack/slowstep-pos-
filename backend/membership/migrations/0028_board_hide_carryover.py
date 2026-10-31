"""
메뉴판 숨김 규칙을 코드에서 DB로 옮긴다.

지금까지는 `web/display/menu.html` 안에 이렇게 박혀 있었다:

    HIDE_CATEGORIES = ["dessert"]     # 매일 바뀌어서
    HIDE_WORDS = ["레온", "킹"]        # 특정 손님 전용

메뉴를 하나 숨기려고 코드를 고치고 배포하는 건 사장님이 할 수 있는 일이
아니다. `show_on_board` 로 옮겨 화면에서 켜고 끌 수 있게 한다.

**지금 화면에 보이는 것이 그대로 유지되도록** 같은 규칙으로 한 번 칠한다.
낱말 맞추기는 공백을 지우고 비교한다 — "레온 킹"인지 "킹 레온"인지,
뒤에 무엇이 붙는지에 흔들리지 않게(화면 코드와 같은 방식).
"""

from django.db import migrations

HIDE_CATEGORIES = ["dessert"]
HIDE_WORDS = ["레온", "킹"]


def _squash(text: str) -> str:
    return "".join((text or "").split())


def _forward(apps, schema_editor):
    MenuItem = apps.get_model("membership", "MenuItem")
    hide_ids = []
    for m in MenuItem.objects.all().only("id", "name", "category"):
        name = _squash(m.name)
        if m.category in HIDE_CATEGORIES or any(
            _squash(w) in name for w in HIDE_WORDS
        ):
            hide_ids.append(m.id)
    MenuItem.objects.filter(id__in=hide_ids).update(show_on_board=False)


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 전부 보이게 만들면 숨겨야 할 메뉴가 노출된다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0027_menu_board_image"),
    ]

    operations = [
        migrations.RunPython(_forward, _noop),
    ]
