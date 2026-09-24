"""
해피아워 끄기 — 예전 시드가 넣어둔 14~16시 ×2배를 걷어낸다.

왜 마이그레이션인가:
- 예전 `seed_demo` 가 `happy_start, happy_end, happy_multiplier = 14, 16, 2` 를
  넣었다. 시드에서는 뺐지만(81db161), 부팅 시드는 **매장이 없을 때만** 돌기
  때문에 그 전에 만들어진 매장 행에는 값이 그대로 남아 있다.
- 서버리스에는 셸이 없어 `manage.py` 를 돌릴 수 없고, 매장 설정을 바꾸는
  API도 없다. 남은 경로는 배포 때 자동으로 도는 마이그레이션뿐이다.

이미 꺼져 있으면 0을 다시 0으로 쓰는 것이라 아무 일도 일어나지 않는다.
나중에 사장님이 해피아워를 다시 켜도 이 마이그레이션은 한 번만 돌므로
그 설정을 건드리지 않는다.
"""

from django.db import migrations


def _off(apps, schema_editor):
    Store = apps.get_model("membership", "Store")
    Store.objects.exclude(happy_start=0, happy_end=0).update(happy_start=0, happy_end=0)


def _noop(apps, schema_editor):
    """되돌리지 않는다 — 예전 시드값으로 되살릴 이유가 없다."""


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0018_stamp_reward_points_unused"),
    ]

    operations = [
        migrations.RunPython(_off, _noop),
    ]
