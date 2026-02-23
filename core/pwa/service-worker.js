/* Roznamcha Offline Service Worker (simple + safe) */

const CACHE_VERSION = "roznamcha-offline-v2";
const OFFLINE_URL = "/offline/";

// Keep this list small (only must-have assets)
const PRECACHE_URLS = [
  OFFLINE_URL,
  "/static/core/branding/roznamcha.png",
];

const BRANDING_PATHS = new Set([
  "/static/core/branding/roznamcha.png",
  "/static/core/pwa/icons/icon-192.png",
  "/static/core/pwa/icons/icon-512.png",
  "/static/core/pwa/icons/maskable-192.png",
  "/static/core/pwa/icons/maskable-512.png",
]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => (key !== CACHE_VERSION ? caches.delete(key) : null))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle GET
  if (req.method !== "GET") return;

  // Navigation requests (HTML pages): Network-first, fallback to offline (NO caching)
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(async () => {
        const cache = await caches.open(CACHE_VERSION);
        const cachedOffline = await cache.match(OFFLINE_URL);
        return cachedOffline || new Response("Offline", { status: 503 });
      })
    );
    return;
  }

  // Branding and app icons: Network-first so logo/icon updates roll out quickly.
  if (url.origin === self.location.origin && BRANDING_PATHS.has(url.pathname)) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          return cached || new Response("Not available offline", { status: 503 });
        })
    );
    return;
  }

  // Other static assets: Cache-first
  if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy));
          return res;
        });
      })
    );
  }
});
