// Fraser Weather: keeps the app's own files on the phone so it opens with no signal.
// The forecasts themselves are saved by the page (localStorage); this only stores the page, its manifest and icons.
// The page is fetched fresh whenever there's a connection, so updates arrive; the stored copy is used only offline.
const CACHE = "fraser-app-v2";
const FILES = ["./", "./index.html", "./manifest.webmanifest", "../img/icon-192.png", "../img/icon-512.png", "../img/apple-touch-icon.png", "../img/favicon-32.png"];

self.addEventListener("install", e => {
  // Each file on its own, so one that can't be fetched doesn't stop the rest being kept
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(FILES.map(f => c.add(f).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Only this app's own files; weather requests go straight to the network and the page falls back to its saved copy
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return r;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }).then(m => m || caches.match("./index.html") || caches.match("./")))
  );
});
