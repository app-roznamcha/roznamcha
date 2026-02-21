from django.db import models
from django.utils import timezone
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.db import transaction
from django.core.exceptions import PermissionDenied
from datetime import timedelta
import re

# --------------------------
# Helpers (Design 1: owner = company)
# --------------------------

def get_company_owner(user: User) -> User:
    """
    If STAFF, return their owner. Otherwise user is the company owner (or superadmin).
    """
    if hasattr(user, "profile") and user.profile.role == "STAFF":
        if not user.profile.owner_id:
            raise ValueError("Staff user has no owner assigned.")
        return user.profile.owner
    return user


def get_company_account(*, owner, code, defaults=None):
    """
    Company-safe account getter/creator.
    Posting must NEVER use global accounts. Everything is per owner(company).
    """
    if owner is None:
        raise ValueError("Owner is required for account posting.")

    obj, _ = Account.objects.get_or_create(
        owner=owner,
        code=code,
        defaults=defaults or {},
    )
    return obj


class OwnerRequiredMixin(models.Model):
    """
    Ensures owner is present for all owner-scoped models.
    """

    def clean(self):
        super().clean()
        owner = getattr(self, "owner", None)

        if owner is None:
            raise ValidationError("Owner (company) must be set.")

        # ✅ SaaS: owner must be an OWNER or SUPERADMIN user
        if hasattr(owner, "profile"):
            if owner.profile.role not in ("OWNER", "SUPERADMIN"):
                raise ValidationError("Owner must be a company owner account.")

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """
    Abstract base: adds created_at / updated_at timestamps.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------
# Users / Roles (Design 1)
# --------------------------

class UserProfile(TimeStampedModel):
    ROLE_CHOICES = [
        ("SUPERADMIN", "Super Admin"),
        ("OWNER", "Owner"),
        ("STAFF", "Staff"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="STAFF")

    # If STAFF, must link to the business owner (company user)
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="staff_members",
        help_text="Only for STAFF: link to the company owner user.",
    )

    # Owner can disable staff accounts
    is_active = models.BooleanField(default=True)

    # --------------------------
    # SaaS Subscription (OWNER only)
    # --------------------------
    SUBSCRIPTION_STATUS_CHOICES = [
        ("TRIAL", "Trial"),
        ("ACTIVE", "Active"),
        ("EXPIRED", "Expired"),
    ]

    subscription_status = models.CharField(
        max_length=10,
        choices=SUBSCRIPTION_STATUS_CHOICES,
        default="TRIAL",
        help_text="Only meaningful for OWNER accounts. STAFF inherits OWNER subscription.",
    )

    trial_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trial started (set on first use / signup).",
    )

    subscription_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When subscription expires (OWNER only).",
    )

    TRIAL_DAYS = 15
    DORMANCY_DAYS_AFTER_EXPIRE = 60  # ~2 months

    
    def is_trial_active(self):
        if self.role != "OWNER":
            return False
        if not self.trial_started_at:
            return True  # treat as trial until initialized
        return timezone.now() < (self.trial_started_at + timedelta(days=self.TRIAL_DAYS))

    def get_effective_status(self):
        """
        Single source of truth for UI + enforcement.
        """
        if self.role == "SUPERADMIN":
            return "ACTIVE"  # superadmin always allowed

        if self.role == "STAFF":
            # staff inherits owner's subscription
            owner_profile = getattr(self.owner, "profile", None)
            return owner_profile.get_effective_status() if owner_profile else "EXPIRED"

        # OWNER logic
        now = timezone.now()

        if self.subscription_expires_at and self.subscription_expires_at > now:
            return "ACTIVE"

        if self.is_trial_active():
            return "TRIAL"

        return "EXPIRED"

    def get_effective_expires_at(self):
        """
        For display on Subscription page:
        - ACTIVE: subscription_expires_at
        - TRIAL: trial_started_at + 15 days
        - EXPIRED: whichever is relevant
        """
        if self.role != "OWNER":
            return None

        status = self.get_effective_status()
        if status == "ACTIVE":
            return self.subscription_expires_at

        if status == "TRIAL":
            if not self.trial_started_at:
                return timezone.now() + timedelta(days=self.TRIAL_DAYS)
            return self.trial_started_at + timedelta(days=self.TRIAL_DAYS)

        # EXPIRED
        # show trial end if no subscription ever existed, else show subscription expiry
        if self.subscription_expires_at:
            return self.subscription_expires_at
        if self.trial_started_at:
            return self.trial_started_at + timedelta(days=self.TRIAL_DAYS)
        return None

    def days_left(self):
        dt = self.get_effective_expires_at()
        if not dt:
            return None
        return (dt.date() - timezone.now().date()).days

    def clean(self):
        super().clean()

        # ---------------------------
        # Role rules
        # ---------------------------
        if self.role == "STAFF":
            if not self.owner_id:
                raise ValidationError("Staff must have an owner.")

            # Owner must be a real OWNER account
            owner_profile = getattr(self.owner, "profile", None)
            if not owner_profile or owner_profile.role != "OWNER":
                raise ValidationError("Staff owner must be an OWNER account.")

            # Max 3 staff per owner
            existing_staff = UserProfile.objects.filter(
                role="STAFF",
                owner=self.owner
            ).exclude(pk=self.pk)

            if existing_staff.count() >= 3:
                raise ValidationError("An owner can have a maximum of 3 staff members.")

        else:
            # OWNER / SUPERADMIN must NOT have an owner link
            if self.owner_id:
                raise ValidationError("Owner/SuperAdmin must not have an owner linked.")

        # ---------------------------
        # Subscription fields
        # ---------------------------
        # Only OWNER accounts can use subscription fields (meaningfully)
        if self.role != "OWNER":
            # Force a neutral state (you chose TRIAL as default)
            if self.subscription_status != "TRIAL":
                raise ValidationError("Only OWNER accounts can have subscription status.")
            if self.trial_started_at or self.subscription_expires_at:
                raise ValidationError("Only OWNER accounts can have trial/subscription dates.")
            
    def __str__(self):
        return f"{self.user.username} ({self.role})"

    class Meta:
        indexes = [
            models.Index(fields=["role"]),
            models.Index(fields=["is_active"]),
        ]


# --------------------------
# 1. Chart of Accounts
# --------------------------

class Account(OwnerRequiredMixin, TimeStampedModel):
    """
    Minimal chart-of-accounts entry (company-scoped).
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="accounts",
        help_text="Company owner (this user is the company).",
    )

    ACCOUNT_TYPE_CHOICES = [
        ("ASSET", "Asset"),
        ("LIABILITY", "Liability"),
        ("EQUITY", "Equity"),
        ("INCOME", "Income"),
        ("EXPENSE", "Expense"),
    ]

    code = models.CharField(max_length=10)
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPE_CHOICES)

    is_cash_or_bank = models.BooleanField(default=False)
    allow_for_payments = models.BooleanField(default=False)

    def clean(self):
        super().clean()
        # No behavior change — just basic field sanity
        if self.code:
            self.code = self.code.strip()
        if self.name:
            self.name = self.name.strip()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "code"],
                name="unique_account_code_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "code"]),
            models.Index(fields=["owner", "account_type"]),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"


# --------------------------
# 3. Products
# --------------------------

class Product(OwnerRequiredMixin, TimeStampedModel):
    """
    Core product/master item (company-scoped).
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="products",
        help_text="Company owner (this user is the company).",
    )

    UNIT_CHOICES = [
        ("BAG", "Bag"),
        ("KG", "Kilogram"),
        ("G", "Gram"),
        ("TON", "Tonne"),
        ("LITRE", "Litre"),
        ("ML", "Millilitre"),
        ("M", "Meter"),
        ("ROLL", "Roll"),
        ("PAIR", "Pair"),
        ("DOZEN", "Dozen"),
        ("UNIT", "Unit / Piece"),
    ]

    PACKING_CHOICES = [
        ("NONE", "No packing / loose"),
        ("CARTON", "Carton"),
        ("BAG", "Bag"),
        ("CONTAINER", "Container"),
        ("BUNDLE", "Bundle"),
        ("BOX", "Box"),
        ("BOTTLE", "Bottle"),
        ("CAN", "Can"),
        ("SACHET", "Sachet"),
        ("PACKET", "Packet / Pack"),
    ]

    code = models.CharField(max_length=30, help_text="Short code like UREA50, DAP50, WHEAT50, etc.")
    name = models.CharField(max_length=150, help_text="Full product name shown on invoices.")

    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default="BAG")
    packing_type = models.CharField(max_length=20, choices=PACKING_CHOICES, default="NONE")
    pieces_per_pack = models.DecimalField(max_digits=10, decimal_places=3, default=1)

    purchase_price_per_unit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sale_price_per_unit = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    current_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if self.code:
            self.code = self.code.strip()
        if self.name:
            self.name = self.name.strip()

    def __str__(self):
        return f"{self.code} - {self.name}"

    def adjust_stock(self, quantity_delta):
        if not isinstance(quantity_delta, Decimal):
            quantity_delta = Decimal(str(quantity_delta))

        base = self.current_stock or Decimal("0")
        self.current_stock = base + quantity_delta
        self.save(update_fields=["current_stock"])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "code"],
                name="unique_product_code_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "code"]),
            models.Index(fields=["owner", "is_active"]),
        ]


# --------------------------
# Parties
# --------------------------

class Party(OwnerRequiredMixin, TimeStampedModel):
    """
    Unified model for both customers and suppliers (company-scoped).
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="parties",
        help_text="Company owner (this user is the company).",
    )

    PARTY_TYPE_CHOICES = [
        ("CUSTOMER", "Customer"),
        ("SUPPLIER", "Supplier"),
    ]

    name = models.CharField(max_length=200)
    party_type = models.CharField(max_length=10, choices=PARTY_TYPE_CHOICES)

    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)

    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    opening_balance_is_debit = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if self.name:
            self.name = self.name.strip()

        # Do NOT change your logic; just prevent negative input silently
        if self.opening_balance is not None and self.opening_balance < 0:
            self.opening_balance = -self.opening_balance

    def __str__(self):
        return f"{self.name} ({self.get_party_type_display()})"

    def post_opening_balance(self):
        """
        Posts Party opening balance into ledger (idempotent).

        If CUSTOMER:
          Dr Customer Control (1300)  Cr Opening Balances (3000)   (if debit opening)
          Dr Opening Balances (3000)  Cr Customer Control (1300)   (if credit opening)

        If SUPPLIER:
          Dr Opening Balances (3000)  Cr Supplier Control (2100)   (if debit opening)
          Dr Supplier Control (2100)  Cr Opening Balances (3000)   (if credit opening)

        NOTE:
        - We treat customer's debit opening as receivable (asset).
        - Supplier's credit opening as payable (liability).
        """
        if self.opening_balance is None or self.opening_balance <= 0:
            return

        if self.owner is None:
            raise PermissionDenied("Owner not resolved for Party")

        control_code = "1300" if self.party_type == "CUSTOMER" else "2100"

        control_acct = get_company_account(
            owner=self.owner,
            code=control_code,
            defaults={
                "name": "Customer Control" if control_code == "1300" else "Supplier Control",
                "account_type": "ASSET" if control_code == "1300" else "LIABILITY",
                "is_cash_or_bank": False,
                "allow_for_payments": False,
            },
        )

        opening_acct = get_company_account(
            owner=self.owner,
            code="3000",
            defaults={
                "name": "Opening Balances",
                "account_type": "EQUITY",
                "is_cash_or_bank": False,
                "allow_for_payments": False,
            },
        )

        # Decide posting direction using opening_balance_is_debit
        is_debit = bool(self.opening_balance_is_debit)

        if self.party_type == "CUSTOMER":
            # Debit opening => Dr 1300 / Cr 3000
            # Credit opening => Dr 3000 / Cr 1300
            debit_account = control_acct if is_debit else opening_acct
            credit_account = opening_acct if is_debit else control_acct
        else:
            # SUPPLIER
            # Debit opening => Dr 3000 / Cr 2100
            # Credit opening => Dr 2100 / Cr 3000
            debit_account = opening_acct if is_debit else control_acct
            credit_account = control_acct if is_debit else opening_acct

        # Idempotent: one opening entry per party
        if JournalEntry.objects.filter(
            owner=self.owner,
            related_model="PartyOpening",
            related_id=self.id,
        ).exists():
            return

        JournalEntry.objects.create(
            owner=self.owner,
            date=timezone.now().date(),
            description=f"Opening balance - {self.name} ({self.party_type})",
            debit_account=debit_account,
            credit_account=credit_account,
            amount=self.opening_balance,
            related_model="PartyOpening",
            related_id=self.id,
        )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name", "party_type"],
                name="unique_party_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "party_type", "name"]),
            models.Index(fields=["owner", "is_active"]),
        ]
# --------------------------
# Journal Entry
# --------------------------

class JournalEntry(OwnerRequiredMixin, TimeStampedModel):
    """
    Single-line journal entry (company-scoped).
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="journal_entries",
        help_text="Company owner (this user is the company).",
    )

    date = models.DateField(default=timezone.now)
    description = models.CharField(max_length=255, blank=True)

    debit_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_debits")
    credit_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_credits")
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    related_model = models.CharField(max_length=50, blank=True)
    related_id = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.date} | {self.debit_account} Dr {self.amount} / {self.credit_account} Cr {self.amount}"

    def clean(self):
        super().clean()

        # ✅ SaaS: accounts must belong to same owner
        if self.owner_id and self.debit_account_id and self.debit_account.owner_id != self.owner_id:
            raise ValidationError("Debit account does not belong to this owner.")
        if self.owner_id and self.credit_account_id and self.credit_account.owner_id != self.owner_id:
            raise ValidationError("Credit account does not belong to this owner.")

        # ✅ Basic sanity (no change in your posting logic; just stops bad data)
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Journal amount must be greater than zero.")

    class Meta:
        constraints = [
            # ✅ One entry per doc forever (you said YES)
            models.UniqueConstraint(
                fields=["owner", "related_model", "related_id"],
                name="uniq_journal_per_doc",
            )
        ]
        indexes = [
            models.Index(fields=["owner", "date", "id"]),
            models.Index(fields=["owner", "related_model", "related_id"]),
        ]
# --------------------------
# Payments
# --------------------------

class Payment(OwnerRequiredMixin, TimeStampedModel):
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="payments",
        help_text="Company owner (this user is the company).",
    )

    DIRECTION_CHOICES = [
        ("IN", "Receipt (money in)"),
        ("OUT", "Payment (money out)"),
    ]

    date = models.DateField(default=timezone.now)

    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        help_text="Customer / Supplier / other counterparty.",
    )

    # Keep nullable (important for adjustments)
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        limit_choices_to={"is_cash_or_bank": True},
        null=True,
        blank=True,
        help_text="Cash or bank account used for this transaction.",
    )

    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, default="IN")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    posted = models.BooleanField(default=False)

    related_model = models.CharField(max_length=50, blank=True)
    related_id = models.PositiveIntegerField(null=True, blank=True)

    # Adjustment flags
    is_adjustment = models.BooleanField(default=False)
    adjustment_side = models.CharField(
        max_length=2,
        choices=[("DR", "Dr"), ("CR", "Cr")],
        blank=True,
        help_text="For adjustments only: DR or CR",
    )

    def __str__(self):
        if self.is_adjustment:
            return f"{self.date} | ADJ {self.adjustment_side} {self.amount} | {self.party}"
        return f"{self.date} | {self.direction} {self.amount} via {self.account}"

    def clean(self):
        super().clean()

        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Amount must be greater than zero.")

        # ✅ SaaS: party/account must belong to same owner
        if self.owner_id and self.party_id and self.party.owner_id != self.owner_id:
            raise ValidationError("Party does not belong to this owner.")
        if self.owner_id and self.account_id and self.account.owner_id != self.owner_id:
            raise ValidationError("Account does not belong to this owner.")

        if self.is_adjustment:
            if not self.party:
                raise ValidationError("Adjustment requires a party.")
            if self.adjustment_side not in ("DR", "CR"):
                raise ValidationError("Adjustment side must be DR or CR.")
            # ✅ Adjustments should not require a cash/bank account (your logic depends on this)
        else:
            if not self.account_id:
                raise ValidationError("Payment requires a cash/bank account.")
            if self.direction not in ("IN", "OUT"):
                raise ValidationError("Direction must be IN or OUT.")

    def post(self):
        """
        Normal Payment:
        IN : Dr Cash/Bank  Cr Customer(1300) or Supplier(2100)
        OUT: Dr Customer(1300) or Supplier(2100)  Cr Cash/Bank

        Adjustment (no cash/bank):
        DR: Dr Control(1300/2100)  Cr Opening Balances(3000)
        CR: Dr Opening Balances(3000)  Cr Control(1300/2100)
        """
        if self.posted or self.amount <= 0:
            return

        # ✅ SaaS hard stop (protects even if someone creates Payment wrongly)
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for Payment")

        with transaction.atomic():
            if not self.party:
                raise ValueError("Cannot post payment/adjustment without a party.")

            # ✅ SaaS safety: party must belong to same owner at posting time too
            if self.party.owner_id != self.owner_id:
                raise PermissionDenied("Cross-owner party detected.")

            if self.party.party_type == "CUSTOMER":
                control_code = "1300"
            elif self.party.party_type == "SUPPLIER":
                control_code = "2100"
            else:
                raise ValueError("Party must be CUSTOMER or SUPPLIER.")

            # ✅ Tenant-safe: create per-owner control accounts if missing
            if control_code == "1300":
                control_account = get_company_account(
                    owner=self.owner,
                    code="1300",
                    defaults={
                        "name": "Customer Control",
                        "account_type": "ASSET",
                        "is_cash_or_bank": False,
                        "allow_for_payments": False,
                    },
                )
            else:  # "2100"
                control_account = get_company_account(
                    owner=self.owner,
                    code="2100",
                    defaults={
                        "name": "Supplier Control",
                        "account_type": "LIABILITY",
                        "is_cash_or_bank": False,
                        "allow_for_payments": False,
                    },
                )

            opening_acct = get_company_account(
                owner=self.owner,
                code="3000",
                defaults={
                    "name": "Opening Balances",
                    "account_type": "EQUITY",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            # Adjustment posting (no cash/bank needed)
            if self.is_adjustment:
                side = (self.adjustment_side or "DR").upper()
                if side not in ("DR", "CR"):
                    side = "DR"

                if side == "DR":
                    debit_account = control_account
                    credit_account = opening_acct
                else:
                    debit_account = opening_acct
                    credit_account = control_account

                if not JournalEntry.objects.filter(
                    owner=self.owner,
                    related_model="Payment",
                    related_id=self.id,
                ).exists():
                    JournalEntry.objects.create(
                        owner=self.owner,
                        date=self.date,
                        description=self.description or f"Payment {self.id}",
                        debit_account=debit_account,
                        credit_account=credit_account,
                        amount=self.amount,
                        related_model="Payment",
                        related_id=self.id,
                    )

                self.posted = True
                self.save(update_fields=["posted"])
                return

            # Normal payment posting
            if not self.account:
                raise ValueError("Normal payment requires a cash/bank account.")

            # ✅ SaaS safety: account must belong to owner
            if self.account.owner_id != self.owner_id:
                raise PermissionDenied("Cross-owner cash/bank account detected.")

            cash_bank_account = self.account

            if self.direction == "IN":
                debit_account = cash_bank_account
                credit_account = control_account
            else:
                debit_account = control_account
                credit_account = cash_bank_account

            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="Payment",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.date,
                    description=self.description or f"Payment {self.id}",
                    debit_account=debit_account,
                    credit_account=credit_account,
                    amount=self.amount,
                    related_model="Payment",
                    related_id=self.id,
                )

            self.posted = True
            self.save(update_fields=["posted"])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "related_model", "related_id"],
                name="unique_payment_per_document_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "date", "id"]),
            models.Index(fields=["owner", "posted"]),
            models.Index(fields=["owner", "related_model", "related_id"]),
        ]


# --------------------------
# Daily Expenses
# --------------------------

class DailyExpense(OwnerRequiredMixin, TimeStampedModel):
    """
    Simple daily expense entry (company-scoped), posted immediately.

    Journal when posted:
      Dr Expense Head (EXPENSE)
      Cr Cash/Bank account
    """

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="daily_expenses",
        help_text="Company owner (this user is the company).",
    )

    date = models.DateField(default=timezone.now)

    paid_from = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="expense_paid_from",
        limit_choices_to={"is_cash_or_bank": True, "allow_for_payments": True},
        help_text="Cash/Bank account used to pay the expense.",
    )

    expense_head = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="expense_heads_used",
        limit_choices_to={"account_type": "EXPENSE"},
        help_text="Expense account head (e.g., Wages, Food, Fuel).",
    )

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True)
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.date} | Expense {self.amount} from {self.paid_from} ({'POSTED' if self.posted else 'DRAFT'})"

    def clean(self):
        super().clean()

        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Amount must be greater than zero.")

        # ✅ SaaS: paid_from & expense_head must belong to same owner
        if self.owner_id and self.paid_from_id and self.paid_from.owner_id != self.owner_id:
            raise ValidationError("Paid-from account does not belong to this owner.")
        if self.owner_id and self.expense_head_id and self.expense_head.owner_id != self.owner_id:
            raise ValidationError("Expense head does not belong to this owner.")

        # ✅ Enforce types (defense-in-depth)
        if self.paid_from_id:
            if not self.paid_from.is_cash_or_bank or not self.paid_from.allow_for_payments:
                raise ValidationError("Paid-from account must be a Cash/Bank account allowed for payments.")

        if self.expense_head_id:
            if self.expense_head.account_type != "EXPENSE":
                raise ValidationError("Expense head must be an EXPENSE-type account.")

        # 🔒 Prevent editing after posting (accounting safety)
        if self.pk:
            old = DailyExpense.objects.filter(pk=self.pk).first()
            if old and old.posted:
                raise ValidationError("Posted expenses cannot be modified.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def post(self):
        """
        Create JournalEntry (idempotent):
          Dr expense_head
          Cr paid_from
        """
        if self.posted or self.amount <= 0:
            return

        if self.owner is None:
            raise PermissionDenied("Owner not resolved for DailyExpense")

        # ✅ SaaS defense-in-depth at posting time
        if self.paid_from and self.paid_from.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner cash/bank account detected.")
        if self.expense_head and self.expense_head.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner expense head detected.")

        with transaction.atomic():
            locked = DailyExpense.objects.select_for_update().get(pk=self.pk)
            if locked.posted:
                return

            # 🔒 Idempotent JournalEntry
            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="DailyExpense",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.date,
                    description=self.notes or f"Daily Expense {self.id}",
                    debit_account=self.expense_head,
                    credit_account=self.paid_from,
                    amount=self.amount,
                    related_model="DailyExpense",
                    related_id=self.id,
                )

            locked.posted = True
            locked.save(update_fields=["posted"])
            self.posted = True

    class Meta:
        indexes = [
            models.Index(fields=["owner", "date", "id"]),
            models.Index(fields=["owner", "posted"]),
            models.Index(fields=["owner", "expense_head"]),
            models.Index(fields=["owner", "paid_from"]),
        ]
# --------------------------
# Sales Invoice (Header only in Part-1)
# --------------------------

class SalesInvoice(OwnerRequiredMixin, TimeStampedModel):
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sales_invoices",
        help_text="Company owner (this user is the company).",
    )

    PAYMENT_TYPE_CHOICES = [
        ("CREDIT", "Credit (customer will pay later)"),
        ("FULL", "Full payment (cash/bank)"),
        ("PARTIAL", "Partial payment"),
    ]

    customer = models.ForeignKey("Party", on_delete=models.PROTECT, related_name="sales_invoices")
    invoice_number = models.CharField(max_length=50, blank=True)
    invoice_date = models.DateField()
    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False)

    payment_type = models.CharField(max_length=10, choices=PAYMENT_TYPE_CHOICES, default="CREDIT")
    payment_account = models.ForeignKey(
        "Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales_payment_invoices",
    )
    payment_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    def __str__(self):
        return f"Sale {self.id} - {self.customer.name} ({self.invoice_date})"

    def clean(self):
        super().clean()

        # ✅ SaaS: customer/payment_account must belong to owner
        if self.owner_id and self.customer_id and self.customer.owner_id != self.owner_id:
            raise ValidationError("Customer does not belong to this owner.")
        if self.owner_id and self.payment_account_id and self.payment_account.owner_id != self.owner_id:
            raise ValidationError("Payment account does not belong to this owner.")

    def calculate_total(self) -> Decimal:
        total = Decimal("0")
        for item in self.items.all():
            total += item.line_total
        return total

    def post(self):

        # 🔒 Hard stop: never post twice
        if self.posted:
            return

        # 🔒 SaaS safety: tenant/owner must exist
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for SalesInvoice")

        # ✅ Extra SaaS safety at posting time too
        if self.customer and self.customer.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner customer detected.")

        if self.payment_account and self.payment_account.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner payment account detected.")

        with transaction.atomic():
            total = self.calculate_total()
            if total <= 0:
                return

            # 🔒 Tenant-safe control accounts
            customer_control = get_company_account(
                owner=self.owner,
                code="1300",
                defaults={
                    "name": "Customer Control",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )
            inventory_acct = get_company_account(
                owner=self.owner,
                code="1200",
                defaults={
                    "name": "Inventory",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            # 🔒 Prevent duplicate journal entry
            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="SalesInvoice",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.invoice_date,
                    description=f"Sales Invoice {self.id}",
                    debit_account=customer_control,
                    credit_account=inventory_acct,
                    amount=total,
                    related_model="SalesInvoice",
                    related_id=self.id,
                )

            # Stock movement (unchanged)
            for item in self.items.select_related("product"):
                qty = item.quantity_units or Decimal("0")
                if qty > 0:
                    item.product.adjust_stock(-qty)

            self.posted = True
            self.save(update_fields=["posted"])

            # 🔒 Payment creation (idempotent)
            if (
                self.payment_type in ("FULL", "PARTIAL")
                and self.payment_amount
                and self.payment_amount > 0
                and self.payment_account is not None
            ):
                if not Payment.objects.filter(
                    owner=self.owner,
                    related_model="SalesInvoice",
                    related_id=self.id,
                    direction="IN",
                ).exists():
                    payment = Payment.objects.create(
                        owner=self.owner,
                        date=self.invoice_date,
                        party=self.customer,
                        account=self.payment_account,
                        direction="IN",
                        amount=self.payment_amount,
                        description=f"Payment for Sales Invoice {self.id}",
                        posted=False,
                        related_model="SalesInvoice",
                        related_id=self.id,
                    )
                    payment.post()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "invoice_number"],
                name="unique_sales_invoice_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "invoice_date", "id"]),
            models.Index(fields=["owner", "posted"]),
            models.Index(fields=["owner", "invoice_number"]),
        ]


class SalesInvoiceItem(OwnerRequiredMixin, TimeStampedModel):
    """
    Line item for SalesInvoice.
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sales_invoice_items",
        help_text="Company owner (this user is the company).",
    )
    sales_invoice = models.ForeignKey(
        SalesInvoice,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "Product",
        on_delete=models.PROTECT,
        related_name="sales_items",
    )

    unit_type = models.CharField(
        max_length=10,
        choices=Product.UNIT_CHOICES,
        default="BAG",
    )
    quantity_units = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        help_text="Quantity in base units (bag/kg/litre/unit).",
    )
    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="Sale price per unit for this invoice.",
    )
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Per-line discount (flat amount, not %).",
    )

    @property
    def line_total(self) -> Decimal:
        qty = self.quantity_units or Decimal("0")
        price = self.unit_price or Decimal("0")
        disc = self.discount_amount or Decimal("0")
        return (qty * price) - disc

    def __str__(self):
        return f"{self.product.code} x {self.quantity_units} on Sale {self.sales_invoice_id}"

    def clean(self):
        super().clean()

        # ✅ SaaS: item owner must match invoice + product
        if self.owner_id and self.sales_invoice_id and self.sales_invoice.owner_id != self.owner_id:
            raise ValidationError("SalesInvoice does not belong to this owner.")
        if self.owner_id and self.product_id and self.product.owner_id != self.owner_id:
            raise ValidationError("Product does not belong to this owner.")

        # ✅ Extra: invoice and product must be from same owner (defense-in-depth)
        if self.sales_invoice_id and self.product_id:
            if self.sales_invoice.owner_id != self.product.owner_id:
                raise ValidationError("Cross-owner invoice/product detected.")

    class Meta:
        indexes = [
            models.Index(fields=["owner", "sales_invoice"]),
            models.Index(fields=["owner", "product"]),
        ]

    
class PurchaseInvoice(OwnerRequiredMixin, TimeStampedModel):
    """
    Purchase invoice (header).

    - supplier: Party with party_type='SUPPLIER'
    - posted: when True, we have:
        * created JournalEntry
        * increased product stock
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="purchase_invoices",
        help_text="Company owner (this user is the company).",
    )
    supplier = models.ForeignKey(
        "Party",
        on_delete=models.PROTECT,
        related_name="purchase_invoices",
    )
    invoice_number = models.CharField(
        max_length=50,
        blank=True,
        help_text="Supplier bill number (optional).",
    )
    invoice_date = models.DateField()
    date_received = models.DateField(
        null=True,
        blank=True,
        help_text="Optional: physical goods received date.",
    )

    freight_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    other_charges = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.TextField(blank=True)

    posted = models.BooleanField(default=False)

    PAYMENT_TYPE_CHOICES = [
        ("CREDIT", "Credit (we will pay later)"),
        ("FULL", "Full payment (cash/bank)"),
        ("PARTIAL", "Partial payment"),
    ]

    payment_type = models.CharField(max_length=10, choices=PAYMENT_TYPE_CHOICES, default="CREDIT")
    payment_account = models.ForeignKey(
        "Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_payment_invoices",
        help_text="Cash/Bank account if payment made at time of purchase.",
    )
    payment_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Amount actually paid at time of purchase (0 for pure credit).",
    )

    def __str__(self):
        return f"Purchase {self.id} - {self.supplier.name} ({self.invoice_date})"

    def clean(self):
        super().clean()

        # ✅ SaaS: supplier/payment_account must belong to same owner
        if self.owner_id and self.supplier_id and self.supplier.owner_id != self.owner_id:
            raise ValidationError("Supplier does not belong to this owner.")
        if self.owner_id and self.payment_account_id and self.payment_account.owner_id != self.owner_id:
            raise ValidationError("Payment account does not belong to this owner.")

    def calculate_total(self) -> Decimal:
        """
        Total = sum(line totals) + freight + other charges.
        """
        items_total = Decimal("0")
        for item in self.items.all():
            items_total += item.line_total
        total = items_total + (self.freight_charges or 0) + (self.other_charges or 0)
        return total

    def post(self):
        """
        Post this purchase:

        Dr Inventory (1200)
        Cr Supplier Control (2100)
        Amount = invoice total

        And increase stock of each product.

        If payment_type is FULL or PARTIAL and payment_amount > 0,
        auto-create a Payment (direction=OUT) and post it.
        """

        if self.posted:
            return

        # 🔒 Safety: must have owner
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for PurchaseInvoice")

        # ✅ SaaS defense-in-depth (posting time)
        if self.supplier and self.supplier.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner supplier detected.")
        if self.payment_account and self.payment_account.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner payment account detected.")

        with transaction.atomic():
            total = self.calculate_total()
            if total <= 0:
                return

            inventory_acct = get_company_account(
                owner=self.owner,
                code="1200",
                defaults={
                    "name": "Inventory",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            supplier_control = get_company_account(
                owner=self.owner,
                code="2100",
                defaults={
                    "name": "Supplier Control",
                    "account_type": "LIABILITY",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            # 🔒 Idempotent main purchase journal
            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="PurchaseInvoice",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.invoice_date,
                    description=f"Purchase Invoice {self.id}",
                    debit_account=inventory_acct,
                    credit_account=supplier_control,
                    amount=total,
                    related_model="PurchaseInvoice",
                    related_id=self.id,
                )

            # Increase stock (unchanged)
            for item in self.items.select_related("product"):
                qty = item.quantity_units or Decimal("0")
                if qty > 0:
                    # ✅ Optional extra guard (doesn't change logic)
                    if item.product.owner_id != self.owner_id:
                        raise PermissionDenied("Cross-owner product detected in purchase items.")
                    item.product.adjust_stock(qty)

            # Auto-create payment for FULL / PARTIAL (idempotent)
            if (
                self.payment_type in ("FULL", "PARTIAL")
                and self.payment_amount
                and self.payment_amount > 0
                and self.payment_account
            ):
                if not Payment.objects.filter(
                    owner=self.owner,
                    related_model="PurchaseInvoice",
                    related_id=self.id,
                    direction="OUT",
                ).exists():
                    payment = Payment.objects.create(
                        owner=self.owner,
                        date=self.invoice_date,
                        party=self.supplier,
                        account=self.payment_account,
                        direction="OUT",
                        amount=self.payment_amount,
                        description=f"Payment for Purchase Invoice {self.id}",
                        posted=False,
                        related_model="PurchaseInvoice",
                        related_id=self.id,
                    )
                    payment.post()

            self.posted = True
            self.save(update_fields=["posted"])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "invoice_number"],
                name="unique_purchase_invoice_per_owner"
            )
        ]
        indexes = [
            models.Index(fields=["owner", "invoice_date", "id"]),
            models.Index(fields=["owner", "posted"]),
            models.Index(fields=["owner", "invoice_number"]),
        ]

class PurchaseInvoiceItem(OwnerRequiredMixin, TimeStampedModel):
    """
    Line item for PurchaseInvoice.
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="purchase_invoice_items",
        help_text="Company owner (this user is the company).",
    )
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "Product",
        on_delete=models.PROTECT,
        related_name="purchase_items",
    )

    unit_type = models.CharField(
        max_length=10,
        choices=Product.UNIT_CHOICES,
        default="BAG",
    )
    quantity_units = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        help_text="Quantity in base units (bag/kg/litre/unit).",
    )
    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        help_text="Purchase price per unit for this invoice.",
    )
    discount_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="Per-line discount (flat amount, not %).",
    )

    @property
    def line_total(self) -> Decimal:
        qty = self.quantity_units or Decimal("0")
        price = self.unit_price or Decimal("0")
        disc = self.discount_amount or Decimal("0")
        return (qty * price) - disc

    def __str__(self):
        return f"{self.product.code} x {self.quantity_units} on Purchase {self.purchase_invoice_id}"

    def clean(self):
        super().clean()

        # ✅ SaaS: item owner must match invoice + product
        if self.owner_id and self.purchase_invoice_id and self.purchase_invoice.owner_id != self.owner_id:
            raise ValidationError("PurchaseInvoice does not belong to this owner.")
        if self.owner_id and self.product_id and self.product.owner_id != self.owner_id:
            raise ValidationError("Product does not belong to this owner.")

        # ✅ Defense-in-depth: invoice and product must be same owner
        if self.purchase_invoice_id and self.product_id:
            if self.purchase_invoice.owner_id != self.product.owner_id:
                raise ValidationError("Cross-owner purchase invoice/product detected.")

    class Meta:
        indexes = [
            models.Index(fields=["owner", "purchase_invoice"]),
            models.Index(fields=["owner", "product"]),
        ]


class SalesReturn(OwnerRequiredMixin, TimeStampedModel):
    """
    Sales Return (customer sends goods back).

    Journal when posted:
      Dr Inventory (1200)
      Cr Customer Control (1300)
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sales_returns",
        help_text="Company owner (this user is the company).",
    )
    customer = models.ForeignKey(
        "Party",
        on_delete=models.PROTECT,
        related_name="sales_returns",
    )
    return_date = models.DateField(default=timezone.now)

    reference_invoice = models.ForeignKey(
        "SalesInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns",
    )

    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"Sales Return {self.id} - {self.customer.name} ({self.return_date})"

    def clean(self):
        super().clean()

        # ✅ SaaS: customer must belong to owner
        if self.owner_id and self.customer_id and self.customer.owner_id != self.owner_id:
            raise ValidationError("Customer does not belong to this owner.")

        # ✅ SaaS: if reference invoice exists, it must belong to owner
        if self.owner_id and self.reference_invoice_id and self.reference_invoice.owner_id != self.owner_id:
            raise ValidationError("Reference invoice does not belong to this owner.")

    def calculate_total(self) -> Decimal:
        total = Decimal("0")
        for item in self.items.all():
            total += item.line_total
        return total

    def post(self):
        if self.posted:
            return

        # ✅ SaaS hard stop
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for SalesReturn")
        if self.customer and self.customer.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner customer detected.")
        if self.reference_invoice and self.reference_invoice.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner reference invoice detected.")

        with transaction.atomic():
            total = self.calculate_total()
            if total <= 0:
                return

            inventory_acct = get_company_account(
                owner=self.owner,
                code="1200",
                defaults={
                    "name": "Inventory",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            customer_control = get_company_account(
                owner=self.owner,
                code="1300",
                defaults={
                    "name": "Customer Control",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="SalesReturn",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.return_date,
                    description=f"Sales Return {self.id}",
                    debit_account=inventory_acct,
                    credit_account=customer_control,
                    amount=total,
                    related_model="SalesReturn",
                    related_id=self.id,
                )

            # Stock goes UP
            for item in self.items.select_related("product"):
                qty = item.quantity_units or Decimal("0")
                if qty > 0:
                    if item.product.owner_id != self.owner_id:
                        raise PermissionDenied("Cross-owner product detected in sales return items.")
                    item.product.adjust_stock(qty)

            self.posted = True
            self.save(update_fields=["posted"])

    class Meta:
        indexes = [
            models.Index(fields=["owner", "return_date", "id"]),
            models.Index(fields=["owner", "posted"]),
        ]


class SalesReturnItem(OwnerRequiredMixin, TimeStampedModel):
    """
    Line item for SalesReturn.
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sales_return_items",
        help_text="Company owner (this user is the company).",
    )
    sales_return = models.ForeignKey(SalesReturn, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="sales_return_items")

    unit_type = models.CharField(max_length=10, choices=Product.UNIT_CHOICES, default="BAG")
    quantity_units = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    @property
    def line_total(self) -> Decimal:
        qty = self.quantity_units or Decimal("0")
        price = self.unit_price or Decimal("0")
        disc = self.discount_amount or Decimal("0")
        return (qty * price) - disc

    def __str__(self):
        return f"{self.product.code} x {self.quantity_units} on Sales Return {self.sales_return_id}"

    def clean(self):
        super().clean()

        if self.owner_id and self.sales_return_id and self.sales_return.owner_id != self.owner_id:
            raise ValidationError("SalesReturn does not belong to this owner.")
        if self.owner_id and self.product_id and self.product.owner_id != self.owner_id:
            raise ValidationError("Product does not belong to this owner.")

        if self.sales_return_id and self.product_id:
            if self.sales_return.owner_id != self.product.owner_id:
                raise ValidationError("Cross-owner sales return/product detected.")

    class Meta:
        indexes = [
            models.Index(fields=["owner", "sales_return"]),
            models.Index(fields=["owner", "product"]),
        ]

class PurchaseReturn(OwnerRequiredMixin, TimeStampedModel):
    """
    Purchase Return (we send goods back to supplier).

    Journal when posted:
      Dr Supplier Control (2100)
      Cr Inventory (1200)
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="purchase_returns",
        help_text="Company owner (this user is the company).",
    )
    supplier = models.ForeignKey("Party", on_delete=models.PROTECT, related_name="purchase_returns")
    return_date = models.DateField(default=timezone.now)

    reference_invoice = models.ForeignKey(
        "PurchaseInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns",
    )

    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"Purchase Return {self.id} - {self.supplier.name} ({self.return_date})"

    def clean(self):
        super().clean()

        if self.owner_id and self.supplier_id and self.supplier.owner_id != self.owner_id:
            raise ValidationError("Supplier does not belong to this owner.")
        if self.owner_id and self.reference_invoice_id and self.reference_invoice.owner_id != self.owner_id:
            raise ValidationError("Reference purchase invoice does not belong to this owner.")

    def calculate_total(self) -> Decimal:
        total = Decimal("0")
        for item in self.items.all():
            total += item.line_total
        return total

    def post(self):
        if self.posted:
            return

        # ✅ SaaS hard stop
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for PurchaseReturn")
        if self.supplier and self.supplier.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner supplier detected.")
        if self.reference_invoice and self.reference_invoice.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner reference invoice detected.")

        with transaction.atomic():
            total = self.calculate_total()
            if total <= 0:
                return

            inventory_acct = get_company_account(
                owner=self.owner,
                code="1200",
                defaults={
                    "name": "Inventory",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            supplier_control = get_company_account(
                owner=self.owner,
                code="2100",
                defaults={
                    "name": "Supplier Control",
                    "account_type": "LIABILITY",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="PurchaseReturn",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.return_date,
                    description=f"Purchase Return {self.id}",
                    debit_account=supplier_control,
                    credit_account=inventory_acct,
                    amount=total,
                    related_model="PurchaseReturn",
                    related_id=self.id,
                )

            # Stock goes DOWN
            for item in self.items.select_related("product"):
                qty = item.quantity_units or Decimal("0")
                if qty > 0:
                    if item.product.owner_id != self.owner_id:
                        raise PermissionDenied("Cross-owner product detected in purchase return items.")
                    item.product.adjust_stock(-qty)

            self.posted = True
            self.save(update_fields=["posted"])

    class Meta:
        indexes = [
            models.Index(fields=["owner", "return_date", "id"]),
            models.Index(fields=["owner", "posted"]),
        ]


class PurchaseReturnItem(OwnerRequiredMixin, TimeStampedModel):
    """
    Line item for PurchaseReturn.
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="purchase_return_items",
        help_text="Company owner (this user is the company).",
    )
    purchase_return = models.ForeignKey(PurchaseReturn, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="purchase_return_items")

    unit_type = models.CharField(max_length=10, choices=Product.UNIT_CHOICES, default="BAG")
    quantity_units = models.DecimalField(max_digits=14, decimal_places=3)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    @property
    def line_total(self) -> Decimal:
        qty = self.quantity_units or Decimal("0")
        price = self.unit_price or Decimal("0")
        disc = self.discount_amount or Decimal("0")
        return (qty * price) - disc

    def __str__(self):
        return f"{self.product.code} x {self.quantity_units} on Purchase Return {self.purchase_return_id}"

    def clean(self):
        super().clean()

        # ✅ SaaS: enforce same-owner relationships
        if self.owner_id and self.purchase_return_id and self.purchase_return.owner_id != self.owner_id:
            raise ValidationError("PurchaseReturn does not belong to this owner.")
        if self.owner_id and self.product_id and self.product.owner_id != self.owner_id:
            raise ValidationError("Product does not belong to this owner.")

        # ✅ Defense-in-depth: purchase_return and product must match owners
        if self.purchase_return_id and self.product_id:
            if self.purchase_return.owner_id != self.product.owner_id:
                raise ValidationError("Cross-owner purchase return/product detected.")

    class Meta:
        indexes = [
            models.Index(fields=["owner", "purchase_return"]),
            models.Index(fields=["owner", "product"]),
        ]

class StockAdjustment(OwnerRequiredMixin, TimeStampedModel):
    """
    Stock adjustment with accounting impact.

    direction:
      - DOWN: damaged/expired/loss -> Dr Expense, Cr Inventory
      - UP:   correction increase  -> Dr Inventory, Cr Opening Balances(3000)
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="stock_adjustments",
        help_text="Company owner (this user is the company).",
    )

    DIRECTION_CHOICES = [
        ("DOWN", "Decrease (damage/expired/loss)"),
        ("UP", "Increase (correction/add stock)"),
    ]

    date = models.DateField(default=timezone.now)
    product = models.ForeignKey("Product", on_delete=models.PROTECT)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)

    qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    reason = models.CharField(max_length=255, blank=True)
    posted = models.BooleanField(default=False)

    def __str__(self):
        sign = "-" if self.direction == "DOWN" else "+"
        return f"{self.date} | {self.product} | {sign}{self.qty} @ {self.unit_cost}"

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
    
    def clean(self):
        super().clean()

        # ✅ SaaS: product must belong to same owner
        if self.owner_id and self.product_id and self.product.owner_id != self.owner_id:
            raise ValidationError("Product does not belong to this owner.")

        # 🔒 Prevent editing after posting
        if self.pk:
            old = StockAdjustment.objects.filter(pk=self.pk).first()
            if old and old.posted:
                raise ValidationError("Posted stock adjustments cannot be modified.")

    @property
    def amount(self):
        try:
            return (self.qty or Decimal("0")) * (self.unit_cost or Decimal("0"))
        except Exception:
            return Decimal("0")

    def post(self):
        """
        Creates JournalEntry:
        DOWN: Dr Inventory Write-off (Expense 5100), Cr Inventory (1200)
        UP:   Dr Inventory (1200), Cr Opening Balances (3000)

        ALSO updates Product.current_stock (single source of truth):
        DOWN -> subtract qty
        UP   -> add qty
        """
        if self.posted or self.amount <= 0:
            return

        # ✅ SaaS hard stop
        if self.owner is None:
            raise PermissionDenied("Owner not resolved for StockAdjustment")
        if self.product and self.product.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner product detected in StockAdjustment")

        with transaction.atomic():
            # Lock this row to prevent double-posts in race conditions
            locked = StockAdjustment.objects.select_for_update().get(pk=self.pk)
            if locked.posted:
                return

            inventory_acct = get_company_account(
                owner=self.owner,
                code="1200",
                defaults={
                    "name": "Inventory",
                    "account_type": "ASSET",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )
            opening_acct = get_company_account(
                owner=self.owner,
                code="3000",
                defaults={
                    "name": "Opening Balances",
                    "account_type": "EQUITY",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )
            writeoff_acct = get_company_account(
                owner=self.owner,
                code="5100",
                defaults={
                    "name": "Inventory Write-off (Damage/Expiry)",
                    "account_type": "EXPENSE",
                    "is_cash_or_bank": False,
                    "allow_for_payments": False,
                },
            )

            if self.direction == "DOWN":
                debit_account = writeoff_acct
                credit_account = inventory_acct
                desc = f"Stock adjustment (DOWN) - {self.product}"

                stock_delta = -(self.qty or Decimal("0"))
            else:
                debit_account = inventory_acct
                credit_account = opening_acct
                desc = f"Stock adjustment (UP) - {self.product}"

                stock_delta = (self.qty or Decimal("0"))

            # 🔒 Idempotent JournalEntry create
            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="StockAdjustment",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.date,
                    description=self.reason or desc,
                    debit_account=debit_account,
                    credit_account=credit_account,
                    amount=self.amount,
                    related_model="StockAdjustment",
                    related_id=self.id,
                )

            # ✅ CRITICAL: Update Product.current_stock once
            if stock_delta != 0:
                self.product.adjust_stock(stock_delta)

            # Mark posted
            locked.posted = True
            locked.save(update_fields=["posted"])

            # Keep in-memory instance consistent
            self.posted = True

    class Meta:
        constraints = []
        indexes = [
            models.Index(fields=["owner", "date", "id"]),
            models.Index(fields=["owner", "posted"]),
            models.Index(fields=["owner", "product"]),
        ]

class CompanyProfile(TimeStampedModel):
    owner = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="company_profile",
        help_text="Company owner user (this user is the company).",
    )
    name = models.CharField(max_length=200)
    logo = models.ImageField(upload_to="company_logo/", null=True, blank=True)

    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    slug = models.SlugField(max_length=50, unique=True, blank=True)

    def save(self, *args, **kwargs):
        # Source for slug:
        # - if slug manually set, use it
        # - else use owner's username
        raw = self.slug or (self.owner.username if self.owner_id else "") or ""

        # DNS-safe slug:
        # - lowercase
        # - convert "_" and spaces to "-"
        # - remove everything except a-z, 0-9, "-"
        # - collapse multiple "-"
        # - trim "-" from ends
        s = raw.lower().strip()
        s = s.replace("_", "-")
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9-]", "", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")

        # keep within SlugField max_length
        self.slug = s[:50] if s else ""

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        indexes = [
            models.Index(fields=["slug"]),
        ]

class AppBranding(models.Model):
    app_name = models.CharField(max_length=100, default="Roznamcha")
    tagline = models.CharField(max_length=150, blank=True, default="Smart Khata & Hisab System")
    logo = models.ImageField(upload_to="branding/", blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.app_name

    class Meta:
        verbose_name = "App Branding"
        verbose_name_plural = "App Branding"

def seed_default_accounts_for_owner(owner: User) -> None:
    if owner is None:
        raise ValueError("Owner is required.")

    required = [
        ("1200", "Inventory", "ASSET", False, False),
        ("1300", "Customer Control", "ASSET", False, False),
        ("2100", "Supplier Control", "LIABILITY", False, False),
        ("3000", "Opening Balances", "EQUITY", False, False),
        ("5100", "Inventory Write-off (Damage/Expiry)", "EXPENSE", False, False),
        ("1010", "Cash", "ASSET", True, True),
        ("1020", "Bank", "ASSET", True, True),
        ("5200", "Wages / Staff Salaries", "EXPENSE", False, False),
        ("5210", "Fuel / Diesel / Petrol", "EXPENSE", False, False),
        ("5220", "Utilities (Electricity / Internet / Gas / Water)", "EXPENSE", False, False),
        ("5230", "Guests / Entertainment", "EXPENSE", False, False),
        ("5240", "Repair & Maintenance", "EXPENSE", False, False),
        ("5250", "Office Supplies / Stationery", "EXPENSE", False, False),
        ("5290", "Miscellaneous Expense", "EXPENSE", False, False),
    ]

    with transaction.atomic():
        for code, name, acc_type, is_cb, allow_pay in required:
            acct = get_company_account(
                owner=owner,
                code=code,
                defaults={
                    "name": name,
                    "account_type": acc_type,
                    "is_cash_or_bank": is_cb,
                    "allow_for_payments": allow_pay,
                },
            )

            needs_update = False
            if acct.name != name:
                acct.name = name
                needs_update = True
            if acct.account_type != acc_type:
                acct.account_type = acc_type
                needs_update = True
            if acct.is_cash_or_bank != is_cb:
                acct.is_cash_or_bank = is_cb
                needs_update = True
            if acct.allow_for_payments != allow_pay:
                acct.allow_for_payments = allow_pay
                needs_update = True

            if needs_update:
                acct.save(update_fields=["name", "account_type", "is_cash_or_bank", "allow_for_payments"])

class CashBankTransfer(OwnerRequiredMixin, TimeStampedModel):
    """
    Transfer money between cash/bank accounts:
      Dr to_account
      Cr from_account
    """
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="cash_bank_transfers",
        help_text="Company owner (this user is the company).",
    )

    date = models.DateField(default=timezone.now)

    from_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="cashbank_transfers_out",
        limit_choices_to={"is_cash_or_bank": True, "allow_for_payments": True},
    )

    to_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="cashbank_transfers_in",
        limit_choices_to={"is_cash_or_bank": True, "allow_for_payments": True},
    )

    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    notes = models.CharField(max_length=255, blank=True, default="")
    posted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.date} | {self.from_account} → {self.to_account} | {self.amount}"

    def post(self):
        """
        Create JournalEntry (idempotent):
          Dr to_account
          Cr from_account
        """
        if self.posted or self.amount <= 0:
            return

        if self.owner is None:
            raise PermissionDenied("Owner not resolved for CashBankTransfer")

        # ✅ SaaS defense
        if self.from_account and self.from_account.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner from_account detected.")
        if self.to_account and self.to_account.owner_id != self.owner_id:
            raise PermissionDenied("Cross-owner to_account detected.")

        if self.from_account_id == self.to_account_id:
            raise PermissionDenied("From and To accounts cannot be the same.")

        with transaction.atomic():
            locked = CashBankTransfer.objects.select_for_update().get(pk=self.pk)
            if locked.posted:
                return

            # 🔒 Idempotent JournalEntry (your unique constraint also enforces this)
            if not JournalEntry.objects.filter(
                owner=self.owner,
                related_model="CashBankTransfer",
                related_id=self.id,
            ).exists():
                JournalEntry.objects.create(
                    owner=self.owner,
                    date=self.date,
                    description=self.notes or f"Transfer {self.id}",
                    debit_account=self.to_account,
                    credit_account=self.from_account,
                    amount=self.amount,
                    related_model="CashBankTransfer",
                    related_id=self.id,
                )

            locked.posted = True
            locked.save(update_fields=["posted"])
            self.posted = True