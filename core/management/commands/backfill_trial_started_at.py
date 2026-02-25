from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import UserProfile


class Command(BaseCommand):
    help = (
        "Backfill OWNER trial_started_at for TRIAL profiles where it is NULL, "
        "using owner.user.date_joined as per-user anchor."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show how many records would be updated without saving changes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = (
            UserProfile.objects.select_related("user")
            .filter(role="OWNER", subscription_status="TRIAL", trial_started_at__isnull=True)
        )

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No trial owners need backfill."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"[DRY RUN] Would update {total} OWNER trial profile(s).")
            )
            return

        updated = 0
        with transaction.atomic():
            for prof in qs:
                joined = prof.user.date_joined
                if timezone.is_naive(joined):
                    joined = timezone.make_aware(joined, timezone.get_current_timezone())
                prof.trial_started_at = joined
                prof.save(update_fields=["trial_started_at"])
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete. Updated {updated} OWNER trial profile(s) using user.date_joined."
            )
        )
