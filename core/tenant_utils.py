# core/tenant_utils.py
# core/tenant_utils.py
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

# Import Account safely (to fix Pylance + runtime)
from .models import Account


def get_owner_user(request):
    """
    Returns the company owner user for this session:

    - OWNER -> self
    - STAFF -> profile.owner
    - SUPERADMIN -> None (no tenant context)

    Assumes middleware sets request.owner safely.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise PermissionDenied("Not authenticated")

    # ✅ SUPERADMIN: no tenant owner by default
    if getattr(user, "is_superuser", False):
        request.owner = None
        return None

    # If middleware already resolved owner, trust it
    owner = getattr(request, "owner", None)
    if owner is not None:
        return owner

    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Profile missing")

    role = getattr(profile, "role", None)

    if role == "OWNER":
        request.owner = user
        return user

    if role == "STAFF":
        if not getattr(profile, "owner_id", None):
            raise PermissionDenied("Staff has no owner assigned")
        request.owner = profile.owner
        return request.owner

    raise PermissionDenied("Invalid role")


def get_tenant(request):
    """
    Returns request.tenant (must be set by middleware).
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise PermissionDenied("Tenant not resolved")
    return tenant






def require_owner(request):
    owner = getattr(request, "owner", None)
    if owner is None:
        raise PermissionDenied("Owner not resolved.")
    return owner


def _request_owner(request):
    """
    Resolve request.owner reliably (OWNER/STAFF model).

    - OWNER user -> owner = user
    - STAFF user -> owner = profile.owner
    - SUPERUSER -> no owner (optional)
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise PermissionDenied("Not authenticated")

    if getattr(user, "is_superuser", False):
        # Super admin has no tenant owner
        request.owner = None
        return None
    # If already set upstream, trust it
    owner = getattr(request, "owner", None)
    if owner is not None:
        return owner

    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Profile missing")

    role = getattr(profile, "role", None)

    if role == "OWNER":
        owner = user
    elif role == "STAFF" and getattr(profile, "owner_id", None):
        owner = profile.owner
    else:
        raise PermissionDenied("Not allowed")

    request.owner = owner
    return owner


def owner_qs(request, model_or_qs):
    """
    Owner-safe queryset scoping:
    - OWNER -> own data
    - STAFF -> owner's data
    - SUPERADMIN -> sees all (optional; keep)
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        if hasattr(model_or_qs, "objects"):
            return model_or_qs.objects.none()
        return model_or_qs.none()

    # superuser: allow full queryset (debug/admin power)
    if user.is_superuser:
        _request_owner(request)  # sets request.owner=None (fine)
        return model_or_qs.objects.all() if hasattr(model_or_qs, "objects") else model_or_qs

    owner = _request_owner(request)
    qs = model_or_qs.objects.none() if hasattr(model_or_qs, "objects") else model_or_qs

    if owner is not None and hasattr(qs.model, "owner_id"):
        return qs.filter(owner=owner)
    return qs


def tenant_qs(request, model_or_qs, *, strict=False):
    """
    BACKWARD COMPAT NAME.
    In your project, "tenant" is not the source of truth anymore.
    We scope by request.owner (company) only.

    strict=True -> owner must resolve, else PermissionDenied
    """
    owner = getattr(request, "owner", None)
    if owner is None and strict:
        owner = _request_owner(request)  # may raise

    qs = model_or_qs.objects.none() if hasattr(model_or_qs, "objects") else model_or_qs

    if owner is not None and hasattr(qs.model, "owner_id"):
        return qs.filter(owner=owner)

    # If model is not owner-scoped, return as-is (rare)
    return qs


def tenant_get_object_or_404(request, model, **kwargs):
    # superuser bypass (keeps your debug/admin power)
    if getattr(request.user, "is_superuser", False):
        return get_object_or_404(model, **kwargs)

    owner = getattr(request, "owner", None)

    # If model has owner_id and caller didn't pass owner, enforce it
    if owner and hasattr(model, "owner_id") and "owner" not in kwargs and "owner_id" not in kwargs:
        kwargs["owner"] = owner

    return get_object_or_404(model, **kwargs)

def set_tenant_on_create_kwargs(request, kwargs: dict, model=None):
    """
    BACKWARD COMPAT NAME.
    For owner-only design: inject owner into kwargs if model has owner_id.
    """
    owner = _request_owner(request)

    # If model is known and has owner_id, set it
    if model is not None and hasattr(model, "owner_id"):
        kwargs.setdefault("owner", owner)
        return kwargs

    # If model unknown, but kwargs has "owner" field or caller expects owner scoping
    # (safe default: only set if "owner" key exists OR model has owner_id)
    if "owner" in kwargs:
        kwargs.setdefault("owner", owner)

    return kwargs


def get_owner_account(*, request=None, owner=None, code: str, defaults=None, **extra_defaults):
    """
    Owner-scoped Account get_or_create by code.
    This replaces tenant-based chart-of-accounts logic.

    Usage:
      ✅ get_owner_account(request=request, code="3000", defaults={...})
      ✅ get_owner_account(owner=party.owner, code="3000", defaults={...})
    """
    code = (code or "").strip()
    if not code:
        raise ValueError("Account code is required")

    merged_defaults = {}
    if defaults:
        merged_defaults.update(defaults)
    merged_defaults.update(extra_defaults)

    if owner is None and request is not None:
        owner = _request_owner(request)

    # superuser fallback (optional): create global
    if owner is None:
        acct, _ = Account.objects.get_or_create(code=code, defaults=merged_defaults)
        return acct

    # owner-scoped
    acct, _ = Account.objects.get_or_create(owner=owner, code=code, defaults=merged_defaults)
    return acct


# BACKWARD COMPAT alias (so your existing code keeps working)
def get_tenant_account(*, request=None, tenant=None, code: str, defaults=None, **extra_defaults):
    return get_owner_account(request=request, owner=getattr(tenant, "owner", None) if tenant else None,
                             code=code, defaults=defaults, **extra_defaults)
