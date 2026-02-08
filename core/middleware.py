from django.conf import settings
from django.core.exceptions import PermissionDenied
from .models import CompanyProfile
from django.utils.deprecation import MiddlewareMixin
from django.shortcuts import redirect


class TenantMiddleware(MiddlewareMixin):
    SAFE_PATH_PREFIXES = (
        "/login/",
        "/logout/",
        "/static/",
        "/media/",
    )

    def process_request(self, request):
        path = request.path or "/"

        # ✅ SUPERADMIN routes must run ONLY on PUBLIC domain (not on tenant subdomains)
        if request.path.startswith("/superadmin/"):
            host = request.get_host().split(":")[0]  # remove port
            public_base = (getattr(settings, "SAAS_BASE_DOMAIN", "") or "").lstrip(".")

            # Local dev special: if SAAS_BASE_DOMAIN is lvh.me, public domain is lvh.me
            # If request is coming from alpha.lvh.me, redirect to lvh.me
            if public_base and host != public_base and host.endswith("." + public_base):
                scheme = "https" if request.is_secure() else "http"
                port = ""
                # keep port in local dev (e.g. :8000)
                if ":" in request.get_host():
                    port = ":" + request.get_host().split(":")[1]

                return redirect(f"{scheme}://{public_base}{port}{request.get_full_path()}")

            # ✅ mark as public request (no tenant resolution)
            request.owner = None
            request.company = None
            return self.get_response(request)

        if any(path.startswith(p) for p in self.SAFE_PATH_PREFIXES):
            return

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            request.tenant = None
            request.owner = None
            return

        # Django superuser (Super Admin) must always live on PUBLIC domain (no tenant subdomain)
        if getattr(user, "is_superuser", False):
            public_base = (getattr(settings, "PUBLIC_BASE_DOMAIN", "") or "").replace("https://", "").replace("http://", "")
            public_host = public_base.split(":")[0]  # lvh.me or roznamcha.app

            host = (request.get_host() or "").split(":")[0]
            scheme = "https" if request.is_secure() else "http"

            # If superadmin accidentally opens a tenant subdomain, bounce to public domain
            if host.endswith("." + public_host):
                return redirect(f"{scheme}://{public_base}{request.get_full_path()}")

            request.tenant = None
            request.owner = user
            return
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
            return
        else:
            raise PermissionDenied("Invalid role")

        company = CompanyProfile.objects.filter(owner=owner).first()
        if not company:
            raise PermissionDenied("Company not found")

        # HARD MATCH if subdomain resolved tenant
        current = getattr(request, "tenant", None)
        if current is not None and getattr(current, "id", None) != company.id:
            raise PermissionDenied("Tenant mismatch (wrong subdomain).")

        request.owner = owner
        request.tenant = company
        SubdomainTenantMiddleware = TenantMiddleware