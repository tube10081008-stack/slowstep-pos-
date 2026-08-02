/**
 * 멤버십 앱(PWA) 서비스워커 — 범위는 /member/ 뿐.
 *
 * **아무것도 캐시하지 않는다.** 포인트·스탬프는 매장에서 수시로 바뀌므로
 * 오래된 값을 보여주면 안 되고, 데이터는 어차피 서버에 있다. 오프라인에서
 * 껍데기만 열어봐야 보여줄 게 없으므로 캐시할 이유가 없다.
 *
 * 이 파일이 존재하는 이유는 하나 — 브라우저가 '홈 화면에 추가'를 제안하려면
 * 등록된 서비스워커를 요구하는 경우가 있어서다. 요청은 그대로 통과시킨다.
 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
// fetch 핸들러는 두되 가로채지 않는다(설치 조건 충족 + 항상 최신 데이터).
self.addEventListener("fetch", () => {});
