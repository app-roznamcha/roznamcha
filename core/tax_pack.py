from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .models import (
    SalesInvoice,
    PurchaseInvoice,
    Product,
    Payment,
    Party,
    Account,
)


def _fmt_date(val):
    if not val:
        return ""
    if isinstance(val, (date, datetime)):
        return val.strftime("%Y-%m-%d")
    return str(val)


def _auto_width(ws, max_cols=40):
    for col in range(1, min(ws.max_column, max_cols) + 1):
        letter = get_column_letter(col)
        ws.column_dimensions[letter].width = 18


def _wb_to_bytes(wb: Workbook) -> bytes:
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# =========================
# SALES LEDGER
# =========================
def generate_sales_ledger(owner) -> bytes:
    """
    Uses your actual fields:
    SalesInvoice: invoice_number, invoice_date, customer, posted, payment_type, payment_account, payment_amount, notes
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales Ledger"

    headers = [
        "Invoice No",
        "Invoice Date",
        "Customer",
        "Customer Phone",
        "Posted",
        "Payment Type",
        "Payment Account",
        "Payment Amount",
        "Notes",
    ]
    ws.append(headers)

    qs = SalesInvoice.objects.filter(owner=owner).select_related("customer", "payment_account").order_by("invoice_date", "id")

    for inv in qs:
        cust = inv.customer
        cust_name = cust.name if cust else ""
        cust_phone = getattr(cust, "phone", "") if cust else ""

        acct = inv.payment_account
        acct_name = acct.name if acct else ""

        ws.append([
            inv.invoice_number or "",
            _fmt_date(inv.invoice_date),
            cust_name,
            cust_phone,
            "YES" if inv.posted else "NO",
            inv.payment_type or "",
            acct_name,
            inv.payment_amount or 0,
            inv.notes or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# PURCHASE LEDGER
# =========================
def generate_purchase_ledger(owner) -> bytes:
    """
    Uses your actual fields:
    PurchaseInvoice: invoice_number, invoice_date, date_received, supplier, freight_charges, other_charges,
                    posted, payment_type, payment_account, payment_amount, notes
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Purchase Ledger"

    headers = [
        "Invoice No",
        "Invoice Date",
        "Date Received",
        "Supplier",
        "Supplier Phone",
        "Freight Charges",
        "Other Charges",
        "Posted",
        "Payment Type",
        "Payment Account",
        "Payment Amount",
        "Notes",
    ]
    ws.append(headers)

    qs = PurchaseInvoice.objects.filter(owner=owner).select_related("supplier", "payment_account").order_by("invoice_date", "id")

    for inv in qs:
        sup = inv.supplier
        sup_name = sup.name if sup else ""
        sup_phone = getattr(sup, "phone", "") if sup else ""

        acct = inv.payment_account
        acct_name = acct.name if acct else ""

        ws.append([
            inv.invoice_number or "",
            _fmt_date(inv.invoice_date),
            _fmt_date(inv.date_received),
            sup_name,
            sup_phone,
            inv.freight_charges or 0,
            inv.other_charges or 0,
            "YES" if inv.posted else "NO",
            inv.payment_type or "",
            acct_name,
            inv.payment_amount or 0,
            inv.notes or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# PAYMENTS LEDGER
# =========================
def generate_payments_ledger(owner) -> bytes:
    """
    Uses your actual fields:
    Payment: date, party, account, direction, amount, description, posted, related_model, related_id, is_adjustment, adjustment_side
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Payments Ledger"

    headers = [
        "Date",
        "Direction",
        "Party",
        "Party Phone",
        "Account",
        "Amount",
        "Posted",
        "Description",
        "Related Model",
        "Related ID",
        "Is Adjustment",
        "Adjustment Side",
    ]
    ws.append(headers)

    qs = Payment.objects.filter(owner=owner).select_related("party", "account").order_by("date", "id")

    for p in qs:
        party = p.party
        party_name = party.name if party else ""
        party_phone = getattr(party, "phone", "") if party else ""

        acct = p.account
        acct_name = acct.name if acct else ""

        ws.append([
            _fmt_date(p.date),
            p.direction or "",
            party_name,
            party_phone,
            acct_name,
            p.amount or 0,
            "YES" if p.posted else "NO",
            p.description or "",
            p.related_model or "",
            p.related_id or "",
            "YES" if p.is_adjustment else "NO",
            p.adjustment_side or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# PRODUCTS LIST
# =========================
def generate_products_list(owner) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    headers = ["Product", "SKU/Code", "Unit", "Cost Price", "Sale Price", "Notes"]
    ws.append(headers)

    qs = Product.objects.filter(owner=owner).order_by("id")
    for pr in qs:
        ws.append([
            getattr(pr, "name", "") or "",
            getattr(pr, "sku", "") or getattr(pr, "code", "") or "",
            getattr(pr, "unit", "") or "",
            getattr(pr, "cost_price", 0) or 0,
            getattr(pr, "sale_price", 0) or 0,
            getattr(pr, "notes", "") or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# PARTIES LIST
# =========================
def generate_parties_list(owner) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Parties"

    headers = ["Party", "Type", "Phone", "City", "Address"]
    ws.append(headers)

    qs = Party.objects.filter(owner=owner).order_by("id")
    for pt in qs:
        ws.append([
            getattr(pt, "name", "") or "",
            getattr(pt, "party_type", "") or getattr(pt, "type", "") or "",
            getattr(pt, "phone", "") or "",
            getattr(pt, "city", "") or "",
            getattr(pt, "address", "") or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# ACCOUNTS LIST
# =========================
def generate_accounts_list(owner) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Accounts"

    headers = ["Account", "Type", "Opening Balance", "Notes"]
    ws.append(headers)

    qs = Account.objects.filter(owner=owner).order_by("id")
    for a in qs:
        ws.append([
            getattr(a, "name", "") or "",
            getattr(a, "account_type", "") or getattr(a, "type", "") or "",
            getattr(a, "opening_balance", 0) or 0,
            getattr(a, "notes", "") or "",
        ])

    _auto_width(ws)
    return _wb_to_bytes(wb)


# =========================
# FULL ZIP
# =========================
def build_tax_pack_zip(owner) -> bytes:
    files = {
        "Sales_Ledger.xlsx": generate_sales_ledger(owner),
        "Purchase_Ledger.xlsx": generate_purchase_ledger(owner),
        "Payments_Ledger.xlsx": generate_payments_ledger(owner),
        "Products_List.xlsx": generate_products_list(owner),
        "Parties_List.xlsx": generate_parties_list(owner),
        "Accounts_List.xlsx": generate_accounts_list(owner),
    }

    bio = BytesIO()
    with ZipFile(bio, "w", compression=ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)

    return bio.getvalue()