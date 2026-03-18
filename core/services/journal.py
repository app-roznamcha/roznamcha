from __future__ import annotations

from datetime import date as date_cls, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone

from core.models import Account, JournalEntry, JournalEntryLine


def _d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or "0"))


def create_journal_entry(*, owner, date, description="", related_model="", related_id=None) -> JournalEntry:
    journal_entry = JournalEntry(
        owner=owner,
        date=date,
        description=description or "",
        related_model=related_model or "",
        related_id=related_id,
    )
    journal_entry.full_clean()
    journal_entry.save()
    return journal_entry


def add_journal_line(
    *,
    journal_entry: JournalEntry,
    account: Account,
    debit=0,
    credit=0,
    description="",
) -> JournalEntryLine:
    line = JournalEntryLine(
        journal_entry=journal_entry,
        account=account,
        debit=_d(debit),
        credit=_d(credit),
        description=description or "",
    )
    line.full_clean()
    line.save()
    return line


def validate_journal_balance(*, journal_entry: JournalEntry) -> None:
    totals = journal_entry.lines.aggregate(
        total_debit=Sum("debit"),
        total_credit=Sum("credit"),
    )
    total_debit = totals["total_debit"] or Decimal("0")
    total_credit = totals["total_credit"] or Decimal("0")

    if total_debit != total_credit:
        raise ValidationError(
            f"Journal entry {journal_entry.id} is not balanced: "
            f"debit={total_debit} credit={total_credit}"
        )


def create_balanced_journal(
    *,
    owner,
    date,
    description="",
    related_model="",
    related_id=None,
    lines=None,
) -> JournalEntry:
    normalized_lines = list(lines or [])
    if not normalized_lines:
        raise ValidationError("Balanced journal requires at least one line.")

    total_debit = sum((_d(line.get("debit", 0)) for line in normalized_lines), Decimal("0"))
    total_credit = sum((_d(line.get("credit", 0)) for line in normalized_lines), Decimal("0"))
    if total_debit != total_credit:
        raise ValidationError(
            f"Journal is not balanced before save: debit={total_debit} credit={total_credit}"
        )

    debit_line = next((line for line in normalized_lines if _d(line.get("debit", 0)) > 0), None)
    credit_line = next((line for line in normalized_lines if _d(line.get("credit", 0)) > 0), None)
    if debit_line is None or credit_line is None:
        raise ValidationError("Balanced journal requires at least one debit line and one credit line.")

    with transaction.atomic():
        journal_entry = JournalEntry(
            owner=owner,
            date=date,
            description=description,
            related_model=related_model,
            related_id=related_id,
            debit_account=debit_line["account"],
            credit_account=credit_line["account"],
            amount=total_debit,
        )
        journal_entry.full_clean()
        journal_entry.save()

        for line in normalized_lines:
            add_journal_line(
                journal_entry=journal_entry,
                account=line["account"],
                debit=line.get("debit", 0),
                credit=line.get("credit", 0),
                description=line.get("description", ""),
            )

        validate_journal_balance(journal_entry=journal_entry)
        return journal_entry


def is_accounting_v2_active(owner, txn_date=None) -> bool:
    company = getattr(owner, "company_profile", None)
    if company is None:
        return False

    if company.accounting_mode != "v2":
        return False

    cutover = company.accounting_cutover_date
    if cutover is None:
        return False

    effective_date = txn_date or timezone.now()
    if isinstance(effective_date, date_cls) and not isinstance(effective_date, datetime):
        effective_date = datetime.combine(effective_date, datetime.min.time())

    if timezone.is_naive(effective_date) and timezone.is_aware(cutover):
        effective_date = timezone.make_aware(effective_date, timezone.get_current_timezone())
    elif timezone.is_aware(effective_date) and timezone.is_naive(cutover):
        cutover = timezone.make_aware(cutover, timezone.get_current_timezone())

    return effective_date >= cutover


def get_weighted_average_cost(*, owner, product, as_of_date=None) -> Decimal:
    from core.models import PurchaseInvoiceItem, PurchaseReturnItem, SalesReturnItem, StockAdjustment

    purchase_value_expr = ExpressionWrapper(
        F("quantity_units") * F("unit_price"),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )
    adjustment_value_expr = ExpressionWrapper(
        F("qty") * F("unit_cost"),
        output_field=DecimalField(max_digits=18, decimal_places=2),
    )

    purchases = PurchaseInvoiceItem.objects.filter(
        owner=owner,
        product=product,
        purchase_invoice__posted=True,
    )
    purchase_returns = PurchaseReturnItem.objects.filter(
        owner=owner,
        product=product,
        purchase_return__posted=True,
    )
    sales_returns = SalesReturnItem.objects.filter(
        owner=owner,
        product=product,
        sales_return__posted=True,
    )
    adjustments = StockAdjustment.objects.filter(
        owner=owner,
        product=product,
        posted=True,
    )

    if as_of_date:
        purchases = purchases.filter(purchase_invoice__invoice_date__lte=as_of_date)
        purchase_returns = purchase_returns.filter(purchase_return__return_date__lte=as_of_date)
        sales_returns = sales_returns.filter(sales_return__return_date__lte=as_of_date)
        adjustments = adjustments.filter(date__lte=as_of_date)

    purchase_qty = purchases.aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")
    purchase_value = purchases.aggregate(s=Sum(purchase_value_expr))["s"] or Decimal("0")

    purchase_return_qty = purchase_returns.aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")
    purchase_return_value = purchase_returns.aggregate(s=Sum(purchase_value_expr))["s"] or Decimal("0")

    sales_return_qty = sales_returns.aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")
    sales_return_value = sales_returns.aggregate(s=Sum(purchase_value_expr))["s"] or Decimal("0")

    adj_up = adjustments.filter(direction="UP")
    adj_down = adjustments.filter(direction="DOWN")

    adj_up_qty = adj_up.aggregate(s=Sum("qty"))["s"] or Decimal("0")
    adj_up_value = adj_up.aggregate(s=Sum(adjustment_value_expr))["s"] or Decimal("0")

    adj_down_qty = adj_down.aggregate(s=Sum("qty"))["s"] or Decimal("0")
    adj_down_value = adj_down.aggregate(s=Sum(adjustment_value_expr))["s"] or Decimal("0")

    stock_quantity = purchase_qty + sales_return_qty + adj_up_qty - purchase_return_qty - adj_down_qty
    total_purchase_value = purchase_value + sales_return_value + adj_up_value - purchase_return_value - adj_down_value

    if stock_quantity <= 0:
        fallback_cost = getattr(product, "purchase_price_per_unit", None) or Decimal("0")
        return _d(fallback_cost)

    return total_purchase_value / stock_quantity
