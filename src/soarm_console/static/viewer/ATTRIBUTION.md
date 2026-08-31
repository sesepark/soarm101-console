# 이 폴더에 들어 있는 남의 것

3D 뷰어가 쓰는 외부 자산의 출처다. 인터넷 없는 환경에서도 열려야 하므로 CDN을 부르지 않고
전부 서버가 직접 서빙한다.

## URDF — `urdf/so101.urdf`

`renesas-rdk/so_arm101_description`의 `urdf/reference/so101_new_calib.urdf`를 그대로 가져왔다.
이것을 고른 이유는 관절 이름이 LeRobot의 모터 이름(`shoulder_pan` … `gripper`)과 정확히
같고, 한계가 0을 가운데 둔 라디안이라 우리가 쓰는 도(degree) 단위와 그대로 대응하기
때문이다. 그 저장소에는 LICENSE 파일이 없다. 형상 자체는 상류
`TheRobotStudio/SO-ARM100`(Apache-2.0)에서 나온 것이다.

## 메시 — `urdf/assets/*.stl`

`timqian/bambot`(Apache-2.0)의 `website/public/URDFs/assets/`에서 가져왔다. 위 URDF가
참조하는 것과 같은 파일이며(이름과 크기가 일치한다), 라이선스가 분명한 쪽을 택했다.

**정점 클러스터링으로 줄여 두었다.** 원본 322,564 삼각형 · 19MB → 38,333 삼각형 · 1.9MB
(격자 1.2mm). 이 뷰어는 조작면이지 렌더링 시연이 아니고, 아이폰이 SSH 터널이나 tailnet
너머로 19MB를 받아야 할 이유가 없다. 원본이 필요하면 위 저장소에서 다시 받으면 된다.

## three.js — `vendor/`

- `three.module.min.js` — three.js r160 (MIT)
- `OrbitControls.js`, `STLLoader.js` — 같은 릴리스의 `examples/jsm` (MIT)

`urdf-loader` 패키지는 쓰지 않는다. 필요한 것은 link/joint/visual 세 가지뿐이고 그것은
`urdf-mini.js` 100여 줄로 끝나서, 의존성 하나를 더 들여올 이유가 없었다.
