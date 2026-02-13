from django.utils import timezone
from .models import CompanyProfile, AppBranding, UserProfile


def _safe_profile(user):
    """
    Safe access for OneToOne reverse relation: user.profile
    Prevents 500 when profile doesn't exist (or user is None).
    """
    if not user:
        return None

    try:
        return user.profile
    except UserProfile.DoesNotExist:
        return None
    except Exception:
        return None


def company_profile(request):
    """
    Tenant-scoped company profile for the current owner/company.
    - If STAFF: request.owner should already be set by tenant middleware/decorator.
    - If OWNER: request.owner is usually the same as request.user.
    - If anonymous: None.
    """
    cp = None

    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        owner = getattr(request, "owner", None) or user

        # If the resolved "owner" is actually STAFF, jump to real OWNER
        owner_prof = _safe_profile(owner)
        if owner_prof and owner_prof.role == "STAFF":
            owner = owner_prof.owner or owner

        cp = CompanyProfile.objects.filter(owner=owner).first()

    return {"company_profile": cp}


def app_branding(request):
    branding = AppBranding.objects.order_by("-id").first()
    return {"app_branding": branding}


def subscription_context(request):
    """
    Global subscription context for templates (banner/UI).
    Uses effective status (trial/active/expired) instead of stored subscription_status.

    Provides BOTH:
    - simple names used by base.html banner
    - *_effective names (optional)
    """
    user = getattr(request, "user", None)

    # Not logged in
    if not user or not user.is_authenticated:
        return {
            "user_profile": None,
            "subscription_status": None,
            "subscription_expires_at": None,
            "subscription_days_left": None,
            "subscription_is_trial": False,
            "subscription_is_active": False,
            "subscription_is_expired": False,
            "subscription_status_effective": None,
            "subscription_expires_effective": None,
        }

    # Prefer middleware/decorator resolved owner
    owner = getattr(request, "owner", None) or user

    # If owner is STAFF, resolve real OWNER
    try:
        owner_prof = _safe_profile(owner)
        if owner_prof and owner_prof.role == "STAFF":
            owner = owner_prof.owner or owner
    except Exception:
        pass

    profile = _safe_profile(owner)

    # Profile missing
    if not profile:
        return {
            "user_profile": _safe_profile(user),
            "subscription_status": None,
            "subscription_expires_at": None,
            "subscription_days_left": None,
            "subscription_is_trial": False,
            "subscription_is_active": False,
            "subscription_is_expired": False,
            "subscription_status_effective": None,
            "subscription_expires_effective": None,
        }

    # Effective status
    try:
        status = profile.get_effective_status()
    except Exception:
        status = getattr(profile, "subscription_status", None)

    # Effective expiry
    try:
        expires_at = profile.get_effective_expires_at()
    except Exception:
        expires_at = getattr(profile, "subscription_expires_at", None)

    # Days left
    days_left = None
    try:
        if hasattr(profile, "days_left"):
            days_left = profile.days_left()
        elif expires_at:
            days_left = (expires_at.date() - timezone.now().date()).days
    except Exception:
        days_left = None

    return {
        # ✅ safe for templates (base.html should use this)
        "user_profile": _safe_profile(user),

        # ✅ simple names used by templates (banner/UI)
        "subscription_status": status,
        "subscription_expires_at": expires_at,
        "subscription_days_left": days_left,
        "subscription_is_trial": (status == "TRIAL"),
        "subscription_is_active": (status == "ACTIVE"),
        "subscription_is_expired": (status == "EXPIRED"),

        # ✅ keep your “effective” names too
        "subscription_status_effective": status,
        "subscription_expires_effective": expires_at,
    }