# core/decorators.py
from functools import wraps
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from core.models import CompanyProfile, UserProfile
from .models import get_company_owner
from django.contrib.auth.views import redirect_to_login


TRIAL_DAYS = 15
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _profile(user):
    """
    Safe access for OneToOne reverse relation: user.profile
    Prevents 500 when profile doesn't exist.
    """
    try:
        return user.profile
    except UserProfile.DoesNotExist:
        return None
    except Exception:
        return None


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

    # ✅ Superuser behavior:
    # - If middleware already resolved a tenant (subdomain), respect it (view tenant as superadmin)
    # - If no tenant (www/base), treat as global superadmin
    if user and getattr(user, "is_superuser", False):
        current_tenant = getattr(request, "tenant", None)
        if current_tenant is not None:
            # tenant mode: owner = tenant owner
            request.owner = current_tenant.owner
            request.tenant = current_tenant
            return request.owner, request.tenant

        # global mode (www/base): no tenant
        request.owner = user
        request.tenant = None
        return user, None
    
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

def _enforce_subscription(request, owner_user):
    """
    Enforces subscription rules using EFFECTIVE status/expiry.
    Rule:
      - Allow SAFE_METHODS (GET/HEAD/OPTIONS) even if expired (so user can view dashboards + renewal page).
      - Block non-safe methods (POST/PUT/PATCH/DELETE) when expired/inactive.
    """
    if getattr(owner_user, "is_superuser", False):
        return

    # ✅ Allow read-only browsing always (your UI already shows banners + blocks actions)
    if getattr(request, "method", "GET") in SAFE_METHODS:
        return

    prof = _profile(owner_user)
    if not prof:
        return

    # ✅ Prefer effective logic
    try:
        status = prof.get_effective_status()
    except Exception:
        status = getattr(prof, "subscription_status", None)

    try:
        expires_at = prof.get_effective_expires_at()
    except Exception:
        expires_at = getattr(prof, "subscription_expires_at", None)

    # If subscription system not wired => allow
    if not status:
        return

    now = timezone.now()

    # Expiry check (effective)
    if expires_at and expires_at < now:
        raise PermissionDenied("Subscription expired. Please renew.")

    if status == "EXPIRED":
        raise PermissionDenied("Subscription expired. Please renew.")

    # ✅ ACTIVE or TRIAL allowed for writes (if not expired)
    if status in ("ACTIVE", "TRIAL"):
        return

    raise PermissionDenied("Subscription inactive")

def owner_required(view_func):
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        owner, tenant = _ensure_owner_and_tenant(request, require_company=False)

        request.owner = owner
        request.tenant = tenant

        _enforce_subscription(request, owner)
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
    Blocks write actions when OWNER subscription is expired/inactive.
    Uses effective subscription logic.
    Allows SAFE_METHODS for viewing dashboards/renewal pages.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)

        if not user or not user.is_authenticated:
            # ✅ Always redirect anonymous users to login with next=
            return redirect_to_login(request.get_full_path())

        # Superadmin bypass
        if getattr(user, "is_superuser", False):
            return view_func(request, *args, **kwargs)

        # Resolve owner (staff inherits owner subscription)
        owner = getattr(request, "owner", None) or get_company_owner(user)
        if not owner:
            raise PermissionDenied("Owner not resolved")

        # ✅ Allow safe methods always (GET/HEAD/OPTIONS)
        if getattr(request, "method", "GET") in SAFE_METHODS:
            return view_func(request, *args, **kwargs)

        profile = getattr(owner, "profile", None)
        if not profile:
            return view_func(request, *args, **kwargs)

        # Effective status
        try:
            status = profile.get_effective_status()
        except Exception:
            status = getattr(profile, "subscription_status", None)

        try:
            expires_at = profile.get_effective_expires_at()
        except Exception:
            expires_at = getattr(profile, "subscription_expires_at", None)

        now = timezone.now()

        if expires_at and expires_at < now:
            raise PermissionDenied("Subscription expired. Please renew.")

        if status == "EXPIRED":
            raise PermissionDenied("Subscription expired. Please renew.")

        if status in ("ACTIVE", "TRIAL"):
            return view_func(request, *args, **kwargs)

        raise PermissionDenied("Subscription inactive")

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
        _enforce_subscription(request, owner)

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