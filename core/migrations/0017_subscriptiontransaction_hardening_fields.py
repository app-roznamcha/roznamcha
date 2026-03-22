from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_subscriptiontransaction"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptiontransaction",
            name="subscription_applied",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="subscriptiontransaction",
            name="applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscriptiontransaction",
            name="last_event_id",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
    ]
