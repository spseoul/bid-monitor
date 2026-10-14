/* 입찰공고 모니터 — 서비스 워커
   index.html/아이콘: 캐시 우선 + 백그라운드 갱신, data.json: 네트워크 우선(8초) + 실패 시 캐시 */
const CACHE = "bidmon-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
function withTimeout(p, ms) { return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]); }
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;
  const isData = req.url.includes("data.json");
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const key = isData ? new Request(req.url.split("?")[0]) : req;   // data.json 은 ?v= 를 떼고 한 벌만 보관
    if (isData) {
      try {
        const res = await withTimeout(fetch(req, { cache: "no-store" }), 8000);
        if (res.ok) cache.put(key, res.clone());
        return res;
      } catch (_) {
        const hit = await cache.match(key);
        if (hit) return hit;
        throw _;
      }
    }
    const hit = await cache.match(key);
    const net = fetch(req).then(res => { if (res.ok) cache.put(key, res.clone()); return res; }).catch(() => null);
    return hit || (await net) || Response.error();
  })());
});
