// 아무것도 캐시하지 않는 서비스 워커.
//
// 있는 이유는 하나다 — 크롬 계열이 "홈 화면에 추가"를 제안하려면 워커가 하나 있어야
// 한다. 아이폰 사파리는 워커 없이도 `apple-mobile-web-app-capable`만으로 전체화면
// 웹앱이 되므로, 이 파일은 안드로이드와 데스크톱 크롬을 위한 것이다.
//
// **캐시는 두지 않는다.** 이 화면은 늘 서버와 이어져 있어야 하고, 오래된 조작 화면이
// 캐시에서 살아 돌아오는 것은 그 자체로 사고다. 옛 화면이 새 서버의 거절 코드를 모르면
// 팔이 왜 안 움직이는지 아무 말도 하지 못한다.
self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
