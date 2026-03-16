# =========================
# Standard library
# =========================
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import StringIO
from pathlib import Path

# =========================
# Django core
# =========================
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import models, transaction
from django.db.models import (
    Case,
    DecimalField,
    ExpressionWrapper,
    F,
    Max,
    Prefetch,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

# =========================
# Local app imports (core)
# =========================
from core.decorators import resolve_tenant_context

from .decorators import owner_required, subscription_required
from .forms import CompanyUpdateForm, OwnerUpdateForm
from .models import (
    Account,
    CompanyProfile,
    JournalEntry,
    Party,
    Payment,
    Product,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    PurchaseReturn,
    PurchaseReturnItem,
    SalesInvoice,
    SalesInvoiceItem,
    SalesReturn,
    SalesReturnItem,
    StockAdjustment,
    UserProfile,
    get_company_owner,
    peek_next_sequence,   # ✅ ADD THIS

)
from .permissions import staff_allowed, staff_blocked
from .services.ledger import (
    get_account_balance,
    get_account_ledger,
    get_party_balance,
    get_party_ledger,
    get_trial_balance,
)

# =========================
# Tenant utilities
# =========================
from .tenant_utils import (
    get_owner_user,
    get_tenant,
    tenant_get_object_or_404,
    set_tenant_on_create_kwargs,
    tenant_qs,
    get_owner_account,
)
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import update_session_auth_hash
from core.forms import OwnerProfileUpdateForm
from django.http import HttpResponse
from .tax_pack import (
    generate_sales_ledger,
    generate_purchase_ledger,
    generate_payments_ledger,
    generate_products_list,
    generate_parties_list,
    generate_accounts_list,
    build_tax_pack_zip,
)
from django.db.models.functions import Cast
from django.db.models import IntegerField
import asyncio
from urllib.parse import urlencode
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from .models import DailyExpense, CashBankTransfer
from core.models import get_next_sequence


class TenantAwareLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    def form_valid(self, form):
        """
        After login:
        - SUPERADMIN: normal behavior
        - OWNER/STAFF: if logged in on wrong/missing subdomain, redirect to correct subdomain
        """
        response = super().form_valid(form)

        user = self.request.user
        if not user or not user.is_authenticated:
            return response

        # SUPERADMIN bypass
        if user.is_superuser:
            return response

        profile = getattr(user, "profile", None)
        if not profile:
            return response

        role = getattr(profile, "role", None)
        if role not in ("OWNER", "STAFF"):
            return response

        # Resolve owner user
        if role == "OWNER":
            owner_user = user
        else:
            if not getattr(profile, "owner_id", None):
                return response
            owner_user = profile.owner

        company = CompanyProfile.objects.filter(owner=owner_user).first()
        if not company:
            return response

        expected_slug = company.slug

        # Current host parsing (keep port)
        full_host = self.request.get_host()  # e.g. beta.localhost:8000
        host_only = full_host.split(":")[0].lower()
        port = full_host.split(":")[1] if ":" in full_host else ""

        base_domain = getattr(settings, "SAAS_BASE_DOMAIN", "localhost")

        # Determine current subdomain
        current_slug = None
        if host_only.endswith(base_domain):
            sub = host_only[: -(len(base_domain))].rstrip(".")
            current_slug = sub or None

        # If already on correct tenant, keep normal redirect
        if current_slug == expected_slug:
            return response

        # Redirect to correct tenant subdomain with same path (?next respected)
        correct_host = f"{expected_slug}.{base_domain}"
        if port:
            correct_host = f"{correct_host}:{port}"

        next_url = self.get_success_url()
        return redirect(f"{self.request.scheme}://{correct_host}{next_url}")


def landing_page(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "core/landing.html")


def signup_page(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "core/signup.html")


@transaction.atomic
def signup_submit(request):
    if request.method != "POST":
        return redirect("signup")

    username = (request.POST.get("username") or "").strip()
    password = (request.POST.get("password") or "").strip()
    password2 = (request.POST.get("password2") or "").strip()  # ✅ NEW (confirm password)
    full_name = (request.POST.get("full_name") or "").strip()
    email = (request.POST.get("email") or "").strip().lower()

    company_name = (request.POST.get("company_name") or "").strip()
    slug_input = (request.POST.get("slug") or "").strip().lower()
    phone = (request.POST.get("phone") or "").strip()

    # ---- Basic validation ----
    if not username or not password or not password2 or not company_name or not email:
        messages.error(request, "Username, email, company name and password are required.")
        return redirect("signup")

    # ✅ NEW: confirm password check
    if password != password2:
        messages.error(request, "Passwords do not match.")
        return redirect("signup")

    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        messages.error(request, "Please enter a valid email address.")
        return redirect("signup")

    if len(password) < 6:
        messages.error(request, "Password must be at least 6 characters.")
        return redirect("signup")

    if User.objects.filter(username=username).exists():
        messages.error(request, "This username is already taken.")
        return redirect("signup")
    if User.objects.filter(email__iexact=email).exists():
        messages.error(request, "This email is already in use.")
        return redirect("signup")
    
    from .models import CompanyProfile, UserProfile  # keep local import

    # ---- Slug (auto-generate if blank) ----
    if slug_input:
        # user provided slug → validate
        if not re.match(r"^[a-z0-9-]+$", slug_input):
            messages.error(request, "Subdomain can only contain letters, numbers, and hyphen.")
            return redirect("signup")
        base_slug = slug_input
    else:
        # auto slug from company name
        base_slug = slugify(company_name)  # "Un Standard Zarai Center" -> "un-standard-zarai-center"
        if not base_slug:
            base_slug = "company"

    # Ensure unique slug
    slug = base_slug
    counter = 2
    while CompanyProfile.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1

    # ---- Create owner user ----
    user = User.objects.create_user(username=username, password=password, email=email)

    if full_name:
        parts = full_name.split()
        user.first_name = parts[0]
        user.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        user.save(update_fields=["first_name", "last_name"])

    # ---- Create/ensure company FIRST (so signals won't create it with wrong slug) ----
    company, created = CompanyProfile.objects.get_or_create(
        owner=user,
        defaults={
            "name": company_name,
            "slug": slug,
            "phone": phone or "",
            "email": email or "",
        },
    )

    # If company existed, you CAN enforce slug here only if you want:
    # (optional) update name/phone/email if blank
    changed = False
    if not company.name:
        company.name = company_name
        changed = True
    if phone and not company.phone:
        company.phone = phone
        changed = True
    if email and not company.email:
        company.email = email
        changed = True
    if changed:
        company.save()

    # ---- Now set OWNER role (this triggers signals to seed accounts) ----
    prof, _ = UserProfile.objects.get_or_create(user=user)
    prof.role = "OWNER"
    prof.owner = None          # ✅ IMPORTANT
    prof.is_active = True
    prof.subscription_status = "TRIAL"
    if not prof.trial_started_at:
        prof.trial_started_at = timezone.now()
    prof.save()

    from .models import seed_default_accounts_for_owner
    seed_default_accounts_for_owner(user)

    # If company existed (rare), ensure slug is set correctly
    if not created:
        # don’t overwrite existing slug if already set; only fill if missing
        changed = False
        if not company.slug:
            company.slug = slug
            changed = True
        if not company.name:
            company.name = company_name
            changed = True
        if changed:
            company.save()

    # ---- Log user in ----
    login(request, user)

    # ---- Redirect to correct subdomain ----
    base_domain = getattr(settings, "SAAS_BASE_DOMAIN", "") or request.get_host()
    base_domain = base_domain.lstrip(".")
    if not base_domain:
        base_domain = request.get_host()

    scheme = "https" if request.is_secure() else "http"
    target = f"{scheme}://{company.slug}.{base_domain}{reverse('dashboard')}"
    return redirect(target)

def superadmin_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")

        # allow Django superuser OR profile role SUPERADMIN
        prof = getattr(request.user, "profile", None)
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        if prof and prof.role == "SUPERADMIN":
            return view_func(request, *args, **kwargs)

        return HttpResponseForbidden("Super Admin access required.")
    return _wrapped

def _tenant_backup_folder(owner, company: CompanyProfile) -> Path:
    base = Path(getattr(settings, "BACKUP_DIR", ""))  # Render: /var/data/backups
    if not str(base).strip():
        base = Path(settings.MEDIA_ROOT) / "backups"  # local fallback
    folder = company.slug or f"owner-{owner.id}"
    return base / folder

def _list_last_backups(folder: Path, limit: int = 3):
    if not folder.exists():
        return []
    files = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.name.endswith(".json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out = []
    for f in files[:limit]:
        out.append({
            "name": f.name,
            "size": f.stat().st_size,
            "modified": timezone.datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.get_current_timezone()
            ),
        })
    return out

def _keep_last_n(folder: Path, n: int = 3):
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

def _wipe_tenant_data(owner):
    # delete children first
    SalesInvoiceItem.objects.filter(owner=owner).delete()
    PurchaseInvoiceItem.objects.filter(owner=owner).delete()
    SalesReturnItem.objects.filter(owner=owner).delete()
    PurchaseReturnItem.objects.filter(owner=owner).delete()

    # ✅ NEW: models that reference Account with PROTECT
    CashBankTransfer.objects.filter(owner=owner).delete()
    DailyExpense.objects.filter(owner=owner).delete()

    # documents
    SalesInvoice.objects.filter(owner=owner).delete()
    PurchaseInvoice.objects.filter(owner=owner).delete()
    SalesReturn.objects.filter(owner=owner).delete()
    PurchaseReturn.objects.filter(owner=owner).delete()
    Payment.objects.filter(owner=owner).delete()
    StockAdjustment.objects.filter(owner=owner).delete()

    # ledger + masters
    JournalEntry.objects.filter(owner=owner).delete()
    Party.objects.filter(owner=owner).delete()
    Product.objects.filter(owner=owner).delete()
    Account.objects.filter(owner=owner).delete()

    # CompanyProfile is NOT deleted here (keep tenant identity)


@login_required
@owner_required
def dashboard(request):
    today = timezone.localdate()
    now = timezone.localtime()

    profile = getattr(request.user, "profile", None)
    role = getattr(profile, "role", "STAFF")
    is_staff = (role == "STAFF")
    is_owner = (role == "OWNER")
    is_superadmin = bool(getattr(request.user, "is_superuser", False))

    # ✅ OPTIONAL: if superadmin is on www/base domain (no tenant), go to superadmin dashboard
    if is_superadmin and getattr(request, "tenant", None) is None:
        return redirect("superadmin_dashboard")


    owner = getattr(request, "owner", None) or get_company_owner(request.user)
    month_start = today.replace(day=1)
    # Prefer middleware-resolved tenant if present (subdomain mode)
    company = getattr(request, "tenant", None) or CompanyProfile.objects.filter(owner=owner).order_by("-id").first()
    # Don't set request.tenant in owner-only design

    MONEY = DecimalField(max_digits=14, decimal_places=2)
    ZERO = Value(Decimal("0.00"), output_field=MONEY)

    # -------------------------
    # Cash/Bank balance (sum of cash/bank accounts)
    # -------------------------
    cash_bank_balance = Decimal("0.00")
    # Option A: if your Account has a balance/current_balance field
    if hasattr(Account, "current_balance"):
        cash_bank_balance = (
            Account.objects.filter(owner=owner, is_cash_or_bank=True)
            .aggregate(total=Coalesce(Sum("current_balance"), ZERO))
            .get("total", Decimal("0.00"))
        )
    elif hasattr(Account, "balance"):
        cash_bank_balance = (
            Account.objects.filter(owner=owner, is_cash_or_bank=True)
            .aggregate(total=Coalesce(Sum("balance"), ZERO))
            .get("total", Decimal("0.00"))
        )
    else:
        # If you don't store balances on Account yet, keep 0 for now.
        cash_bank_balance = Decimal("0.00")

    customers_count = Party.objects.filter(owner=owner, party_type="CUSTOMER", is_active=True).count()
    products_count = Product.objects.filter(owner=owner, is_active=True).count()
    sales_invoices_count = SalesInvoice.objects.filter(owner=owner).count()

    line_total_expr = ExpressionWrapper(
        (F("quantity_units") * F("unit_price")) - Coalesce(F("discount_amount"), ZERO),
        output_field=MONEY,
    )
    # -------------------------
    # This month Sales / Purchases (posted)
    # -------------------------
    month_sales = (
        SalesInvoiceItem.objects.filter(
            owner=owner,
            sales_invoice__posted=True,
            sales_invoice__invoice_date__gte=month_start,
            sales_invoice__invoice_date__lte=today,
        )
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    month_purchase_items = (
        PurchaseInvoiceItem.objects.filter(
            owner=owner,
            purchase_invoice__posted=True,
            purchase_invoice__invoice_date__gte=month_start,
            purchase_invoice__invoice_date__lte=today,
        )
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    charges_expr = ExpressionWrapper(
        Coalesce(F("freight_charges"), ZERO) + Coalesce(F("other_charges"), ZERO),
        output_field=MONEY,
    )

    month_purchase_charges = (
        PurchaseInvoice.objects.filter(
            owner=owner,
            posted=True,
            invoice_date__gte=month_start,
            invoice_date__lte=today,
        )
        .aggregate(total=Coalesce(Sum(charges_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    month_purchases = (month_purchase_items or Decimal("0.00")) + (month_purchase_charges or Decimal("0.00"))

    # Simple profit snapshot (not full accounting profit, but very useful for traders)
    month_profit_simple = (month_sales or Decimal("0.00")) - (month_purchases or Decimal("0.00"))
    
    total_sales_all = (
        SalesInvoiceItem.objects.filter(owner=owner, sales_invoice__posted=True)
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    total_sales_returns_all = (
        SalesReturnItem.objects.filter(owner=owner, sales_return__posted=True)
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    total_customer_receipts = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=False,
            direction="IN",
            party__party_type="CUSTOMER",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    customer_adj_dr = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=True,
            party__party_type="CUSTOMER",
            adjustment_side="DR",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    customer_adj_cr = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=True,
            party__party_type="CUSTOMER",
            adjustment_side="CR",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    raw_customer_balance = (
        (total_sales_all or Decimal("0.00"))
        - (total_customer_receipts or Decimal("0.00"))
        - (total_sales_returns_all or Decimal("0.00"))
        + ((customer_adj_dr or Decimal("0.00")) - (customer_adj_cr or Decimal("0.00")))
    )

    customer_receivable = raw_customer_balance if raw_customer_balance > 0 else Decimal("0.00")
    customer_advance = (-raw_customer_balance) if raw_customer_balance < 0 else Decimal("0.00")

    total_purchase_items_all = (
        PurchaseInvoiceItem.objects.filter(owner=owner, purchase_invoice__posted=True)
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    total_purchase_charges_all = (
        PurchaseInvoice.objects.filter(owner=owner, posted=True)
        .aggregate(total=Coalesce(Sum(charges_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    total_purchase_returns_all = (
        PurchaseReturnItem.objects.filter(owner=owner, purchase_return__posted=True)
        .aggregate(total=Coalesce(Sum(line_total_expr), ZERO))
        .get("total", Decimal("0.00"))
    )

    total_supplier_payments = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=False,
            direction="OUT",
            party__party_type="SUPPLIER",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    supplier_adj_cr = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=True,
            party__party_type="SUPPLIER",
            adjustment_side="CR",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    supplier_adj_dr = (
        Payment.objects.filter(
            owner=owner,
            posted=True,
            is_adjustment=True,
            party__party_type="SUPPLIER",
            adjustment_side="DR",
        )
        .aggregate(total=Coalesce(Sum("amount"), ZERO))
        .get("total", Decimal("0.00"))
    )

    raw_supplier_balance = (
        (total_purchase_items_all or Decimal("0.00"))
        + (total_purchase_charges_all or Decimal("0.00"))
        - (total_supplier_payments or Decimal("0.00"))
        - (total_purchase_returns_all or Decimal("0.00"))
        + ((supplier_adj_cr or Decimal("0.00")) - (supplier_adj_dr or Decimal("0.00")))
    )

    supplier_payable = raw_supplier_balance if raw_supplier_balance > 0 else Decimal("0.00")
    supplier_advance = (-raw_supplier_balance) if raw_supplier_balance < 0 else Decimal("0.00")

    context = {
        "customers_count": customers_count,
        "products_count": products_count,
        "sales_invoices_count": sales_invoices_count,
        "customer_receivable": customer_receivable,
        "customer_advance": customer_advance,
        "supplier_payable": supplier_payable,
        "supplier_advance": supplier_advance,
        "today": today,
        "company": company,
        "now": now,
        "context_role": role,
        "is_staff": is_staff,
        "is_owner": is_owner,
        "is_superadmin": is_superadmin,
        "cash_bank_balance": cash_bank_balance,
        "month_sales": month_sales,
        "month_purchases": month_purchases,
        "month_profit_simple": month_profit_simple,
    }

    return render(request, "core/dashboard.html", context)


# --------------------------
# Party (Customer / Supplier) Views
# --------------------------

# ✅ Customers List
@login_required
@owner_required
def customer_list(request):
    owner = request.owner

    parties = (
        Party.objects.filter(owner=owner, party_type="CUSTOMER")
        .order_by("name")
    )
    return render(request, "core/party_list.html", {
        "parties": parties,
        "party_type": "CUSTOMER",
        "party_type_label": "Customer",
    })


# ✅ Suppliers List
@login_required
@owner_required
def supplier_list(request):
    owner = request.owner

    parties = (
        Party.objects.filter(owner=owner, party_type="SUPPLIER")
        .order_by("name")
    )
    return render(request, "core/party_list.html", {
        "parties": parties,
        "party_type": "SUPPLIER",
        "party_type_label": "Supplier",
    })

def _parse_opening_balance(raw: str) -> Decimal:
    raw = (raw or "").strip()
    if not raw:
        return Decimal("0")
    try:
        val = Decimal(raw)
    except (InvalidOperation, TypeError):
        return Decimal("0")
    if val < 0:
        val = -val
    return val


# ✅ Create Customer
@login_required
@owner_required
@subscription_required
def customer_create(request):
    owner = request.owner

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        city = (request.POST.get("city") or "").strip()
        address = (request.POST.get("address") or "").strip()

        opening_balance_raw = request.POST.get("opening_balance")
        opening_side = (request.POST.get("opening_side") or "DR").upper()

        if not name:
            messages.error(request, "Name is required.")
            return render(request, "core/party_form.html", {
                "party": None,
                "is_edit": False,
                "party_type": "CUSTOMER",
                "party_type_label": "Customer",
            })

        opening_balance = _parse_opening_balance(opening_balance_raw)
        opening_is_debit = (opening_side != "CR")

        kwargs = {
            "owner": owner,
            "name": name,
            "phone": phone,
            "city": city,
            "address": address,
            "party_type": "CUSTOMER",
            "opening_balance": opening_balance,
            "opening_balance_is_debit": opening_is_debit,
        }
        kwargs = set_tenant_on_create_kwargs(request, kwargs, Party)
        party = Party.objects.create(**kwargs)

        if opening_balance > 0:
            create_opening_entry_for_party(request, party, opening_balance, opening_side)

        return redirect("customer_list")

    return render(request, "core/party_form.html", {
        "party": None,
        "is_edit": False,
        "party_type": "CUSTOMER",
        "party_type_label": "Customer",
    })


@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def supplier_create(request):
    owner = request.owner

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        city = (request.POST.get("city") or "").strip()
        address = (request.POST.get("address") or "").strip()

        opening_balance_raw = request.POST.get("opening_balance")
        opening_side = (request.POST.get("opening_side") or "DR").upper()

        if not name:
            messages.error(request, "Name is required.")
            return render(request, "core/party_form.html", {
                "party": None,
                "is_edit": False,
                "party_type": "SUPPLIER",
                "party_type_label": "Supplier",
            })

        opening_balance = _parse_opening_balance(opening_balance_raw)
        opening_is_debit = (opening_side != "CR")

        kwargs = {
            "owner": owner,
            "name": name,
            "phone": phone,
            "city": city,
            "address": address,
            "party_type": "SUPPLIER",
            "opening_balance": opening_balance,
            "opening_balance_is_debit": opening_is_debit,
        }
        kwargs = set_tenant_on_create_kwargs(request, kwargs, Party)
        party = Party.objects.create(**kwargs)

        if opening_balance > 0:
            create_opening_entry_for_party(request, party, opening_balance, opening_side)

        return redirect("supplier_list")

    return render(request, "core/party_form.html", {
        "party": None,
        "is_edit": False,
        "party_type": "SUPPLIER",
        "party_type_label": "Supplier",
    })

# ✅ Edit Customer
@login_required
@owner_required
@subscription_required
@staff_blocked
def customer_edit(request, pk):
    owner = request.owner

    party = tenant_get_object_or_404(
        request,
        Party,
        pk=pk,
        party_type="CUSTOMER",
    )

    if request.method == "POST":
        party.name = (request.POST.get("name", party.name) or "").strip()
        party.phone = (request.POST.get("phone", party.phone) or "").strip()
        party.city = (request.POST.get("city", party.city) or "").strip()
        party.address = (request.POST.get("address", party.address) or "").strip()
        party.save(update_fields=["name", "phone", "city", "address"])
        return redirect("customer_list")

    return render(request, "core/party_form.html", {
        "party": party,
        "is_edit": True,
        "party_type": "CUSTOMER",
        "party_type_label": "Customer",
    })


# ✅ Edit Supplier
@login_required
@owner_required
@subscription_required
@staff_blocked
def supplier_edit(request, pk):
    owner = request.owner

    party = tenant_get_object_or_404(
        request,
        Party,
        pk=pk,
        party_type="SUPPLIER",
    )
    if request.method == "POST":
        party.name = (request.POST.get("name", party.name) or "").strip()
        party.phone = (request.POST.get("phone", party.phone) or "").strip()
        party.city = (request.POST.get("city", party.city) or "").strip()
        party.address = (request.POST.get("address", party.address) or "").strip()
        party.save(update_fields=["name", "phone", "city", "address"])
        return redirect("supplier_list")

    return render(request, "core/party_form.html", {
        "party": party,
        "is_edit": True,
        "party_type": "SUPPLIER",
        "party_type_label": "Supplier",
    })


def _party_create_or_edit(request, party_type, instance=None):
    is_edit = instance is not None
    is_new = not is_edit
    label = "Customer" if party_type == "CUSTOMER" else "Supplier"

    owner = request.owner

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        city = (request.POST.get("city") or "").strip()
        address = (request.POST.get("address") or "").strip()

        opening_balance_str = (request.POST.get("opening_balance") or "").strip()
        opening_side = (request.POST.get("opening_side") or "DR").upper()

        try:
            opening_amount = Decimal(opening_balance_str) if opening_balance_str else Decimal("0")
        except (InvalidOperation, TypeError):
            opening_amount = Decimal("0")

        if opening_amount < 0:
            opening_amount = -opening_amount

        if opening_side not in ("DR", "CR"):
            opening_side = "DR"

        if not name:
            messages.error(request, f"{label} name is required.")
        else:
            if is_new:
                instance = Party(
                    owner=owner,
                    party_type=party_type,
                )

            instance.name = name
            instance.phone = phone
            instance.city = city
            instance.address = address
            instance.is_active = True

            if is_new:
                instance.opening_balance = opening_amount
                instance.opening_balance_is_debit = (opening_side == "DR")

            instance.save()

            if is_new and opening_amount > 0:
                create_opening_entry_for_party(request, instance, opening_amount, opening_side)

            messages.success(
                request,
                f"{label} {'updated' if is_edit else 'created'} successfully.",
            )

            return redirect("customer_list" if party_type == "CUSTOMER" else "supplier_list")

    context = {
        "is_edit": is_edit,
        "party": instance,
        "party_type": party_type,
        "party_type_label": label,
    }
    return render(request, "core/party_form.html", context)


def _get_date_range(request):
    """
    Read ?from=YYYY-MM-DD&to=YYYY-MM-DD from querystring.
    Default: current month to today.
    """
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    today = date.today()
    default_from = today.replace(day=1)
    default_to = today

    date_from = parse_date(from_str) if from_str else default_from
    date_to = parse_date(to_str) if to_str else default_to

    # Guard against invalid query values, e.g. ?from=invalid-date
    if date_from is None:
        date_from = default_from
    if date_to is None:
        date_to = default_to

    # Normalize inverted ranges to avoid empty/misleading reports
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    return date_from, date_to


@login_required
@owner_required
@staff_blocked
def product_list(request):
    products = (
        Product.objects.filter(owner=request.owner)
        .order_by("code")
    )
    return render(request, "core/product_list.html", {"products": products})

@login_required
@owner_required
@subscription_required
def product_create(request):
    error = None
    owner = request.owner

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        name = (request.POST.get("name") or "").strip()
        unit = request.POST.get("unit") or "BAG"

        purchase_price_raw = (request.POST.get("purchase_price_per_unit") or "0").strip()
        sale_price_raw = (request.POST.get("sale_price_per_unit") or "0").strip()
        is_active = bool(request.POST.get("is_active"))

        packing_type = request.POST.get("packing_type") or "NONE"
        pieces_per_pack_raw = (request.POST.get("pieces_per_pack") or "1").strip()

        if not code or not name:
            error = "Code and Name are required."
        else:
            try:
                purchase_price = Decimal(purchase_price_raw)
            except (InvalidOperation, TypeError):
                purchase_price = Decimal("0")

            try:
                sale_price = Decimal(sale_price_raw)
            except (InvalidOperation, TypeError):
                sale_price = Decimal("0")

            try:
                pieces_per_pack = Decimal(pieces_per_pack_raw)
            except (InvalidOperation, TypeError):
                pieces_per_pack = Decimal("1")

            if pieces_per_pack <= 0:
                pieces_per_pack = Decimal("1")

            kwargs = {
                "owner": owner,
                "code": code,
                "name": name,
                "unit": unit,
                "purchase_price_per_unit": purchase_price,
                "sale_price_per_unit": sale_price,
                "is_active": is_active,
                "packing_type": packing_type,
                "pieces_per_pack": pieces_per_pack,
            }
            kwargs = set_tenant_on_create_kwargs(request, kwargs, Product)
            Product.objects.create(**kwargs)
            return redirect("product_list")

    return render(request, "core/product_form.html", {
        "error": error,
        "product": None,
        "UNIT_CHOICES": Product.UNIT_CHOICES,
        "PACKING_CHOICES": Product.PACKING_CHOICES,
    })


@login_required
@owner_required
@subscription_required
@staff_blocked
def product_edit(request, pk):
    owner = request.owner

    product = tenant_get_object_or_404(
        request,
        Product,
        pk=pk,
    )

    error = None

    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        name = (request.POST.get("name") or "").strip()
        unit = request.POST.get("unit") or product.unit

        purchase_price_raw = (request.POST.get("purchase_price_per_unit") or "").strip()
        sale_price_raw = (request.POST.get("sale_price_per_unit") or "").strip()
        is_active = bool(request.POST.get("is_active"))

        packing_type = request.POST.get("packing_type") or product.packing_type
        pieces_per_pack_raw = (request.POST.get("pieces_per_pack") or "").strip()

        if not code or not name:
            error = "Code and Name are required."
        else:
            try:
                purchase_price = Decimal(purchase_price_raw) if purchase_price_raw else product.purchase_price_per_unit
            except (InvalidOperation, TypeError):
                purchase_price = product.purchase_price_per_unit

            try:
                sale_price = Decimal(sale_price_raw) if sale_price_raw else product.sale_price_per_unit
            except (InvalidOperation, TypeError):
                sale_price = product.sale_price_per_unit

            try:
                pieces_per_pack = Decimal(pieces_per_pack_raw) if pieces_per_pack_raw else (product.pieces_per_pack or Decimal("1"))
            except (InvalidOperation, TypeError):
                pieces_per_pack = product.pieces_per_pack or Decimal("1")

            if pieces_per_pack <= 0:
                pieces_per_pack = Decimal("1")

            product.code = code
            product.name = name
            product.unit = unit
            product.purchase_price_per_unit = purchase_price
            product.sale_price_per_unit = sale_price
            product.is_active = is_active
            product.packing_type = packing_type
            product.pieces_per_pack = pieces_per_pack
            product.save(update_fields=[
                "code",
                "name",
                "unit",
                "purchase_price_per_unit",
                "sale_price_per_unit",
                "is_active",
                "packing_type",
                "pieces_per_pack",
            ])
            return redirect("product_list")

    return render(request, "core/product_form.html", {
        "error": error,
        "product": product,
        "UNIT_CHOICES": Product.UNIT_CHOICES,
        "PACKING_CHOICES": Product.PACKING_CHOICES,
    })

@login_required
@owner_required
@staff_blocked
def payment_list(request):
    qs = (
        Payment.objects.filter(owner=request.owner)
        .select_related("party", "account")
        .order_by("-date", "-id")
    )

    parties = (
        Party.objects.filter(owner=request.owner, is_active=True)
        .order_by("name")
    )

    direction = request.GET.get("direction", "")
    if direction in ("IN", "OUT"):
        qs = qs.filter(direction=direction)

    party_id = request.GET.get("party") or ""
    if party_id:
        qs = qs.filter(party_id=party_id)

    from_date = request.GET.get("from_date") or ""
    to_date = request.GET.get("to_date") or ""

    if from_date:
        qs = qs.filter(date__gte=from_date)
    if to_date:
        qs = qs.filter(date__lte=to_date)

    total_amount = qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    context = {
        "payments": qs,
        "parties": parties,
        "selected_direction": direction,
        "selected_party_id": party_id,
        "from_date": from_date,
        "to_date": to_date,
        "total_amount": total_amount,
    }
    return render(request, "core/payments_list.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def payment_new(request):
    parties = (
        Party.objects.filter(owner=request.owner, is_active=True)
        .order_by("name")
    )

    accounts = (
        Account.objects.filter(owner=request.owner, is_cash_or_bank=True, allow_for_payments=True)
        .order_by("code")
    )

    error = None

    if request.method == "POST":
        date_str = request.POST.get("date") or ""
        party_id = request.POST.get("party") or ""
        account_id = request.POST.get("account") or ""
        direction = request.POST.get("direction") or ""
        amount_str = (request.POST.get("amount") or "0").strip()
        description = (request.POST.get("description") or "").strip()

        is_adjustment = request.POST.get("is_adjustment") == "on"
        adjustment_side = (request.POST.get("adjustment_side", "") or "").upper()

        if date_str:
            try:
                payment_date = date.fromisoformat(date_str)
            except ValueError:
                payment_date = date.today()
        else:
            payment_date = date.today()

        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, TypeError):
            amount = Decimal("0")

        if direction not in ("IN", "OUT"):
            error = "Please choose whether this is a Receipt (IN) or Payment (OUT)."
        elif not party_id:
            error = "Please select a party (customer or supplier)."
        elif (not is_adjustment) and (not account_id):
            error = "Please select a cash/bank account."
        elif amount <= 0:
            error = "Amount must be greater than zero."

        if not error:
            party = tenant_get_object_or_404(
                request,
                Party,
                pk=party_id,
                is_active=True,
            )

            account = None
            if not is_adjustment:
                account = tenant_get_object_or_404(
                    request,
                    Account,
                    pk=account_id,
                    is_cash_or_bank=True,
                    allow_for_payments=True,
                )

            kwargs = {
                "owner": request.owner,
                "date": payment_date,
                "party": party,
                "direction": direction,
                "amount": amount,
                "description": description,
                "posted": False,
            }

            if is_adjustment:
                kwargs["is_adjustment"] = True
                kwargs["adjustment_side"] = adjustment_side
                kwargs["account"] = None
            else:
                kwargs["is_adjustment"] = False
                kwargs["adjustment_side"] = ""
                kwargs["account"] = account

            kwargs = set_tenant_on_create_kwargs(request, kwargs, Payment)

            payment = Payment.objects.create(**kwargs)
            payment.post()

            return redirect("payments_list")

    context = {
        "parties": parties,
        "accounts": accounts,
        "error": error,
        "today": date.today().isoformat(),
    }
    return render(request, "core/payment_new.html", context)


# ---------- SALES: LIST ----------

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@subscription_required
def sales_list(request):
    """
    Sales list page:
    - Owner sees: Edit/Delete/Post/Share
    - Staff sees: Share only (no edit/delete/post)
    """
    invoices = (
        SalesInvoice.objects.filter(owner=request.owner)
        .select_related("customer")
        .prefetch_related("items__product")
        .order_by("-invoice_date", "-id")
    )
    profile = getattr(request.user, "profile", None)
    is_staff_user = bool(profile and getattr(profile, "role", None) == "STAFF")
    from_date = request.GET.get("from") or ""
    to_date = request.GET.get("to") or ""
    customer_id = request.GET.get("customer") or ""
    posted_filter = request.GET.get("posted") or "all"

    if from_date:
        invoices = invoices.filter(invoice_date__gte=from_date)
    if to_date:
        invoices = invoices.filter(invoice_date__lte=to_date)
    if customer_id:
        invoices = invoices.filter(customer_id=customer_id)

    if posted_filter == "yes":
        invoices = invoices.filter(posted=True)
    elif posted_filter == "no":
        invoices = invoices.filter(posted=False)

    customers = (
        Party.objects.filter(owner=request.owner, party_type="CUSTOMER", is_active=True)
        .order_by("name")
    )

    # ✅ Role flags for template
    prof = getattr(request.user, "profile", None)
    role = getattr(prof, "role", None)
    is_staff_user = (role == "STAFF")
    is_owner_user = (role == "OWNER") or getattr(request.user, "is_superuser", False)

    context = {
        "invoices": invoices,
        "customers": customers,
        "from_date": from_date,
        "to_date": to_date,
        "customer_id": customer_id,
        "posted_filter": posted_filter,

        # ✅ used by sales_list.html to hide/show buttons
        "is_staff_user": is_staff_user,
        "is_owner_user": is_owner_user,

    }
    return render(request, "core/sales_list.html", context)


@login_required
@owner_required
@subscription_required
@transaction.atomic
def sales_new(request):
    customers = (
        Party.objects.filter(owner=request.owner, party_type="CUSTOMER", is_active=True)
        .order_by("name")
    )

    products = (
        Product.objects.filter(owner=request.owner, is_active=True)
        .order_by("code")
    )

    accounts = (
        Account.objects.filter(
            owner=request.owner,
            is_cash_or_bank=True,
            allow_for_payments=True,
        )
        .order_by("code")
    )

    today_str = timezone.now().date().isoformat()
    suggested_invoice_number = peek_next_sequence(request.owner, "sales_invoice")
    error = None
    if request.method == "POST":
        customer_id = request.POST.get("customer")

        # ✅ Don't read invoice_number from POST (pre-filled values cause duplicates)
        invoice_number = None

        invoice_date_str = request.POST.get("invoice_date") or ""
        notes = (request.POST.get("notes") or "").strip()

        payment_type = (request.POST.get("payment_type") or "CREDIT").upper()
        if payment_type not in ("CREDIT", "FULL", "PARTIAL"):
            payment_type = "CREDIT"

        payment_account_id = request.POST.get("payment_account") or ""
        payment_amount_str = (request.POST.get("payment_amount") or "0").strip()

        if not customer_id:
            error = "Customer is required."

        if invoice_date_str:
            try:
                invoice_date = timezone.datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
            except ValueError:
                invoice_date = timezone.now().date()
        else:
            invoice_date = timezone.now().date()

        customer = None
        if not error:
            customer = tenant_get_object_or_404(
                request,
                Party,
                pk=customer_id,
                party_type="CUSTOMER",
                is_active=True,
            )

        line_items = []
        row_indices = set()

        for key in request.POST.keys():
            if key.startswith("product_"):
                idx = key.split("_", 1)[1]
                row_indices.add(idx)

        row_indices = sorted(row_indices, key=lambda x: int(x) if x.isdigit() else x)

        for idx in row_indices:
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id or not qty_str:
                continue

            product = tenant_get_object_or_404(
                request,
                Product,
                pk=product_id,
                is_active=True,
            )

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("0")

            if qty <= 0:
                continue

            # ✅ ADD THIS HERE
            available = product.current_stock or Decimal("0")
            if qty > available:
                error = f"Not enough stock for {product.code} - {product.name}. Available: {available}, You entered: {qty}."
                break

            try:
                unit_price = Decimal(unit_price_str or product.sale_price_per_unit)
            except Exception:
                unit_price = product.sale_price_per_unit

            line_items.append({"product": product, "qty": qty, "unit_price": unit_price})

        if error:
            line_items = []

        if not line_items and not error:
            error = "Please enter at least one product line."

        invoice_total = Decimal("0")
        for li in line_items:
            invoice_total += li["qty"] * li["unit_price"]

        payment_account_obj = None
        try:
            payment_amount = Decimal(payment_amount_str)
        except Exception:
            payment_amount = Decimal("0")

        if not error:
            if payment_type == "CREDIT":
                payment_amount = Decimal("0")
                payment_account_obj = None
            else:
                if not payment_account_id:
                    error = "Please select the cash/bank account for this payment."
                else:
                    payment_account_obj = tenant_get_object_or_404(
                        request,
                        Account,
                        pk=payment_account_id,
                        is_cash_or_bank=True,
                        allow_for_payments=True,
                    )

                if payment_amount <= 0 and not error:
                    error = "Payment amount must be greater than zero."

                if not error:
                    if invoice_total <= 0:
                        error = "Invoice total must be greater than zero."
                    else:
                        if payment_type == "FULL" and payment_amount != invoice_total:
                            error = f"For full payment, amount must equal invoice total ({invoice_total})."
                        elif payment_type == "PARTIAL" and not (Decimal("0") < payment_amount < invoice_total):
                            error = "For partial payment, amount must be > 0 and < invoice total."

        if not error:
            # ✅ Generate invoice number only after all validations succeed
            invoice_number = str(get_next_sequence(request.owner, "sales_invoice"))

            inv_kwargs = {
                "customer": customer,
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "notes": notes,
                "posted": False,
                "payment_type": payment_type,
                "payment_account": payment_account_obj,
                "payment_amount": payment_amount,
            }

            invoice = SalesInvoice.objects.create(
                owner=request.owner,
                **inv_kwargs,
            )

            for li in line_items:
                item_kwargs = {
                    "sales_invoice": invoice,
                    "product": li["product"],
                    "unit_type": li["product"].unit,
                    "quantity_units": li["qty"],
                    "unit_price": li["unit_price"],
                    "discount_amount": Decimal("0"),
                }

                SalesInvoiceItem.objects.create(
                    owner=request.owner,
                    **item_kwargs,
                )

            return redirect("sales_list")

    context = {
        "customers": customers,
        "products": products,
        "accounts": accounts,
        "error": error,
        "today": today_str,
        "suggested_invoice_number": str(suggested_invoice_number),
    }
    return render(request, "core/sales_new.html", context)

# ---------- SALES: POST ----------

@login_required
@resolve_tenant_context(require_company=True)
@staff_blocked
@subscription_required
@transaction.atomic
def sales_post(request, pk):
    invoice = tenant_get_object_or_404(request, SalesInvoice, pk=pk)
    if not invoice.posted:
        try:
            invoice.post()
            messages.success(request, f"Sale #{invoice.invoice_number or invoice.id} posted successfully.")
        except ValidationError as exc:
            messages.error(request, str(exc))
    return redirect("sales_list")


@login_required
@owner_required
@staff_blocked
def purchase_list(request):
    invoices = (
        PurchaseInvoice.objects.filter(owner=request.owner)
        .select_related("supplier")
        .prefetch_related("items__product")
        .order_by("-invoice_date", "-id")
    )
    from_date = request.GET.get("from") or ""
    to_date = request.GET.get("to") or ""
    supplier_id = request.GET.get("supplier") or ""
    posted_filter = request.GET.get("posted") or "all"

    if from_date:
        invoices = invoices.filter(invoice_date__gte=from_date)
    if to_date:
        invoices = invoices.filter(invoice_date__lte=to_date)
    if supplier_id:
        invoices = invoices.filter(supplier_id=supplier_id)

    if posted_filter == "yes":
        invoices = invoices.filter(posted=True)
    elif posted_filter == "no":
        invoices = invoices.filter(posted=False)

    suppliers = (
        Party.objects.filter(owner=request.owner, party_type="SUPPLIER", is_active=True)
        .order_by("name")
    )

    context = {
        "invoices": invoices,
        "suppliers": suppliers,
        "from_date": from_date,
        "to_date": to_date,
        "supplier_id": supplier_id,
        "posted_filter": posted_filter,
    }
    return render(request, "core/purchase_list.html", context)


@login_required
@owner_required
@subscription_required
@transaction.atomic
def purchase_new(request):
    suppliers = (
        Party.objects.filter(owner=request.owner, party_type="SUPPLIER", is_active=True)
        .order_by("name")
    )

    products = (
        Product.objects.filter(owner=request.owner, is_active=True)
        .order_by("code")
    )

    accounts = (
        Account.objects.filter(owner=request.owner, is_cash_or_bank=True, allow_for_payments=True)
        .order_by("code")
    )
    suggested_invoice_number = peek_next_sequence(request.owner, "purchase_invoice")
    error = None

    if request.method == "POST":
        supplier_id = request.POST.get("supplier")
        invoice_number = (request.POST.get("invoice_number") or "").strip()
        if not invoice_number and not error:
            error = "Invoice number is required."
        invoice_date_str = request.POST.get("invoice_date") or ""
        date_received_str = request.POST.get("date_received") or ""
        notes = (request.POST.get("notes") or "").strip()

        freight_str = request.POST.get("freight_charges") or "0"
        other_str = request.POST.get("other_charges") or "0"

        payment_type = (request.POST.get("payment_type") or "CREDIT").upper()
        if payment_type not in ("CREDIT", "FULL", "PARTIAL"):
            payment_type = "CREDIT"

        payment_account_id = request.POST.get("payment_account") or ""
        payment_amount_str = (request.POST.get("payment_amount") or "0").strip()

        if not supplier_id:
            error = "Supplier is required."

        if invoice_date_str:
            try:
                invoice_date = timezone.datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
            except ValueError:
                invoice_date = timezone.now().date()
        else:
            invoice_date = timezone.now().date()

        if date_received_str:
            try:
                date_received = timezone.datetime.strptime(date_received_str, "%Y-%m-%d").date()
            except ValueError:
                date_received = None
        else:
            date_received = None

        supplier = None
        if not error:
            supplier = tenant_get_object_or_404(
                request,
                Party,
                pk=supplier_id,
                party_type="SUPPLIER",
                is_active=True,
            )

        try:
            freight = Decimal(freight_str)
        except Exception:
            freight = Decimal("0")

        try:
            other = Decimal(other_str)
        except Exception:
            other = Decimal("0")

        line_items = []
        row_indices = set()

        for key in request.POST.keys():
            if key.startswith("product_"):
                idx = key.split("_", 1)[1]
                row_indices.add(idx)

        row_indices = sorted(row_indices, key=lambda x: int(x) if x.isdigit() else x)

        for idx in row_indices:
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id or not qty_str:
                continue

            product = tenant_get_object_or_404(
                request,
                Product,
                pk=product_id,
                is_active=True,
            )

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("0")

            if qty <= 0:
                continue

            try:
                unit_price = Decimal(unit_price_str or product.purchase_price_per_unit)
            except Exception:
                unit_price = product.purchase_price_per_unit

            line_items.append({"product": product, "qty": qty, "unit_price": unit_price})

        if not line_items and not error:
            error = "Please enter at least one product line."

        items_total = Decimal("0")
        for li in line_items:
            items_total += li["qty"] * li["unit_price"]

        invoice_total = items_total + (freight or 0) + (other or 0)

        payment_account_obj = None
        try:
            payment_amount = Decimal(payment_amount_str)
        except Exception:
            payment_amount = Decimal("0")

        if not error:
            if payment_type == "CREDIT":
                payment_amount = Decimal("0")
                payment_account_obj = None
            else:
                if not payment_account_id:
                    error = "Please select the cash/bank account for this payment."
                else:
                    payment_account_obj = tenant_get_object_or_404(
                        request,
                        Account,
                        pk=payment_account_id,
                        is_cash_or_bank=True,
                        allow_for_payments=True,
                    )

                if payment_amount <= 0 and not error:
                    error = "Payment amount must be greater than zero."

                if not error:
                    if invoice_total <= 0:
                        error = "Invoice total must be greater than zero."
                    else:
                        if payment_type == "FULL" and payment_amount != invoice_total:
                            error = f"For full payment, amount must equal invoice total ({invoice_total})."
                        elif payment_type == "PARTIAL" and not (Decimal("0") < payment_amount < invoice_total):
                            error = "For partial payment, amount must be > 0 and < invoice total."

        if not error:
            inv_kwargs = {
                "supplier": supplier,
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "date_received": date_received,
                "freight_charges": freight,
                "other_charges": other,
                "notes": notes,
                "posted": False,
                "payment_type": payment_type,
                "payment_account": payment_account_obj,
                "payment_amount": payment_amount,
            }

            # ✅ Prevent duplicate invoice number per owner
            if invoice_number:
                if PurchaseInvoice.objects.filter(
                    owner=request.owner,
                    invoice_number=invoice_number
                ).exists():
                    error = f"Invoice number '{invoice_number}' already exists. Please use a different invoice number."

            if not error:
                invoice = PurchaseInvoice.objects.create(
                    owner=request.owner,
                    **inv_kwargs,
                )

                for li in line_items:
                    item_kwargs = {
                        "purchase_invoice": invoice,
                        "product": li["product"],
                        "unit_type": li["product"].unit,
                        "quantity_units": li["qty"],
                        "unit_price": li["unit_price"],
                        "discount_amount": Decimal("0"),
                    }

                    PurchaseInvoiceItem.objects.create(
                        owner=request.owner,
                        **item_kwargs,
                    )

                return redirect("purchase_list")

    context = {
        "suppliers": suppliers,
        "products": products,
        "accounts": accounts,
        "error": error,
        "today": timezone.now().date().isoformat(),
        "suggested_invoice_number": str(suggested_invoice_number),
    }
    return render(request, "core/purchase_new.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
@transaction.atomic
def purchase_post(request, pk):    
    """
    Run the .post() logic of a PurchaseInvoice:
    - creates JournalEntry
    - increases stock
    """
    invoice = tenant_get_object_or_404(request, PurchaseInvoice, pk=pk)

    if request.method == "POST":
        if not invoice.posted:
            invoice.post()

    return redirect("purchase_list")


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@subscription_required
def sales_invoice_item_prices_api(request, invoice_id):
    """
    Returns a map of {product_id: unit_price} for a posted SalesInvoice.
    Tenant-safe by owner + tenant_get_object_or_404.
    """
    owner = request.owner

    inv = tenant_get_object_or_404(
        request,
        SalesInvoice,
        pk=invoice_id,
        owner=owner,
        posted=True,
    )

    items = (
        SalesInvoiceItem.objects
        .filter(owner=owner, sales_invoice=inv)
        .select_related("product")
    )

    data = {}
    for it in items:
        if it.product_id:
            data[str(it.product_id)] = str(it.unit_price or "0")

    return JsonResponse({"invoice_id": inv.id, "prices": data})


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@subscription_required
def purchase_invoice_item_prices_api(request, invoice_id):
    """
    Returns a map of {product_id: unit_price} for a posted PurchaseInvoice.
    Tenant-safe by owner + tenant_get_object_or_404.
    """
    owner = request.owner

    inv = tenant_get_object_or_404(
        request,
        PurchaseInvoice,
        pk=invoice_id,
        owner=owner,
        posted=True,
    )

    items = (
        PurchaseInvoiceItem.objects
        .filter(owner=owner, purchase_invoice=inv)
        .select_related("product")
    )

    data = {}
    for it in items:
        if it.product_id:
            data[str(it.product_id)] = str(it.unit_price or "0")

    return JsonResponse({"invoice_id": inv.id, "prices": data})

@login_required
@owner_required
@staff_blocked
def day_summary(request):
    """
    Simple day-book style summary:
    - Sales invoices (posted) for selected date
    - Purchase invoices (posted) for selected date
    - Payments IN for selected date
    - Payments OUT for selected date
    """

    # 1) Which date?
    date_str = request.GET.get("date")
    if date_str:
        try:
            selected_date = date.fromisoformat(date_str)
        except ValueError:
            selected_date = date.today()
    else:
        selected_date = date.today()

    # 2) Query posted invoices only (TENANT SAFE)
    sales_qs = (
        SalesInvoice.objects
        .filter(owner=request.owner, posted=True, invoice_date=selected_date)
        .select_related("customer")
        .prefetch_related(Prefetch("items", queryset=SalesInvoiceItem.objects.select_related("product")))
    )

    purchase_qs = (
        PurchaseInvoice.objects
        .filter(owner=request.owner, posted=True, invoice_date=selected_date)
        .select_related("supplier")
        .prefetch_related(Prefetch("items", queryset=PurchaseInvoiceItem.objects.select_related("product")))
    )

    payments_in_qs = (
        Payment.objects.filter(
            owner=request.owner,
            date=selected_date,
            posted=True,
            is_adjustment=False,
            direction="IN",
        )
        .select_related("party", "account")
    )
    payments_out_qs = (
        Payment.objects.filter(
            owner=request.owner,
            date=selected_date,
            posted=True,
            is_adjustment=False,
            direction="OUT",
        )
        .select_related("party", "account")
    )
    # 4) Totals (all in Python, no weird ORM math)
    total_sales = sum((inv.calculate_total() for inv in sales_qs), Decimal("0"))
    total_purchases = sum((inv.calculate_total() for inv in purchase_qs), Decimal("0"))

    total_payments_in = payments_in_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    total_payments_out = payments_out_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    net_movement = total_payments_in - total_payments_out
        # ✅ Net cash flow for the day
    net_cash_flow = total_payments_in - total_payments_out

    # ✅ Breakdown by account (Money In / Money Out)
    payments_in_by_account = (
        payments_in_qs.values("account__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )

    payments_out_by_account = (
        payments_out_qs.values("account__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")
    )

    context = {
        "selected_date": selected_date,
        "sales_qs": sales_qs,
        "purchase_qs": purchase_qs,
        "payments_in_qs": payments_in_qs,
        "payments_out_qs": payments_out_qs,
        "total_sales": total_sales,
        "total_purchases": total_purchases,
        "total_payments_in": total_payments_in,
        "total_payments_out": total_payments_out,
        "net_movement": net_movement,
        "net_cash_flow": net_cash_flow,
        "payments_in_by_account": payments_in_by_account,
        "payments_out_by_account": payments_out_by_account,
    }
    return render(request, "core/day_summary.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def stock_report(request):
    """
    Stock report (tenant-safe via owner scoping).

    NOTE:
    Product.current_stock is the single source of truth because
    it is updated by:
      - purchases
      - sales
      - returns
      - stock adjustments (StockAdjustment.post())
    """
    products = Product.objects.filter(owner=request.owner).order_by("code")

    rows = []
    for p in products:
        stock = getattr(p, "current_stock", None) or Decimal("0")
        rows.append({
            "product": p,
            "final_stock": stock,
        })

    context = {
        "rows": rows,
        "products": products,
    }
    return render(request, "core/stock_report.html", context)

def party_adjustments_net(party, as_of):
    """
    Net adjustments for a party up to a date.
    Convention:
      - DR => +amount
      - CR => -amount
    Returns a Decimal (positive or negative).
    """
    qs = Payment.objects.filter(
        owner=party.owner,
        party=party,
        posted=True,
        is_adjustment=True,
        date__lte=as_of,
    )

    dr = qs.filter(adjustment_side="DR").aggregate(
        total=Coalesce(Sum("amount"), Decimal("0"))
    )["total"] or Decimal("0")

    cr = qs.filter(adjustment_side="CR").aggregate(
        total=Coalesce(Sum("amount"), Decimal("0"))
    )["total"] or Decimal("0")

    return dr - cr

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def customer_balances(request):
    """
    Per-customer balances as of a given date.

    Formula (per customer):
        opening  (Dr = +, Cr = -)
      + total_sales
      - total_receipts
      + adjustments_net (DR +, CR -)
      = net balance (Dr positive, Cr negative)
    """
    as_of_str = request.GET.get("date")
    if as_of_str:
        try:
            as_of = date.fromisoformat(as_of_str)
        except ValueError:
            as_of = date.today()
    else:
        as_of = date.today()

    owner = request.owner

    customers = (
        Party.objects.filter(owner=request.owner, party_type="CUSTOMER", is_active=True)
        .order_by("name")
    )
    invoices_by_customer = (
        SalesInvoice.objects.filter(
            owner=request.owner,
            posted=True,
            invoice_date__lte=as_of,
            customer__party_type="CUSTOMER",
            customer__is_active=True,
        )
        .select_related("customer")
        .prefetch_related("items")
    )
    receipts_by_customer = (
        Payment.objects.filter(
            owner=request.owner,
            direction="IN",
            posted=True,
            is_adjustment=False,
            date__lte=as_of,
            party__party_type="CUSTOMER",
            party__is_active=True,
        )
        .values("party_id")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
    )
    receipts_map = {r["party_id"]: (r["total"] or Decimal("0")) for r in receipts_by_customer}

    invoices_map = {}
    for inv in invoices_by_customer:
        cid = inv.customer_id
        invoices_map.setdefault(cid, []).append(inv)

    rows = []
    grand_opening = Decimal("0")
    grand_sales = Decimal("0")
    grand_receipts = Decimal("0")
    grand_balance = Decimal("0")

    for cust in customers:
        opening = cust.opening_balance or Decimal("0")
        if not cust.opening_balance_is_debit:
            opening = -opening

        total_sales = Decimal("0")
        for inv in invoices_map.get(cust.id, []):
            total_sales += inv.calculate_total()

        receipts_total = receipts_map.get(cust.id, Decimal("0"))

        adj_net = party_adjustments_net(cust, as_of)

        net = opening + total_sales - receipts_total + adj_net
        balance_type = "Dr" if net >= 0 else "Cr"
        balance_abs = abs(net)

        rows.append({
            "party": cust,
            "opening": opening,
            "sales": total_sales,
            "receipts": receipts_total,
            "adjustments": adj_net,
            "net": net,
            "net_abs": balance_abs,
            "net_type": balance_type,
        })

        grand_opening += opening
        grand_sales += total_sales
        grand_receipts += receipts_total
        grand_balance += net

    context = {
        "as_of": as_of,
        "rows": rows,
        "grand_opening": grand_opening,
        "grand_sales": grand_sales,
        "grand_receipts": grand_receipts,
        "grand_balance": grand_balance,
        "grand_balance_abs": abs(grand_balance),
        "grand_balance_type": "Dr" if grand_balance >= 0 else "Cr",
    }
    return render(request, "core/customer_balances.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def supplier_balances(request):
    """
    Per-supplier balances as of a given date.

    Consistent with dashboard logic:

        opening  (Dr = +, Cr = -)
      + total_purchases
      - payments_out (excluding adjustments)
      - total_purchase_returns
      + adjustments_net (CR increases payable, DR decreases payable)
      = net (Dr positive, Cr negative)

    Where adjustments_net = supplier_adj_cr - supplier_adj_dr
    """
    as_of_str = request.GET.get("date")
    if as_of_str:
        try:
            as_of = date.fromisoformat(as_of_str)
        except ValueError:
            as_of = date.today()
    else:
        as_of = date.today()

    owner = request.owner

    suppliers = (
        Party.objects.filter(owner=request.owner, party_type="SUPPLIER", is_active=True)
        .order_by("name")
    )

    purchases_qs = (
        PurchaseInvoice.objects.filter(
            owner=request.owner,
            posted=True,
            invoice_date__lte=as_of,
            supplier__party_type="SUPPLIER",
        )
        .select_related("supplier")
        .prefetch_related("items")
    )

    returns_qs = (
        PurchaseReturn.objects.filter(
            owner=request.owner,
            posted=True,
            return_date__lte=as_of,
            supplier__party_type="SUPPLIER",
        )
        .select_related("supplier")
        .prefetch_related("items")
    )
    payments_out_qs = (
        Payment.objects.filter(
            owner=request.owner,
            direction="OUT",
            posted=True,
            date__lte=as_of,
            is_adjustment=False,
            party__party_type="SUPPLIER",
        )
        .values("party_id")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
    )
    payments_out_map = {
        r["party_id"]: (r["total"] or Decimal("0"))
        for r in payments_out_qs
    }

    adj_cr_qs = (
        Payment.objects.filter(
            owner=request.owner,
            posted=True,
            is_adjustment=True,
            adjustment_side="CR",
            date__lte=as_of,
            party__party_type="SUPPLIER",
        )
        .values("party_id")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
    )
    adj_cr_map = {r["party_id"]: (r["total"] or Decimal("0")) for r in adj_cr_qs}

    adj_dr_qs = (
        Payment.objects.filter(
            owner=request.owner,
            posted=True,
            is_adjustment=True,
            adjustment_side="DR",
            date__lte=as_of,
            party__party_type="SUPPLIER",
        )
        .values("party_id")
        .annotate(total=Coalesce(Sum("amount"), Decimal("0")))
    )
    adj_dr_map = {r["party_id"]: (r["total"] or Decimal("0")) for r in adj_dr_qs}

    purchases_map = {}
    for inv in purchases_qs:
        sid = inv.supplier_id
        purchases_map.setdefault(sid, []).append(inv)

    returns_map = {}
    for ret in returns_qs:
        sid = ret.supplier_id
        returns_map.setdefault(sid, []).append(ret)

    rows = []
    grand_opening = Decimal("0")
    grand_purchases = Decimal("0")
    grand_returns = Decimal("0")
    grand_payments = Decimal("0")
    grand_adjustments = Decimal("0")
    grand_balance = Decimal("0")

    for supp in suppliers:
        opening = supp.opening_balance or Decimal("0")
        if not supp.opening_balance_is_debit:
            opening = -opening

        total_purchases = Decimal("0")
        for inv in purchases_map.get(supp.id, []):
            total_purchases += inv.calculate_total()

        total_returns = Decimal("0")
        for ret in returns_map.get(supp.id, []):
            total_returns += ret.calculate_total()

        payments_out_total = payments_out_map.get(supp.id, Decimal("0"))

        supplier_adj_cr = adj_cr_map.get(supp.id, Decimal("0"))
        supplier_adj_dr = adj_dr_map.get(supp.id, Decimal("0"))
        adj_net = supplier_adj_cr - supplier_adj_dr

        net = opening + total_purchases - payments_out_total - total_returns + adj_net

        balance_type = "Dr" if net >= 0 else "Cr"
        balance_abs = abs(net)

        rows.append({
            "party": supp,
            "opening": opening,
            "purchases": total_purchases,
            "returns": total_returns,
            "payments": payments_out_total,
            "adjustments": adj_net,
            "net": net,
            "net_abs": balance_abs,
            "net_type": balance_type,
        })

        grand_opening += opening
        grand_purchases += total_purchases
        grand_returns += total_returns
        grand_payments += payments_out_total
        grand_adjustments += adj_net
        grand_balance += net

    context = {
        "as_of": as_of,
        "rows": rows,
        "grand_opening": grand_opening,
        "grand_purchases": grand_purchases,
        "grand_returns": grand_returns,
        "grand_payments": grand_payments,
        "grand_adjustments": grand_adjustments,
        "grand_balance": grand_balance,
        "grand_balance_abs": abs(grand_balance),
        "grand_balance_type": "Dr" if grand_balance >= 0 else "Cr",
    }
    return render(request, "core/supplier_balances.html", context)

def build_party_ledger(party, date_from=None, date_to=None):
    """
    Ledger rows for a single Party (customer/supplier), OWNER-SAFE.

    Rules:
    - Sales/Purchases are posted-only.
    - Normal payments are posted-only AND is_adjustment=False.
    - Adjustments (is_adjustment=True) are included as separate rows:
        * CUSTOMER: DR increases receivable (debit), CR decreases (credit)
        * SUPPLIER: CR increases payable (credit), DR decreases payable (debit)
    """
    owner = party.owner  # hard lock to tenant owner

    opening_balance = party.opening_balance or Decimal("0")
    balance = opening_balance if party.opening_balance_is_debit else -opening_balance

    rows = []

    # ------------------------
    # A) INVOICES
    # ------------------------
    if party.party_type == "CUSTOMER":
        sales_qs = (
            SalesInvoice.objects.filter(
                owner=owner,
                customer=party,
                posted=True,
            )
            .order_by("invoice_date", "id")
        )
        if date_from:
            sales_qs = sales_qs.filter(invoice_date__gte=date_from)
        if date_to:
            sales_qs = sales_qs.filter(invoice_date__lte=date_to)

        for inv in sales_qs:
            amount = inv.calculate_total()
            rows.append({
                "date": inv.invoice_date,
                "description": f"Sales Invoice #{inv.id}",
                "debit": amount,
                "credit": Decimal("0"),
            })

    elif party.party_type == "SUPPLIER":
        purchases_qs = (
            PurchaseInvoice.objects.filter(
                owner=owner,
                supplier=party,
                posted=True,
            )
            .order_by("invoice_date", "id")
        )
        if date_from:
            purchases_qs = purchases_qs.filter(invoice_date__gte=date_from)
        if date_to:
            purchases_qs = purchases_qs.filter(invoice_date__lte=date_to)

        for inv in purchases_qs:
            amount = inv.calculate_total()
            rows.append({
                "date": inv.invoice_date,
                "description": f"Purchase Invoice #{inv.id}",
                "debit": Decimal("0"),
                "credit": amount,
            })

    # ------------------------
    # B) NORMAL PAYMENTS (exclude adjustments)
    # ------------------------
    payments_qs = (
        Payment.objects.filter(
            owner=owner,
            party=party,
            posted=True,
            is_adjustment=False,
        )
        .order_by("date", "id")
    )
    if date_from:
        payments_qs = payments_qs.filter(date__gte=date_from)
    if date_to:
        payments_qs = payments_qs.filter(date__lte=date_to)

    for p in payments_qs:
        if party.party_type == "CUSTOMER":
            if p.direction == "IN":
                debit = Decimal("0")
                credit = p.amount
            else:  # OUT (rare)
                debit = p.amount
                credit = Decimal("0")
        else:  # SUPPLIER
            if p.direction == "OUT":
                debit = p.amount
                credit = Decimal("0")
            else:  # IN (rare)
                debit = Decimal("0")
                credit = p.amount

        rows.append({
            "date": p.date,
            "description": p.description or f"Payment ({p.get_direction_display()})",
            "debit": debit,
            "credit": credit,
        })

    # ------------------------
    # C) ADJUSTMENTS (posted-only)
    # ------------------------
    adj_qs = (
        Payment.objects.filter(
            owner=owner,
            party=party,
            posted=True,
            is_adjustment=True,
        )
        .order_by("date", "id")
    )
    if date_from:
        adj_qs = adj_qs.filter(date__gte=date_from)
    if date_to:
        adj_qs = adj_qs.filter(date__lte=date_to)

    for a in adj_qs:
        side = (a.adjustment_side or "DR").upper()

        if party.party_type == "CUSTOMER":
            if side == "DR":
                debit, credit = a.amount, Decimal("0")
            else:
                debit, credit = Decimal("0"), a.amount
        else:  # SUPPLIER
            if side == "CR":
                debit, credit = Decimal("0"), a.amount
            else:
                debit, credit = a.amount, Decimal("0")

        rows.append({
            "date": a.date,
            "description": a.description or f"Adjustment ({side})",
            "debit": debit,
            "credit": credit,
        })

    # ------------------------
    # D) SORT + RUNNING BALANCE
    # ------------------------
    rows.sort(key=lambda r: r["date"] or timezone.now().date())

    for r in rows:
        balance += (r["debit"] or Decimal("0")) - (r["credit"] or Decimal("0"))
        r["balance"] = balance

    closing_balance = balance
    return rows, opening_balance, closing_balance


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def party_statement(request, pk):
    party = tenant_get_object_or_404(request, Party, pk=pk)

    from_str = request.GET.get("from")
    to_str = request.GET.get("to")
    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    rows, opening_balance, closing_balance = build_party_ledger(
        party, date_from=date_from, date_to=date_to
    )

    context = {
        "party": party,
        "rows": rows,
        "opening_balance": opening_balance,
        "opening_is_debit": party.opening_balance_is_debit,
        "closing_balance": closing_balance,
        "date_from": date_from,
        "date_to": date_to,
        "from_str": from_str,
        "to_str": to_str,
    }
    return render(request, "core/party_statement.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def customer_ledger(request):
    """
    Customer Ledger screen:
    - Dropdown to pick a customer
    - Optional date range
    - Shows running balance from opening balance + sales + payments
    """
    customers = (
        Party.objects.filter(owner=request.owner, party_type="CUSTOMER", is_active=True)
        .order_by("name")
    )

    customer_id = request.GET.get("customer") or ""
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    selected_customer = None
    rows = []
    opening_balance = Decimal("0")
    closing_balance = Decimal("0")
    closing_is_debit = True

    if customer_id:
        selected_customer = tenant_get_object_or_404(
            request,
            Party,
            pk=customer_id,
            party_type="CUSTOMER",
            is_active=True,
        )
        rows, opening_balance, closing_balance = build_party_ledger(
            selected_customer,
            date_from=date_from,
            date_to=date_to,
        )
        closing_is_debit = (closing_balance >= 0)

    context = {
        "customers": customers,
        "selected_customer": selected_customer,
        "rows": rows,
        "opening_balance": opening_balance,
        "opening_is_debit": (
            selected_customer.opening_balance_is_debit
            if selected_customer else True
        ),
        "closing_balance": closing_balance,
        "closing_is_debit": closing_is_debit,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "core/customer_ledger.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def supplier_ledger(request):
    """
    Supplier Ledger screen:
    - Dropdown to pick a supplier
    - Optional date range
    - Shows running balance from opening balance + purchases + payments
    """
    owner = request.owner

    suppliers = (
        Party.objects.filter(owner=request.owner, party_type="SUPPLIER", is_active=True)
        .order_by("name")
    )
    supplier_id = request.GET.get("supplier") or ""
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    selected_supplier = None
    rows = []
    opening_balance = Decimal("0")
    closing_balance = Decimal("0")
    closing_is_debit = True

    if supplier_id:
        selected_supplier = tenant_get_object_or_404(
            request,
            Party,
            pk=supplier_id,
            party_type="SUPPLIER",
            is_active=True,
        )
        rows, opening_balance, closing_balance = build_party_ledger(
            selected_supplier,
            date_from=date_from,
            date_to=date_to,
        )
        closing_is_debit = (closing_balance >= 0)

    context = {
        "suppliers": suppliers,
        "selected_supplier": selected_supplier,
        "rows": rows,
        "opening_balance": opening_balance,
        "opening_is_debit": (
            selected_supplier.opening_balance_is_debit
            if selected_supplier else True
        ),
        "closing_balance": closing_balance,
        "closing_is_debit": closing_is_debit,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "core/supplier_ledger.html", context)



@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def account_ledger(request):
    """
    Simple account ledger for a single account, with optional date range.
    Uses JournalEntry as the single source of truth.

    Enhancement:
    - Checkbox toggle to show/hide system accounts in the account dropdown.
    - Counter account is computed for display.
    """

    SYSTEM_CODES = [
        "1000", "1010", "1020", "1200", "1300", "2100", "3000", "5100",
        # ✅ add ALL your system expense/category codes here too (example)
        "5200", "5210", "5220", "5230", "5240", "5250", "5290",
    ]

    show_system = request.GET.get("show_system") == "1"

    base_qs = Account.objects.filter(owner=request.owner)

    if show_system:
        # show everything
        accounts = base_qs.order_by("code")
    else:
        # ✅ default: show owner-defined accounts + cash/bank
        accounts = (
            base_qs
            .filter(
                Q(is_cash_or_bank=True) |
                ~Q(code__in=SYSTEM_CODES)
            )
            .order_by("code")
        )

    account_id = request.GET.get("account")
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    selected_account = None
    rows = []
    opening_balance = Decimal("0")
    closing_balance = Decimal("0")

    if account_id:
        selected_account = tenant_get_object_or_404(request, Account, pk=account_id)

        base_qs = (
            JournalEntry.objects
            .filter(owner=request.owner)
            .filter(Q(debit_account=selected_account) | Q(credit_account=selected_account))
            .select_related("debit_account", "credit_account")
            .order_by("date", "id")
        )

        # Opening balance = sum before date_from
        if date_from:
            before_qs = base_qs.filter(date__lt=date_from)
            bal = Decimal("0")
            for je in before_qs:
                if je.debit_account_id == selected_account.id:
                    bal += je.amount
                if je.credit_account_id == selected_account.id:
                    bal -= je.amount
            opening_balance = bal

        if date_from:
            base_qs = base_qs.filter(date__gte=date_from)
        if date_to:
            base_qs = base_qs.filter(date__lte=date_to)

        balance = opening_balance
        for je in base_qs:
            is_debit = (je.debit_account_id == selected_account.id)
            debit = je.amount if is_debit else Decimal("0")
            credit = je.amount if not is_debit else Decimal("0")
            balance += debit - credit

            # ✅ Counter account for UI column
            counter_acc = je.credit_account if is_debit else je.debit_account
            counter_label = f"{counter_acc.code} - {counter_acc.name}" if counter_acc else "—"

            rows.append({
                "date": je.date,
                "type": je.related_model or "Journal",
                "counter": counter_label,
                "description": je.description,
                "debit": debit,
                "credit": credit,
                "balance": balance,
            })

        closing_balance = balance

    context = {
        "accounts": accounts,
        "selected_account": selected_account,
        "rows": rows,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "date_from": date_from,
        "date_to": date_to,
        "show_system": show_system,  # ✅ pass to template
    }
    return render(request, "core/account_ledger.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def trial_balance(request):
    """
    Trial balance based on JournalEntry.
    Shows net Dr/Cr per account for an optional date range.
    """
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    qs = (
        JournalEntry.objects.filter(owner=request.owner)
        .select_related("debit_account", "credit_account")
    )
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    data = defaultdict(lambda: {
        "account": None,
        "debit_total": Decimal("0"),
        "credit_total": Decimal("0"),
    })

    for je in qs:
        d = data[je.debit_account_id]
        d["account"] = je.debit_account
        d["debit_total"] += je.amount

        c = data[je.credit_account_id]
        c["account"] = je.credit_account
        c["credit_total"] += je.amount

    rows = []
    total_debits = Decimal("0")
    total_credits = Decimal("0")

    for account_id, item in data.items():
        net = item["debit_total"] - item["credit_total"]
        if net > 0:
            debit = net
            credit = Decimal("0")
        elif net < 0:
            debit = Decimal("0")
            credit = -net
        else:
            continue

        total_debits += debit
        total_credits += credit

        rows.append({
            "account": item["account"],
            "debit": debit,
            "credit": credit,
        })

    rows.sort(key=lambda r: r["account"].code)

    context = {
        "rows": rows,
        "total_debits": total_debits,
        "total_credits": total_credits,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "core/trial_balance.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def profit_loss(request):
    """
    Simple Profit & Loss for a date range, using JournalEntry only.

    - Income accounts: net = credits - debits
    - Expense accounts: net = debits - credits
    """
    date_from, date_to = _get_date_range(request)

    entries = JournalEntry.objects.filter(
        owner=request.owner,
        date__range=(date_from, date_to),
    )
    income_rows = []
    expense_rows = []
    total_income = Decimal("0")
    total_expense = Decimal("0")

    debit_totals = {
        row["debit_account"]: (row["total"] or Decimal("0"))
        for row in (
            entries.values("debit_account")
            .annotate(total=Sum("amount"))
        )
    }
    credit_totals = {
        row["credit_account"]: (row["total"] or Decimal("0"))
        for row in (
            entries.values("credit_account")
            .annotate(total=Sum("amount"))
        )
    }

    for account in (
        Account.objects.filter(
            owner=request.owner,
            account_type__in=["INCOME", "EXPENSE"],
        ).order_by("code")
    ):
        debit_sum = debit_totals.get(account.id, Decimal("0"))
        credit_sum = credit_totals.get(account.id, Decimal("0"))

        if account.account_type == "INCOME":
            net = credit_sum - debit_sum
            if net != 0:
                income_rows.append({"account": account, "amount": net})
                total_income += net
        else:
            net = debit_sum - credit_sum
            if net != 0:
                expense_rows.append({"account": account, "amount": net})
                total_expense += net

    net_profit = total_income - total_expense

    context = {
        "date_from": date_from,
        "date_to": date_to,
        "income_rows": income_rows,
        "expense_rows": expense_rows,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": net_profit,
    }
    return render(request, "core/profit_loss.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def balance_sheet(request):
    """
    Simple Balance Sheet as of a date, using JournalEntry only.

    - Assets:  debit - credit
    - Liabilities/Equity: credit - debit
    """
    _, date_to = _get_date_range(request)

    entries = (
        JournalEntry.objects.filter(
            owner=request.owner,
            date__lte=date_to,
        )
    )
    assets = []
    liabilities = []
    equity = []

    total_assets = Decimal("0")
    total_liabilities = Decimal("0")
    total_equity = Decimal("0")

    for account in (
        Account.objects.filter(
            owner=request.owner,
            account_type__in=["ASSET", "LIABILITY", "EQUITY"],
        )
        .order_by("code")
    ):
        debit_sum = (
            entries.filter(debit_account=account)
            .aggregate(total=Sum("amount"))["total"] or Decimal("0")
        )
        credit_sum = (
            entries.filter(credit_account=account)
            .aggregate(total=Sum("amount"))["total"] or Decimal("0")
        )

        if account.account_type == "ASSET":
            balance = debit_sum - credit_sum
            if balance != 0:
                assets.append({"account": account, "amount": balance})
                total_assets += balance
        elif account.account_type == "LIABILITY":
            balance = credit_sum - debit_sum
            if balance != 0:
                liabilities.append({"account": account, "amount": balance})
                total_liabilities += balance
        else:
            balance = credit_sum - debit_sum
            if balance != 0:
                equity.append({"account": account, "amount": balance})
                total_equity += balance

    context = {
        "as_of": date_to,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
    }
    return render(request, "core/balance_sheet.html", context)


# ---------- HELPER: Opening balance for cash/bank accounts ----------

def create_opening_entry_for_cash_account(request, account, amount: Decimal, side: str):
    """
    Create a single JournalEntry for the opening balance of a cash/bank account.

    Owner-scoped (NOT tenant-scoped) because Account is owner-based in your DB.

    side = "DR"  -> normal positive cash/bank balance
    side = "CR"  -> overdraft / credit balance
    """
    if amount is None:
        return

    try:
        amt = Decimal(amount)
    except Exception:
        return

    if amt <= 0:
        return

    side = (side or "DR").upper()
    if side not in ("DR", "CR"):
        side = "DR"

    owner = account.owner

    opening_acct = (
        Account.objects.filter(owner=owner, code="3000")
        .order_by("id")
        .first()
    )
    if not opening_acct:
        kwargs = {
            "owner": owner,
            "code": "3000",
            "name": "Opening Balances",
            "account_type": "EQUITY",
            "is_cash_or_bank": False,
            "allow_for_payments": False,
        }
        opening_acct = Account.objects.create(**kwargs)

    has_any_entries = (
        JournalEntry.objects.filter(
            owner=owner
        )
        .filter(Q(debit_account=account) | Q(credit_account=account))
        .exists()
    )
    if has_any_entries:
        return

    if side == "CR":
        debit_account = opening_acct
        credit_account = account
    else:
        debit_account = account
        credit_account = opening_acct

    je_kwargs = {
        "owner": owner,
        "date": timezone.now().date(),
        "description": f"Opening balance for {account.code} - {account.name}",
        "debit_account": debit_account,
        "credit_account": credit_account,
        "amount": amt,
        "related_model": "AccountOpening",
        "related_id": account.id,
    }
    JournalEntry.objects.create(**je_kwargs)


def create_opening_entry_for_party(request, party, amount, side):
    try:
        amt = Decimal(amount)
    except Exception:
        return

    if amt <= 0:
        return

    side = (side or "DR").upper()
    if side not in ("DR", "CR"):
        side = "DR"

    owner = party.owner

    if JournalEntry.objects.filter(
        owner=owner,
        related_model="PartyOpening",
        related_id=party.id,
    ).exists():
        return

    opening_acct = get_owner_account(
        owner=owner,
        code="3000",
        defaults={
            "name": "Opening Balances",
            "account_type": "EQUITY",
            "is_cash_or_bank": False,
            "allow_for_payments": False,
        },
    )

    if party.party_type == "CUSTOMER":
        control_acct = get_owner_account(
            owner=owner,
            code="1300",
            defaults={
                "name": "Customer Control",
                "account_type": "ASSET",
                "is_cash_or_bank": False,
                "allow_for_payments": False,
            },
        )
    else:
        control_acct = get_owner_account(
            owner=owner,
            code="2100",
            defaults={
                "name": "Supplier Control",
                "account_type": "LIABILITY",
                "is_cash_or_bank": False,
                "allow_for_payments": False,
            },
        )

    if side == "CR":
        debit_account = opening_acct
        credit_account = control_acct
    else:
        debit_account = control_acct
        credit_account = opening_acct

    JournalEntry.objects.create(
        owner=owner,
        date=timezone.now().date(),
        description=f"Opening balance for {party.get_party_type_display()} - {party.name}",
        debit_account=debit_account,
        credit_account=credit_account,
        amount=amt,
        related_model="PartyOpening",
        related_id=party.id,
    )


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def user_accounts(request):
    owner = request.owner
    error = None
    message = None

    if request.method == "POST":
        account_id = request.POST.get("account_id")
        code = (request.POST.get("code") or "").strip()
        name = (request.POST.get("name") or "").strip()

        opening_amount_str = (request.POST.get("opening_amount") or "").strip()
        opening_side = (request.POST.get("opening_side") or "DR").upper()

        try:
            opening_amount = Decimal(opening_amount_str) if opening_amount_str else Decimal("0")
        except Exception:
            opening_amount = Decimal("0")

        if opening_amount < 0:
            opening_amount = -opening_amount

        if opening_side not in ("DR", "CR"):
            opening_side = "DR"

        if not code or not name:
            error = "Code and Name are required."
        else:
            if account_id:
                acc = tenant_get_object_or_404(
                    request,
                    Account,
                    pk=account_id,
                    is_cash_or_bank=True,
                )
                acc.code = code
                acc.name = name
                acc.account_type = "ASSET"
                acc.is_cash_or_bank = True
                acc.allow_for_payments = True
                acc.save(
                    update_fields=[
                        "code",
                        "name",
                        "account_type",
                        "is_cash_or_bank",
                        "allow_for_payments",
                    ]
                )
                message = "Account updated successfully."
            else:
                qs = (
                    Account.objects.filter(
                        owner=request.owner,
                        code=code,
                    )
                    .order_by("id")
                )
                acc = qs.first()

                if acc:
                    created = False
                else:
                    kwargs = {
                        "owner": owner,
                        "code": code,
                        "name": name,
                        "account_type": "ASSET",
                        "is_cash_or_bank": True,
                        "allow_for_payments": True,
                    }
                    acc = Account.objects.create(**kwargs)
                    created = True

                if not created and name and acc.name != name:
                    acc.name = name
                    acc.save(update_fields=["name"])

                message = "Account created successfully."

                if opening_amount > 0:
                    side = "CR" if opening_side == "CR" else "DR"
                    create_opening_entry_for_cash_account(
                        request,
                        acc,
                        opening_amount,
                        side,
                    )

        if not error:
            return redirect("user_accounts")

    accounts = (
        Account.objects.filter(
            owner=request.owner,
            is_cash_or_bank=True,
            allow_for_payments=True,
        )
        .order_by("code")
    )

    context = {
        "accounts": accounts,
        "error": error,
        "message": message,
    }

    return render(request, "core/user_accounts.html", context)


# core/views.py
from django.contrib import messages
from django.db.models.deletion import ProtectedError

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def user_account_delete(request, pk):
    acc = tenant_get_object_or_404(
        request,
        Account,
        pk=pk,
        is_cash_or_bank=True,
        allow_for_payments=True,
    )

    if request.method == "POST":
        try:
            acc.delete()
            messages.success(request, f"Account '{acc.code} - {acc.name}' deleted.")
        except ProtectedError:
            messages.error(
                request,
                f"Cannot delete '{acc.code} - {acc.name}' because it is used in transactions. "
                f"Please remove/move related payments/sales/journal entries first."
            )

    return redirect("user_accounts")

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def backup_dashboard(request):
    owner = request.owner
    company = getattr(owner, "company_profile", None)

    backups = []
    if company:
        folder = _tenant_backup_folder(owner, company)
        backups = _list_last_backups(folder, limit=3)

    return render(request, "core/backup.html", {"backups": backups})


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def create_backup(request):
    if request.method != "POST":
        return redirect("backup_dashboard")

    from django.core import serializers

    owner = request.owner
    company = getattr(owner, "company_profile", None)
    if not company:
        messages.error(request, "Company not found for backup.")
        return redirect("backup_dashboard")

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
        # ✅ NEW
        DailyExpense,
        CashBankTransfer,
    ]

    all_objects = []
    all_objects.extend(CompanyProfile.objects.filter(owner=owner))

    for m in models_to_dump:
        if m is CompanyProfile:
            continue
        if hasattr(m, "owner_id"):
            all_objects.extend(list(m.objects.filter(owner=owner)))

    data = serializers.serialize("json", all_objects)

    folder = _tenant_backup_folder(owner, company)
    folder.mkdir(parents=True, exist_ok=True)

    filename = f"backup_{company.slug or owner.username}_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path = folder / filename
    out_path.write_text(data, encoding="utf-8")

    _keep_last_n(folder, n=3)

    messages.success(request, "Backup created and saved (last 3 retained).")
    return redirect("backup_dashboard")


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@transaction.atomic
@subscription_required
def restore_backup(request):
    if request.method != "POST":
        return redirect("backup_dashboard")

    uploaded = request.FILES.get("backup_file")
    if not uploaded:
        messages.error(request, "Please choose a backup file first.")
        return redirect("backup_dashboard")

    from django.core import serializers

    owner = request.owner
    company = getattr(owner, "company_profile", None)
    if not company:
        messages.error(request, "Company not found.")
        return redirect("backup_dashboard")

    raw = uploaded.read().decode("utf-8", errors="ignore")

    try:
        objs = list(serializers.deserialize("json", raw))
    except Exception:
        messages.error(request, "Invalid backup file format.")
        return redirect("backup_dashboard")

    try:
        # ✅ Only allow known models in restore file
        ALLOWED_MODELS = {
            CompanyProfile,
            Account, Party, Product,
            SalesInvoice, SalesInvoiceItem,
            PurchaseInvoice, PurchaseInvoiceItem,
            SalesReturn, SalesReturnItem,
            PurchaseReturn, PurchaseReturnItem,
            Payment, JournalEntry, StockAdjustment,
            DailyExpense,
            CashBankTransfer,
        }

        for obj in objs:
            instance = obj.object
            if type(instance) not in ALLOWED_MODELS:
                raise ValueError(f"Unsupported data in backup: {type(instance).__name__}")

        # ✅ make restore deterministic (no duplicates)
        _wipe_tenant_data(owner)

        for obj in objs:
            instance = obj.object

            # force tenant ownership on owner-scoped models
            if hasattr(instance, "owner_id"):
                instance.owner = owner

            # company profile: update current (never create new)
            if isinstance(instance, CompanyProfile):
                existing = CompanyProfile.objects.filter(owner=owner).first()
                if existing:
                    instance.pk = existing.pk
                instance.owner = owner

            obj.save()

    except Exception as e:
        messages.error(request, f"Restore failed: {e}")
        return redirect("backup_dashboard")

    messages.success(request, "Backup restored successfully.")
    return redirect("backup_dashboard")


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def download_backup(request, filename):
    owner = request.owner
    company = getattr(owner, "company_profile", None)
    if not company:
        raise PermissionDenied("Company not found")

    folder = _tenant_backup_folder(owner, company)
    folder.mkdir(parents=True, exist_ok=True)

    # ✅ prevent path traversal
    safe_folder = folder.resolve()
    file_path = (folder / filename).resolve()

    if safe_folder not in file_path.parents:
        raise PermissionDenied("Invalid backup path")

    if not file_path.exists() or not file_path.is_file() or not file_path.name.endswith(".json"):
        raise PermissionDenied("Backup file not found")

    fh = open(file_path, "rb")
    return FileResponse(fh, as_attachment=True, filename=file_path.name)
# ---------- RETURNS: SALES RETURN ----------

def run_backup_job(request):
    token = (
        request.headers.get("X-CRON-TOKEN")
        or request.GET.get("token")
        or request.GET.get("key")   # ✅ accept Render's key param
    )

    if not token or token != getattr(settings, "CRON_TOKEN", ""):
        return HttpResponseForbidden("Forbidden")

    call_command("backupdata", keep=3)
    return JsonResponse({"ok": True})

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@transaction.atomic
@subscription_required
def sales_return_new(request):
    """
    Create a new Sales Return.

    - Increases inventory
    - Reduces customer's receivable (via SalesReturn.post())
    - Does NOT move cash
    """

    owner = request.owner

    customers = (
        Party.objects.filter(
            owner=owner,
            party_type="CUSTOMER",
            is_active=True,
        )
        .order_by("name")
    )
    products = (
        Product.objects.filter(
            owner=owner,
            is_active=True,
        )
        .order_by("code")
    )
    invoices = (
        SalesInvoice.objects.filter(
            owner=owner,
            posted=True,
        )
        .select_related("customer")
        .order_by("-invoice_date", "-id")
    )

    today_str = timezone.now().date().isoformat()
    error = None

    if request.method == "POST":
        customer_id = request.POST.get("party") or ""
        original_invoice_id = request.POST.get("original_invoice") or ""
        return_date_str = request.POST.get("return_date") or ""
        notes = (request.POST.get("notes") or "").strip()

        if not customer_id:
            error = "Customer is required."

        try:
            return_date = date.fromisoformat(return_date_str) if return_date_str else timezone.now().date()
        except ValueError:
            return_date = timezone.now().date()

        customer = None
        original_invoice = None

        if not error:
            customer = tenant_get_object_or_404(
                request,
                Party,
                pk=customer_id,
                owner=owner,
                party_type="CUSTOMER",
                is_active=True,
            )

            if original_invoice_id:
                original_invoice = tenant_get_object_or_404(
                    request,
                    SalesInvoice,
                    pk=original_invoice_id,
                    owner=owner,
                    customer=customer,
                    posted=True,
                )

        line_items = []
        row_indices = {
            key.split("_", 1)[1]
            for key in request.POST.keys()
            if key.startswith("product_")
        }

        for idx in sorted(row_indices, key=lambda x: int(x) if x.isdigit() else x):
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id or not qty_str:
                continue

            product = tenant_get_object_or_404(
                request,
                Product,
                pk=product_id,
                owner=owner,
                is_active=True,
            )

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("0")

            if qty <= 0:
                continue

            try:
                unit_price = Decimal(unit_price_str or product.sale_price_per_unit)
            except Exception:
                unit_price = product.sale_price_per_unit

            line_items.append(
                {"product": product, "qty": qty, "unit_price": unit_price}
            )

        if not line_items and not error:
            error = "Please enter at least one product line."

        if not error:
            ret_kwargs = {
                "owner": owner,
                "customer": customer,
                "reference_invoice": original_invoice,
                "return_date": return_date,
                "notes": notes,
                "posted": False,
            }

            ret = SalesReturn.objects.create(**ret_kwargs)

            for li in line_items:
                item_kwargs = {
                    "owner": owner,
                    "sales_return": ret,
                    "product": li["product"],
                    "unit_type": li["product"].unit,
                    "quantity_units": li["qty"],
                    "unit_price": li["unit_price"],
                    "discount_amount": Decimal("0"),
                }

                SalesReturnItem.objects.create(**item_kwargs)

            ret.post()
            messages.success(request, "Sales return saved and posted.")
            return redirect("sales_list")

    context = {
        "mode": "sales",
        "party_label": "Customer",
        "parties": customers,
        "products": products,
        "invoices": invoices,
        "today": today_str,
        "error": error,
        "back_url_name": "sales_list",
    }
    return render(request, "core/returns_new.html", context)

# ---------- RETURNS: PURCHASE RETURN ----------

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@transaction.atomic
@subscription_required
def purchase_return_new(request):
    """
    Create a new Purchase Return.

    - Decreases inventory
    - Reduces amount payable to supplier (via PurchaseReturn.post())
    - Does NOT move cash
    """

    owner = request.owner

    suppliers = (
        Party.objects.filter(
            owner=owner,
            party_type="SUPPLIER",
            is_active=True,
        )
        .order_by("name")
    )
    products = (
        Product.objects.filter(
            owner=owner,
            is_active=True,
        )
        .order_by("code")
    )
    invoices = (
        PurchaseInvoice.objects.filter(
            owner=owner,
            posted=True,
        )
        .select_related("supplier")
        .order_by("-invoice_date", "-id")
    )

    today_str = timezone.now().date().isoformat()
    error = None

    if request.method == "POST":
        supplier_id = request.POST.get("party") or ""
        original_invoice_id = request.POST.get("original_invoice") or ""
        return_date_str = request.POST.get("return_date") or ""
        notes = (request.POST.get("notes") or "").strip()

        if not supplier_id:
            error = "Supplier is required."

        try:
            return_date = date.fromisoformat(return_date_str) if return_date_str else timezone.now().date()
        except ValueError:
            return_date = timezone.now().date()

        supplier = None
        original_invoice = None

        if not error:
            supplier = tenant_get_object_or_404(
                request,
                Party,
                pk=supplier_id,
                owner=owner,
                party_type="SUPPLIER",
                is_active=True,
            )
            if original_invoice_id:
                original_invoice = tenant_get_object_or_404(
                    request,
                    PurchaseInvoice,
                    pk=original_invoice_id,
                    owner=owner,
                    supplier=supplier,
                    posted=True,
                )

        line_items = []
        row_indices = {
            key.split("_", 1)[1]
            for key in request.POST.keys()
            if key.startswith("product_")
        }

        for idx in sorted(row_indices, key=lambda x: int(x) if x.isdigit() else x):
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id or not qty_str:
                continue

            product = tenant_get_object_or_404(
                request,
                Product,
                pk=product_id,
                owner=owner,
                is_active=True,
            )

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("0")

            if qty <= 0:
                continue

            try:
                unit_price = Decimal(unit_price_str or product.purchase_price_per_unit)
            except Exception:
                unit_price = product.purchase_price_per_unit

            line_items.append(
                {"product": product, "qty": qty, "unit_price": unit_price}
            )

        if not line_items and not error:
            error = "Please enter at least one product line."

        if not error:
            ret_kwargs = {
                "owner": owner,
                "supplier": supplier,
                "reference_invoice": original_invoice,
                "return_date": return_date,
                "notes": notes,
                "posted": False,
            }
            ret_kwargs = set_tenant_on_create_kwargs(request, ret_kwargs, PurchaseReturn)

            ret = PurchaseReturn.objects.create(**ret_kwargs)

            for li in line_items:
                item_kwargs = {
                    "owner": owner,
                    "purchase_return": ret,
                    "product": li["product"],
                    "unit_type": li["product"].unit,
                    "quantity_units": li["qty"],
                    "unit_price": li["unit_price"],
                    "discount_amount": Decimal("0"),
                }
                item_kwargs = set_tenant_on_create_kwargs(request, item_kwargs, PurchaseReturnItem)

                PurchaseReturnItem.objects.create(**item_kwargs)

            ret.post()
            messages.success(request, "Purchase return saved and posted.")
            return redirect("purchase_list")

    context = {
        "mode": "purchase",
        "party_label": "Supplier",
        "parties": suppliers,
        "products": products,
        "invoices": invoices,
        "today": today_str,
        "error": error,
        "back_url_name": "purchase_list",
    }
    return render(request, "core/returns_new.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@transaction.atomic
@subscription_required
def sales_edit(request, pk):
    invoice = tenant_get_object_or_404(request, SalesInvoice, pk=pk)

    # 🔒 No edit after post
    if getattr(invoice, "posted", False):
        return redirect("sales_list")

    if request.method == "POST":
        customer_id = request.POST.get("customer")
        invoice_date_str = request.POST.get("invoice_date") or ""
        notes = (request.POST.get("notes") or "").strip()

        # ✅ Customer (tenant-safe)
        if customer_id:
            customer = tenant_get_object_or_404(
                request,
                Party,
                pk=customer_id,
                party_type="CUSTOMER",
                is_active=True,
            )
            invoice.customer = customer

        # ✅ Date
        if invoice_date_str:
            try:
                invoice.invoice_date = timezone.datetime.strptime(invoice_date_str, "%Y-%m-%d").date()
            except ValueError:
                pass

        invoice.notes = notes

        # ✅ Payment fields (tenant-safe)
        payment_type = (request.POST.get("payment_type") or "CREDIT").upper()
        if payment_type not in ("CREDIT", "FULL", "PARTIAL"):
            payment_type = "CREDIT"

        payment_amount_str = (request.POST.get("payment_amount") or "0").strip()
        payment_account_id = request.POST.get("payment_account") or ""

        try:
            payment_amount = Decimal(payment_amount_str)
        except Exception:
            payment_amount = Decimal("0")

        payment_account_obj = None
        if payment_type == "CREDIT":
            payment_amount = Decimal("0")
            payment_account_obj = None
        else:
            if payment_account_id:
                payment_account_obj = tenant_get_object_or_404(
                    request,
                    Account,
                    pk=payment_account_id,
                    is_cash_or_bank=True,
                    allow_for_payments=True,
                )
            else:
                payment_account_obj = None

        invoice.payment_type = payment_type
        invoice.payment_amount = payment_amount
        invoice.payment_account = payment_account_obj
        invoice.save()

        # ✅ Delete old items (tenant-safe)
        try:
            invoice.items.all().delete()
        except Exception:
            tenant_qs(request, SalesInvoiceItem, strict=True).filter(sales_invoice=invoice).delete()

        # ✅ Recreate items
        row_indexes = set()
        for key in request.POST.keys():
            if key.startswith("product_"):
                parts = key.split("_", 1)
                if len(parts) == 2:
                    row_indexes.add(parts[1])

        row_indexes = sorted(row_indexes, key=lambda x: int(x) if x.isdigit() else x)

        for idx in row_indexes:
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id or not qty_str:
                continue

            product = tenant_get_object_or_404(request, Product, pk=product_id, is_active=True)

            try:
                quantity = Decimal(qty_str)
            except Exception:
                quantity = Decimal("0")

            if quantity <= 0:
                continue

            try:
                unit_price = Decimal(unit_price_str or product.sale_price_per_unit)
            except Exception:
                unit_price = product.sale_price_per_unit

            item_kwargs = {
                "sales_invoice": invoice,
                "product": product,
                "unit_type": product.unit,
                "quantity_units": quantity,
                "unit_price": unit_price,
                "discount_amount": Decimal("0"),
            }
            item_kwargs = set_tenant_on_create_kwargs(request, item_kwargs, SalesInvoiceItem)

            SalesInvoiceItem.objects.create(
                owner=request.owner,
                **item_kwargs,
            )

        return redirect("sales_list")

    customers = (
        tenant_qs(request, Party, strict=True)
        .filter(party_type="CUSTOMER", is_active=True)
        .order_by("name")
    )

    products = (
        tenant_qs(request, Product, strict=True)
        .filter(is_active=True)
        .order_by("code")
    )

    accounts = (
        tenant_qs(request, Account, strict=True)
        .filter(is_cash_or_bank=True, allow_for_payments=True)
        .order_by("code")
    )

    try:
        items = invoice.items.select_related("product").all()
    except Exception:
        items = tenant_qs(request, SalesInvoiceItem, strict=True).filter(sales_invoice=invoice).select_related("product")

    context = {
        "edit_mode": True,
        "invoice": invoice,
        "items": items,
        "customers": customers,
        "products": products,
        "accounts": accounts,
        "today": timezone.now().date(),
        "suggested_invoice_number": invoice.invoice_number,
        "selected_customer_id": str(invoice.customer_id) if invoice.customer_id else "",
    }
    return render(request, "core/sales_new.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def sales_delete(request, pk):
    invoice = tenant_get_object_or_404(request, SalesInvoice, pk=pk)

    # 🔒 Cannot delete posted invoices
    if invoice.posted:
        return redirect("sales_list")

    if request.method == "POST":
        # ✅ Tenant-safe cascade delete
        try:
            invoice.items.all().delete()
        except Exception:
            tenant_qs(request, SalesInvoiceItem, strict=True).filter(sales_invoice=invoice).delete()

        invoice.delete()
        return redirect("sales_list")

    return redirect("sales_list")


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@transaction.atomic
@subscription_required
def purchase_edit(request, pk):
    invoice = tenant_get_object_or_404(request, PurchaseInvoice, pk=pk)

    # ❌ Don’t allow editing posted purchases
    if getattr(invoice, "posted", False):
        messages.error(request, "Posted purchases cannot be edited.")
        return redirect("purchase_list")

    # Common dropdown data (tenant scoped)
    suppliers = (
        tenant_qs(request, Party, strict=True)
        .filter(party_type="SUPPLIER", is_active=True)
        .order_by("name")
    )
    products = (
        tenant_qs(request, Product, strict=True)
        .filter(is_active=True)
        .order_by("code")
    )
    accounts = (
        tenant_qs(request, Account, strict=True)
        .filter(is_cash_or_bank=True, allow_for_payments=True)
        .order_by("code")
    )

    error = None

    if request.method == "POST":
        # ----- HEADER FIELDS -----
        supplier_id = request.POST.get("supplier")
        invoice_number = (request.POST.get("invoice_number") or "").strip()
        invoice_date = request.POST.get("invoice_date")
        date_received = request.POST.get("date_received")
        notes = (request.POST.get("notes") or "").strip()

        freight_str = request.POST.get("freight_charges") or "0"
        other_str = request.POST.get("other_charges") or "0"

        # ----- PAYMENT FIELDS -----
        payment_type = request.POST.get("payment_type", "CREDIT")
        payment_amount_str = request.POST.get("payment_amount") or "0"
        payment_account_id = request.POST.get("payment_account") or None

        if not supplier_id:
            error = "Supplier is required."

        # Parse freight / other
        try:
            freight = Decimal(freight_str)
        except Exception:
            freight = Decimal("0")

        try:
            other = Decimal(other_str)
        except Exception:
            other = Decimal("0")

        # Collect line indexes from POST (product_1, product_2, ...)
        row_indexes = set()
        for key in request.POST.keys():
            if key.startswith("product_"):
                parts = key.split("_", 1)
                if len(parts) == 2:
                    row_indexes.add(parts[1])

        # Stable iteration (logic unchanged, just predictable order)
        row_indexes = sorted(row_indexes, key=lambda x: int(x) if str(x).isdigit() else str(x))

        line_items = []
        for idx in row_indexes:
            product_id = request.POST.get(f"product_{idx}")
            qty_str = request.POST.get(f"quantity_{idx}")
            unit_price_str = request.POST.get(f"unit_price_{idx}")

            if not product_id:
                continue

            product = tenant_get_object_or_404(request, Product, pk=product_id)

            try:
                qty = Decimal(qty_str or "0")
            except Exception:
                qty = Decimal("0")

            if qty <= 0:
                continue

            try:
                unit_price = Decimal(unit_price_str or product.purchase_price_per_unit)
            except Exception:
                unit_price = product.purchase_price_per_unit

            line_items.append(
                {
                    "product": product,
                    "qty": qty,
                    "unit_price": unit_price,
                }
            )

        if not line_items and not error:
            error = "Please enter at least one product line."

        # If everything looks OK → update invoice & items
        if not error:
            supplier = tenant_get_object_or_404(
                request,
                Party,
                pk=supplier_id,
                party_type="SUPPLIER",
                is_active=True,
            )

            invoice.supplier = supplier
            invoice.invoice_number = invoice_number or invoice.invoice_number

            if invoice_date:
                invoice.invoice_date = invoice_date
            if date_received:
                invoice.date_received = date_received

            invoice.notes = notes
            invoice.freight_charges = freight
            invoice.other_charges = other

            # Payment
            if hasattr(invoice, "payment_type"):
                invoice.payment_type = payment_type

            if hasattr(invoice, "payment_amount"):
                try:
                    invoice.payment_amount = Decimal(payment_amount_str)
                except Exception:
                    invoice.payment_amount = Decimal("0")

            if hasattr(invoice, "payment_account"):
                if payment_account_id:
                    tenant_get_object_or_404(
                        request,
                        Account,
                        pk=payment_account_id,
                        is_cash_or_bank=True,
                        allow_for_payments=True,
                    )
                invoice.payment_account_id = payment_account_id

            invoice.save()

            # ----- REPLACE ITEMS -----
            if hasattr(invoice, "items"):
                invoice.items.all().delete()
            else:
                tenant_qs(request, PurchaseInvoiceItem, strict=True).filter(purchase_invoice=invoice).delete()

            for li in line_items:
                item_kwargs = {
                    "purchase_invoice": invoice,
                    "product": li["product"],
                    "unit_type": li["product"].unit,
                    "quantity_units": li["qty"],
                    "unit_price": li["unit_price"],
                    "discount_amount": Decimal("0"),
                }
                item_kwargs = set_tenant_on_create_kwargs(request, item_kwargs, PurchaseInvoiceItem)

                PurchaseInvoiceItem.objects.create(
                    owner=request.owner,
                    **item_kwargs,
                )

            messages.success(request, "Purchase updated successfully.")
            return redirect("purchase_list")

    # GET, or POST with error → show form
    try:
        items = invoice.items.select_related("product").all()
    except Exception:
        items = (
            tenant_qs(request, PurchaseInvoiceItem, strict=True)
            .filter(purchase_invoice=invoice)
            .select_related("product")
        )

    context = {
        "edit_mode": True,
        "invoice": invoice,
        "items": items,
        "suppliers": suppliers,
        "products": products,
        "accounts": accounts,
        "error": error,
    }
    return render(request, "core/purchase_new.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
@subscription_required
def purchase_delete(request, pk):
    invoice = tenant_get_object_or_404(request, PurchaseInvoice, pk=pk)

    # ❌ Block deleting posted invoices
    if invoice.posted:
        messages.error(request, "Posted purchases cannot be deleted.")
        return redirect("purchase_list")

    if request.method == "POST":
        # ✅ delete items first (safe)
        try:
            invoice.items.all().delete()
        except Exception:
            tenant_qs(request, PurchaseInvoiceItem, strict=True).filter(purchase_invoice=invoice).delete()

        invoice.delete()
        messages.success(request, "Purchase deleted successfully.")
        return redirect("purchase_list")

    return redirect("purchase_list")


def build_product_ledger(product, date_from=None, date_to=None):
    """
    Build a movement ledger for a single Product (SaaS SAFE via owner).

    Movements included:
      - PurchaseInvoiceItem (IN)
      - PurchaseReturnItem (OUT)
      - SalesInvoiceItem (OUT)
      - SalesReturnItem (IN)
      - StockAdjustment (UP/DOWN)

    Returns:
      rows: list of dicts with running stock balance
      opening_qty: Decimal (balance before date_from)
      closing_qty: Decimal (final balance after all rows)
    """
    rows = []

    # ✅ SaaS-safe scope: all related models use `owner`
    owner = product.owner

    # ---------- 1) Compute opening qty (all movements BEFORE date_from) ----------
    opening_qty = Decimal("0")

    if date_from:
        pre_purchases = PurchaseInvoiceItem.objects.filter(
            owner=owner,
            product=product,
            purchase_invoice__posted=True,
            purchase_invoice__invoice_date__lt=date_from,
        )
        for it in pre_purchases:
            opening_qty += (it.quantity_units or Decimal("0"))

        pre_purch_returns = PurchaseReturnItem.objects.filter(
            owner=owner,
            product=product,
            purchase_return__posted=True,
            purchase_return__return_date__lt=date_from,
        )
        for it in pre_purch_returns:
            opening_qty -= (it.quantity_units or Decimal("0"))

        pre_sales = SalesInvoiceItem.objects.filter(
            owner=owner,
            product=product,
            sales_invoice__posted=True,
            sales_invoice__invoice_date__lt=date_from,
        )
        for it in pre_sales:
            opening_qty -= (it.quantity_units or Decimal("0"))

        pre_sales_returns = SalesReturnItem.objects.filter(
            owner=owner,
            product=product,
            sales_return__posted=True,
            sales_return__return_date__lt=date_from,
        )
        for it in pre_sales_returns:
            opening_qty += (it.quantity_units or Decimal("0"))

        pre_adj = StockAdjustment.objects.filter(
            owner=owner,
            product=product,
            posted=True,
            date__lt=date_from,
        )
        for a in pre_adj:
            qty = a.qty or Decimal("0")
            if a.direction == "UP":
                opening_qty += qty
            else:  # DOWN
                opening_qty -= qty

    # ---------- 2) Movements within selected range ----------
    purchases = PurchaseInvoiceItem.objects.filter(
        owner=owner,
        product=product,
        purchase_invoice__posted=True,
    )
    if date_from:
        purchases = purchases.filter(purchase_invoice__invoice_date__gte=date_from)
    if date_to:
        purchases = purchases.filter(purchase_invoice__invoice_date__lte=date_to)
    purchases = purchases.select_related("purchase_invoice__supplier")

    for it in purchases:
        inv = it.purchase_invoice
        qty = it.quantity_units or Decimal("0")
        unit_price = it.unit_price or Decimal("0")
        value = qty * unit_price

        rows.append({
            "date": inv.invoice_date,
            "source": "PURCHASE",
            "ref": f"PI #{inv.id}",
            "counterparty": getattr(inv.supplier, "name", "") if inv.supplier_id else "",
            "in_qty": qty,
            "out_qty": Decimal("0"),
            "unit_price": unit_price,
            "value": value,
        })

    purch_returns = PurchaseReturnItem.objects.filter(
        owner=owner,
        product=product,
        purchase_return__posted=True,
    )
    if date_from:
        purch_returns = purch_returns.filter(purchase_return__return_date__gte=date_from)
    if date_to:
        purch_returns = purch_returns.filter(purchase_return__return_date__lte=date_to)
    purch_returns = purch_returns.select_related("purchase_return__supplier")

    for it in purch_returns:
        ret = it.purchase_return
        qty = it.quantity_units or Decimal("0")
        unit_price = it.unit_price or Decimal("0")
        value = qty * unit_price

        rows.append({
            "date": ret.return_date,
            "source": "PURCHASE_RETURN",
            "ref": f"PR #{ret.id}",
            "counterparty": getattr(ret.supplier, "name", "") if ret.supplier_id else "",
            "in_qty": Decimal("0"),
            "out_qty": qty,
            "unit_price": unit_price,
            "value": value,
        })

    sales = SalesInvoiceItem.objects.filter(
        owner=owner,
        product=product,
        sales_invoice__posted=True,
    )
    if date_from:
        sales = sales.filter(sales_invoice__invoice_date__gte=date_from)
    if date_to:
        sales = sales.filter(sales_invoice__invoice_date__lte=date_to)
    sales = sales.select_related("sales_invoice__customer")

    for it in sales:
        inv = it.sales_invoice
        qty = it.quantity_units or Decimal("0")
        unit_price = it.unit_price or Decimal("0")
        value = qty * unit_price

        rows.append({
            "date": inv.invoice_date,
            "source": "SALE",
            "ref": f"SI #{inv.id}",
            "counterparty": getattr(inv.customer, "name", "") if inv.customer_id else "",
            "in_qty": Decimal("0"),
            "out_qty": qty,
            "unit_price": unit_price,
            "value": value,
        })

    sales_returns = SalesReturnItem.objects.filter(
        owner=owner,
        product=product,
        sales_return__posted=True,
    )
    if date_from:
        sales_returns = sales_returns.filter(sales_return__return_date__gte=date_from)
    if date_to:
        sales_returns = sales_returns.filter(sales_return__return_date__lte=date_to)
    sales_returns = sales_returns.select_related("sales_return__customer")

    for it in sales_returns:
        ret = it.sales_return
        qty = it.quantity_units or Decimal("0")
        unit_price = it.unit_price or Decimal("0")
        value = qty * unit_price

        rows.append({
            "date": ret.return_date,
            "source": "SALES_RETURN",
            "ref": f"SR #{ret.id}",
            "counterparty": getattr(ret.customer, "name", "") if ret.customer_id else "",
            "in_qty": qty,
            "out_qty": Decimal("0"),
            "unit_price": unit_price,
            "value": value,
        })

    adjustments = StockAdjustment.objects.filter(
        owner=owner,
        product=product,
        posted=True,
    )
    if date_from:
        adjustments = adjustments.filter(date__gte=date_from)
    if date_to:
        adjustments = adjustments.filter(date__lte=date_to)

    for a in adjustments:
        qty = a.qty or Decimal("0")
        unit_cost = a.unit_cost or Decimal("0")
        value = qty * unit_cost

        if a.direction == "UP":
            in_qty = qty
            out_qty = Decimal("0")
            src = "STOCK_ADJ_UP"
        else:
            in_qty = Decimal("0")
            out_qty = qty
            src = "STOCK_ADJ_DOWN"

        rows.append({
            "date": a.date,
            "source": src,
            "ref": f"ADJ #{a.id}",
            "counterparty": "",
            "in_qty": in_qty,
            "out_qty": out_qty,
            "unit_price": unit_cost,
            "value": value,
        })

    # ---------- 3) Sort + running balance ----------
    rows.sort(key=lambda r: (r["date"] or timezone.now().date(), r["ref"]))

    balance_qty = opening_qty
    for r in rows:
        balance_qty += (r["in_qty"] - r["out_qty"])
        r["balance_qty"] = balance_qty

    closing_qty = balance_qty
    return rows, opening_qty, closing_qty

def get_current_stock_including_adjustments(product):
    """
    CONTRACT SAFE (Owner == Tenant).

    Current stock = Purchases + SalesReturns - Sales - PurchaseReturns + StockAdjustments(UP/DOWN)
    Only posted documents count.
    """
    owner = product.owner  # hard lock to tenant owner

    purchases_in = PurchaseInvoiceItem.objects.filter(
        owner=owner,
        product=product,
        purchase_invoice__posted=True,
    ).aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")

    purch_returns_out = PurchaseReturnItem.objects.filter(
        owner=owner,
        product=product,
        purchase_return__posted=True,
    ).aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")

    sales_out = SalesInvoiceItem.objects.filter(
        owner=owner,
        product=product,
        sales_invoice__posted=True,
    ).aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")

    sales_returns_in = SalesReturnItem.objects.filter(
        owner=owner,
        product=product,
        sales_return__posted=True,
    ).aggregate(s=Sum("quantity_units"))["s"] or Decimal("0")

    adj_up = StockAdjustment.objects.filter(
        owner=owner,
        product=product,
        posted=True,
        direction="UP",
    ).aggregate(s=Sum("qty"))["s"] or Decimal("0")

    adj_down = StockAdjustment.objects.filter(
        owner=owner,
        product=product,
        posted=True,
        direction="DOWN",
    ).aggregate(s=Sum("qty"))["s"] or Decimal("0")

    return purchases_in + sales_returns_in - sales_out - purch_returns_out + adj_up - adj_down

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def product_ledger(request):
    """
    Product Ledger screen:
    - Dropdown to pick a product
    - Optional date range (?from=YYYY-MM-DD&to=YYYY-MM-DD)
    - Shows IN / OUT movements and running stock balance.
    """
    # ✅ SaaS: only this tenant's products
    products = Product.objects.filter(owner=request.owner).filter(is_active=True).order_by("code")

    product_id = request.GET.get("product") or ""
    from_str = request.GET.get("from")
    to_str = request.GET.get("to")

    date_from = parse_date(from_str) if from_str else None
    date_to = parse_date(to_str) if to_str else None

    selected_product = None
    rows = []
    opening_qty = Decimal("0")
    closing_qty = Decimal("0")

    if product_id:
        # ✅ SaaS-safe fetch
        selected_product = tenant_get_object_or_404(request, Product, pk=product_id, is_active=True)

        # NOTE: build_product_ledger signature unchanged to avoid breaking logic.
        # Tenant safety comes from selected_product being tenant-scoped.
        rows, opening_qty, closing_qty = build_product_ledger(
            selected_product,
            date_from=date_from,
            date_to=date_to,
        )

    context = {
        "products": products,
        "selected_product": selected_product,
        "rows": rows,
        "opening_qty": opening_qty,
        "closing_qty": closing_qty,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "core/product_ledger.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def adjustments_page(request):
    """
    Single adjustments page:
      - Party adjustments (opening balance adjustment style)
      - Stock adjustments
      - show list tables below
    """
    owner = request.owner

    # =========================
    # OWNER-SCOPED LIST DATA
    # =========================
    parties = (
        Party.objects.filter(owner=owner, is_active=True)
        .order_by("name")
    )

    products = (
        Product.objects.filter(owner=owner, is_active=True)
        .order_by("name")
    )

    adjustments_qs = (
        Payment.objects.filter(owner=owner, is_adjustment=True)
        .select_related("party")
        .order_by("-date", "-id")
    )
    total_adjustments = adjustments_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")

    stock_qs = (
        StockAdjustment.objects.filter(owner=owner)
        .select_related("product")
        .order_by("-date", "-id")[:200]
    )

    # =========================
    # POST HANDLING (MUTATIONS)
    # =========================
    if request.method == "POST":
        form_type = (request.POST.get("form_type") or "party_adjust").strip()

        # -------------------------------------------------
        # A) PARTY ADJUSTMENT
        # -------------------------------------------------
        if form_type == "party_adjust":
            date_str = request.POST.get("date") or ""
            party_id = request.POST.get("party") or ""
            side = (request.POST.get("side") or "DR").upper()
            amount_str = request.POST.get("amount") or "0"
            note = (request.POST.get("note") or "").strip()

            try:
                adj_date = date.fromisoformat(date_str) if date_str else date.today()
            except Exception:
                adj_date = date.today()

            try:
                amount = Decimal(amount_str)
            except Exception:
                amount = Decimal("0")

            if side not in ("DR", "CR"):
                side = "DR"

            if not party_id:
                messages.error(request, "Please select a party.")
            elif amount <= 0:
                messages.error(request, "Amount must be greater than zero.")
            else:
                party = tenant_get_object_or_404(
                    request,
                    Party,
                    pk=party_id,
                    is_active=True,
                    party_type__in=["CUSTOMER", "SUPPLIER"],
                )

                kwargs = {
                    "owner": owner,
                    "date": adj_date,
                    "party": party,
                    "account": None,
                    "direction": "IN",  # neutral for adjustments (non-cash)
                    "amount": amount,
                    "description": note or f"Adjustment ({side})",
                    "posted": False,
                    "is_adjustment": True,
                    "adjustment_side": side,
                }
                kwargs = set_tenant_on_create_kwargs(request, kwargs, Payment)

                p = Payment.objects.create(**kwargs)
                p.post()
                messages.success(request, "Adjustment saved & posted successfully.")
                return redirect("adjustments_page")


                # -------------------------------------------------
        # B) STOCK ADJUSTMENT
        # -------------------------------------------------
        if form_type == "stock_adjust":
            date_str = request.POST.get("date") or ""
            product_id = request.POST.get("product") or ""
            direction = (request.POST.get("direction") or "DOWN").upper()  # UP / DOWN
            qty_str = request.POST.get("qty") or "0"
            unit_cost_str = request.POST.get("unit_cost") or "0"
            reason = (request.POST.get("reason") or "").strip()

            try:
                adj_date = date.fromisoformat(date_str) if date_str else date.today()
            except Exception:
                adj_date = date.today()

            try:
                qty = Decimal(qty_str)
            except Exception:
                qty = Decimal("0")

            try:
                unit_cost = Decimal(unit_cost_str)
            except Exception:
                unit_cost = Decimal("0")

            if direction not in ("UP", "DOWN"):
                direction = "DOWN"

            if not product_id:
                messages.error(request, "Please select a product.")
                return redirect("adjustments_page")

            if qty <= 0:
                messages.error(request, "Quantity must be greater than zero.")
                return redirect("adjustments_page")

            if unit_cost < 0:
                messages.error(request, "Unit cost cannot be negative.")
                return redirect("adjustments_page")

            product = tenant_get_object_or_404(
                request,
                Product,
                pk=product_id,
                is_active=True,
            )

            kwargs = {
                "owner": owner,
                "date": adj_date,
                "product": product,
                "direction": direction,  # UP/DOWN
                "qty": qty,
                "unit_cost": unit_cost,
                "reason": reason,
                "posted": False,
            }
            kwargs = set_tenant_on_create_kwargs(request, kwargs, StockAdjustment)

            sa = StockAdjustment.objects.create(**kwargs)

            # If your model has post(), call it so accounting + stock updates happen
            if hasattr(sa, "post") and callable(sa.post):
                sa.post()

            messages.success(request, "Stock adjustment saved & posted successfully.")
            return redirect("adjustments_page")

    # =========================
    # RENDER
    # =========================
    context = {
        "today": date.today().isoformat(),
        "parties": parties,
        "adjustments": adjustments_qs,
        "total_adjustments": total_adjustments,
        "products": products,
        "stock_adjustments": stock_qs,
    }
    return render(request, "core/adjustments.html", context)

def subscription_forbidden(request, exception=None):
    # Unauthenticated/session-expired users should always go to login.
    message_text = str(exception) if exception else ""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        messages.warning(request, "Session expired. Please sign in again.")
        return redirect_to_login(
            request.get_full_path(),
            login_url=f"{reverse('login')}?reason=session_expired",
        )

    lower_message = message_text.lower()
    if (
        "not authenticated" in lower_message
        or "authentication required" in lower_message
    ):
        messages.warning(request, "Session expired. Please sign in again.")
        return redirect_to_login(
            request.get_full_path(),
            login_url=f"{reverse('login')}?reason=session_expired",
        )

    owner = getattr(request, "owner", None)

    profile = getattr(owner, "profile", None) if owner else None
    status = getattr(profile, "subscription_status", None) if profile else None
    expires_at = getattr(profile, "subscription_expires_at", None) if profile else None

    days_left = None
    if expires_at:
        try:
            delta = expires_at.date() - timezone.now().date()
            days_left = delta.days
        except Exception:
            days_left = None

    context = {
        "message": str(exception) if exception else "Access denied.",
        "status": status,
        "expires_at": expires_at,
        "days_left": days_left,
    }
    return render(request, "403.html", context, status=403)


@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def subscription_page(request):
    owner = request.owner
    profile = getattr(owner, "profile", None)

    if not profile:
        # Should never happen, but keep safe
        messages.error(request, "Profile not found.")
        return redirect("dashboard")

        # ✅ Always use effective logic (single source of truth)
    status = "TRIAL"
    expires_at = None

    if profile:
        status = profile.get_effective_status()
        expires_at = profile.get_effective_expires_at()

    trial_started = getattr(profile, "trial_started_at", None)

    # Days left
    days_left = None
    if expires_at:
        try:
            days_left = (expires_at.date() - timezone.now().date()).days
        except Exception:
            days_left = None

    # -------------------------
    # Renewal component (placeholder)
    # -------------------------
    # Later Phase: payment gateway -> create invoice -> mark active -> set expires_at
    plan = (request.POST.get("plan") or "").upper()  # MONTHLY / YEARLY
    if request.method == "POST":
        if plan not in ("MONTHLY", "YEARLY"):
            messages.error(request, "Please select a valid plan (Monthly/Yearly).")
            return redirect("subscription_page")

        # ✅ For now: just show message (no payment yet)
        messages.info(request, f"Renewal request received for {plan}. Payment integration will come in Phase 4.")
        return redirect("subscription_page")

    context = {
        "status": status,
        "expires_at": expires_at,
        "trial_started_at": trial_started,
        "owner_username": owner.username,
        "days_left": days_left,
        "is_expired": (status == "EXPIRED"),
        "is_trial": (status == "TRIAL"),
        "is_active": (status == "ACTIVE"),
    }
    return render(request, "core/subscription_page.html", context)

@login_required
@resolve_tenant_context(require_company=True)
def tenant_check(request):
    t = getattr(request, "tenant", None)
    return JsonResponse({
        "user": request.user.username,
        "role": getattr(getattr(request.user, "profile", None), "role", None),
        "tenant_id": getattr(t, "id", None),
        "tenant_name": getattr(t, "name", None),
        "tenant_slug": getattr(t, "slug", None),
    })


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def account_balance_api(request):
    """
    GET /api/ledger/account-balance/?code=1200&as_of=2026-02-05
    Returns: {code, name, balance}
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    code = request.GET.get("code")
    if not code:
        return JsonResponse({"error": "Missing required query param: code"}, status=400)

    as_of_raw = request.GET.get("as_of")
    as_of = None
    if as_of_raw:
        try:
            as_of = datetime.strptime(as_of_raw, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"error": "Invalid as_of format. Use YYYY-MM-DD."}, status=400)

    # ✅ Owner-scoped, avoids global Account.objects.get(...)
    account = Account.objects.filter(owner=owner, code=code).first()
    if not account:
        return JsonResponse({"error": f"Account not found for this owner: {code}"}, status=404)

    balance = get_account_balance(owner=owner, account=account, as_of=as_of)

    return JsonResponse(
        {
            "code": account.code,
            "name": account.name,
            "balance": str(balance),
        }
    )


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def account_ledger_api(request):
    """
    GET /api/ledger/account-ledger/?code=1200&from=2026-01-01&to=2026-02-05
    Returns JSON: opening_balance + closing_balance + rows (running_balance).
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    code = request.GET.get("code")
    if not code:
        return JsonResponse({"error": "Missing required query param: code"}, status=400)

    date_from = request.GET.get("from")
    date_to = request.GET.get("to")

    data = build_account_ledger_for_owner(
        owner=owner,
        account_code=code,
        date_from=date_from,
        date_to=date_to,
    )
    return JsonResponse(data, safe=False)


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def party_balance_api(request):
    """
    GET /api/ledger/party-balance/?party_id=5&as_of=YYYY-MM-DD
    Returns closing balance/side for that date.
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    party_id = request.GET.get("party_id")
    if not party_id:
        return JsonResponse({"error": "Missing required query param: party_id"}, status=400)

    party = Party.objects.filter(owner=owner, id=party_id).first()
    if not party:
        return JsonResponse({"error": "Party not found for this owner."}, status=404)

    as_of = request.GET.get("as_of")
    data = build_party_ledger_for_owner(
        owner=owner,
        party=party,
        date_from=None,
        date_to=as_of,
    )

    return JsonResponse({
        "party_id": party.id,
        "party_name": party.name,
        "party_type": party.party_type,
        "as_of": as_of or None,
        "balance": data["closing_balance"],
        "side": data["closing_side"],
    })


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def party_ledger_api(request):
    """
    GET /api/ledger/party-ledger/?party_id=5&from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    party_id = request.GET.get("party_id")
    if not party_id:
        return JsonResponse({"error": "Missing required query param: party_id"}, status=400)

    party = Party.objects.filter(owner=owner, id=party_id).first()
    if not party:
        return JsonResponse({"error": "Party not found for this owner."}, status=404)

    date_from = request.GET.get("from")
    date_to = request.GET.get("to")

    data = build_party_ledger_for_owner(
        owner=owner,
        party=party,
        date_from=date_from,
        date_to=date_to,
    )
    return JsonResponse(data, status=200)


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def trial_balance_api(request):
    """
    GET /api/ledger/trial-balance/?as_of=2026-02-05
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    as_of_raw = request.GET.get("as_of")
    as_of = None

    if as_of_raw:
        try:
            as_of = datetime.strptime(as_of_raw, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"error": "Invalid as_of format. Use YYYY-MM-DD."}, status=400)

    data = get_trial_balance(owner=owner, as_of=as_of)
    data["as_of"] = as_of.isoformat() if as_of else None

    return JsonResponse(data)


def build_account_ledger_for_owner(*, owner, account_code: str, date_from=None, date_to=None):
    """
    Returns ledger for ONE account (by code) for a specific owner.
    Includes opening balance + running balance.
    """
    if owner is None:
        raise PermissionDenied("Owner not resolved.")

    if not account_code:
        raise ValidationError("Account code is required.")

    acct = Account.objects.filter(owner=owner, code=account_code).first()
    if not acct:
        raise ValidationError(f"Account not found for code {account_code}")

    # Normalize dates (Date objects or None)
    if isinstance(date_from, str):
        date_from = parse_date(date_from)
    if isinstance(date_to, str):
        date_to = parse_date(date_to)

    # Opening balance = sum of (debits - credits) before date_from
    opening = Decimal("0.00")
    if date_from:
        deb_before = JournalEntry.objects.filter(
            owner=owner,
            debit_account=acct,
            date__lt=date_from,
        ).aggregate(s=models.Sum("amount"))["s"] or Decimal("0.00")

        cr_before = JournalEntry.objects.filter(
            owner=owner,
            credit_account=acct,
            date__lt=date_from,
        ).aggregate(s=models.Sum("amount"))["s"] or Decimal("0.00")

        opening = deb_before - cr_before

    # Period entries
    qs = JournalEntry.objects.filter(owner=owner).filter(
        models.Q(debit_account=acct) | models.Q(credit_account=acct)
    )

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    qs = qs.select_related("debit_account", "credit_account").order_by("date", "id")

    running = opening
    rows = []

    for je in qs:
        dr = je.amount if je.debit_account_id == acct.id else Decimal("0.00")
        cr = je.amount if je.credit_account_id == acct.id else Decimal("0.00")
        running = running + dr - cr

        rows.append({
            "id": je.id,
            "date": je.date,
            "description": je.description,
            "debit": str(dr),
            "credit": str(cr),
            "running_balance": str(running),
            "related_model": je.related_model,
            "related_id": je.related_id,
            "contra": (
                f"{je.credit_account.code} - {je.credit_account.name}"
                if je.debit_account_id == acct.id
                else f"{je.debit_account.code} - {je.debit_account.name}"
            ),
        })

    return {
        "account": {
            "code": acct.code,
            "name": acct.name,
            "type": acct.account_type,
        },
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "opening_balance": str(opening),
        "closing_balance": str(running),
        "rows": rows,
    }


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
@staff_blocked
def account_ledger_view(request):
    """
    GET /api/ledger/account-ledger/?code=1200&from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    user = request.user
    owner = request.owner  # trust resolved tenant

    code = request.GET.get("code")
    date_from = request.GET.get("from")
    date_to = request.GET.get("to")

    data = build_account_ledger_for_owner(
        owner=owner,
        account_code=code,
        date_from=date_from,
        date_to=date_to,
    )

    return JsonResponse(data, safe=False)


@require_GET
@login_required
@resolve_tenant_context(require_company=True)
def party_ledger_view(request, party_id: int):
    """
    GET /ledger/party/<party_id>/?from=YYYY-MM-DD&to=YYYY-MM-DD
    (JSON statement)
    """
    owner = getattr(request, "owner", None)
    if owner is None:
        return JsonResponse({"error": "Tenant not resolved."}, status=403)

    party = Party.objects.filter(owner=owner, id=party_id).first()
    if not party:
        return JsonResponse({"detail": "Party not found."}, status=404)

    date_from = request.GET.get("from")
    date_to = request.GET.get("to")

    data = build_party_ledger_for_owner(
        owner=owner,
        party=party,
        date_from=date_from,
        date_to=date_to,
    )
    return JsonResponse(data, status=200)


def build_party_ledger_for_owner(*, owner, party: Party, date_from=None, date_to=None):
    """
    Builds Customer/Supplier statement with:
      - opening row
      - transactions
      - running balance with DR/CR side
    Supports optional date range filtering (from/to).
    """
    if owner is None:
        raise PermissionDenied("Owner not resolved.")

    # Normalize dates
    if isinstance(date_from, str):
        date_from = parse_date(date_from)
    if isinstance(date_to, str):
        date_to = parse_date(date_to)

    is_customer = (party.party_type == "CUSTOMER")
    control_code = "1300" if is_customer else "2100"

    # -----------------------
    # 1) Related docs (posted only)
    # -----------------------
    sales_ids = []
    purchase_ids = []
    sales_return_ids = []
    purchase_return_ids = []

    if is_customer:
        sales_ids = list(
            SalesInvoice.objects.filter(owner=owner, customer=party, posted=True)
            .values_list("id", flat=True)
        )
        sales_return_ids = list(
            SalesReturn.objects.filter(owner=owner, customer=party, posted=True)
            .values_list("id", flat=True)
        )
    else:
        purchase_ids = list(
            PurchaseInvoice.objects.filter(owner=owner, supplier=party, posted=True)
            .values_list("id", flat=True)
        )
        purchase_return_ids = list(
            PurchaseReturn.objects.filter(owner=owner, supplier=party, posted=True)
            .values_list("id", flat=True)
        )

    payment_ids = list(
        Payment.objects.filter(owner=owner, party=party, posted=True)
        .values_list("id", flat=True)
    )

    # -----------------------
    # 2) Base journal query for this party
    # -----------------------
    base_q = Q(owner=owner) & (
        Q(debit_account__code=control_code) | Q(credit_account__code=control_code)
    ) & (
        (Q(related_model="SalesInvoice") & Q(related_id__in=sales_ids)) |
        (Q(related_model="PurchaseInvoice") & Q(related_id__in=purchase_ids)) |
        (Q(related_model="SalesReturn") & Q(related_id__in=sales_return_ids)) |
        (Q(related_model="PurchaseReturn") & Q(related_id__in=purchase_return_ids)) |
        (Q(related_model="Payment") & Q(related_id__in=payment_ids))
    )

    # -----------------------
    # 3) Opening balance (Party opening + journals BEFORE date_from)
    # -----------------------
    opening_amt = party.opening_balance or Decimal("0.00")

    # Convert party opening into "signed" running base:
    # Customer control (ASSET): DR positive
    # Supplier control (LIABILITY): CR positive
    if is_customer:
        opening_signed = opening_amt if party.opening_balance_is_debit else -opening_amt
    else:
        opening_signed = opening_amt if (not party.opening_balance_is_debit) else -opening_amt

    running = opening_signed

    # If date_from given, include journal effect BEFORE date_from into opening
    if date_from:
        before_entries = JournalEntry.objects.filter(base_q, date__lt=date_from)
        for je in before_entries:
            amt = je.amount or Decimal("0.00")
            debit_on_control = (je.debit_account.code == control_code)
            credit_on_control = (je.credit_account.code == control_code)

            dr = amt if debit_on_control else Decimal("0.00")
            cr = amt if credit_on_control else Decimal("0.00")

            if is_customer:
                running += (dr - cr)
            else:
                running += (cr - dr)

    def balance_label(balance_value: Decimal):
        if is_customer:
            return "DR" if balance_value >= 0 else "CR"
        return "CR" if balance_value >= 0 else "DR"

    rows = []

    # Opening row (shows only the PARTY opening, not the pre-date journal accumulation)
    rows.append({
        "date": None,
        "type": "OPENING",
        "ref": None,
        "description": f"Opening balance for {party.name}",
        "debit": float(opening_amt) if party.opening_balance_is_debit else 0.0,
        "credit": float(opening_amt) if (not party.opening_balance_is_debit) else 0.0,
        "balance": float(abs(running)),
        "balance_side": balance_label(running),
    })

    # -----------------------
    # 4) Period entries (date_from..date_to)
    # -----------------------
    entries_qs = JournalEntry.objects.filter(base_q)
    if date_from:
        entries_qs = entries_qs.filter(date__gte=date_from)
    if date_to:
        entries_qs = entries_qs.filter(date__lte=date_to)

    entries = list(
        entries_qs.select_related("debit_account", "credit_account")
        .order_by("date", "id")
    )

    for je in entries:
        amt = je.amount or Decimal("0.00")

        debit_on_control = (je.debit_account.code == control_code)
        credit_on_control = (je.credit_account.code == control_code)

        dr = amt if debit_on_control else Decimal("0.00")
        cr = amt if credit_on_control else Decimal("0.00")

        if is_customer:
            running += (dr - cr)
        else:
            running += (cr - dr)

        rows.append({
            "date": je.date.isoformat() if je.date else None,
            "type": je.related_model or "Journal",
            "ref": je.related_id,
            "description": je.description or "",
            "debit": float(dr),
            "credit": float(cr),
            "balance": float(abs(running)),
            "balance_side": balance_label(running),
        })

    return {
        "party": {"id": party.id, "name": party.name, "type": party.party_type},
        "control_code": control_code,
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "rows": rows,
        "closing_balance": str(abs(running)),
        "closing_side": balance_label(running),
    }


def get_trial_balance(*, owner, as_of=None):
    """
    Computes trial balance for an owner as of a date.

    Returns:
    {
        "accounts": [
            {code, name, type, debit, credit},
            ...
        ],
        "total_debit": "...",
        "total_credit": "..."
    }
    """

    if owner is None:
        raise PermissionDenied("Owner not resolved.")

    if isinstance(as_of, str):
        as_of = parse_date(as_of)

    accounts = Account.objects.filter(owner=owner).order_by("code")

    result = []
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")

    for acct in accounts:

        debits = JournalEntry.objects.filter(
            owner=owner,
            debit_account=acct,
        )

        credits = JournalEntry.objects.filter(
            owner=owner,
            credit_account=acct,
        )

        if as_of:
            debits = debits.filter(date__lte=as_of)
            credits = credits.filter(date__lte=as_of)

        deb_sum = debits.aggregate(s=models.Sum("amount"))["s"] or Decimal("0.00")
        cr_sum = credits.aggregate(s=models.Sum("amount"))["s"] or Decimal("0.00")

        balance = deb_sum - cr_sum

        debit_val = Decimal("0.00")
        credit_val = Decimal("0.00")

        if balance >= 0:
            debit_val = balance
        else:
            credit_val = -balance

        total_debit += debit_val
        total_credit += credit_val

        result.append({
            "code": acct.code,
            "name": acct.name,
            "type": acct.account_type,
            "debit": str(debit_val),
            "credit": str(credit_val),
        })

    return {
        "accounts": result,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
    }

@login_required
@resolve_tenant_context(require_company=True)
@subscription_required
@transaction.atomic
def stock_adjustments_page(request):
    """
    Stock Adjustments ONLY.
    Staff are allowed (one of the 8 allowed actions).
    Party adjustments are NOT available here.
    """
    owner = request.owner

    products = (
        Product.objects.filter(owner=owner, is_active=True)
        .order_by("name")
    )

    stock_qs = (
        StockAdjustment.objects.filter(owner=owner, posted=True)
        .select_related("product")
        .order_by("-date", "-id")[:200]
    )

    if request.method == "POST":
        date_str = request.POST.get("date") or ""
        product_id = request.POST.get("product") or ""
        direction = request.POST.get("direction") or "DOWN"
        qty_str = (request.POST.get("qty") or "0").strip()
        unit_cost_str = (request.POST.get("unit_cost") or "0").strip()
        reason = (request.POST.get("reason") or "").strip()

        try:
            adj_date = date.fromisoformat(date_str) if date_str else date.today()
        except Exception:
            adj_date = date.today()

        try:
            qty = Decimal(qty_str)
        except Exception:
            qty = Decimal("0")

        try:
            unit_cost = Decimal(unit_cost_str)
        except Exception:
            unit_cost = Decimal("0")

        if direction not in ("UP", "DOWN"):
            direction = "DOWN"

        if not product_id:
            messages.error(request, "Please select a product.")
        elif qty <= 0:
            messages.error(request, "Quantity must be greater than zero.")
        elif unit_cost <= 0:
            messages.error(request, "Unit cost must be greater than zero.")
        else:
            product = tenant_get_object_or_404(request, Product, pk=product_id, is_active=True)

            kwargs = {
                "owner": owner,
                "date": adj_date,
                "product": product,
                "direction": direction,
                "qty": qty,
                "unit_cost": unit_cost,
                "reason": reason,
                "posted": False,
            }
            kwargs = set_tenant_on_create_kwargs(request, kwargs, StockAdjustment)

            adj = StockAdjustment.objects.create(**kwargs)
            adj.post()
            messages.success(request, "Stock adjustment saved & posted successfully.")
            return redirect("stock_adjustments_page")

    context = {
        "today": date.today().isoformat(),
        "products": products,
        "stock_adjustments": stock_qs,
    }
    return render(request, "core/stock_adjustments.html", context)

User = get_user_model()

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def owner_profile_page(request):
    owner = request.owner
    profile = owner.profile

    company = getattr(owner, "company_profile", None)

    # If missing, repair safely (never invent random slugs)
    if company is None:
        # preferred: use existing slug rule (owner.username)
        safe_slug = (owner.username or "").lower().strip()

        # if somehow slug is empty, last fallback uses owner id (still deterministic)
        if not safe_slug:
            safe_slug = f"owner-{owner.id}"

        company = CompanyProfile.objects.create(
            owner=owner,
            name=owner.get_full_name() or "New Company",
            slug=safe_slug,
            phone="",
            email=owner.email or "",
        )
    # staff listing (MUST be outside the if, so it exists for GET + POST)
    staff_profiles = (
        UserProfile.objects.select_related("user")
        .filter(role="STAFF", owner=owner)
        .order_by("user__username")
    )

    # Forms (prefill)
    owner_form = OwnerUpdateForm(instance=owner)
    profile_form = OwnerProfileUpdateForm(instance=profile)
    company_form = CompanyUpdateForm(instance=company)
    # Password form (prefill)
    password_form = PasswordChangeForm(user=request.user)

    # =========================
    # POST actions
    # =========================
    if request.method == "POST":
        action = request.POST.get("action")

        # (1) Create staff (your existing logic)
        if action == "create_staff":
            if staff_profiles.count() >= 3:
                messages.error(request, "You can create maximum 3 staff members.")
                return redirect("owner_profile_page")

            username = (request.POST.get("username") or "").strip()
            full_name = (request.POST.get("full_name") or "").strip()
            password = (request.POST.get("password") or "").strip()
            email = (request.POST.get("email") or "").strip().lower()

            if not username:
                messages.error(request, "Username is required.")
                return redirect("owner_profile_page")

            if not password:
                messages.error(request, "Password is required.")
                return redirect("owner_profile_page")
            
            if not email:
                messages.error(request, "Email is required.")
                return redirect("owner_profile_page")

            if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                messages.error(request, "Please enter a valid email address.")
                return redirect("owner_profile_page")

            if User.objects.filter(email__iexact=email).exists():
                messages.error(request, "This email is already in use.")
                return redirect("owner_profile_page")
            if User.objects.filter(username=username).exists():
                messages.error(request, "This username already exists.")
                return redirect("owner_profile_page")

            user = User.objects.create_user(
                username=username,
                password=password,
                email=email,
                is_active=True
            )

            if full_name:
                parts = full_name.split()
                user.first_name = parts[0]
                user.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
                user.save()

            staff_prof, _ = UserProfile.objects.get_or_create(user=user)
            staff_prof.role = "STAFF"
            staff_prof.owner = owner
            staff_prof.is_active = True
            staff_prof.save()

            messages.success(request, f"Staff created: {username}")
            return redirect("owner_profile_page")

        # (2) Update owner info + owner NTN
        if action == "update_owner":
            owner_form = OwnerUpdateForm(request.POST, instance=owner)
            profile_form = OwnerProfileUpdateForm(request.POST, instance=profile)

            if owner_form.is_valid() and profile_form.is_valid():
                owner_form.save()
                profile_form.save()
                messages.success(request, "Owner profile updated successfully.")
                return redirect("owner_profile_page")
            else:
                messages.error(request, "Please correct the owner form errors.")
                # IMPORTANT: do NOT redirect, render page with errors
                context = {
                    "owner": owner,
                    "profile": profile,
                    "company": company,
                    "staff_profiles": staff_profiles,
                    "owner_form": owner_form,
                    "profile_form": profile_form,
                    "company_form": company_form,
                    "password_form": password_form,
                    "profile_form": profile_form,
                }
                return render(request, "core/owner_profile_page.html", context)

        # (3) Update company info
        if action == "update_company":
            company_form = CompanyUpdateForm(request.POST, request.FILES, instance=company)
            if company_form.is_valid():
                company_form.save()
                messages.success(request, "Company profile updated successfully.")
            else:
                messages.error(request, "Please correct the company form errors.")
            return redirect("owner_profile_page")
        
        # (7) Change password
        if action == "change_password":
            password_form = PasswordChangeForm(user=request.user, data=request.POST)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)  # keep logged in
                messages.success(request, "Password updated successfully.")
            else:
                messages.error(request, "Please fix the password form errors.")

            return redirect("owner_profile_page")

        # (4) Deactivate staff
        if action == "deactivate_staff":
            staff_id = request.POST.get("staff_id")
            if not staff_id:
                messages.error(request, "Invalid staff selection.")
                return redirect("owner_profile_page")

            staff_prof = tenant_get_object_or_404(
                request,
                UserProfile,
                pk=staff_id,
                owner=owner,
                role="STAFF",
            )

            staff_prof.is_active = False
            staff_prof.save(update_fields=["is_active"])

            # IMPORTANT: also disable Django user login
            staff_prof.user.is_active = False
            staff_prof.user.save(update_fields=["is_active"])

            messages.success(request, "Staff account deactivated.")
            return redirect("owner_profile_page")

        # (5) Activate staff
        if action == "activate_staff":
            staff_id = request.POST.get("staff_id")
            if not staff_id:
                messages.error(request, "Invalid staff selection.")
                return redirect("owner_profile_page")

            staff_prof = tenant_get_object_or_404(
                request,
                UserProfile,
                pk=staff_id,
                owner=owner,
                role="STAFF",
            )

            staff_prof.is_active = True
            staff_prof.save(update_fields=["is_active"])

            # IMPORTANT: also enable Django user login
            staff_prof.user.is_active = True
            staff_prof.user.save(update_fields=["is_active"])

            messages.success(request, "Staff account activated.")
            return redirect("owner_profile_page")
            # (6) Delete staff (permanent)
        if action == "delete_staff":
            staff_id = request.POST.get("staff_id")
            if not staff_id:
                messages.error(request, "Invalid staff selection.")
                return redirect("owner_profile_page")

            staff_prof = tenant_get_object_or_404(
                request,
                UserProfile,
                pk=staff_id,
                owner=owner,
                role="STAFF",
            )

            user_to_delete = staff_prof.user

            # delete profile first (safe)
            staff_prof.delete()

            # delete the django user
            user_to_delete.delete()

            messages.success(request, "Staff account deleted permanently.")
            return redirect("owner_profile_page")
        
    context = {
        "owner": owner,
        "profile": profile,
        "company": company,
        "staff_profiles": staff_profiles,
        "owner_form": owner_form,
        "company_form": company_form,
        "password_form": password_form,
    }
    return render(request, "core/owner_profile_page.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@owner_required
@staff_blocked
def company_page(request):
    owner = request.owner
    company = getattr(owner, "company_profile", None)

    if request.method == "POST":
        if not company:
            company = CompanyProfile(owner=owner)

        company.name = (request.POST.get("name") or "").strip() or company.name
        company.phone = (request.POST.get("phone") or "").strip()
        company.email = (request.POST.get("email") or "").strip()
        company.address = (request.POST.get("address") or "").strip()

        # logo upload (optional)
        if request.FILES.get("logo"):
            company.logo = request.FILES["logo"]

        company.save()
        messages.success(request, "Company profile updated.")
        return redirect("company_page")

    context = {"company": company}
    return render(request, "core/company_page.html", context)

@superadmin_required
def superadmin_dashboard(request):
    # List all OWNER profiles
    owners = (
        UserProfile.objects.select_related("user")
        .filter(role="OWNER")
        .order_by("-created_at")
    )

    rows = []
    for prof in owners:
        owner = prof.user
        company = CompanyProfile.objects.filter(owner=owner).first()

        status = prof.get_effective_status()
        expires_at = prof.get_effective_expires_at()
        days_left = prof.days_left()

        # suspended logic (manual override)
        suspended = (not owner.is_active) or (not prof.is_active)

        # purge eligibility (expired for >= 60 days)
        eligible = False
        if status == "EXPIRED" and expires_at:
            eligible = timezone.now().date() >= (expires_at.date() + timedelta(days=prof.DORMANCY_DAYS_AFTER_EXPIRE))

        rows.append({
            "owner": owner,
            "profile": prof,
            "company": company,
            "status": status,
            "expires_at": expires_at,
            "days_left": days_left,
            "suspended": suspended,
            "eligible_for_purge": eligible,
        })

    return render(request, "core/superadmin/dashboard.html", {"rows": rows})


@superadmin_required
def superadmin_owner_detail(request, owner_id):
    owner = get_object_or_404(UserProfile, role="OWNER", user__id=owner_id).user
    prof = owner.profile
    company = CompanyProfile.objects.filter(owner=owner).first()

    status = prof.get_effective_status()
    expires_at = prof.get_effective_expires_at()
    days_left = prof.days_left()
    suspended = (not owner.is_active) or (not prof.is_active)

    eligible = False
    if status == "EXPIRED" and expires_at:
        eligible = timezone.now().date() >= (expires_at.date() + timedelta(days=prof.DORMANCY_DAYS_AFTER_EXPIRE))

    ctx = {
        "owner": owner,
        "profile": prof,
        "company": company,
        "status": status,
        "expires_at": expires_at,
        "days_left": days_left,
        "suspended": suspended,
        "eligible_for_purge": eligible,
    }
    return render(request, "core/superadmin/owner_detail.html", ctx)


@superadmin_required
@require_POST
def superadmin_toggle_suspend(request, owner_id):
    owner = get_object_or_404(UserProfile, role="OWNER", user__id=owner_id).user
    prof = owner.profile

    # toggle both flags
    new_state = not owner.is_active
    owner.is_active = new_state
    owner.save(update_fields=["is_active"])

    prof.is_active = new_state
    prof.save(update_fields=["is_active"])

    messages.success(request, "Account suspended." if not new_state else "Account unsuspended.")
    return redirect("superadmin_owner_detail", owner_id=owner_id)


@superadmin_required
def superadmin_subscription_update(request, owner_id):
    owner = get_object_or_404(UserProfile, role="OWNER", user__id=owner_id).user
    prof = owner.profile

    if request.method == "POST":
        plan = request.POST.get("plan")  # monthly/yearly/custom
        custom_days = request.POST.get("custom_days")
        start_today = True

        days = 30
        if plan == "yearly":
            days = 365
        elif plan == "custom":
            try:
                days = int(custom_days)
                if days < 1:
                    days = 30
            except Exception:
                days = 30

        now = timezone.now()
        prof.subscription_status = "ACTIVE"
        prof.subscription_expires_at = now + timedelta(days=days)

        # if trial never started, set it (keeps your system consistent)
        if not prof.trial_started_at:
            prof.trial_started_at = now

        prof.save(update_fields=["subscription_status", "subscription_expires_at", "trial_started_at"])

        messages.success(request, f"Subscription activated/extended for {days} days.")
        return redirect("superadmin_owner_detail", owner_id=owner_id)

    return render(request, "core/superadmin/subscription_form.html", {"owner": owner, "profile": prof})


@superadmin_required
@require_POST
def superadmin_hard_purge_owner(request, owner_id):
    """
    Manual purge is dangerous. Only allow if eligible AND confirmed.
    """
    owner = get_object_or_404(UserProfile, role="OWNER", user__id=owner_id).user
    prof = owner.profile
    company = CompanyProfile.objects.filter(owner=owner).first()

    confirm = (request.POST.get("confirm") or "").strip().lower()
    expected = (company.slug if company else owner.username).lower()

    status = prof.get_effective_status()
    expires_at = prof.get_effective_expires_at()

    eligible = False
    if status == "EXPIRED" and expires_at:
        eligible = timezone.now().date() >= (expires_at.date() + timedelta(days=prof.DORMANCY_DAYS_AFTER_EXPIRE))

    if not eligible:
        messages.error(request, "Not eligible for purge (must be expired for 60+ days).")
        return redirect("superadmin_owner_detail", owner_id=owner_id)

    if confirm != expected:
        messages.error(request, f"Confirmation failed. Type exactly: {expected}")
        return redirect("superadmin_owner_detail", owner_id=owner_id)

    # HARD PURGE: delete company data safely by deleting owner (cascades)
    owner.delete()
    messages.success(request, "Company hard purged successfully.")
    return redirect("superadmin_dashboard")

def offline_page(request):
    # simple offline page
    from django.shortcuts import render
    return render(request, "core/offline.html")


def service_worker(request):
    # Serve /service-worker.js from the repo file core/pwa/service-worker.js
    sw_path = Path(settings.BASE_DIR) / "core" / "pwa" / "service-worker.js"
    content = sw_path.read_text(encoding="utf-8")

    response = HttpResponse(content, content_type="application/javascript")
    # allow SW to control all paths
    response["Service-Worker-Allowed"] = "/"
    # avoid aggressive caching while developing
    response["Cache-Control"] = "no-store, must-revalidate"
    return response

def privacy_policy(request):
    return render(request, "legal/privacy.html")

def terms_conditions(request):
    return render(request, "legal/terms.html")

def refund_policy(request):
    return render(request, "legal/refund.html")

def service_policy(request):
    return render(request, "legal/service.html")

@staff_blocked
def tax_pack_page(request):
    owner = get_company_owner(request.user)
    ctx = {
        "today": timezone.now().date(),
    }
    return render(request, "core/tax_pack/tax_pack_page.html", ctx)


@staff_blocked
def tax_sales_ledger_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_sales_ledger(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Sales_Ledger.xlsx"
    return resp


@staff_blocked
def tax_purchase_ledger_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_purchase_ledger(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Purchase_Ledger.xlsx"
    return resp


@staff_blocked
def tax_payments_ledger_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_payments_ledger(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Payments_Ledger.xlsx"
    return resp


@staff_blocked
def tax_products_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_products_list(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Products_List.xlsx"
    return resp


@staff_blocked
def tax_parties_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_parties_list(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Parties_List.xlsx"
    return resp


@staff_blocked
def tax_accounts_download(request):
    owner = get_company_owner(request.user)
    file_bytes = generate_accounts_list(owner)
    resp = HttpResponse(
        file_bytes,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = "attachment; filename=Accounts_List.xlsx"
    return resp


@staff_blocked
def tax_pack_zip_download(request):
    owner = get_company_owner(request.user)
    zip_bytes = build_tax_pack_zip(owner)
    resp = HttpResponse(zip_bytes, content_type="application/zip")
    resp["Content-Disposition"] = "attachment; filename=Tax_Pack_Full.zip"
    return resp

def sitemap_xml(request):
    pages = [
        reverse("landing"),
        reverse("privacy_policy"),
        reverse("terms_conditions"),
        reverse("refund_policy"),
        reverse("service_policy"),
        # Add more public pages here later:
        # reverse("pricing"),
        # reverse("features"),
    ]

    base = "https://roznamcha.app"
    xml_items = []
    for p in pages:
        xml_items.append(f"""
  <url>
    <loc>{base}{p}</loc>
  </url>
""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{''.join(xml_items)}
</urlset>
"""
    return HttpResponse(xml, content_type="application/xml")

from decimal import Decimal  # ensure this exists at top of file

@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed  # ✅ because your doc says OWNER + STAFF
@subscription_required
def sales_invoice_share(request, pk):
    """
    Shareable invoice preview:
    - visible to OWNER + STAFF
    - shows watermark if not posted
    """
    invoice = tenant_get_object_or_404(
        request,
        SalesInvoice.objects.select_related("customer").prefetch_related("items__product"),
        pk=pk,
    )

    is_draft = not bool(getattr(invoice, "posted", False))

    # Totals
    total = Decimal(str(invoice.calculate_total() or 0))
    paid = Decimal(str(getattr(invoice, "payment_amount", 0) or 0))

    # This invoice due
    invoice_due = total - paid
    if invoice_due < 0:
        invoice_due = Decimal("0")

    # Previous due (BEFORE this invoice)
    #
    # IMPORTANT:
    # If customer.current_balance already INCLUDES this invoice, then we must subtract invoice_due.
    previous_due = Decimal("0")
    try:
        if hasattr(invoice.customer, "current_balance"):
            current_bal = Decimal(str(invoice.customer.current_balance or 0))
            previous_due = current_bal - invoice_due
        elif hasattr(invoice.customer, "get_balance"):
            current_bal = Decimal(str(invoice.customer.get_balance() or 0))
            previous_due = current_bal - invoice_due
        else:
            previous_due = Decimal("0")
    except Exception:
        previous_due = Decimal("0")

    if previous_due < 0:
        previous_due = Decimal("0")

    # Current due (after adding this invoice due)
    current_due = previous_due + invoice_due

    # Useful flags for template
    prof = getattr(request.user, "profile", None)
    role = getattr(prof, "role", None)
    is_staff_user = (role == "STAFF")

    # Ledger rows to keep screenshot practical
    raw_limit = request.GET.get("ledger", "20")
    try:
        ledger_limit = int(raw_limit)
    except (TypeError, ValueError):
        ledger_limit = 20
    ledger_limit = max(5, min(ledger_limit, 50))

    # Pull some extra rows so we can safely sort and still keep last N.
    # Keep bounded to avoid large payloads in screenshot page.
    fetch_limit = min(max(ledger_limit * 4, 80), 200)

    def _d(val):
        try:
            return Decimal(str(val or 0))
        except Exception:
            return Decimal("0")

    customer = invoice.customer
    owner = request.owner
    ledger_rows_raw = []

    # Sales (posted), plus current invoice explicitly marked as "this invoice"
    sales_qs = (
        SalesInvoice.objects.filter(owner=owner, customer=customer, posted=True)
        .exclude(pk=invoice.pk)
        .only("id", "invoice_date", "invoice_number")
        .order_by("-invoice_date", "-id")[:fetch_limit]
    )
    for inv in sales_qs:
        ledger_rows_raw.append(
            {
                "date": inv.invoice_date,
                "type": "Sale",
                "ref": inv.invoice_number or f"SI-{inv.id}",
                "debit": _d(inv.calculate_total()),
                "credit": Decimal("0"),
                "note": "",
                "is_this_invoice": False,
                "sort_pk": inv.id,
            }
        )

    ledger_rows_raw.append(
        {
            "date": invoice.invoice_date,
            "type": "Sale",
            "ref": invoice.invoice_number or f"SI-{invoice.id}",
            "debit": total,
            "credit": Decimal("0"),
            "note": "This invoice",
            "is_this_invoice": True,
            "sort_pk": invoice.id,
        }
    )

    # Customer payments / receipts (posted only)
    pay_qs = (
        Payment.objects.filter(owner=owner, party=customer, posted=True)
        .only(
            "id",
            "date",
            "direction",
            "amount",
            "description",
            "is_adjustment",
            "adjustment_side",
            "related_model",
            "related_id",
        )
        .order_by("-date", "-id")[:fetch_limit]
    )
    for p in pay_qs:
        debit = Decimal("0")
        credit = Decimal("0")

        if p.is_adjustment:
            side = (p.adjustment_side or "DR").upper()
            if side == "DR":
                debit = _d(p.amount)
            else:
                credit = _d(p.amount)
            row_type = "Adjustment"
            ref = f"ADJ-{p.id}"
        else:
            if p.direction == "IN":
                credit = _d(p.amount)
                row_type = "Receipt"
            else:
                debit = _d(p.amount)
                row_type = "Payment"
            ref = f"PAY-{p.id}"

        ledger_rows_raw.append(
            {
                "date": p.date,
                "type": row_type,
                "ref": ref,
                "debit": debit,
                "credit": credit,
                "note": p.description or "",
                "is_this_invoice": bool(
                    (not p.is_adjustment)
                    and p.related_model == "SalesInvoice"
                    and p.related_id == invoice.id
                ),
                "sort_pk": p.id,
            }
        )

    # Sales returns (posted only) reduce customer balance (credit)
    returns_qs = (
        SalesReturn.objects.filter(owner=owner, customer=customer, posted=True)
        .only("id", "return_date", "reference_invoice_id")
        .order_by("-return_date", "-id")[:fetch_limit]
    )
    for ret in returns_qs:
        ledger_rows_raw.append(
            {
                "date": ret.return_date,
                "type": "Return",
                "ref": f"SR-{ret.id}",
                "debit": Decimal("0"),
                "credit": _d(ret.calculate_total()),
                "note": (
                    f"Ref SI-{ret.reference_invoice_id}"
                    if ret.reference_invoice_id
                    else ""
                ),
                "is_this_invoice": bool(ret.reference_invoice_id == invoice.id),
                "sort_pk": ret.id,
            }
        )

    # Sort ascending and compute running balance from opening (previous due)
    ledger_rows_raw.sort(
        key=lambda r: (
            r.get("date") or timezone.now().date(),
            r.get("sort_pk") or 0,
        )
    )

    running_balance = previous_due
    for row in ledger_rows_raw:
        running_balance += (row["debit"] - row["credit"])
        row["running_balance"] = running_balance

    # Keep screenshot practical while centering window around this invoice row.
    total_rows = len(ledger_rows_raw)
    if total_rows <= ledger_limit:
        ledger_rows = ledger_rows_raw
    else:
        this_idx = None
        for i, r in enumerate(ledger_rows_raw):
            if r.get("type") == "Sale" and r.get("sort_pk") == invoice.id:
                this_idx = i
                break
        if this_idx is None:
            for i, r in enumerate(ledger_rows_raw):
                if r.get("is_this_invoice"):
                    this_idx = i
                    break
        if this_idx is None:
            this_idx = total_rows - 1

        half = ledger_limit // 2
        start = max(0, this_idx - half)
        end = start + ledger_limit
        if end > total_rows:
            end = total_rows
            start = max(0, end - ledger_limit)
        ledger_rows = ledger_rows_raw[start:end]

    # Optional last receipt date
    last_payment_date = (
        Payment.objects.filter(
            owner=owner,
            party=customer,
            posted=True,
            is_adjustment=False,
            direction="IN",
        )
        .order_by("-date", "-id")
        .values_list("date", flat=True)
        .first()
    )

    # Company branding (safe fallbacks)
    company = getattr(request, "company", None)
    if company is None:
        company = getattr(request.owner, "company", None)

    company_name = (
        getattr(company, "name", None)
        or getattr(company, "company_name", None)
        or getattr(request.owner, "name", None)
        or getattr(request.owner, "username", None)
        or "Roznamcha"
    )

    company_phone = (
        getattr(company, "phone", None)
        or getattr(company, "phone_number", None)
        or getattr(request.owner, "phone", None)
        or getattr(request.owner, "phone_number", None)
        or ""
    )

    context = {
        "invoice": invoice,
        "items": invoice.items.all(),
        "customer": invoice.customer,
        "total": total,
        "paid": paid,
        "invoice_due": invoice_due,
        "previous_due": previous_due,
        "current_due": current_due,
        "is_draft": is_draft,
        "is_staff_user": is_staff_user,
        "company_name": company_name,
        "company_phone": company_phone,
        "ledger_rows": ledger_rows,
        "ledger_limit": ledger_limit,
        "opening_balance": previous_due,
        "show_ledger_note": True,
        "last_payment_date": last_payment_date,
    }
    return render(request, "core/sales_invoice_share.html", context)


@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def sales_invoice_share_png(request, pk):
    # We intentionally generate PNG client-side (html2canvas) for Render compatibility.
    # Redirect users to the share page.
    return redirect(reverse("sales_invoice_share", args=[pk]))

@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def expenses_page(request):
    """
    One-page Daily Expenses:
    - Top: create expense (auto-post)
    - Filters: day / week / month or custom date range
    - Table: list existing expenses (latest first)
    """
    DAILY_EXPENSE_CODES = ["5200", "5210", "5220", "5230", "5240", "5250", "5290"]
    
    # Dropdowns
    cash_bank_accounts = (
        Account.objects.filter(
            owner=request.owner,
            is_cash_or_bank=True,
            allow_for_payments=True,
        )
        .order_by("code")
    )

    expense_heads = (
        Account.objects.filter(
            owner=request.owner,
            account_type="EXPENSE",
            code__in=DAILY_EXPENSE_CODES,
        )
        .order_by("code")
    )
    # Filters
    today = timezone.now().date()
    period = (request.GET.get("period") or "day").lower()  # day/week/month/custom
    from_str = request.GET.get("from_date") or ""
    to_str = request.GET.get("to_date") or ""

    date_from = None
    date_to = None

    if period == "week":
        date_from = today - timedelta(days=6)
        date_to = today
    elif period == "month":
        date_from = today.replace(day=1)
        date_to = today
    elif period == "custom":
        # custom range
        try:
            date_from = date.fromisoformat(from_str) if from_str else None
        except ValueError:
            date_from = None
        try:
            date_to = date.fromisoformat(to_str) if to_str else None
        except ValueError:
            date_to = None
    else:
        # day default
        date_from = today
        date_to = today

    qs = DailyExpense.objects.filter(owner=request.owner).order_by("-date", "-id")
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    total_amount = qs.aggregate(models.Sum("amount"))["amount__sum"] or Decimal("0")

    error = None

    # Create expense
    if request.method == "POST":
        date_str = request.POST.get("date") or ""
        paid_from_id = request.POST.get("paid_from") or ""
        expense_head_id = request.POST.get("expense_head") or ""
        amount_str = (request.POST.get("amount") or "0").strip()
        notes = (request.POST.get("notes") or "").strip()

        # date
        if date_str:
            try:
                exp_date = date.fromisoformat(date_str)
            except ValueError:
                exp_date = today
        else:
            exp_date = today

        # amount
        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, TypeError):
            amount = Decimal("0")

        if not paid_from_id:
            error = "Please select the cash/bank account."
        elif not expense_head_id:
            error = "Please select the expense head."
        elif amount <= 0:
            error = "Amount must be greater than zero."

        if not error:
            paid_from = tenant_get_object_or_404(
                request,
                Account,
                pk=paid_from_id,
                is_cash_or_bank=True,
                allow_for_payments=True,
            )
            expense_head = tenant_get_object_or_404(
                request,
                Account,
                pk=expense_head_id,
                account_type="EXPENSE",
                code__in=DAILY_EXPENSE_CODES,
            )
            kwargs = {
                "owner": request.owner,
                "date": exp_date,
                "paid_from": paid_from,
                "expense_head": expense_head,
                "amount": amount,
                "notes": notes,
                "posted": False,
            }
            kwargs = set_tenant_on_create_kwargs(request, kwargs, DailyExpense)

            with transaction.atomic():
                obj = DailyExpense.objects.create(**kwargs)
                obj.post()

            return redirect("expenses_page")

    context = {
        "cash_bank_accounts": cash_bank_accounts,
        "expense_heads": expense_heads,
        "expenses": qs,
        "total_amount": total_amount,
        "error": error,
        "today": today.isoformat(),
        "period": period,
        "from_date": date_from.isoformat() if date_from else "",
        "to_date": date_to.isoformat() if date_to else "",
    }
    return render(request, "core/expenses.html", context)

@login_required
@resolve_tenant_context(require_company=True)
@staff_allowed
@subscription_required
def cash_bank_transfer_page(request):
    """
    One-page Cash/Bank Transfer:
    - Top: create transfer (auto-post)
    - Filters: day/week/month/custom
    - Table: list transfers (latest first)
    """
    today = timezone.now().date()
    period = (request.GET.get("period") or "day").lower()
    from_str = request.GET.get("from_date") or ""
    to_str = request.GET.get("to_date") or ""

    date_from = None
    date_to = None

    if period == "week":
        date_from = today - timedelta(days=6)
        date_to = today
    elif period == "month":
        date_from = today.replace(day=1)
        date_to = today
    elif period == "custom":
        try:
            date_from = date.fromisoformat(from_str) if from_str else None
        except ValueError:
            date_from = None
        try:
            date_to = date.fromisoformat(to_str) if to_str else None
        except ValueError:
            date_to = None
    else:
        date_from = today
        date_to = today

    cash_bank_accounts = Account.objects.filter(
        owner=request.owner,
        is_cash_or_bank=True,
        allow_for_payments=True,
    ).order_by("code")

    qs = CashBankTransfer.objects.filter(owner=request.owner).order_by("-date", "-id")
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    total_amount = qs.aggregate(models.Sum("amount"))["amount__sum"] or Decimal("0")
    error = None

    if request.method == "POST":
        date_str = request.POST.get("date") or ""
        from_id = request.POST.get("from_account") or ""
        to_id = request.POST.get("to_account") or ""
        amount_str = (request.POST.get("amount") or "0").strip()
        notes = (request.POST.get("notes") or "").strip()

        try:
            tx_date = date.fromisoformat(date_str) if date_str else today
        except ValueError:
            tx_date = today

        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, TypeError):
            amount = Decimal("0")

        if not from_id:
            error = "Please select the From (Cash/Bank) account."
        elif not to_id:
            error = "Please select the To (Cash/Bank) account."
        elif from_id == to_id:
            error = "From and To accounts cannot be the same."
        elif amount <= 0:
            error = "Amount must be greater than zero."

        if not error:
            from_account = tenant_get_object_or_404(
                request,
                Account,
                pk=from_id,
                is_cash_or_bank=True,
                allow_for_payments=True,
            )
            to_account = tenant_get_object_or_404(
                request,
                Account,
                pk=to_id,
                is_cash_or_bank=True,
                allow_for_payments=True,
            )

            kwargs = {
                "owner": request.owner,
                "date": tx_date,
                "from_account": from_account,
                "to_account": to_account,
                "amount": amount,
                "notes": notes,
                "posted": False,
            }
            kwargs = set_tenant_on_create_kwargs(request, kwargs, CashBankTransfer)

            with transaction.atomic():
                obj = CashBankTransfer.objects.create(**kwargs)
                obj.post()

            return redirect("cash_bank_transfer_page")

    context = {
        "cash_bank_accounts": cash_bank_accounts,
        "transfers": qs,
        "total_amount": total_amount,
        "error": error,
        "today": today.isoformat(),
        "period": period,
        "from_date": date_from.isoformat() if date_from else "",
        "to_date": date_to.isoformat() if date_to else "",
    }
    return render(request, "core/cash_bank_transfer.html", context)

@require_GET
def run_backup_internal(request):
    key = request.GET.get("key")
    if key != settings.INTERNAL_BACKUP_KEY:
        return HttpResponseForbidden("Forbidden")

    call_command("backupdata", keep=3)
    return JsonResponse({"status": "backup completed"})
