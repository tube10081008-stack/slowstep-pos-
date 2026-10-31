from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0006_transaction_approval_no_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "paid"))
                & ~models.Q(("toss_order_id", "")),
                fields=("toss_order_id",),
                name="uniq_paid_toss_order_id",
            ),
        ),
    ]
