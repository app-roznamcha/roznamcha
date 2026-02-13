# core/permissions.py
from functools import wraps
from django.core.exceptions import PermissionDenied

# Reuse the single source of truth you already built
from .decorators import _ensure_owner_and_tenant, _enforce_subscription


def _get_role(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "role", None)


# =====================================================
# STAFF ALLOWED — operational pages
# =====================================================
def staff_allowed(view_func):
    """
    Allows STAFF + OWNER + SUPERADMIN (and Django superuser),
    BUT enforces OWNER subscription (staff inherits owner subscription).

    Use on operational pages:
      - create customer/supplier/product
      - new sale/purchase
      - returns
      - payments
      - adjustments
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")

        # Django superuser: bypass subscription + role checks
        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        role = _get_role(user)
        if not role:
            raise PermissionDenied("User profile missing or invalid")

        # SUPERADMIN role (profile-based) bypass (no tenant/subscription enforcement)
        if role == "SUPERADMIN":
            return view_func(request, *args, **kwargs)

        # STAFF / OWNER must be on a valid tenant and must pass OWNER subscription
        if role in ("STAFF", "OWNER"):
            owner, _company = _ensure_owner_and_tenant(request, require_company=True)
            _enforce_subscription(request, owner)
            return view_func(request, *args, **kwargs)

        raise PermissionDenied("Not allowed")

    return _wrapped


# =====================================================
# OWNER ONLY — business control pages
# =====================================================
def owner_only(view_func):
    """
    Allows only OWNER + SUPERADMIN (and Django superuser).
    Blocks STAFF completely.
    Also enforces OWNER subscription.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")

        # Django superuser bypass
        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        role = _get_role(user)
        if not role:
            raise PermissionDenied("User profile missing or invalid")

        # Profile SUPERADMIN bypass
        if role == "SUPERADMIN":
            return view_func(request, *args, **kwargs)

        # Staff blocked
        if role == "STAFF":
            raise PermissionDenied("Staff cannot access this page.")

        # OWNER: must be on tenant + subscription must be valid
        if role == "OWNER":
            owner, _company = _ensure_owner_and_tenant(request, require_company=True)
            _enforce_subscription(request, owner)
            return view_func(request, *args, **kwargs)

        raise PermissionDenied("Not allowed")

    return _wrapped


# =====================================================
# BACKWARD COMPATIBILITY ALIAS
# =====================================================
staff_blocked = owner_only