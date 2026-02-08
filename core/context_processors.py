from .models import CompanyProfile, AppBranding

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