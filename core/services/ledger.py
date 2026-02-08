from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional


@dataclass
class LedgerRow:
    date: date
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal
    related_model: str = ""
    related_id: int | None = None


def _d(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x or "0"))


# -------------------------
# ACCOUNT LEDGER (JournalEntry-based)
# -------------------------

def get_account_balance(*, owner, account, as_of: Optional[date] = None) -> Decimal:
    from core.models import JournalEntry

    qs = JournalEntry.objects.filter(owner=owner).filter(
        debit_account=account
    ) | JournalEntry.objects.filter(owner=owner).filter(
        credit_account=account
    )

    if as_of:
        qs = qs.filter(date__lte=as_of)

    qs = qs.order_by("date", "id")

    balance = Decimal("0")

    # Asset/Expense => normal debit
    is_debit_normal = account.account_type in ("ASSET", "EXPENSE")

    for je in qs:
        debit = je.amount if je.debit_account_id == account.id else Decimal("0")
        credit = je.amount if je.credit_account_id == account.id else Decimal("0")

        if is_debit_normal:
            balance += (debit - credit)
        else:
            balance += (credit - debit)

    return balance


def get_account_ledger(
    *,
    owner,
    account,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[LedgerRow]:
    from core.models import JournalEntry

    # Opening balance before start date (running)
    opening = Decimal("0")
    if start:
        opening = get_account_balance(owner=owner, account=account, as_of=(start))

        # opening currently includes start day itself if entries exist on start day.
        # We want "before start", so re-calc with start-1 day if start provided.
        # Safer: compute opening manually:
        opening = get_account_balance(owner=owner, account=account, as_of=start)
        # then subtract start-day movements below by filtering strictly < start
        # We'll do correct opening via < start:
        from core.models import JournalEntry as JE
        qs_open = (JE.objects.filter(owner=owner, date__lt=start, debit_account=account)
                   | JE.objects.filter(owner=owner, date__lt=start, credit_account=account)).order_by("date", "id")
        opening = Decimal("0")
        is_debit_normal = account.account_type in ("ASSET", "EXPENSE")
        for je in qs_open:
            debit = je.amount if je.debit_account_id == account.id else Decimal("0")
            credit = je.amount if je.credit_account_id == account.id else Decimal("0")
            opening += (debit - credit) if is_debit_normal else (credit - debit)

    qs = (JournalEntry.objects.filter(owner=owner, debit_account=account)
          | JournalEntry.objects.filter(owner=owner, credit_account=account))

    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)

    qs = qs.order_by("date", "id")

    rows: List[LedgerRow] = []
    running = opening
    is_debit_normal = account.account_type in ("ASSET", "EXPENSE")

    # Add an opening line only if start was provided
    if start:
        rows.append(
            LedgerRow(
                date=start,
                description="Opening Balance",
                debit=Decimal("0"),
                credit=Decimal("0"),
                balance=running,
                related_model="",
                related_id=None,
            )
        )

    for je in qs:
        debit = je.amount if je.debit_account_id == account.id else Decimal("0")
        credit = je.amount if je.credit_account_id == account.id else Decimal("0")

        if is_debit_normal:
            running += (debit - credit)
        else:
            running += (credit - debit)

        rows.append(
            LedgerRow(
                date=je.date,
                description=je.description or f"Journal {je.id}",
                debit=_d(debit),
                credit=_d(credit),
                balance=_d(running),
                related_model=je.related_model or "",
                related_id=je.related_id,
            )
        )

    return rows


# -------------------------
# PARTY LEDGER (Document-based, tenant-safe)
# -------------------------

def get_party_balance(*, owner, party, as_of: Optional[date] = None) -> Decimal:
    rows = get_party_ledger(owner=owner, party=party, start=None, end=as_of)
    if not rows:
        return Decimal("0")
    return rows[-1].balance


def get_party_ledger(
    *,
    owner,
    party,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[LedgerRow]:
    """
    CUSTOMER statement:
      + SalesInvoice (Debit)
      - SalesReturn (Credit)
      - Payment IN (Credit)
      Adjustments:
        DR => Debit
        CR => Credit
      Balance shown as (Debit - Credit): positive = customer owes us

    SUPPLIER statement:
      + PurchaseInvoice (Credit)
      - PurchaseReturn (Debit)
      - Payment OUT (Debit)
      Adjustments:
        DR => Debit (reduces payable)
        CR => Credit (increases payable)
      Balance shown as (Credit - Debit): positive = we owe supplier
    """
    from core.models import SalesInvoice, SalesReturn, PurchaseInvoice, PurchaseReturn, Payment

    def in_range(d: date) -> bool:
        if start and d < start:
            return False
        if end and d > end:
            return False
        return True

    items = []

    # Opening balance line (uses Party fields)
    opening = _d(party.opening_balance)
    opening_is_debit = bool(party.opening_balance_is_debit)

    # CUSTOMER: normal debit
    # SUPPLIER: normal credit
    if party.party_type == "CUSTOMER":
        running = opening if opening_is_debit else -opening  # debit positive
    else:
        # For supplier, we keep running as "payable positive"
        running = opening if (not opening_is_debit) else -opening  # credit positive

    # Add opening row if start provided (so statement has context)
    rows: List[LedgerRow] = []
    if start:
        rows.append(
            LedgerRow(
                date=start,
                description="Opening Balance",
                debit=Decimal("0"),
                credit=Decimal("0"),
                balance=running,
                related_model="",
                related_id=None,
            )
        )

    if party.party_type == "CUSTOMER":
        # Sales Invoices (Debit)
        for inv in SalesInvoice.objects.filter(owner=owner, customer=party, posted=True).order_by("invoice_date", "id"):
            if not in_range(inv.invoice_date):
                continue
            amt = _d(inv.calculate_total())
            items.append(("SalesInvoice", inv.id, inv.invoice_date, f"Sales Invoice #{inv.id}", amt, Decimal("0")))

        # Sales Returns (Credit)
        for ret in SalesReturn.objects.filter(owner=owner, customer=party, posted=True).order_by("return_date", "id"):
            if not in_range(ret.return_date):
                continue
            amt = _d(ret.calculate_total())
            items.append(("SalesReturn", ret.id, ret.return_date, f"Sales Return #{ret.id}", Decimal("0"), amt))

        # Payments IN (Credit) + Adjustments
        for p in Payment.objects.filter(owner=owner, party=party, posted=True).order_by("date", "id"):
            if not in_range(p.date):
                continue

            if p.is_adjustment:
                side = (p.adjustment_side or "DR").upper()
                if side == "DR":
                    items.append(("Payment", p.id, p.date, f"Adjustment DR #{p.id}", _d(p.amount), Decimal("0")))
                else:
                    items.append(("Payment", p.id, p.date, f"Adjustment CR #{p.id}", Decimal("0"), _d(p.amount)))
            else:
                if p.direction == "IN":
                    items.append(("Payment", p.id, p.date, f"Receipt #{p.id}", Decimal("0"), _d(p.amount)))
                elif p.direction == "OUT":
                    # Should not happen for customer usually; keep safe if it exists
                    items.append(("Payment", p.id, p.date, f"Payment OUT #{p.id}", _d(p.amount), Decimal("0")))

        items.sort(key=lambda x: (x[2], x[1]))

        for model, rid, d, desc, debit, credit in items:
            running += (_d(debit) - _d(credit))
            rows.append(
                LedgerRow(
                    date=d,
                    description=desc,
                    debit=_d(debit),
                    credit=_d(credit),
                    balance=_d(running),
                    related_model=model,
                    related_id=rid,
                )
            )

        return rows

    # SUPPLIER
    # Purchase Invoices (Credit)
    for inv in PurchaseInvoice.objects.filter(owner=owner, supplier=party, posted=True).order_by("invoice_date", "id"):
        if not in_range(inv.invoice_date):
            continue
        amt = _d(inv.calculate_total())
        items.append(("PurchaseInvoice", inv.id, inv.invoice_date, f"Purchase Invoice #{inv.id}", Decimal("0"), amt))

    # Purchase Returns (Debit)
    for ret in PurchaseReturn.objects.filter(owner=owner, supplier=party, posted=True).order_by("return_date", "id"):
        if not in_range(ret.return_date):
            continue
        amt = _d(ret.calculate_total())
        items.append(("PurchaseReturn", ret.id, ret.return_date, f"Purchase Return #{ret.id}", amt, Decimal("0")))

    # Payments OUT (Debit) + Adjustments
    for p in Payment.objects.filter(owner=owner, party=party, posted=True).order_by("date", "id"):
        if not in_range(p.date):
            continue

        if p.is_adjustment:
            side = (p.adjustment_side or "DR").upper()
            if side == "DR":
                items.append(("Payment", p.id, p.date, f"Adjustment DR #{p.id}", _d(p.amount), Decimal("0")))
            else:
                items.append(("Payment", p.id, p.date, f"Adjustment CR #{p.id}", Decimal("0"), _d(p.amount)))
        else:
            if p.direction == "OUT":
                items.append(("Payment", p.id, p.date, f"Payment #{p.id}", _d(p.amount), Decimal("0")))
            elif p.direction == "IN":
                # Should not happen for supplier usually; keep safe if it exists
                items.append(("Payment", p.id, p.date, f"Receipt IN #{p.id}", Decimal("0"), _d(p.amount)))

    items.sort(key=lambda x: (x[2], x[1]))

    # Supplier running is "Credit - Debit" (payable positive)
    for model, rid, d, desc, debit, credit in items:
        running += (_d(credit) - _d(debit))
        rows.append(
            LedgerRow(
                date=d,
                description=desc,
                debit=_d(debit),
                credit=_d(credit),
                balance=_d(running),
                related_model=model,
                related_id=rid,
            )
        )

    return rows

def get_trial_balance(*, owner, as_of: Optional[date] = None):
    """
    Returns list of rows:
      [{code, name, type, debit, credit}, ...]
    Rule:
      - For ASSET/EXPENSE: positive balance => debit
      - For LIABILITY/EQUITY/INCOME: positive balance => credit
    """
    from core.models import Account

    qs = Account.objects.filter(owner=owner).order_by("code")

    rows = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")

    for acct in qs:
        bal = get_account_balance(owner=owner, account=acct, as_of=as_of)

        debit = Decimal("0")
        credit = Decimal("0")

        if acct.account_type in ("ASSET", "EXPENSE"):
            # normal debit
            if bal >= 0:
                debit = bal
            else:
                credit = -bal
        else:
            # normal credit
            if bal >= 0:
                credit = bal
            else:
                debit = -bal

        total_debit += debit
        total_credit += credit

        rows.append(
            {
                "code": acct.code,
                "name": acct.name,
                "account_type": acct.account_type,
                "debit": str(debit),
                "credit": str(credit),
            }
        )

    return {
        "rows": rows,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "balanced": (total_debit == total_credit),
    }