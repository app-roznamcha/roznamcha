# core/permissions.py
from functools import wraps
from django.core.exceptions import PermissionDenied


def _get_role(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "role", None)


# =====================================================
# STAFF ALLOWED — operational pages
# =====================================================
def staff_allowed(view_func):
    """
    Allows STAFF + OWNER + SUPERADMIN (and Django superuser).

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

        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        role = _get_role(user)

        if not role:
            raise PermissionDenied("User profile missing or invalid")

        if role in ("STAFF", "OWNER", "SUPERADMIN"):
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

    Use on:
      - reports
      - backups
      - subscription
      - settings
      - user management
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")

        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        role = _get_role(user)

        if not role:
            raise PermissionDenied("User profile missing or invalid")

        if role == "STAFF":
            raise PermissionDenied("Staff cannot access this page.")

        if role in ("OWNER", "SUPERADMIN"):
            return view_func(request, *args, **kwargs)

        raise PermissionDenied("Not allowed")

    return _wrapped


# =====================================================
# BACKWARD COMPATIBILITY ALIAS
# =====================================================
# staff_blocked == owner_only
# Keeps old URLs working without breaking imports
staff_blocked = owner_only