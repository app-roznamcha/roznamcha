import json

from django.core.management.base import BaseCommand, CommandError

from core.views import _safepay_create_plan


class Command(BaseCommand):
    help = "Create Roznamcha monthly and yearly Safepay plans and print plan IDs for env configuration."

    def handle(self, *args, **options):
        try:
            monthly_payload = {
                "product": "Roznamcha Monthly",
                "amount": 100000,
                "currency": "PKR",
                "interval": "MONTH",
                "interval_count": 1,
                "active": True,
            }
            monthly_response = _safepay_create_plan(**monthly_payload)
            monthly_id = ((monthly_response.get("data") or {}).get("id")) or ((monthly_response.get("data") or {}).get("token")) or ""

            self.stdout.write("MONTHLY payload:")
            self.stdout.write(json.dumps(monthly_payload, indent=2))
            self.stdout.write("MONTHLY raw response:")
            self.stdout.write(json.dumps(monthly_response, indent=2))
            self.stdout.write(self.style.SUCCESS(f"SAFEPAY_MONTHLY_PLAN_ID={monthly_id or 'UNKNOWN'}"))

            yearly_payload = {
                "product": "Roznamcha Yearly",
                "amount": 900000,
                "currency": "PKR",
                "interval": "YEAR",
                "interval_count": 1,
                "active": True,
            }
            yearly_response = _safepay_create_plan(**yearly_payload)
            yearly_id = ((yearly_response.get("data") or {}).get("id")) or ((yearly_response.get("data") or {}).get("token")) or ""

            self.stdout.write("")
            self.stdout.write("YEARLY payload:")
            self.stdout.write(json.dumps(yearly_payload, indent=2))
            self.stdout.write("YEARLY raw response:")
            self.stdout.write(json.dumps(yearly_response, indent=2))
            self.stdout.write(self.style.SUCCESS(f"SAFEPAY_YEARLY_PLAN_ID={yearly_id or 'UNKNOWN'}"))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
