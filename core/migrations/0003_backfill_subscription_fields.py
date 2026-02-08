from django.db import migrations
from django.utils import timezone

def forwards(apps, schema_editor):
    UserProfile = apps.get_model("core", "UserProfile")
    from django.utils import timezone

    now = timezone.now()

    owners = UserProfile.objects.filter(role="OWNER")

    for profile in owners:
        updated = False

        if not getattr(profile, "subscription_status", None):
            profile.subscription_status = "TRIAL"
            updated = True

        if not getattr(profile, "trial_started_at", None):
            profile.trial_started_at = now
            updated = True

        if updated:
            profile.save(
                update_fields=["subscription_status", "trial_started_at"]
            )

def backwards(apps, schema_editor):
    # no rollback needed
    pass

class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_userprofile_subscription_expires_at_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]