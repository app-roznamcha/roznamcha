from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from core.models import seed_default_accounts_for_owner


class Command(BaseCommand):
    help = "Seed/repair default accounts (including Daily Expense heads) for all OWNER users."

    def handle(self, *args, **options):
        owners = (
            User.objects.filter(profile__role="OWNER")
            .select_related("profile")
            .order_by("id")
        )

        count = 0
        for owner in owners:
            seed_default_accounts_for_owner(owner)
            count += 1
            self.stdout.write(self.style.SUCCESS(f"Seeded accounts for owner: {owner.username} (id={owner.id})"))

        self.stdout.write(self.style.SUCCESS(f"Done. Owners processed: {count}"))