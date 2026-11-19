// Service worker mínimo: permite instalar la app y abrir el último análisis sin conexión.
const CACHE = 'pronosticos-v1';
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(['./', 'index.html', 'icon.svg', 'manifest.webmanifest']))));
self.addEventListener('fetch', e => {
  // red primero (datos siempre frescos), caché como respaldo
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    caches.open(CACHE).then(c => c.put(e.request.url.split('?')[0], copy));
    return r;
  }).catch(() => caches.match(e.request.url.split('?')[0])));
});
