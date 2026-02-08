from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils.deprecation import MiddlewareMixin

from .models import CompanyProfile


class TenantMiddleware(MiddlewareMixin):
    SAFE_PATH_PREFIXES = (
        "/login/",
        "/logout/",
        "/static/",
        "/media/",
        "/admin/",       # ✅ allow Django admin without tenant forcing
        "/superadmin/",  # ✅ allow superadmin without tenant forcing
    )

    def _host_no_port(self, request):
        return (request.get_host() or "").split(":")[0].lower()

    def _get_base_domain(self):
        """
        Returns base domain like: 'roznamcha.app' or 'lvh.me'
        """
        base = getattr(settings, "SAAS_BASE_DOMAIN", "") or ""
        return base.lstrip(".").lower()

    def _is_public_host(self, host, base):
        """
        Treat BOTH base domain and www.base as PUBLIC.
        Example: roznamcha.app and www.roznamcha.app
        """
        if host == base:
            return True
        if host == f"www.{base}":
            return True
        return False

    def _is_tenant_host(self, host, base):
        """
        Tenant hosts are: <something>.base (but NOT www.base)
        """
        if not host.endswith(f".{base}"):
            return False
        if host == f"www.{base}":
            return False
        return True

    def process_request(self, request):
        path = request.path or "/"

        # ✅ Always allow safe paths without forcing tenant resolution
        if any(path.startswith(p) for p in self.SAFE_PATH_PREFIXES):
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        host = self._host_no_port(request)
        base = self._get_base_domain()

        # If base domain isn't configured, don't guess
        if not base:
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        # ✅ Public host: no tenant resolution
        if self._is_public_host(host, base):
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        # ✅ Tenant host: requires authenticated user with OWNER/STAFF
        if not self._is_tenant_host(host, base):
            # Unknown host (safety)
            raise PermissionDenied("Invalid host.")

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            # Must login first
            request.tenant = None
            request.owner = None
            return None

        # Superuser should NOT be tenant-bound
        if getattr(user, "is_superuser", False):
            request.tenant = None
            request.owner = user
            return None

        profile = getattr(user, "profile", None)
        if not profile:
            raise PermissionDenied("User profile missing")

        role = getattr(profile, "role", None)

        if role == "OWNER":
            owner = user
        elif role == "STAFF":
            if not getattr(profile, "owner_id", None):
                raise PermissionDenied("Staff has no owner assigned")
            owner = profile.owner
        elif role == "SUPERADMIN":
            # profile-based superadmin bypass if you use it
            request.tenant = None
            request.owner = user
            return None
        else:
            raise PermissionDenied("Invalid role")

        company = CompanyProfile.objects.filter(owner=owner).first()
        if not company:
            raise PermissionDenied("Company not found")

        request.owner = owner
        request.tenant = company
        request.company = company
        return None