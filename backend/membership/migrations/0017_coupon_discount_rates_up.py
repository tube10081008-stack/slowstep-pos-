"""
룰렛 할인 쿠폰 상향: 5% → 10%, 10% → 20%.

**순서가 중요하다.** 10→20 을 먼저 하고 5→10 을 나중에 한다. 반대로 하면
원래 5%였던 쿠폰이 10%를 거쳐 20%까지 두 번 올라간다.

이미 발급돼 아직 안 쓴 쿠폰도 함께 올린다 — 같은 '할인 쿠폰'인데 발급
시점에 따라 손님마다 할인율이 다르면 계산대에서 설명할 방법이 없다.
"""

from django.db import migrations, models


def _up(apps, schema_editor):
    Coupon = apps.get_model("membership", "Coupon")
    # 순서 주의: 위쪽부터 처리해야 5% 쿠폰이 두 단계 올라가지 않는다.
    Coupon.objects.filter(kind="discount_10").update(kind="discount_20")
    Coupon.objects.filter(kind="discount_5").update(kind="discount_10")


def _down(apps, schema_editor):
    Coupon = apps.get_model("membership", "Coupon")
    # 되돌릴 때는 반대 순서.
    Coupon.objects.filter(kind="discount_10").update(kind="discount_5")
    Coupon.objects.filter(kind="discount_20").update(kind="discount_10")


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0016_store_signup_bonus_points_alter_pointentry_reason"),
    ]

    operations = [
        migrations.RunPython(_up, _down),
        migrations.AlterField(
            model_name="coupon",
            name="kind",
            field=models.CharField(
                choices=[
                    ("discount_10", "10% 할인"),
                    ("discount_20", "20% 할인"),
                    ("bogo", "음료 1+1"),
                    ("free_drink", "무료 음료"),
                    ("beans_200", "원두 200g"),
                ],
                max_length=20,
                verbose_name="종류",
            ),
        ),
    ]
