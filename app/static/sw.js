const CACHE_NAME = 'chat-audit-static-v16';
const STATIC_ASSETS = [
  '/',
  '/assets/favicon.svg',
  '/assets/app.min.css?v=20260827-1',
  '/assets/app.min.js?v=20260827-1',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).then((response) => {
        // 只缓存成功响应：把一次 500 或 401 存进来，之后即使服务恢复，
        // 离线回退也会一直把那个错误页当成"首页"返回。
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('/', copy));
        }
        return response;
      }).catch(() => caches.match('/'))
    );
    return;
  }
  if (!STATIC_ASSETS.some((asset) => new URL(asset, self.location.origin).pathname === url.pathname)) {
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
      }
      return response;
    }))
  );
});
