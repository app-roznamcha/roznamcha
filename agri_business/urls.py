# agri_business/urls.py
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.views.static import serve



urlpatterns = [
    path("admin/", admin.site.urls),
    
    # App routes
    path("", include("core.urls")),

    # Custom login
    path("login/", __import__("core.views").views.TenantAwareLoginView.as_view(), name="login"),

    # Logout (use Django default)
    path("logout/", auth_views.LogoutView.as_view(next_page="landing"), name="logout"),

]

urlpatterns += [
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]

handler403 = "core.views.subscription_forbidden"
