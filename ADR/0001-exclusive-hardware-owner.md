# ADR 0001: Exclusive Hardware Owner and Leased Command Authority

- Status: Accepted as initial architecture
- Date: 2026-08-30

## Context

SO-ARM101 Leader/Follower와 카메라는 서버 PC에 USB로 연결되어 있다. MacBook, LeRobot policy, VLA, ROS 2 및 향후 controller가 동일한 Follower를 사용할 수 있다.

각 controller가 serial/camera를 직접 열면 장치 충돌, command 경쟁, calibration 중복, ownership 전환 문제를 해결하기 어렵다. 반대로 특정 LeRobot process를 영구 owner로 고정하면 ros2_control이나 다른 runtime으로 확장하기 어렵다.

## Decision

특정 구현이 아니라 `Hardware Owner 역할`을 배타적으로 둔다.

- 한 시점에 장치별 owner는 하나다.
- 현재 기본 구현은 서버 LeRobot Hardware Runtime이다.
- 향후 ros2_control 등으로 owner 구현을 교체할 수 있다.
- Owner 전환 전에 기존 owner가 장치를 닫고 lock을 해제해야 한다.

Hardware ownership과 command authority를 분리한다.

- Owner는 장치를 열고 최소 validation과 I/O를 수행한다.
- Leader, Mac policy, VLA, ROS planner는 command lease를 얻어 action을 제안한다.
- Follower motion lease는 동시에 하나만 활성화한다.
- Observation subscriber는 여러 개 허용한다.

이 결정은 새로운 owner 기술을 배제하기 위한 것이 아니다. 직접 장치 소유가 특정 확장에서 더 올바른 해결책이면 기존 owner를 완전히 종료하는 전환 절차와 영향 범위를 설명하고 사용자 확인 후 새 owner adapter 또는 후속 ADR로 채택할 수 있다.

## Consequences

장점:

- Follower 동시 명령을 구조적으로 방지
- Mac/서버/Jetson/ROS 2 사이 compute 위치 변경 용이
- 공통 logging과 validation 가능
- Controller 전환 시 장치 driver 중복 감소

비용:

- Owner/lease/protocol 구현 필요
- 하나의 추가 IPC hop 발생
- Owner process 장애가 hardware I/O에 영향을 줌

## Alternatives

### 각 controller의 직접 장치 소유

초기 구현은 단순하지만 전환과 충돌 처리가 어려워 기본 구조로 선택하지 않았다. Maintenance나 조사 목적의 직접 접근은 Hardware Owner를 완전히 종료한 상태에서 허용할 수 있다.

### LeRobot Runtime 영구 고정

초기에는 유용하지만 향후 ros2_control 또는 새로운 hardware stack 도입을 제한하므로 선택하지 않았다.

### 모든 command를 과도하게 제한

VLA와 새로운 manipulation 방식의 확장을 방해하므로 선택하지 않았다. Core validator는 형식, ownership, 절대 기계 범위 등 최소 조건만 담당하고 나머지는 profile/config로 관리한다.

## Follow-up

- Owner lock 형식 결정
- Lease/heartbeat protocol 구현
- Mock Hardware Owner로 ownership/timeout test
- 실제 control 주기 측정 후 profile 기본값 결정
- 원격 무인 운용 전 독립 power cutoff 검토
