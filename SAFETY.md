# Safety and Operational Invariants

## 목적

이 문서는 실험을 불필요하게 막지 않으면서, 동시 명령 충돌과 복구 불가능한 상태처럼 명백한 위험만 일관되게 방지한다.

이 프로젝트는 산업용 안전 인증을 받은 시스템이 아니다. 아래 원칙은 위험을 줄이기 위한 개발 규칙이며 안전 인증을 의미하지 않는다.

## 현재 보장 수준

**가상 리더 경로(2026-09-01)**: lease, heartbeat, watchdog, HOLD, 절대 관절 한계, 틱당
변화량, 자세 동기화, 부하·전류·추종오차·온도 트립이 구현되어 있고 fault injection을 포함한
hardware-free 시험을 통과한다. 문턱값과 근거, 그리고 **아직 실측하지 못한 것**은 이 저장소의
`RUNBOOK.md` 6절에 정직하게 적혀 있다. 실제 motion 검증은 아직 하지 않았다 —
접촉 문턱값은 데이터시트와 LeRobot 기본값에서 유추한 값이다.

**기존 물리 리더 텔레옵 경로**: lease도 watchdog도 없다. `soarm_console.teleoperating`
서브프로세스 하나이고, 보호 장치는 LeRobot의 `max_relative_target`과 사람의 손뿐이다.
(2026-09-05 이전에는 `lerobot-teleoperate` 바이너리였다. 아래 "부드러운 시작"이 CLI
플래그로 부탁할 수 있는 일이 아니어서 우리 모듈로 옮겼고, 루프 자체와 처리 파이프라인은
`lerobot_teleoperate.teleop_loop`를 그대로 쓴다.)

**부드러운 시작(2026-09-05)**: 텔레옵과 수집이 시작하는 순간 팔이 튀던 두 자리를 없앴다.

*붙는 순간*: STS3215는 `Torque_Enable=1`을 쓰는 순간 서보에 남아 있던 옛 `Goal_Position`을
향해 **최고 속도로** 간다(현재 자세를 목표로 삼는 것은 그 레지스터에 값 128을 쓸 때뿐이고,
LeRobot은 그렇게 쓰지 않는다). 그래서 `follower_start.sync_goal_to_present`가 토크가 켜지기
전에 목표를 지금 자리로 옮긴다 — 재생 경로와 가상 리더 백엔드가 이미 하던 일을 수집과
물리 리더 텔레옵에도 넣은 것이다. `SOFollower.configure`를 감싸는 방식이라, 자식 프로세스
안쪽에서 LeRobot이 로봇을 만드는 경로에서도 반드시 지난다.

*첫 틱*: LeRobot의 텔레옵 루프는 첫 틱에 리더 자세를 그대로 팔로워에 보낸다. 두 팔이 다른
자세로 서 있으면 팔로워는 그 차이만큼 한 번에 뛰고, `SOARM_MAX_RELATIVE_TARGET=1000`이라
잘리지도 않는다. 루프에 들어가기 전에 팔로워를 리더의 **지금** 자세까지 재생과 같은
s-curve로 걸어간다(첨두 40°/s, 집게 50%/s, 최소 1초·최대 6초). 수집에서는 첫 회차의
`record_loop` 직전에 한 번만 하고 그동안 `phase="aligning"`을 내보낸다. 가상 리더로 찍을
때는 하지 않는다 — `vleader.start_relay`가 팔로워의 지금 자세에서 목표를 이어 준다.
SIGINT가 오면 그 자리에서 서고 토크는 유지한다.

**진단이 더 이상 토크를 묻지 않는다(2026-09-05)**: 한때 텔레옵·수집 시작은 여섯 모터가 전부
`Torque_Enable == 0`이어야 통과했다. 그런데 두 경로 모두 팔이 떨어지지 않도록
`disable_torque_on_disconnect=False`로 끝나므로, **정상적으로 끝낸 세션 다음의 시작은 반드시
거절**이었다. 사람은 세션마다 진단을 돌리고 토크를 풀어 팔을 떨어뜨린 뒤 다시 시작했는데,
그 처진 팔이야말로 다음 `configure()`가 토크를 걸 때 위로 튀는 원인이었다 — 게이트가 요구한
절차가 게이트가 막으려던 사고를 만들고 있었다. 지금 시작을 막는 것은 모터가 답하지 않거나
전압이 범위 밖일 때뿐이고(`Hardware doctor did not pass: <어느 팔, 무엇이>`), "다른 프로세스가
이미 팔을 쥐고 있는가"는 `flock` owner lock이 맡는다(ADR 0003). `torque_enabled`는 진단
응답에 그대로 남아 있고 화면의 `토크 해제` 단추가 그것을 본다. 모터별 `Present_Temperature`도
함께 읽는다 — STS3215는 과열로 스스로 토크를 끊으므로, 팔이 이유 없이 힘을 잃었을 때 물어볼
곳이 있어야 한다.

프로젝트가 제어하는 가상 리더·물리 텔레옵·수집·카메라·진단·접촉 측정 경로에는 장치별
`flock` owner lock이 구현되어 있다([ADR 0003](ADR/0003-device-owner-lock.md)). 프로세스
상태 검사와 409만 있던 때와 달리 콘솔 프로세스가 달라도 서로 막는다. 다만 advisory lock을
무시하고 upstream LeRobot/serial library를 직접 실행한 프로세스까지 OS가 막아 주는 것은
아니다. 그 경계에는 udev 권한 격리 같은 별도 설계가 필요하다.

**재생 경로(2026-09-05)**: 찍어 둔 에피소드를 팔로워에 다시 흘린다. 이 프로젝트에서 사람의
손이 팔의 목표를 만들지 않는 유일한 경로이므로 앞뒤에 게이트를 더 두었다. 시작은
`REPLAY SOARM101` 확인 문구와 모션 게이트를 지나고, 텔레옵·수집·가상 리더가 도는 동안에는
409로 거절한다(팔로워의 명령 권한은 하나다). 시작하기 전에 팔로워의 `Present_Position`을
읽어 에피소드의 첫 action과 견준다. 그 거리로 거절하는 문턱은 한때 60도였는데 지금은 관절
최대 폭(360도)이다 — 사실상 거리로는 거절하지 않고, NaN·Inf와 관절 이름 불일치처럼 **뜻을 알
수 없는 값**만 400으로 거절한다는 뜻이다. 60도를 걷어 낸 이유는 그 거절이 사람에게 시키는
일이 거절이 막으려던 일보다 번거로웠기 때문이다: 거절당한 팔은 토크가 걸린 채 서 있어 손으로
옮길 수 없고, 가까이 데려가려면 텔레옵을 한 번 띄워야 했다. 대신 판단할 재료를 준다 —
`GET /api/replay/preview?dataset=&episode=`가 관절별 `from`·`to`·`distance`·`unit`과
`align_seconds`를 그대로 돌려주고(다른 모드가 돌면 409), 맥 앱의 확인 시트가 그것을 적는다.
통과하면 먼저 "정렬" 단계가 지금 자세에서 첫 action까지 관절 공간에서 s-curve(smoothstep)로
보간해 가는데, 전체 이동 시간은 가장 먼 관절이 초당 20도(집게 초당 25%)를 **넘지 않도록**
정하고 최소 2초·최대 15초로 자른다. 상한을 첨두 속도에 거는 이유는 s-curve의 최고 속도가
평균의 1.5배이기 때문이다. 15초에서 시간이 잘리면 그 위로는 속도가 다시 올라가므로 "초당
20도"가 실제 상한인 구간은 200도까지이고, 가장 먼 자세(360도)에서도 첨두는 36°/s로 시작
정렬(40°/s)보다 느리다 — 이 경계는 `tests/test_replay.py`가 지킨다. 첫 프레임으로 뛰지
않는 것이 이 단계의 존재 이유다 — 팔은 아무 자세로나 서 있고, 그 차이를 한 틱에 명령하면
서보가 최대 속도로 따라가며 사람이 손쓸 시간이 없다. 붙는 순간에도 같은 일이 일어날 수
있으므로(LeRobot의 `configure()`가 토크를 켜면 서보가 지난번 `Goal_Position`으로 달린다)
토크가 켜지기 전에 지금 자세를 목표로 먼저 써 넣는다. 재생 중에는
`runtime/replay/control.json`의 `stop`을 감시해 그 자리에서 루프를 벗어나되 **토크는 걸어 둔
채** 현재 자세를 유지한다 — 멈추는 것과 힘을 놓는 것은 다른 일이고, 팔이 든 것을 떨어뜨리면
안 된다. 같은 이유로 `disable_torque_on_disconnect=False`이며, 속도 배율(0.25/0.5/1.0, 기본
0.5)은 틱 간격에만 곱하고 action 값은 건드리지 않는다. 재생은 데이터셋을 읽기만 하고 카메라를
열지 않는다. 여기에 없는 것도 분명히 해 둔다: 충돌 회피 경로 계획이 없다. 느린 정렬 속도,
시작 전에 관절별 거리를 읽는 미리보기, 그리고 **옆에 있는 사람**이 그 자리를 대신한다. 실물 확인은 사람이 옆에 있을 때
`speed=0.25`로 한 에피소드를 끝까지 돌려 보고 중간에 stop이 듣는지 본 뒤에 한다.

**수집 중 회차를 버리는 길(2026-09-05)**: `POST /api/recording/control`의 `abort`는 찍던
회차를 **버리고** 수집을 끝낸다. `esc`와 다르다 — `esc`는 루프를 빠져나온 뒤 `save_episode()`가
그대로 돌아 찍다 만 회를 저장하고, 실제로 `data/soarm101_20260905_092024`의 2회차가 82프레임
2.7초짜리 조각으로 남았다. 이미 들어 있는 회차는 `DELETE /api/datasets/{name}/episodes/{index}`
가 `lerobot-edit-dataset`으로 들어낸다. 데이터셋 자체를 지우는 `DELETE /api/datasets/{name}`은
`data/.trash/<name>-<시각>`으로 **옮기기만** 한다 — 몇 시간짜리 시연을 웹 요청 하나가 영구히
없애야 할 이유가 없다. 둘 다 수집이나 재생이 도는 동안에는 409다.

수집 중 부하·전류·온도·추종오차 감지는 아직 없다. 보완안은
[ADR 0004 초안](ADR/0004-recording-safety-ladder-draft.md)에 조사만 했고 구현하지 않았다.
독립 actuator power cutoff와 물리 E-stop도 여전히 없다.

- 문서에 적혀 있다는 이유만으로 현재 동작한다고 가정하지 않는다.
- 구현된 기능과 제안 단계 기능을 run 시작 전에 구분한다.
- 독립 actuator power cutoff와 물리 E-stop은 현재 없다.
- 구현 전에는 소프트웨어 timeout이 실제 arm을 멈출 것이라고 의존하지 않는다.

## 원칙 적용과 예외

이 원칙 때문에 올바르거나 직접적인 해결책을 무작정 배제하지 않는다.

1. 충돌하는 원칙과 그 원래 목적을 확인한다.
2. 직접 해결책의 이점, 실제 위험, 영향 범위, 복구 방법을 설명한다.
3. 사용자에게 예외 적용 또는 원칙 변경 여부를 재질문한다.
4. 명시적 확인 후 제한된 범위에서 진행한다.
5. 반복 가능한 결정이면 config/profile 또는 ADR에 기록한다.

예외 절차는 검토를 가능하게 하기 위한 것이며, 위험을 숨기거나 동시 Follower 명령 같은 핵심 불변조건을 암묵적으로 우회하기 위한 것이 아니다.

## 최소 불변조건 (`MUST`)

1. Follower에는 한 시점에 하나의 active command authority만 존재한다.
2. 각 serial/camera 장치에는 한 시점에 하나의 Hardware Owner만 존재한다.
3. 모델, Mac, ROS 2 client는 raw servo packet을 직접 보내지 않는다.
4. NaN/Inf, 잘못된 구조, 검증된 절대 joint 범위 밖 command는 실행하지 않는다. 검증되지 않은 범위를 추정해 절대 제한으로 사용하지 않는다.
5. Command는 session/sequence를 가지며 과거 또는 중복 command를 구분할 수 있어야 한다.
6. 유효기간이 없는 마지막 command를 연결 단절 후 무기한 반복하지 않는다.
7. Fault나 owner 변경 후 자동으로 이전 motion을 재개하지 않는다.
8. Calibration과 hardware mapping은 active motion 중 변경하지 않는다.
9. Firmware flash, servo ID 변경, calibration 변경은 일반 운용 mode와 분리한다.
10. 독립 전원 차단 수단이 없는 현재 상태를 원격 무인 운용에서 항상 고려한다.

## 변경 가능한 정책 (`DEFAULT`)

다음 항목은 안전 불변조건이 아니라 profile 설정이다.

- heartbeat 주기와 lease timeout
- action chunk horizon
- update 단절 후 grace 구간
- 최대 속도/가속도/action delta
- 카메라 한 대 손실 시 계속 운용 여부
- 특정 workspace 제한
- 연결 단절 후 HOLD와 torque-off 순서
- 원격 운용에서 로컬 관찰자 필요 여부

기본값은 보수적으로 시작하되 실측과 운용 경험에 따라 변경한다. 변경 이유와 결과는 config 또는 ADR에 남긴다.

## 상태 모델

```text
SAFE -> READY -> ACTIVE
  ^       |        |
  |       v        v
  +----- HOLD <- FAULT
```

### SAFE

- 장치 진단과 observation 허용
- Follower motion command 거부
- 시작과 crash recovery의 기본 상태

`SAFE`는 논리적 command 상태 이름이다. Servo torque-off, 전원 차단, arm 무에너지 상태 또는 물리적으로 안전한 자세를 보장하지 않는다.

### READY

- 장치와 config를 확인한 상태
- command authority가 없으면 motion 없음

### ACTIVE

- 유효한 command authority가 있음
- profile 정책과 최소 validator를 통과한 command 실행

### HOLD

- 새 motion 진행을 멈추거나 현재 상태를 유지
- 원인 확인 후 새 lease/명시적 전이 필요

`HOLD`의 물리 동작은 hardware/profile별로 정의해야 한다. 아직 검증되지 않은 상태에서 HOLD가 즉시 정지, 위치 유지 또는 torque-off를 보장한다고 가정하지 않는다.

### FAULT

- hardware/communication/validation 오류
- recovery 절차 없이 자동 ACTIVE 복귀 금지

`ARMED` 같은 추가 상태는 실제 운용에서 유용성이 확인될 때 도입한다. 상태를 늘리는 것 자체를 안전으로 간주하지 않는다.

## Action chunk 중단 원칙

Action chunk 전체를 무조건 완료하거나 무조건 즉시 폐기하는 단일 규칙을 사용하지 않는다.

### Streaming action

지속적으로 갱신될 것을 전제로 하는 command다.

- Update가 끊기면 짧은 configurable grace 뒤 HOLD
- 남은 command를 끝까지 실행하지 않는 것이 기본

### Bounded action chunk

짧은 미래 action sequence다.

- 서버는 최신 chunk의 앞부분만 실행
- 새 chunk가 오면 실행하지 않은 뒷부분을 교체 가능
- Update가 끊기면 profile에 지정된 prefix/grace까지만 실행 후 HOLD

### Atomic action

완료 자체에 의미가 있는 제한된 작업이다. 예: 이미 시작된 짧은 gripper close.

- 명시적으로 `allow_completion`이 설정된 action만 연결 단절 후 완료 가능
- 최대 실행시간과 종료 조건이 있어야 함
- 일반 VLA action을 자동으로 atomic으로 취급하지 않음
- 실제 arm에서 종료 조건과 중단 동작을 검증하기 전에는 `allow_completion`을 기본 비활성으로 둠

## 원격 운용

외부 네트워크에서 perception, 데이터 확인, 학습, 상태 관찰은 가능하다.

현재 독립된 물리 E-stop이나 원격 actuator power cutoff가 없다. 전원 플러그를 직접 차단하는 것이 유일한 물리 대응이다.

따라서 현재 기본 정책은 다음과 같다.

- 원격 observation/compute: 허용
- 현장 사람이 있는 원격 motion: profile로 허용 가능
- 완전 무인 원격 motion: 독립 차단 수단이 생기기 전에는 기본 비활성

이는 protocol의 영구 제한이 아니다. 독립 power cutoff, 안전한 작업 셀 또는 적절한 현장 대응 방법이 마련되면 profile 변경으로 활성화할 수 있다.

완전 무인 원격 motion을 활성화할 때는 단순 config 변경으로 끝내지 않고 현재 잔여 위험을 다시 설명하고 사용자 확인을 기록한다.

## 전원과 E-stop

Software stop은 runtime이 정상 동작할 때만 유효하다. Runtime/OS/USB 전체 오류에 대비하려면 actuator power를 독립적으로 차단할 수 있어야 한다.

장기 권장사항:

- Servo 전원만 차단하고 PC/기록 장치는 유지하는 수단
- Arm 근처에서 즉시 누를 수 있는 물리 switch
- 외부 원격 운용을 위한 독립 경로의 power cutoff

Torque를 갑자기 끄면 arm이 낙하할 수 있으므로 torque-off를 모든 fault의 자동 기본값으로 정하지 않는다. 기본은 profile에 따른 HOLD/controlled stop이며, power cutoff는 더 큰 위험을 막기 위한 최종 수단이다.

## Model/VLA 출력

VLA는 높은 수준의 command authority가 될 수 있지만 Safety Validator를 우회하지 않는다. Validator는 VLA의 전략을 평가하지 않고 command의 최소 유효성만 검사한다.

- 언어 또는 model confidence만으로 안전 조건을 해제하지 않음
- Model-specific range와 normalization은 model adapter/config에 둠
- 실행 action과 model proposal을 모두 기록하여 validator 동작을 검증 가능하게 함

## 자원 격리

- Live manipulation 중 서버에서 무거운 학습을 실행하지 않는 것이 기본
- Recorder/dataset failure가 control process를 중단시키지 않도록 분리
- Root filesystem이 가득 차지 않도록 데이터 전용 저장공간을 사용
- 기존 cookierunhub container를 로봇 runtime이 stop/restart하지 않음

이 항목들의 구체적인 CPU/memory limit는 실측 후 정하며 core 불변조건으로 고정하지 않는다.
