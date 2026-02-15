from django.core.exceptions import PermissionDenied


class SuperAdminOnlyAdminMiddleware:
    """
    Blocks /admin/ for everyone except:
    - Django superuser, OR
    - UserProfile.role == "SUPERADMIN"
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info or ""

        # Only guard the Django admin
        if path.startswith("/admin/"):
            user = getattr(request, "user", None)

            # must be logged in
            if not user or not user.is_authenticated:
                # Let Django admin handle login redirects
                return self.get_response(request)

            prof = getattr(user, "profile", None)
            is_allowed = bool(user.is_superuser or (prof and prof.role == "SUPERADMIN"))

            if not is_allowed:
                # 403 is fine (clear + secure). You can change to 404 if you want.
                raise PermissionDenied("Admin is SuperAdmin-only.")

        return self.get_response(request)