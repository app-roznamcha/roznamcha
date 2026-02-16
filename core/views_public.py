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