from django.db import migrations
from django.utils import timezone

def forwards(apps, schema_editor):
    UserProfile = apps.get_model("core", "UserProfile")
    now = timezone.now()

    owners = UserProfile.objects.filter(role="OWNER")

    for profile in owners:
        updated = False

        if not profile.subscription_status:
            profile.subscription_status = "TRIAL"
            updated = True

        if not profile.trial_started_at:
            profile.trial_started_at = now
            updated = True

        if updated:
            profile.save(
                update_fields=["subscription_status", "trial_started_at"]
            )

def backwards(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_backfill_subscription_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]