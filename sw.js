/* v24_album_rich Service Worker
 * - HTML: network-first (ensure updates)
 * - Images/Audio: cache-first (instant repeat visits)
 * - Others: stale-while-revalidate
 */

const VERSION = 'v24-2026-07-06-v035-caption-story';
const SHELL_CACHE = `v24-shell-${VERSION}`;
const RUNTIME_CACHE = `v24-runtime-${VERSION}`;

const SHELL_ASSETS = [
  './',
  './index.html',
  './assets/bgm.mp3',
  // small critical images (used only when album query hits)
  './assets/opt/photo_1.webp',
  './assets/opt/wedding/img_v3_02105_09d5c28a-b9ad-45b6-b430-3736e2f9b0fg.webp'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await Promise.allSettled(
        SHELL_ASSETS.map((asset) =>
          cache.add(asset).catch((err) => {
            console.warn('[SW] precache skipped:', asset, err);
          })
        )
      );
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
          if (k !== SHELL_CACHE && k !== RUNTIME_CACHE && (k.startsWith('v23-') || k.startsWith('v24-'))) {
            return caches.delete(k);
          }
          return Promise.resolve();
        })
      );
      await self.clients.claim();
    })()
  );
});

function offlineResponse(request) {
  if (request.mode === 'navigate' || request.destination === 'document') {
    return new Response('<!doctype html><meta charset="utf-8"><title>Offline</title><p>网络暂时不可用，请恢复连接后重试。</p>', {
      status: 503,
      statusText: 'Offline',
      headers: { 'Content-Type': 'text/html; charset=utf-8' }
    });
  }
  const contentTypes = {
    script: 'application/javascript; charset=utf-8',
    style: 'text/css; charset=utf-8',
    audio: 'audio/mpeg'
  };
  const contentType = contentTypes[request.destination];
  return new Response(null, {
    status: 503,
    statusText: 'Offline',
    headers: contentType ? { 'Content-Type': contentType } : undefined
  });
}

async function cacheSuccessfulResponse(cacheName, request, response) {
  if (!response || response.status !== 200 || request.headers.has('range')) return;
  try {
    const cache = await caches.open(cacheName);
    await cache.put(request, response.clone());
  } catch (error) {
    console.warn('[SW] runtime cache skipped:', request.url, error);
  }
}

async function cacheFirst(request) {
  if (request.headers.has('range')) {
    try {
      return await fetch(request);
    } catch (_) {
      return offlineResponse(request);
    }
  }
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    await cacheSuccessfulResponse(RUNTIME_CACHE, request, res);
    return res;
  } catch (_) {
    return offlineResponse(request);
  }
}

async function networkFirst(request) {
  try {
    const res = await fetch(request);
    await cacheSuccessfulResponse(SHELL_CACHE, request, res);
    return res;
  } catch (_) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return offlineResponse(request);
  }
}

async function staleWhileRevalidate(request, event) {
  const fetchPromise = fetch(request)
    .then(async (res) => {
      await cacheSuccessfulResponse(RUNTIME_CACHE, request, res);
      return res;
    })
    .catch(() => null);
  event.waitUntil(fetchPromise.then(() => undefined));
  const cached = await caches.match(request);

  if (cached) return cached;
  return (await fetchPromise) || offlineResponse(request);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin
  if (url.origin !== self.location.origin) return;

  // Never cache private uploads, runtime configuration, API calls, or non-GET requests.
  if (
    req.method !== 'GET'
    || url.pathname.startsWith('/api/')
    || url.pathname.startsWith('/uploads/')
    || url.pathname.endsWith('/config.js')
  ) return;

  // Navigation (HTML): network-first
  if (req.mode === 'navigate' || req.destination === 'document') {
    event.respondWith(networkFirst(req));
    return;
  }

  // Images / Audio: cache-first
  if (req.destination === 'image' || req.destination === 'audio') {
    event.respondWith(cacheFirst(req));
    return;
  }

  // Others: stale-while-revalidate
  event.respondWith(staleWhileRevalidate(req, event));
});

