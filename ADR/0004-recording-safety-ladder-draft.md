# ADR 0004 초안: 데이터 수집 중 관측 안전 사다리

- Status: Proposed — 조사만 완료, 구현하지 않음
- Date: 2026-09-01
- 관련: ADR 0002
- 조사 기준: 저장소에 설치된 LeRobot 0.6.1

## Context

가상 리더 단독 운용에서는 콘솔의 in-process owner가 follower bus를 쥐고 위치를 30Hz,
`Present_Load`/`Present_Current`/`Present_Temperature`를 10Hz로 읽는다. 수집을 시작하면
`lerobot-record`의 `SOFollower`가 follower와 camera의 유일한 owner가 되고 콘솔은 목표만
중계한다. 이때 절대 한계, 틱당 변화량, lease, watchdog, LeRobot의
`max_relative_target`은 남지만 부하·전류·온도·추종오차 검사는 없다.

LeRobot 0.6.1의 `record_loop()` 순서는 다음과 같다.

```text
robot.get_observation()
teleop.get_action()
teleop_action_processor((action, observation))
robot_action_processor((action, observation))
robot.send_action()
```

기본 `SOFollower.get_observation()`은 관절 위치와 camera만 읽는다. health register는 읽지
않는다. `robot_action_processor`에는 robot/bus 객체가 전달되지 않는다. 별도 감시 프로세스의
"read-only"도 serial bus에서는 packet을 보내는 두 번째 owner다.

현재 프로젝트는 물리 텔레옵과 수집 config에 `disable_torque_on_disconnect=false`를 명시한다.
LeRobot 기본값 `true`에 기대면 record 예외/종료의 `disconnect()`가 토크를 꺼 팔을 떨어뜨릴
수 있기 때문이다. 이것은 안전 사다리 구현이 아니라 기존 불변조건을 지키기 위한 설정이다.

## 후보 1: `robot_action_processor`에 검사기 삽입

장점:

- `lerobot_record.record(..., robot_action_processor=...)`는 이미 공개된 주입 지점이다.
- action과 같은 tick의 위치 observation으로 절대 한계, 변화량, 일부 추종오차는 검사할 수 있다.
- 데이터셋에 저장하기 전·`send_action()` 전에 거절할 수 있다.

위험과 빈칸:

- processor에는 bus가 없고 observation에도 load/current/temperature가 없다. 요구한 네 신호를
  모두 보려면 결국 Robot 쪽 변경이나 health observation 확장이 필요하다.
- processor에서 예외만 던지면 record 전체가 finalize/disconnect된다. 토크를 유지하는 설정은
  가능하지만, 현재 자세를 명시적으로 HOLD하고 episode를 invalid로 표시하는 계약은 별도다.
- health read를 매 30Hz에 넣으면 loop budget과 camera 기록에 영향을 줄 수 있다. 가상 리더처럼
  10Hz cache가 필요하다.

상류 포크 비용: 추종오차 일부만이면 낮음(로컬 processor). health와 HOLD까지면 중간 이상.
상류 API가 robot/context를 processor에 넘기도록 바꾸면 계속 rebase해야 한다.

## 후보 2: LeRobot `Robot`을 감싸는 adapter

`SOFollower`를 상속/합성한 프로젝트 전용 Robot을 factory에 등록하고, 같은 프로세스·같은 bus에서
health를 10Hz로 읽어 cache한다. `get_observation()` 또는 `send_action()` 경계에서 기존
`TripDetector`와 같은 판정을 할 수 있다.

장점:

- 유일한 bus owner 안에서 네 신호를 전부 읽으므로 owner 불변조건을 지킨다.
- `send_action()` 직전의 실제 목표와 최근 위치를 함께 보아 추종오차를 계산할 수 있다.
- LeRobot core loop를 포크하지 않고 local Robot config/registry로 붙일 가능성이 가장 높다.

위험과 빈칸:

- health key를 dataset observation에 노출할지 내부 cache로만 둘지 정해야 한다. features와 frame
  모양이 어긋나면 수집 자체가 실패한다.
- trip 뒤에는 새 action을 쓰지 않고 현재 위치를 goal로 한 번 고정한 뒤 HOLD해야 한다. 예외로
  빠지는 것만으로 HOLD가 검증되었다고 볼 수 없다.
- Robot은 `record_loop`의 episode event 표를 받지 않는다. episode invalid/stop 사유를 전달할
  callback 또는 작은 upstream hook이 필요하다.
- bus read 실패 중에는 현재 자세 write도 실패할 수 있다. 이 경우에도 torque-off는 하지 않고
  fault를 기록해야 한다.

상류 포크 비용: local adapter와 명시적 callback을 허용하면 중간, LeRobot core 포크는 낮거나
없음. 0.6.1 factory/registry 동작을 고정하는 회귀 시험이 필요하다.

## 후보 3: 별도 read-only 감시 프로세스

채택 후보에서 제외한다. Feetech half-duplex serial에서 `Present_*` read는 수동 관찰이 아니라
요청 packet을 보내고 응답을 받는 I/O다. record와 감시 프로세스의 요청/응답이 섞이면 이미 본
`Incorrect status packet` 유형의 고장을 만들며 ADR 0001과 ADR 0003을 위반한다. 파일 lock을
read/shared로 바꾸어도 전기적 bus 경쟁은 해결되지 않는다.

상류 포크 비용은 낮아 보여도 위험이 구조적이므로 선택하지 않는다.

## 잠정 결론

**후보 2의 Robot adapter를 우선 prototype하고, episode event 전달에 필요한 최소 hook만 후보
1의 processor/callback으로 보완**하는 방향이 가장 작다. 다음 구현 전에 다음을 먼저 시험한다.

1. simulated Robot으로 health 10Hz cache와 30Hz loop budget 시험
2. trip 뒤 torque를 유지한 HOLD, 새 action 거절, episode invalid 표기 시험
3. 정상/예외 disconnect 모두 torque-off를 호출하지 않는 회귀 시험
4. 물리/가상 leader 수집 모두 같은 adapter를 거치는지 factory 시험
5. 실제 팔 시험은 현장 사람과 별도 절차가 있을 때만 수행

이 문서는 설계 초안이다. adapter, processor, 수집 중 health 감지는 **아직 구현되지 않았다.**
