from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils.deprecation import MiddlewareMixin

from .models import CompanyProfile


class TenantMiddleware(MiddlewareMixin):
    SAFE_PATH_PREFIXES = (
        "/login/",
        "/logout/",
        "/subscription/",   # ✅ add this
        "/static/",
        "/media/",
        "/admin/",
        "/superadmin/",
    )

    def _host_no_port(self, request):
        return (request.get_host() or "").split(":")[0].lower()

    def _get_base_domain(self):
        base = getattr(settings, "SAAS_BASE_DOMAIN", "") or ""
        return base.lstrip(".").lower()

    def _is_public_host(self, host, base):
        return host == base or host == f"www.{base}"

    def _is_tenant_host(self, host, base):
        return host.endswith(f".{base}") and host != f"www.{base}"

    def _extract_slug(self, host, base):
        # zafar.roznamcha.app -> zafar
        return host[: -(len(base) + 1)]

    def process_request(self, request):
        path = request.path or "/"

        # ✅ Safe paths: no tenant forcing
        if any(path.startswith(p) for p in self.SAFE_PATH_PREFIXES):
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        host = self._host_no_port(request)
        base = self._get_base_domain()

        if not base:
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        # ✅ Public domain
        if self._is_public_host(host, base):
            request.tenant = None
            request.owner = None
            request.company = None
            return None

        # ❌ Invalid host
        if not self._is_tenant_host(host, base):
            raise PermissionDenied("Invalid host.")

        # ✅ Resolve company from slug
        slug = self._extract_slug(host, base)

        try:
            company = CompanyProfile.objects.select_related("owner").get(slug=slug)
        except CompanyProfile.DoesNotExist:
            raise PermissionDenied("Unknown tenant.")

        user = getattr(request, "user", None)

        # Not logged in → allow login flow
        if not user or not user.is_authenticated:
            request.tenant = company
            request.owner = company.owner
            request.company = company
            return None

        # Superuser bypass (but keep tenant context based on host)
        if user.is_superuser:
            request.tenant = company
            request.owner = company.owner   # tenant owner (not superadmin)
            request.company = company
            return None
        profile = getattr(user, "profile", None)
        if not profile:
            raise PermissionDenied("User profile missing")

        # Determine expected owner
        if profile.role == "OWNER":
            owner = user
        elif profile.role == "STAFF":
            owner = profile.owner
        elif profile.role == "SUPERADMIN":
            request.tenant = None
            request.owner = user
            request.company = None
            return None
        else:
            raise PermissionDenied("Invalid role")

        # 🔒 HARD TENANT MATCH
        if owner != company.owner:
            raise PermissionDenied("Tenant mismatch.")
        
                # =========================
        # Subscription enforcement (OWNER + STAFF)
        # =========================
        if not user.is_superuser:
            # owner is already resolved as OWNER user (either user itself or staff's owner)
            owner_profile = getattr(owner, "profile", None)
            status = owner_profile.get_effective_status() if owner_profile else "EXPIRED"

            # Allow subscription page + auth pages even if expired
            if status == "EXPIRED":
                if not path.startswith("/subscription/"):
                    # force them to renew
                    from django.shortcuts import redirect
                    return redirect("subscription_page")

        request.tenant = company
        request.owner = owner
        request.company = company

        return None