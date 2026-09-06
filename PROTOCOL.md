# Observation and Action Protocol

> 구현 상태 (2026-09-01): command lease, heartbeat, replay protection(session/sequence),
> command TTL, observation age는 **가상 리더 경로에서 구현되어 돌고 있다**
> (`src/soarm_console/vleader/`). 구체적인 wire 형식 — REST 엔드포인트, WebSocket 메시지,
> 단위, 추가된 거절 코드 — 는 맥 앱 저장소의 `docs/원격_텔레옵_프로토콜.md`에 한 벌로 적혀
> 있고, 맥·폰·서버 세 구현이 그것을 읽는다.
>
> 장치별 `flock` owner lock이 구현되어 있고, 프로세스 상태 검사와 409도 사용자에게 모드
> 충돌 이유를 먼저 설명한다.

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

현재 콘솔이 시작하는 teleop, recording, replay, policy 프로세스는 장치별 owner lock을
부모에서 잡고 자식에 file descriptor를 물려준다. advisory lock을 무시하는 외부 프로세스는
별도 운영 점검 대상이다.

## 모델과 정책 실행 REST 계약

경로와 JSON 필드 이름은 맥 앱과 공유하는 계약이다.

| 메서드 | 경로 | 의미 |
| --- | --- | --- |
| `GET` | `/api/models` | 로컬 모델 명세, `camera_map`, `runnable`, 문장형 `problems` |
| `POST` | `/api/models/{run}/{step}` | Spark 체크포인트를 `models/`로 회수하고 명세 생성 |
| `DELETE` | `/api/models/{run}/{step}` | 로컬 사본 삭제, `{run, step, freed_bytes}` 반환 |
| `POST` | `/api/policy/start` | `{run, step, task, fps, max_seconds, home?}`로 rollout 시작 |
| `POST` | `/api/policy/stop` | SIGTERM으로 rollout 중지 또는 복귀 중 현재 자세 정지 |

`POST /api/policy/start`는 `X-SOARM-Motion-Token`을 요구한다. `max_seconds`는 기본 120,
`home`은 선택적인 관절 자세 객체다. 관절은 도, `gripper`는 퍼센트이며, 주어지면 재생과
같은 s-curve/20°/s 첨두 속도로 먼저 그 자세에 정렬한 뒤 rollout을 시작한다. 이름은 팔로워의
여섯 관절과 정확히 같아야 하고 calibration 범위 밖 값은 400으로 거절한다. `/api/status`의
`policy.home`은 요청에서 실제 사용한 자세(없으면 `{}`), `policy.phase`는 시작 자세로 가는
`aligning`, rollout이 도는 `running`, 콘솔이 기준 자세로 복귀하는 `returning` 중 하나다.
복귀 목표는 `home`이 있으면 그 값이고, 없으면 rollout 직전에 읽은 관절값이다. LeRobot 자체의
시간제 복귀는 끄며, 콘솔이 같은 s-curve로 실제 도착을 확인할 때까지 복귀한다. 복귀 중에도
`running=true`이고 stop을 받으면 현재 자세에서 토크를 유지한 채 선다. 복귀 실패는 `error`에 남는다.
허용 범위 1–600초이고 LeRobot `RolloutConfig.duration`에 그대로 들어간다. `/api/status`와
시작·중지 응답의 `policy` 상태에는 `run`, `step`, `task`, `started_at`, `expires_at`,
`fps_target`, `fps_actual`, `chunk_seconds`, `chunks`, `camera_map`, `home`, `phase`, `max_relative_target`,
`inference`, `log_tail`, `error`가 실린다. 측정할 수 없는 성능 값은 `null`이다.

Spark의 실행 목록은 sparkq `GET /api/runs`가 소유한다. 따라서 콘솔의
`GET /api/spark/runs`와 예전 회수 경로 `POST /api/spark/runs/{run}/{step}`은 없다.
`POST /api/spark/train`과 `POST /api/spark/runs/{run}/stop`은 유지한다.

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
