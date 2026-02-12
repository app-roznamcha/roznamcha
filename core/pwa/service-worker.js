/* Roznamcha Offline Service Worker (simple + safe) */

const CACHE_VERSION = "roznamcha-offline-v1";
const OFFLINE_URL = "/offline/";

// Keep this list small (only must-have assets)
const PRECACHE_URLS = [
  OFFLINE_URL,

  // Branding/logo
  "/static/core/branding/roznamcha.png",

  // Manifest + icons (keep paths matching your static locations)
  "/static/core/pwa/manifest.webmanifest",
  "/static/core/pwa/icons/icon-192.png",
  "/static/core/pwa/icons/icon-512.png",
  "/static/core/pwa/icons/maskable-512.png",
  "/static/core/pwa/icons/apple-touch-icon.png",
];

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

  // Only handle GET
  if (req.method !== "GET") return;

  // Navigation requests (HTML pages): Network-first, fallback to offline
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          // Optionally cache the latest visited page (light caching)
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, copy));
          return res;
        })
        .catch(async () => {
          // If offline, show offline page
          const cache = await caches.open(CACHE_VERSION);
          const cachedOffline = await cache.match(OFFLINE_URL);
          return cachedOffline || new Response("Offline", { status: 503 });
        })
    );
    return;
  }

  // Static assets: Cache-first
  const url = new URL(req.url);
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