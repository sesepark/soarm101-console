# Architecture

> 구현 상태 (2026-09-01): 이 문서는 목표 구조를 정의한다. **가상 리더 경로에 한해**
> Hardware Owner, Authority Manager(follower motion lease), Safety Validator, watchdog이
> 구현되어 있고 `tests/test_vleader*.py`로 검증된다 — `src/soarm_console/vleader/`와
> [ADR 0002](ADR/0002-virtual-leader-owner.md)를 보라. 실물 팔로워를 열어 30Hz로 읽는
> 것까지 확인했고(루프 4.4ms), **팔이 실제로 움직이는 시험은 아직 하지 않았다.**
> 접촉 문턱값은 유추값이고 실측이 필요하다.
>
> 아직 구현되지 않은 것: owner lock 파일, ROS 2 Bridge, VLA worker, 독립 power cutoff.
> 기존 `lerobot-teleoperate` 텔레옵 경로에는 lease도 watchdog도 없다(서브프로세스가
> 리더 팔에서 읽은 값을 그대로 흘려보내는 구조라 끼어들 자리가 없다).

## 목표

하나의 코드베이스로 다음 운용 형태를 모두 지원한다.

- Leader/Follower teleoperation
- 카메라 데이터 수집
- 규칙 기반 또는 학습 기반 perception
- 일반 manipulation policy
- VLA 기반 action 생성
- ROS 2 연동
- MacBook, 서버 GPU, 외부 GPU 또는 향후 Jetson으로 compute 위치 변경

특정 모델이나 middleware를 전체 구조에 고정하지 않는다. 하드웨어 접근, observation, action 계약을 안정적으로 유지하고 그 위의 compute 구현은 교체 가능하게 한다.

## 배치

```text
MacBook M2 Max
├── VS Code Remote SSH
├── Perception / Policy / VLA worker
└── Operator UI
         │
         │ observations / command proposals / heartbeat
         ▼
Server PC
├── Authority Manager
├── Safety Validator
├── Hardware Owner
│   ├── Leader adapter
│   ├── Follower adapter
│   └── Cameras
├── Recorder
├── ROS 2 Bridge (향후)
└── Existing cookierunhub services (분리)
```

Compute worker는 MacBook에 한정되지 않는다. 동일한 protocol을 구현하면 서버 GPU, 외부 GPU, Jetson 또는 새로운 runtime으로 옮길 수 있다.

## 두 종류의 소유권

### Hardware ownership

장치 파일을 실제로 열고 읽고 쓰는 프로세스의 권리다.

`MUST`:

- 한 시점에 각 serial/camera 장치를 여는 Hardware Owner는 하나여야 한다.
- 다른 owner로 전환하기 전에 기존 owner는 장치를 닫고 lock을 해제해야 한다.
- 현재 owner가 누구인지 상태와 로그에서 확인 가능해야 한다.

현재 기본 owner는 LeRobot 기반 서버 Hardware Runtime이다. 이것은 영구 고정이 아니다. 향후 `ros2_control`이나 다른 runtime이 owner가 될 수 있지만, 전환 중 두 owner가 겹치면 안 된다.

### Command authority

Hardware Owner에게 Follower action을 제안할 수 있는 논리적 권리다.

가능한 authority 예:

- `manual_leader`
- `mac_policy`
- `vla`
- `ros_planner`
- `maintenance`

`MUST`: Follower의 활성 command authority는 한 시점에 하나뿐이어야 한다.

Observer, recorder, UI는 여러 개 존재할 수 있다. 읽기 권한은 배타적이지 않다.

## 구성요소

### Hardware Owner

- 안정적인 `by-id`/`by-path`로 장치 식별
- arm/camera 읽기 및 Follower 쓰기
- server timestamp 부여
- 최소 command validation
- owner lock 관리
- 장치 오류를 상위 계층에 보고

### Authority Manager

- command lease 발급/회수
- 동시에 하나의 active authority만 허용
- authority 전환 기록
- reconnect 시 이전 lease를 자동 복원하지 않음

### Safety Validator

Safety Validator는 모델의 행동 전략을 제한하는 planner가 아니다. 다음과 같은 명백히 잘못된 명령만 거부하는 최소 방어선이다.

- NaN/Inf, 잘못된 shape/type
- 알려진 절대 joint 범위 밖 값
- 만료되거나 중복된 command
- 현재 lease 소유자가 아닌 command
- 단일 update에서 비정상적으로 큰 jump

속도, action horizon, workspace 등 실험에 따라 달라지는 제한은 profile/config에 둔다.

절대 joint 범위도 검증된 hardware/calibration 자료가 있을 때만 적용한다. 확인되지 않은 범위를 추정하여 올바른 command를 임의로 차단하지 않는다.

### Compute Worker

- perception, policy 또는 VLA 실행
- observation을 받아 command proposal 생성
- hardware device를 직접 열지 않음
- raw servo packet이 아닌 표준 action을 반환

### Recorder

- observation, 제안된 action, 실행된 action, authority 변화 기록
- control loop와 분리
- 기록 실패가 Hardware Owner process를 비정상 종료시키지 않음

### ROS 2 Bridge

- ROS topic/service/action과 내부 protocol 변환
- 초기에는 hardware owner가 아닌 client/bridge
- 나중에 ros2_control이 owner가 되면 명시적인 owner 전환 절차 사용

## 운용 profile

Profile은 코드 경로를 막는 보안 등급이 아니라 기본 정책 묶음이다.

| Profile | Arm motion | 기본 command source | 목적 |
|---|---|---|---|
| `observe` | 없음 | 없음 | 카메라, 상태, perception |
| `teleop` | 있음 | Leader | 데이터 수집과 수동 조작 |
| `policy` | 있음 | 학습 policy | 일반 manipulation |
| `vla` | 있음 | VLA worker | 언어 조건 행동 |
| `maintenance` | 제한적 | 유지보수 도구 | calibration/진단 전용 |

새로운 profile은 추가할 수 있다. Profile 이름이 모델 종류나 compute 위치를 강제하지 않는다.

## 확장 원칙

- transport는 ZMQ, gRPC, ROS 2 등으로 교체 가능해야 한다.
- action/observation schema는 version을 가진다.
- 장치별 driver는 공통 Hardware Owner interface 뒤에 둔다.
- model-specific 제한은 model/profile config에 둔다.
- core validator에는 기계적·형식적 최소 조건만 둔다.
- calibration과 장치 역할은 runtime code가 아니라 config/data로 관리한다.
- 기존 원칙보다 직접적이고 올바른 소유/제어 방식이 확인되면 이를 배제하지 않고 사용자 확인 후 owner adapter 또는 ADR로 반영한다.

## 현재 미확정 사항

- scene/wrist 카메라의 실제 물리 역할
- 정상 운용 control frequency
- VLA action chunk 길이와 grace 구간
- 독립 actuator power cutoff 구현
- ROS 2 bridge와 Hardware Owner 사이의 최종 IPC
- 데이터 전용 저장장치 위치

미확정 값은 구현 중 측정하여 profile에 기록하며 `MUST`로 승격하지 않는다.
