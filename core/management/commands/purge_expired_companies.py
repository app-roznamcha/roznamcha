from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User

from core.models import (
    UserProfile, CompanyProfile,
    Account, Product, Party,
    JournalEntry, Payment,
    SalesInvoice, SalesInvoiceItem,
    PurchaseInvoice, PurchaseInvoiceItem,
    SalesReturn, SalesReturnItem,
    PurchaseReturn, PurchaseReturnItem,
    StockAdjustment,
)

class Command(BaseCommand):
    help = "Hard purge OWNER companies that are expired for 60+ days (deletes tenant data safely)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=60)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        days = opts["days"]
        dry = opts["dry_run"]
        now = timezone.now()

        # Your source of truth is UserProfile (OWNER subscription lives here)
        owners = UserProfile.objects.select_related("user").filter(role="OWNER")

        candidates = []
        for prof in owners:
            expired_at = prof.get_effective_expires_at()  # trial end or subscription expiry
            if not expired_at:
                continue
            if prof.get_effective_status() != "EXPIRED":
                continue
            if now >= (expired_at + timedelta(days=days)):
                candidates.append(prof.user)

        self.stdout.write(f"Found {len(candidates)} companies eligible for hard purge (days={days}).")

        for owner in candidates:
            company = getattr(owner, "company_profile", None)
            slug = company.slug if company else "(no company_profile)"
            self.stdout.write(f"\nPURGE: owner={owner.username} company_slug={slug}")

            if dry:
                continue

            with transaction.atomic():
                # 0) staff users
                staff_profiles = UserProfile.objects.select_related("user").filter(role="STAFF", owner=owner)
                staff_user_ids = list(staff_profiles.values_list("user_id", flat=True))

                # 1) line items
                SalesInvoiceItem.objects.filter(owner=owner).delete()
                PurchaseInvoiceItem.objects.filter(owner=owner).delete()
                SalesReturnItem.objects.filter(owner=owner).delete()
                PurchaseReturnItem.objects.filter(owner=owner).delete()

                # 2) documents
                SalesInvoice.objects.filter(owner=owner).delete()
                PurchaseInvoice.objects.filter(owner=owner).delete()
                SalesReturn.objects.filter(owner=owner).delete()
                PurchaseReturn.objects.filter(owner=owner).delete()
                Payment.objects.filter(owner=owner).delete()
                StockAdjustment.objects.filter(owner=owner).delete()

                # 3) ledger
                JournalEntry.objects.filter(owner=owner).delete()

                # 4) masters
                Party.objects.filter(owner=owner).delete()
                Product.objects.filter(owner=owner).delete()
                Account.objects.filter(owner=owner).delete()

                # 5) tenant identity
                CompanyProfile.objects.filter(owner=owner).delete()
                staff_profiles.delete()
                UserProfile.objects.filter(user=owner).delete()

                # delete staff users then owner
                User.objects.filter(id__in=staff_user_ids).delete()
                owner.delete()

        self.stdout.write(self.style.SUCCESS("Done."))