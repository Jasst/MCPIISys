/* Service worker: делает приложение устанавливаемым и открывает оболочку офлайн.
 * Ответы ИИ, входа и API НЕ кешируются: это всегда свежие данные пользователя.
 */
const VERSION = 'ai-v2';
const STATIC_CACHE = `static-${VERSION}`;
const SHELL = [
  '/static/style.css',
  '/static/ai.css',
  '/static/js/ai-app.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/favicon.ico',
  '/static/icons/Attach.png',
  '/static/icons/Copy.png',
  '/static/icons/Download.png',
  '/static/icons/Edit.png',
  '/static/icons/Remove.png',
  '/static/icons/Share.png',
  '/manifest.json',
  '/offline',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('static-') && k !== STATIC_CACHE).map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

async function networkFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    return (await cache.match(request)) || Response.error();
  }
}

// Обновляем статику в фоне, а из кеша отдаём сразу
async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;   // внешние CDN не трогаем

  // Страницы: сначала сеть, без связи — офлайн-страница
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/offline'))
    );
    return;
  }

  // JS и CSS всегда сначала из сети: иначе старый скрипт может работать с новым HTML
  if (url.pathname.endsWith('.js') || url.pathname.endsWith('.css')) {
    event.respondWith(networkFirst(request));
    return;
  }

  // Иконки и манифест: из кеша, с фоновым обновлением
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    event.respondWith(staleWhileRevalidate(request));
  }
});
