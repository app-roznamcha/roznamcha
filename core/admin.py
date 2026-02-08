from django.contrib import admin
from django.contrib.auth.models import User, Group
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import PermissionDenied

from .models import (
    UserProfile,
    CompanyProfile,
    AppBranding,
    Account,
    Product,
    Party,
    JournalEntry,
    Payment,
    SalesInvoice,
    SalesInvoiceItem,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    SalesReturn,
    SalesReturnItem,
    PurchaseReturn,
    PurchaseReturnItem,
    StockAdjustment,
)
from .models import get_company_owner

# -------------------------
# Role helpers
# -------------------------
def is_staff_user(user):
    return hasattr(user, "profile") and user.profile.role == "STAFF"

def is_owner_user(user):
    return hasattr(user, "profile") and user.profile.role == "OWNER"

def get_role(user):
    if user.is_superuser:
        return "SUPERADMIN"
    if hasattr(user, "profile"):
        return user.profile.role
    return "STAFF"

def _obj_belongs_to_request_owner(request, obj) -> bool:
    """
    Object-level SaaS guard: prevents cross-tenant access even via direct URL.
    """
    if request.user.is_superuser:
        return True

    if obj is None:
        return True

    if not hasattr(obj, "owner_id"):
        return True

    owner = getattr(request, "owner", None) or get_company_owner(request.user)
    return obj.owner_id == getattr(owner, "id", None)


# -------------------------
# Base Admin: owner-scoped + restricted
# -------------------------
class OwnerScopedAdmin(admin.ModelAdmin):
    """
    - STAFF sees only their owner's data.
    - Auto-sets owner on create.
    - Blocks cross-owner access at object-level too.
    - Filters FK dropdowns to owner-scoped data.
    - Optionally blocks staff from add/delete.
    """
    staff_can_add = True
    staff_can_delete = False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        owner = getattr(request, "owner", None) or get_company_owner(request.user)
        if hasattr(self.model, "owner_id"):
            return qs.filter(owner=owner)
        return qs

    def save_model(self, request, obj, form, change):
        # Auto-assign owner if missing
        if hasattr(obj, "owner_id") and not obj.owner_id:
            obj.owner = getattr(request, "owner", None) or get_company_owner(request.user)
        super().save_model(request, obj, form, change)

    # ---- SaaS object-level hardening ----
    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is not None and not _obj_belongs_to_request_owner(request, obj):
            return False
        return super().has_view_permission(request, obj=obj)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is not None and not _obj_belongs_to_request_owner(request, obj):
            return False
        return super().has_change_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is not None and not _obj_belongs_to_request_owner(request, obj):
            return False
        if is_staff_user(request.user) and not self.staff_can_delete:
            return False
        return super().has_delete_permission(request, obj=obj)

    def has_add_permission(self, request):
        if is_staff_user(request.user) and not self.staff_can_add:
            return False
        return super().has_add_permission(request)

    # ---- SaaS FK dropdown hardening ----
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Ensures dropdowns don't leak cross-tenant objects.
        Filters any related model that has 'owner'.
        """
        if not request.user.is_superuser:
            owner = getattr(request, "owner", None) or get_company_owner(request.user)

            # If the related model is owner-scoped, filter it.
            rel_model = getattr(db_field.remote_field, "model", None)
            if rel_model is not None and hasattr(rel_model, "owner_id"):
                qs = rel_model.objects.filter(owner=owner)
                kwargs["queryset"] = qs

        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class StaffCreateOnlyAdmin(OwnerScopedAdmin):
    """
    STAFF: can ONLY ADD (create). No view list, no edit, no delete.
    OWNER/SUPERADMIN: normal behavior.
    """
    staff_can_add = True
    staff_can_delete = False

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if is_staff_user(request.user):
            return False
        # still enforce object-level tenant check via parent
        return super().has_view_permission(request, obj=obj)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if is_staff_user(request.user):
            return False
        return super().has_change_permission(request, obj=obj)

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if is_staff_user(request.user):
            return False
        return super().has_delete_permission(request, obj=obj)


# -------------------------
# Hide whole modules from STAFF
# -------------------------
class HiddenFromStaffAdmin(admin.ModelAdmin):

    def has_module_permission(self, request):
        if request.user.is_superuser:
            return True
        return not is_staff_user(request.user)

    def has_view_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        return not is_staff_user(request.user)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# -------------------------
# What STAFF is allowed to use
# -------------------------
@admin.register(Product)
class ProductAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("code", "name", "unit", "current_stock", "is_active")
    search_fields = ("code", "name")


@admin.register(Party)
class PartyAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("name", "party_type", "phone", "city", "is_active")
    search_fields = ("name", "phone", "city")


@admin.register(Payment)
class PaymentAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("date", "direction", "party", "account", "amount", "posted", "is_adjustment")
    list_filter = ("direction", "posted", "is_adjustment")


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("id", "customer", "invoice_date", "payment_type", "payment_amount", "posted")


@admin.register(SalesInvoiceItem)
class SalesInvoiceItemAdmin(OwnerScopedAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("sales_invoice", "product", "quantity_units", "unit_price", "discount_amount")


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("id", "supplier", "invoice_date", "payment_type", "payment_amount", "posted")


@admin.register(PurchaseInvoiceItem)
class PurchaseInvoiceItemAdmin(OwnerScopedAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("purchase_invoice", "product", "quantity_units", "unit_price", "discount_amount")


@admin.register(SalesReturn)
class SalesReturnAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("id", "customer", "return_date", "posted")


@admin.register(SalesReturnItem)
class SalesReturnItemAdmin(OwnerScopedAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("sales_return", "product", "quantity_units", "unit_price", "discount_amount")


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(StaffCreateOnlyAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("id", "supplier", "return_date", "posted")


@admin.register(PurchaseReturnItem)
class PurchaseReturnItemAdmin(OwnerScopedAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("purchase_return", "product", "quantity_units", "unit_price", "discount_amount")


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(OwnerScopedAdmin):
    staff_can_add = True
    staff_can_delete = False
    list_display = ("date", "product", "direction", "qty", "unit_cost", "posted")


# -------------------------
# Hide these from STAFF
# -------------------------
admin.site.register(UserProfile, HiddenFromStaffAdmin)
admin.site.register(CompanyProfile, HiddenFromStaffAdmin)
admin.site.register(AppBranding, HiddenFromStaffAdmin)
admin.site.register(Account, HiddenFromStaffAdmin)
admin.site.register(JournalEntry, HiddenFromStaffAdmin)

# Also hide Django auth Users/Groups from STAFF
admin.site.unregister(Group)
admin.site.unregister(User)


@admin.register(User)
class UserAdminHiddenFromStaff(DjangoUserAdmin):
    """
    Keep Django's proper user admin (so passwords are HASHED),
    but hide it from STAFF (only SUPERADMIN can manage users).
    """
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Group)
class GroupAdminHiddenFromStaff(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser