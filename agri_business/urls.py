# agri_business/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static
from django.conf import settings
from django.contrib.auth import views as auth_views
from core.admin import SuperAdminOnlyAdminSite



superadmin_site = SuperAdminOnlyAdminSite()

urlpatterns = [
    path("admin/", superadmin_site.urls),

    # App routes
    path("", include("core.urls")),

    # Custom login
    path("login/", __import__("core.views").views.TenantAwareLoginView.as_view(), name="login"),

    # Logout (use Django default)
    path("logout/", auth_views.LogoutView.as_view(next_page="landing"), name="logout"),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler403 = "core.views.subscription_forbidden"