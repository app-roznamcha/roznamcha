import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.core import serializers

from core.models import (
    CompanyProfile,
    Account, Party, Product,
    SalesInvoice, SalesInvoiceItem,
    PurchaseInvoice, PurchaseInvoiceItem,
    SalesReturn, SalesReturnItem,
    PurchaseReturn, PurchaseReturnItem,
    Payment, JournalEntry, StockAdjustment,
)


def _backup_dir_for_company(company: CompanyProfile) -> Path:
    base = Path(getattr(settings, "BACKUP_DIR", ""))  # should be set in settings.py
    if not str(base).strip():
        # local fallback (only if BACKUP_DIR not configured)
        base = Path(settings.BASE_DIR) / "backups"

    folder = company.slug or f"owner-{company.owner_id}"
    return base / folder

def _write_tenant_backup(owner, company: CompanyProfile) -> Path:
    models_to_dump = [
        CompanyProfile,
        Account,
        Party,
        Product,
        SalesInvoice,
        SalesInvoiceItem,
        PurchaseInvoice,
        PurchaseInvoiceItem,
        SalesReturn,
        SalesReturnItem,
        PurchaseReturn,
        PurchaseReturnItem,
        Payment,
        JournalEntry,
        StockAdjustment,
    ]

    all_objects = []
    all_objects.extend(CompanyProfile.objects.filter(owner=owner))

    for m in models_to_dump:
        if m is CompanyProfile:
            continue
        if hasattr(m, "owner_id"):
            all_objects.extend(list(m.objects.filter(owner=owner)))

    data = serializers.serialize("json", all_objects)

    out_dir = _backup_dir_for_company(company)
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"backup_{company.slug or owner.username}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path = out_dir / filename
    out_path.write_text(data, encoding="utf-8")

    return out_path


def _keep_last_n_files(folder: Path, n: int = 3):
    if not folder.exists():
        return
    files = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.name.endswith(".json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[n:]:
        try:
            old.unlink()
        except Exception:
            pass


class Command(BaseCommand):
    help = "Create tenant-scoped JSON backups for each company and keep only last 3 per company."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=3, help="How many backups to keep per company (default 3).")

    def handle(self, *args, **options):
        keep_n = int(options["keep"] or 3)

        companies = CompanyProfile.objects.select_related("owner").all().order_by("id")
        if not companies.exists():
            self.stdout.write(self.style.WARNING("No companies found. Nothing to back up."))
            return

        count = 0
        for company in companies:
            owner = company.owner
            # only owners should have tenant data
            prof = getattr(owner, "profile", None)
            if not prof or prof.role != "OWNER":
                continue

            out_path = _write_tenant_backup(owner, company)
            _keep_last_n_files(_backup_dir_for_company(company), n=keep_n)

            count += 1
            self.stdout.write(self.style.SUCCESS(f"Backup created: {out_path}"))

        self.stdout.write(self.style.SUCCESS(f"Done. Backed up {count} companies."))