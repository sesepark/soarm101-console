# Observation and Action Protocol

> 구현 상태 (2026-09-01): command lease, heartbeat, replay protection(session/sequence),
> command TTL, observation age는 **가상 리더 경로에서 구현되어 돌고 있다**
> (`src/soarm_console/vleader/`). 구체적인 wire 형식 — REST 엔드포인트, WebSocket 메시지,
> 단위, 추가된 거절 코드 — 는 맥 앱 저장소의 `docs/원격_텔레옵_프로토콜.md`에 한 벌로 적혀
> 있고, 맥·폰·서버 세 구현이 그것을 읽는다.
>
> owner lock 파일은 여전히 없다. 상호배타는 프로세스 상태 검사와 409로 하고 있다.

## 목적

MacBook, 서버, ROS 2, VLA 또는 향후 compute node 사이의 계약을 transport와 독립적으로 정의한다. ZMQ, gRPC, ROS 2 등 구현은 바뀔 수 있지만 의미는 유지한다.

## 공통 envelope

모든 message는 최소한 다음 필드를 가진다.

```text
schema_version
session_id
message_id 또는 sequence
source
server_time 관련 정보
payload
```

알 수 없는 필드를 무조건 오류로 처리하지 않는다. `schema_version` 호환 범위 안에서는 모르는 optional field를 무시하여 확장을 허용한다.

## Observation

권장 필드:

```text
observation_id
server_monotonic_time
camera_frames:
  - camera_role
  - source_path
  - capture_time
  - encoding
  - frame_sequence
robot_state:
  - joint_positions
  - optional velocity/current/temperature
hardware_status
active_authority
```

서버가 hardware에 가장 가까운 timestamp source다. Mac의 wall clock과 직접 비교하기보다 `observation_id`와 왕복 지연을 사용한다.

카메라 영상은 초기에는 MJPEG 전달을 우선 검토한다. 서버에서 불필요하게 decode 후 재압축하지 않는다.

## Command proposal

```text
session_id
lease_id
sequence
observation_id
command_type
execution_policy
valid_for
payload
optional model_id
optional confidence
```

`execution_policy`:

- `streaming`: 다음 update가 계속 올 것을 전제로 함
- `bounded_chunk`: 제한된 길이의 action sequence
- `atomic`: 제한된 종료 조건을 가진 단일 작업

## 실행과 중단

Command validity와 연결 heartbeat는 분리한다.

- 느린 VLA inference라도 별도 heartbeat로 authority 생존을 알릴 수 있다.
- Heartbeat가 정상이어도 command가 만료되면 해당 command를 실행하지 않는다.
- Command가 유효해도 lease가 취소되면 새 실행을 시작하지 않는다.
- Bounded chunk의 전체 완료 여부는 profile과 command metadata로 결정한다.

Timeout 수치는 protocol에 hard-code하지 않고 profile config에서 정한다.

## Command lease

Lease는 Hardware Owner가 아니라 command authority를 임대한다.

```text
request authority
  -> grant lease_id + scope + expiry policy
  -> heartbeat/renew
  -> release 또는 expire
```

Lease scope 예:

- observation only
- Leader teleoperation
- Follower motion
- gripper only
- maintenance

Follower motion scope lease는 동시에 하나만 발급한다. Observation lease는 여러 client에 허용할 수 있다.

## Hardware owner lock

Hardware ownership은 command lease와 별개다.

- Owner는 process lock과 실제 장치 open 상태를 함께 관리한다.
- 새 owner는 기존 owner 종료와 장치 close를 확인한 뒤 시작한다.
- Stale lock은 process/device 상태를 확인한 뒤 명시적으로 복구한다.
- Lock 파일만 삭제하여 강제로 ownership을 빼앗지 않는다.

현재 owner lock 구현은 아직 없다. 구현 전에는 `fuser`, process 상태, 실제 device open 여부를 수동으로 함께 확인한다.

## 오류와 호환성

거부 응답은 machine-readable reason을 가진다.

```text
NO_ACTIVE_LEASE
WRONG_AUTHORITY
STALE_OBSERVATION
EXPIRED_COMMAND
DUPLICATE_SEQUENCE
INVALID_SHAPE
NON_FINITE_VALUE
OUTSIDE_ABSOLUTE_LIMIT
HARDWARE_NOT_READY
```

새 오류 코드를 추가할 수 있으며 client는 모르는 오류를 일반 거부로 처리한다.

## 보안

- 외부 네트워크에 unauthenticated command endpoint를 노출하지 않는다.
- 초기에는 SSH tunnel 또는 신뢰된 LAN + 인증 token을 사용한다.
- Message sequence와 session을 검사하여 단순 replay를 막는다.
- 관찰 endpoint와 motion command endpoint 권한을 분리한다.

구체적인 TLS/ZMQ CURVE/VPN 선택은 deployment 단계에서 결정한다.
