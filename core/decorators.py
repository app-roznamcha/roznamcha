# core/decorators.py
from functools import wraps
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from core.models import CompanyProfile
from .models import get_company_owner



TRIAL_DAYS = 15
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _profile(user):
    return getattr(user, "profile", None)


def _resolve_owner(user):
    """
    Returns the owner user for this session:
    - Django superuser -> itself
    - OWNER -> itself
    - STAFF -> profile.owner
    """
    if not user or not user.is_authenticated:
        raise PermissionDenied("Not authenticated")

    if getattr(user, "is_superuser", False):
        return user

    profile = _profile(user)
    if not profile:
        raise PermissionDenied("User profile missing")

    role = getattr(profile, "role", None)

    if role == "OWNER":
        return user

    if role == "STAFF":
        owner_id = getattr(profile, "owner_id", None)
        if not owner_id:
            raise PermissionDenied("Staff has no owner assigned")
        return profile.owner

    raise PermissionDenied("Not allowed")


def _get_company_for_owner(owner_user):
    return CompanyProfile.objects.filter(owner=owner_user).first()


def _ensure_owner_and_tenant(request, require_company=False):
    """
    Ensures request.owner and request.tenant are set consistently.

    If tenant middleware already set request.tenant (from subdomain),
    it MUST match the resolved owner's company.

    For Django superuser:
    - keep tenant context if middleware already resolved it
      (so superadmin can browse inside a tenant subdomain safely).
    """
    user = getattr(request, "user", None)

    # If tenant middleware set tenant, keep it available
    current_tenant = getattr(request, "tenant", None) or getattr(request, "company", None)

    # Django superuser: bypass subscription/role, but DO NOT wipe tenant context
    if user and getattr(user, "is_superuser", False):
        request.owner = user
        request.tenant = current_tenant
        request.company = current_tenant
        if require_company and not current_tenant:
            raise PermissionDenied("Company not found for this tenant")
        return user, current_tenant

    owner = _resolve_owner(user)
    company = _get_company_for_owner(owner)

    if require_company and not company:
        raise PermissionDenied("Company not found for this user")

    # If subdomain middleware already set a tenant, it MUST match
    if current_tenant is not None and company is not None:
        if getattr(current_tenant, "id", None) != getattr(company, "id", None):
            raise PermissionDenied("Tenant mismatch (wrong subdomain for this account)")

    # Always set both (some views use request.company)
    request.owner = owner
    request.tenant = company or current_tenant
    request.company = company or current_tenant

    # If require_company=True, enforce after final resolution
    if require_company and not request.tenant:
        raise PermissionDenied("Company not found")

    return request.owner, request.tenant

def _enforce_subscription(owner_user):
    """
    Enforces subscription rules using fields on owner's profile (if present).
    Dev-safe: if subscription fields aren't wired, do not block.
    """
    if getattr(owner_user, "is_superuser", False):
        return

    prof = _profile(owner_user)
    if not prof:
        return

    status = getattr(prof, "subscription_status", None)
    expires_at = getattr(prof, "subscription_expires_at", None)
    trial_started = getattr(prof, "trial_started_at", None)

    # If subscription system not wired => allow
    if not status:
        return

    now = timezone.now()

    if expires_at and expires_at < now:
        raise PermissionDenied("Subscription expired. Please renew.")

    if status == "EXPIRED":
        raise PermissionDenied("Subscription expired. Please renew.")

    if status == "TRIAL":
        if not trial_started:
            trial_started = getattr(prof, "created_at", None)

        if trial_started and (trial_started + timedelta(days=TRIAL_DAYS)) < now:
            # optional auto-mark expired
            try:
                prof.subscription_status = "EXPIRED"
                prof.save(update_fields=["subscription_status"])
            except Exception:
                pass
            raise PermissionDenied("Trial expired. Please activate subscription.")

    if status == "ACTIVE" or status == "TRIAL":
        return

    raise PermissionDenied("Subscription inactive")


def owner_required(view_func):
    """
    Sets request.owner and request.tenant.
    Enforces subscription for ALL methods (read + write).
    """
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        owner, _ = _ensure_owner_and_tenant(request, require_company=False)
        _enforce_subscription(owner)
        return view_func(request, *args, **kwargs)

    return _wrapped


def resolve_tenant_context(require_company: bool = False):
    """
    Sets request.owner and request.tenant.
    - require_company=True => 403 if company missing
    - require_company=False => tenant can be None
    """
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            _ensure_owner_and_tenant(request, require_company=require_company)
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def subscription_required(view_func):
    """
    Blocks if OWNER subscription is not active.
    Staff inherit Owner subscription automatically.
    Django superuser bypass always.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")

        # Django superuser bypass
        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        # ✅ CRITICAL: Always check OWNER subscription (staff inherits)
        owner = getattr(request, "owner", None) or get_company_owner(user)
        if not owner:
            raise PermissionDenied("Owner not resolved")

        profile = getattr(owner, "profile", None)
        if not profile:
            raise PermissionDenied("Owner profile missing")

        status = getattr(profile, "subscription_status", None)
        expires_at = getattr(profile, "subscription_expires_at", None)

        today = timezone.now().date()
        expired = False
        if expires_at:
            try:
                expired = expires_at.date() < today
            except Exception:
                expired = False

        # ✅ Allow TRIAL and ACTIVE as long as not expired
        if status in ("TRIAL", "ACTIVE") and not expired:
            return view_func(request, *args, **kwargs)

        # ❌ Block only if expired or inactive
        raise PermissionDenied("Subscription expired. Please renew.")
    return _wrapped

def staff_blocked(view_func):
    """
    Blocks STAFF users from restricted views.
    Also enforces OWNER subscription (staff inherits owner subscription).
    Use this on OWNER-only pages.
    """
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        # ✅ Resolve owner + tenant and enforce subscription first
        owner, _ = _ensure_owner_and_tenant(request, require_company=False)
        _enforce_subscription(owner)

        user = request.user

        # Superadmin bypass (but subscription already checked safely)
        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        profile = _profile(user)
        if not profile:
            raise PermissionDenied("User profile missing")

        if profile.role == "STAFF":
            raise PermissionDenied("Staff not allowed to access this feature")

        return view_func(request, *args, **kwargs)

    return _wrapped