// Consolidated Weather: keeps the app's own files on the device so it opens with no signal.
// The forecasts and the list of locations are saved by the page (localStorage); this stores the page, its manifest,
// the icons, the background pictures it has already shown, and the Google fonts.
// The page is fetched fresh whenever there's a connection, so updates arrive; the stored copy is used only when the
// network fails, or is too slow to answer (a weak signal that never quite connects).
const APP = "cw-app-v1", FONTS = "cw-fonts-v1";
const FILES = ["./", "./index.html", "./manifest.webmanifest", "./img/icon-192.png", "./img/icon-512.png", "./img/apple-touch-icon.png", "./img/favicon-32.png"];
const BASE = new URL("./", self.location).href;  // the app's folder, e.g. https://deskjet808.github.io/consolidated-weather/
const PAGE = BASE + "index.html";
// Separate apps in sub-folders, each with its own page (and Fraser with its own service worker): never answered here
const OTHERS = ["fraser/", "aero/"];
const SLOW_PAGE_MS = 5000;

// Each file on its own, so one that can't be fetched doesn't stop the rest being kept.
// "reload" skips the browser's own short-term copy, so a new version stores the newest files.
// After a fresh page visit only missing files are fetched again: the Fraser app's worker clears every store but its
// own when it updates (they share this site), so this puts ours back the next time there's a connection.
const topUp = all => caches.open(APP).then(c => Promise.all(FILES.map(f => {
  const url = new URL(f, BASE).href;
  return (all ? Promise.resolve(null) : c.match(url)).then(m => m || c.add(new Request(url, { cache: "reload" })).catch(() => {}));
})));

self.addEventListener("install", e => {
  e.waitUntil(topUp(true).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  // Only this app's old stores: Fraser, Aero and the private app share this site's storage and keep their own
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith("cw-") && k !== APP && k !== FONTS).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === "https://fonts.googleapis.com") return e.respondWith(fontCss(e, req));
  if (url.origin === "https://fonts.gstatic.com") return e.respondWith(fontFile(e, req));
  if (!url.href.startsWith(BASE)) return;
  const path = url.pathname.slice(new URL(BASE).pathname.length);
  if (OTHERS.some(p => path.startsWith(p))) return;
  const page = path === "" || path === "index.html";
  // Only the app's own files. Forecast data, the scraper's files and anything else go straight to the network
  // (the page falls back to its own saved copy); so do the storm videos, which are fetched in pieces (Range requests)
  // that can't be stored, and are large; their still pictures are stored instead.
  if (!(page || path === "manifest.webmanifest" || /^img\/[^/]+\.(png|jpe?g|webp|svg)$/i.test(path))) return;
  if (req.headers.has("range")) return;
  e.respondWith(networkFirst(e, req, page));
});

// Fetch, and keep a copy of a good answer under `key`. The keeping is registered with the event straight away,
// so it still finishes after the page has been given an answer.
function fetchAndKeep(e, req, store, key, ok = r => r.ok) {
  let kept = null;
  const fresh = fetch(req).then(r => {
    if (ok(r)) { const copy = r.clone(); kept = caches.open(store).then(c => c.put(key, copy)); }
    return r;
  });
  e.waitUntil(fresh.then(() => kept).catch(() => {}));
  return fresh;
}

// Fresh from the network when it answers, else the stored copy. Every ?loc= link is the same page, so it's kept once.
function networkFirst(e, req, page) {
  const key = page ? PAGE : req.url;
  const saved = () => caches.open(APP).then(c => c.match(key, { ignoreSearch: true }).then(m => m || (page ? c.match(BASE) : undefined)));
  // A redirected answer can't be given back for a page visit later, so it isn't kept
  const fresh = fetchAndKeep(e, req, APP, key, r => r.ok && !r.redirected);
  if (!page) return fresh.catch(() => saved().then(m => m || Response.error()));
  e.waitUntil(fresh.then(r => r.ok && topUp(false)).catch(() => {}));
  // The page itself: if the network is still thinking after a few seconds, open the stored copy and let the
  // fresh one finish in the background, ready for next time
  return new Promise(resolve => {
    let done = false;
    const give = r => { if (!done && r) { done = true; resolve(r); } };
    fresh.then(give, () => saved().then(m => give(m || Response.error())));
    setTimeout(() => saved().then(give), SLOW_PAGE_MS);
  });
}

// The fonts' style sheet: the stored copy straight away (it holds up drawing the page), refreshed in the background.
// It's asked for with CORS (Google Fonts allows it) so the stored copy is a readable answer that can be checked,
// not an opaque one, which can't be checked for errors and counts as several MB against the storage allowance.
function fontCss(e, req) {
  const fresh = fetchAndKeep(e, new Request(req.url, { mode: "cors", credentials: "omit" }), FONTS, req.url);
  return caches.open(FONTS).then(c => c.match(req.url)).then(m => m ||
    // Nothing stored yet: the fresh copy, or failing that the browser's own request as it was
    fresh.then(r => r.ok ? r : fetch(req)).catch(() => fetch(req)));
}

// The font files themselves never change at the same address, so a stored one is used without asking again.
// The page asks for them with CORS, so only proper (non-opaque) answers are kept.
function fontFile(e, req) {
  return caches.open(FONTS).then(c => c.match(req)).then(m => m || fetchAndKeep(e, req, FONTS, req, r => r.ok && r.type !== "opaque"));
}
