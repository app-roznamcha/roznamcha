# core/views_public.py (or wherever your public views are)
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET


def robots_txt(request):
    content = """User-agent: *
Disallow: /admin/
Disallow: /login/
Disallow: /signup/
Allow: /

Sitemap: https://roznamcha.app/sitemap.xml
"""
    return HttpResponse(content, content_type="text/plain")

def google_verify(request):
    return render(request, "googlea8d36177338cf4b5.html")


@require_GET
def pwa_manifest(request):
    manifest = {
        "id": "/",
        "name": "Roznamcha",
        "short_name": "Roznamcha",
        "description": "Smart Khata & Hisab System",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": "#f3f4f6",
        "theme_color": "#2563eb",
        "orientation": "portrait",
        "icons": [
            {
                "src": static("core/pwa/icons/icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("core/pwa/icons/icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("core/pwa/icons/maskable-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "maskable",
            },
            {
                "src": static("core/pwa/icons/maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JsonResponse(manifest, json_dumps_params={"ensure_ascii": False})


@require_GET
def assetlinks_json(request):
    app_id = getattr(settings, "ANDROID_APP_ID", "").strip()
    fingerprints = [
        item.strip()
        for item in getattr(settings, "ANDROID_SHA256_CERT_FINGERPRINTS", [])
        if item.strip()
    ]

    if not app_id or not fingerprints:
        # Keep response valid JSON even before Android metadata is configured.
        response = JsonResponse([], safe=False)
        response["Cache-Control"] = "no-store, must-revalidate"
        return response

    payload = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": app_id,
                "sha256_cert_fingerprints": fingerprints,
            },
        }
    ]
    response = JsonResponse(payload, safe=False)
    response["Cache-Control"] = "public, max-age=300"
    return response


# ✅ ADD THIS
def sitemap_xml(request):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://roznamcha.app/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/login/</loc>
    <changefreq>monthly</changefreq>
    <priority>0.4</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/signup/</loc>
    <changefreq>monthly</changefreq>
    <priority>0.4</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/privacy/</loc>
    <changefreq>yearly</changefreq>
    <priority>0.2</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/terms/</loc>
    <changefreq>yearly</changefreq>
    <priority>0.2</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/refund/</loc>
    <changefreq>yearly</changefreq>
    <priority>0.2</priority>
  </url>
  <url>
    <loc>https://roznamcha.app/service/</loc>
    <changefreq>yearly</changefreq>
    <priority>0.2</priority>
  </url>
</urlset>
"""
    return HttpResponse(xml, content_type="application/xml")
