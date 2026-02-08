# core/tenant_utils.py
from django.core.exceptions import PermissionDenied


def get_owner_user(request):
    """
    Returns the "company owner user" for the current authenticated session:
    - OWNER -> self
    - STAFF -> profile.owner

    Assumes SaasTenantMiddleware already enforced tenant safety and set request.owner/request.tenant.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        raise PermissionDenied("Not authenticated")

    # If middleware already set it, trust it
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