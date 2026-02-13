from .models import CompanyProfile, AppBranding
from django.utils import timezone


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

        # If user is STAFF and owner isn't resolved, try profile.owner
        if getattr(owner, "profile", None) and owner.profile.role == "STAFF":
            owner = owner.profile.owner or owner

        cp = CompanyProfile.objects.filter(owner=owner).first()

    return {"company_profile": cp}

def app_branding(request):
    branding = AppBranding.objects.order_by("-id").first()
    return {"app_branding": branding}


def subscription_context(request):
    """
    Global subscription context for templates (banner/UI).
    Uses effective status (trial/active/expired) instead of stored subscription_status.

    Returns:
      subscription_status_effective: "TRIAL"/"ACTIVE"/"EXPIRED"/None
      subscription_expires_effective: datetime/None
      subscription_days_left: int/None
      subscription_is_trial / _is_active / _is_expired: bool
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "subscription_status_effective": None,
            "subscription_expires_effective": None,
            "subscription_days_left": None,
            "subscription_is_trial": False,
            "subscription_is_active": False,
            "subscription_is_expired": False,
        }

    # Prefer middleware/decorator resolved owner
    owner = getattr(request, "owner", None) or user

    # If somehow owner is STAFF, resolve its real OWNER
    try:
        if getattr(owner, "profile", None) and owner.profile.role == "STAFF":
            owner = owner.profile.owner or owner
    except Exception:
        pass

    profile = getattr(owner, "profile", None)
    if not profile:
        return {
            "subscription_status_effective": None,
            "subscription_expires_effective": None,
            "subscription_days_left": None,
            "subscription_is_trial": False,
            "subscription_is_active": False,
            "subscription_is_expired": False,
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

    # Days left (safe)
    days_left = None
    try:
        if hasattr(profile, "days_left"):
            days_left = profile.days_left()
        elif expires_at:
            days_left = (expires_at.date() - timezone.now().date()).days
    except Exception:
        days_left = None

        return {
        # ✅ simple names used by templates (base.html banner)
        "subscription_status": status,
        "subscription_expires_at": expires_at,
        "subscription_days_left": days_left,
        "subscription_is_trial": (status == "TRIAL"),
        "subscription_is_active": (status == "ACTIVE"),
        "subscription_is_expired": (status == "EXPIRED"),

        # ✅ keep your “_effective” names too (optional but harmless)
        "subscription_status_effective": status,
        "subscription_expires_effective": expires_at,
    }