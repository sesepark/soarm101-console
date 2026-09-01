# ADR 0002: 가상 리더 — 서버 in-process Hardware Owner와 목표 관절 명령

- Status: Accepted
- Date: 2026-09-01
- 관련: ADR 0001 (Exclusive Hardware Owner and Leased Command Authority)

## Context

기존 텔레옵은 `lerobot-teleoperate`를 서브프로세스로 띄우는 것이 전부였다. 그 프로세스는
물리 리더 팔의 관절값을 읽어 팔로워에 흘려보내고, 바깥에서 목표를 넣을 자리가 없다. 즉

- 물리 리더 팔이 **있어야만** 팔이 움직인다.
- 관절 상태를 밖으로 내보내는 경로가 없다.
- 목표 위치를 받아들이는 API가 없다.

ADR 0001은 command authority를 lease로 빌려주는 구조를 정했지만 구현은 없었다. Authority
Manager, Safety Validator, watchdog은 문서 상태로 남아 있었다.

한편 MacBook 앱과 아이폰에서 팔을 조작하고 싶다는 요구가 생겼다. 두 기기 모두 하드웨어에서
멀고, 둘 다 동시에 붙을 수 있다.

## Decision

**서버 안에 "가상 리더"라는 두 번째 Hardware Owner 구현을 둔다.** 물리 리더 팔이 있던 자리를
대신하며, 팔로워 serial을 in-process로 잡고 30Hz 루프를 돌린다.

- 현재 관절 위치·부하·전류·온도를 읽어 WebSocket으로 내보낸다.
- 검증을 통과한 목표 관절값을 받아 `SOFollower.send_action()`으로 쓴다.
- 기존 `lerobot-teleoperate` 텔레옵, `lerobot-record` 수집과 **상호배타**다. 셋 중 하나만
  장치를 연다(ADR 0001의 불변조건). 프로세스 경계의 장치별 lock은 ADR 0003에서 구현했다.

명령 권한은 ADR 0001이 정한 대로 lease다. Follower motion lease는 동시에 하나만 발급하고,
빼앗기는 없으며 명시적 반납이나 만료로만 풀린다. 관찰 구독은 여럿을 허용한다.

**데이터 수집은 소유권을 넘긴다.** `lerobot-record`가 도는 동안 팔로워 serial의 소유자는
record 프로세스이고, 콘솔은 장치를 놓은 **중계 모드**로 내려가 검증된 목표만 들고 있는다.
record 프로세스 안의 `SOArmVirtualLeader`(LeRobot `Teleoperator` 서브클래스)가 그 목표를
매 틱 가져가 `get_action()`으로 돌려준다.

## Consequences

장점:

- 물리 리더 팔 없이 텔레옵과 데이터 수집이 된다.
- 목표가 서버를 통과하므로 Safety Validator가 실제로 붙을 자리가 생겼다. 절대 한계, 틱당
  변화량, 자세 동기화, 부하·전류·추종오차·온도, 워치독이 전부 이 루프 안에 있다.
- 하나의 조작면(3D 뷰어)을 맥과 폰이 나눠 쓴다. 서버가 한 벌만 서빙한다.

비용:

- 서버가 제어 루프의 실시간성을 책임진다. 측정된 루프 시간은 30Hz에서 4.4ms이고, 부하·전류·
  온도는 10Hz로 낮춰 읽어 예산을 지킨다.
- **중계 모드에서는 안전 사다리의 관측 쪽 칸이 없다.** 부하·전류·온도·추종오차는 버스를 쥔
  쪽만 읽을 수 있고, 수집 중에는 그쪽이 LeRobot이다. 이 차이는 문서에 분명히 적었고, 없애려면
  record 루프 안으로 검사를 넣어야 한다(LeRobot을 고치는 일).
- 조작 권한을 가르는 토큰(`SOARM_MOTION_TOKEN`)이 하나 늘었다. 아이폰이 tailnet에서 붙는
  경로가 생겼기 때문이고, 없으면 서버는 어떤 조작 권한도 발급하지 않는다.

## Alternatives

### Mac에서 직접 serial을 연다

가장 짧은 길이지만 ADR 0001의 전제를 정면으로 깬다. 같은 팔에 두 곳에서 명령이 들어가고,
calibration과 장치 역할이 두 벌이 된다. 채택하지 않았다.

### `lerobot-teleoperate`에 목표를 주입한다

그 프로세스는 리더 버스에서 읽은 값을 그대로 쓰도록 되어 있어, 목표를 넣으려면 LeRobot을
고쳐야 한다. 상류를 포크하는 비용이 in-process 루프를 하나 두는 것보다 크다.

### 조작면을 서버 웹 콘솔에만 둔다

맥 앱은 이미 SSH 터널과 화면을 갖고 있고, 사용자는 앱 안에서 조작하기를 원했다. 3D는 웹으로
한 번만 만들고 맥이 `WKWebView`로 품는 절충을 택했다 — 3D 구현이 둘이면 두 기기가 같은 팔에
대해 서로 다른 그림을 그린다.

## Follow-up

- 접촉 문턱(`Present_Load` 400, `Present_Current` 108)은 유추값이다. 실물에서 재고 조정한다.
- URDF 관절 부호가 실물 회전 방향과 맞는지 확인한다(토크를 끈 채 손으로 움직이며).
- 수집 중에도 관측 쪽 안전 칸을 살릴 방법을 검토한다.
- 독립 actuator power cutoff는 여전히 없다.
