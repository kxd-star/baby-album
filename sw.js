/* v23_album_rich Service Worker
 * - HTML: network-first (ensure updates)
 * - Images/Audio: cache-first (instant repeat visits)
 * - Others: stale-while-revalidate
 */

const VERSION = 'v23-2026-04-03-1';
const SHELL_CACHE = `v23-shell-${VERSION}`;
const RUNTIME_CACHE = `v23-runtime-${VERSION}`;

const SHELL_ASSETS = [
  './',
  './index.html',
  './assets/bgm.mp3',
  // small critical images (used only when album query hits)
  './assets/opt/photo_1.webp',
  './assets/opt/wedding/img_v3_02105_00a228c8-e3af-4e96-9c24-3ecbb865066g.webp'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await cache.addAll(SHELL_ASSETS);
      self.skipWaiting();
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.map((k) => {
          if (k !== SHELL_CACHE && k !== RUNTIME_CACHE && k.startsWith('v23-')) {
            return caches.delete(k);
          }
          return Promise.resolve();
        })
      );
      await self.clients.claim();
    })()
  );
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const res = await fetch(request);
  if (res && res.ok) {
    const cache = await caches.open(RUNTIME_CACHE);
    cache.put(request, res.clone());
  }
  return res;
}

async function networkFirst(request) {
  try {
    const res = await fetch(request);
    if (res && res.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, res.clone());
    }
    return res;
  } catch (_) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw _;
  }
}

async function staleWhileRevalidate(request) {
  const cached = await caches.match(request);
  const fetchPromise = fetch(request)
    .then(async (res) => {
      if (res && res.ok) {
        const cache = await caches.open(RUNTIME_CACHE);
        cache.put(request, res.clone());
      }
      return res;
    })
    .catch(() => null);

  return cached || (await fetchPromise);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin
  if (url.origin !== self.location.origin) return;

  // Navigation (HTML): network-first
  if (req.mode === 'navigate' || (req.destination === 'document')) {
    event.respondWith(networkFirst(req));
    return;
  }

  // Images / Audio: cache-first
  if (req.destination === 'image' || req.destination === 'audio') {
    event.respondWith(cacheFirst(req));
    return;
  }

  // Others: stale-while-revalidate
  event.respondWith(staleWhileRevalidate(req));
});

