# core/views_public.py (or wherever your public views are)
from django.http import HttpResponse
from django.shortcuts import render


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