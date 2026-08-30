# Failure Modes

> 구현 상태: 이 표는 목표 탐지/대응 정책이다. 현재 heartbeat, lease, watchdog, 자동 HOLD가 구현됐음을 나타내지 않는다. 구현 전에는 수동 점유 확인과 전원 차단이 실제 대응 수단이다.

이 표의 대응은 현재 권장 기본값이며 `MUST`가 아닌 항목은 profile에서 변경할 수 있다.

| 상황 | 탐지 | 기본 대응 | 자동 재개 | 비고 |
|---|---|---|---|---|
| Mac compute 연결 끊김 | Heartbeat/lease 만료 | Grace 후 HOLD | 안 함 | Atomic action은 명시된 경우만 완료 가능 |
| SSH/VS Code 끊김 | SSH session 종료 | Control heartbeat와 별개로 판단 | 해당 없음 | SSH를 제어 생존 신호로 사용하지 않음 |
| 과거/중복 action | Session/sequence 검사 | 거부하고 기록 | 가능 | 정상 최신 action은 계속 처리 가능 |
| Action update 지연 | Command TTL/observation age | Profile에 따른 grace 후 HOLD | 새 lease 또는 명시적 resume | Timeout 수치는 측정 후 설정 |
| Camera 1대 끊김 | Frame sequence/장치 상태 | 필수 profile은 HOLD, 허용 profile은 degraded | Profile 의존 | 누락 camera를 명시적으로 표시 |
| Camera 2대 끊김 | 동일 | 새 vision action 중단 | 안 함 | 상태 기반 안전 종료만 허용 가능 |
| Leader 끊김 | Serial read 오류 | Teleop action 중단, HOLD | 안 함 | Follower에 마지막 action 반복 금지 |
| Follower 끊김 | Serial read/write 오류 | FAULT, command 중단 | 안 함 | 재연결 후 state 재확인 |
| Hardware Runtime crash | Process monitor/lock | Servo 상태 불명으로 취급 | SAFE로만 재시작 | 독립 power cutoff가 없는 현재 핵심 잔여 위험 |
| Recorder 실패 | Write 오류/queue 상태 | Run 표시, profile에 따라 episode 종료 | Control은 가능한 한 유지 | 데이터 수집 profile에서는 HOLD 권장 |
| Disk 부족 | Free-space threshold | 새 episode/weight 저장 중단 | 공간 확보 후 | Root filesystem 보호 |
| CPU 과부하 | Loop deadline/latency | 새 action 완화 또는 HOLD | 상태 정상화 후 | Live motion 중 heavy training 지양 |
| Model crash | Worker heartbeat | 해당 lease 만료, HOLD | 새 worker/lease 필요 | Hardware Runtime은 유지 가능 |
| VLA invalid output | Schema/finite/limit 검사 | 해당 command 거부 | 다음 정상 command 가능 | 즉시 전체 FAULT로 만들 필요는 없음 |
| ROS graph/DDS 문제 | Liveliness/deadline + app watchdog | 해당 authority 중단 | 새 lease 필요 | QoS 알림만으로 safety 보장하지 않음 |
| 네트워크 재연결 | 새 session 관찰 | 상태 동기화 | 자동 motion 재개 안 함 | 이전 session command 폐기 |
| USB device 번호 변경 | by-id/by-path 검증 | Stable path 사용 | 가능 | `/dev/videoN`, `/dev/ttyACMN` 고정 가정 금지 |
| Calibration 불일치 | ID/hash/schema 검사 | Motion mode 진입 거부 | 올바른 calibration 로드 후 | Observation-only는 가능 |
| Runtime 재부팅 | Process start | SAFE 상태 | 자동 ACTIVE 금지 | Service auto-start는 가능하나 auto-motion 금지 |

## Fault injection 계획

Servo 이동 전에 mock 또는 power-off 상태에서 다음을 검증한다.

- Compute worker kill
- Heartbeat 정지
- 중복/역순 sequence
- 만료 action
- Camera 한 대 제거
- Recorder write 실패
- Disk threshold 도달 모의
- Hardware Owner lock 충돌

실제 motion 상태의 fault injection은 낮은 속도, 지지된 arm, 즉시 전원 차단 가능한 현장에서 별도 수행한다.

## 미결정 위험

- Runtime/OS가 완전히 정지했을 때 actuator를 독립적으로 차단할 수 없음
- Torque-off 시 arm 낙하 형태 미확인
- 원격 무인 motion을 위한 작업 셀/감시 방법 미정
- Servo별 current/temperature telemetry 사용 가능성 미확인
- 실제 control frequency와 허용 network jitter 미측정

이 항목들은 숨기지 않고 profile 활성화 조건과 향후 hardware 개선 작업으로 관리한다.

표의 기본 대응이 올바른 직접 해결책을 방해하는 경우에는 대응을 자동 폐기하지도, 원칙을 이유로 해결책을 자동 배제하지도 않는다. 충돌과 위험을 설명하고 사용자 확인 후 profile 또는 ADR을 수정한다.
