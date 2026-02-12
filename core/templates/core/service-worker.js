/* Roznamcha PWA Service Worker (Phase 3 - safe minimal cache) */

const CACHE_NAME = "roznamcha-v1";

const STATIC_ASSETS = [
  "/",                       // landing/dashboard redirect handled by app
  "/static/core/branding/roznamcha.png",
  "/static/core/pwa/manifest.webmanifest",
  "/static/core/pwa/icons/icon-192.png",
  "/static/core/pwa/icons/icon-512.png",
  "/static/core/pwa/icons/maskable-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

/**
 * Safe strategy:
 * - GET only
 * - cache-first for static assets
 * - network-first for pages (so forms/data stay fresh)
 */
self.addEventListener("fetch", (event) => {
  const req = event.request;

  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Cache-first for static files
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  // Network-first for pages
  event.respondWith(
    fetch(req)
      .then((res) => {
        // Optional: cache HTML pages lightly (disabled for now to stay safest)
        return res;
      })
      .catch(() => caches.match(req))
  );
});