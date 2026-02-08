from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from core.models import seed_default_accounts_for_owner  # adjust app name if different


class Command(BaseCommand):
    help = "Seed default chart-of-accounts for all OWNER users (company accounts)."

    def handle(self, *args, **options):
        owners = User.objects.filter(profile__role="OWNER").distinct()
        total = 0

        for owner in owners:
            seed_default_accounts_for_owner(owner)
            total += 1

        self.stdout.write(self.style.SUCCESS(f"Seeded accounts for {total} owner(s)."))