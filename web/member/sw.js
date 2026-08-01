/**
 * 멤버십 앱(PWA) 서비스워커 — 범위는 /member/ 뿐.
 *
 * 원칙: **적립 정보는 절대 캐시하지 않는다.** 포인트·스탬프는 매장에서 수시로
 * 바뀌므로, 오래된 숫자를 보여주면 손님이 잘못 알게 된다. 따라서 API 응답은
 * 항상 네트워크로 가져오고, 캐시는 화면 껍데기(HTML·아이콘)에만 쓴다.
 */
const CACHE = "slowstep-member-v1";
const SHELL = ["./", "./index.html", "../assets/icon-192.png", "../assets/logo-mark.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  // API(적립 정보)는 캐시하지 않는다 — 항상 최신을 받아온다.
  if (req.url.includes("/api/")) return;

  // 화면 껍데기: 네트워크 우선, 실패하면 캐시(비행기모드·지하 등에서도 열리게)
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok && new URL(req.url).origin === location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("./")))
  );
});
