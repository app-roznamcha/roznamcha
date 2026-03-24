from django.core.management.base import BaseCommand, CommandError

from core.views import _safepay_create_plan


class Command(BaseCommand):
    help = "Provision Roznamcha monthly and yearly Safepay subscription plans."

    def handle(self, *args, **options):
        try:
            monthly = _safepay_create_plan(
                product="Roznamcha Monthly",
                amount=100000,
                currency="PKR",
                interval="MONTH",
                interval_count=1,
                active=True,
            )
            monthly_id = ((monthly.get("data") or {}).get("id")) or ((monthly.get("data") or {}).get("token")) or ""
            self.stdout.write(self.style.SUCCESS(f"Monthly plan created. Plan ID: {monthly_id or 'unknown'}"))

            yearly = _safepay_create_plan(
                product="Roznamcha Yearly",
                amount=900000,
                currency="PKR",
                interval="YEAR",
                interval_count=1,
                active=True,
            )
            yearly_id = ((yearly.get("data") or {}).get("id")) or ((yearly.get("data") or {}).get("token")) or ""
            self.stdout.write(self.style.SUCCESS(f"Yearly plan created. Plan ID: {yearly_id or 'unknown'}"))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
