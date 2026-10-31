"""
확정된 종이 메뉴판에 맞춰 POS 메뉴를 맞춘다 (2026-09 확정본).

손님은 벽에 붙은 메뉴판을 보고 주문한다. 거기 있는데 POS 에 없으면 직원이
찍을 수가 없고, 없는데 POS 에 있으면 "그건 지금 안 팔아요"를 반복하게 된다.

■ 추가 — 확정본에 새로 들어간 것들
■ 내림 — 확정본에서 빠진 것들. **지우지 않고 판매중지로 내린다.**
  지난 주문 기록이 그 메뉴를 가리키고 있고, 다시 올릴 수도 있다.
■ 세트 할인 — 음료 + **플레인 휘낭시에**만. 확정본 문구가 그렇게 바뀌었다.

**여기 적힌 이름만 건드린다.** 사장님이 따로 추가한 메뉴(엑셀렌트라떼·컵빙수
등)는 확정본에 없더라도 손대지 않는다 — 내가 모르는 사정으로 팔고 있을 수
있고, 파는 메뉴를 말없이 내리는 건 되돌리기 어려운 쪽의 실수다.
"""

from django.db import migrations

# (이름, 가격, 분류, 온도, 디카페인, 오트, 샷)
ADD = [
    ("청귤 에이드", 5500, "ade", "ice", False, False, False),
    ("밤라떼", 6000, "noncoffee", "hotice", False, True, False),
    ("애플 시나몬차", 5000, "tea", "hotice", False, False, False),
    ("청귤차", 5000, "tea", "hotice", False, False, False),
    ("대추 생강차", 6000, "tea", "hotice", False, False, False),
    ("자스민", 4000, "tea", "hotice", False, False, False),
    ("레몬 에이드", 5500, "ade", "ice", False, False, False),
]

# 확정본에서 빠진 메뉴 — 판매중지 + 메뉴판에서 내림
RETIRE = ["레드 청포도 스파클링", "시트러스 요거트 스무디", "골든애플커피"]

SET_MENU = "플레인 휘낭시에"


def _forward(apps, schema_editor):
    MenuItem = apps.get_model("membership", "MenuItem")
    Store = apps.get_model("membership", "Store")
    store = Store.objects.first()
    if store is None:
        return

    last = MenuItem.objects.order_by("-sort_order").first()
    order = (last.sort_order if last else 0) + 1
    for name, price, cat, temp, decaf, oat, shot in ADD:
        # 이미 있으면 건드리지 않는다 — 사장님이 먼저 넣고 값을 바꿨을 수 있다.
        if MenuItem.objects.filter(name=name).exists():
            continue
        MenuItem.objects.create(
            store=store, name=name, price=price, category=cat, temp_option=temp,
            decaf_available=decaf, oatmilk_available=oat, shot_available=shot,
            sort_order=order, is_available=True, show_on_board=True,
        )
        order += 1

    MenuItem.objects.filter(name__in=RETIRE).update(
        is_available=False, show_on_board=False
    )

    # 세트 할인은 플레인 휘낭시에만. 다른 데 켜져 있으면 끈다.
    MenuItem.objects.filter(set_eligible=True).exclude(name=SET_MENU).update(
        set_eligible=False
    )
    MenuItem.objects.filter(name=SET_MENU).update(set_eligible=True)


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 내린 메뉴를 되살리면 메뉴판과 또 어긋난다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0030_set_eligible"),
    ]

    operations = [
        migrations.RunPython(_forward, _noop),
    ]
