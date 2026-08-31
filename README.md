# SO-ARM101 Console

> SO-ARM101 leader–follower 팔을 위한 로컬 우선 운영 콘솔입니다. 브라우저 하나에서 하드웨어 상태 확인, 캘리브레이션, 안전 게이트가 있는 텔레옵, LeRobot 호환 데이터 수집을 수행합니다.

이 프로젝트는 실제 운영에서 필요한 안정적인 장치 식별, serial bus 단일 소유권, calibration 검증, 카메라 lifecycle, 명시적인 motion 시작 절차를 구현하는 데 초점을 둡니다.

## 주요 기능

- **브라우저 기반 운영** — FastAPI 콘솔에서 하드웨어 상태, 카메라 프리뷰, 텔레옵, 데이터 수집을 제어합니다.
- **안정적인 장치 식별** — serial은 `/dev/serial/by-id`, 카메라는 `/dev/v4l/by-path`를 사용해 변하는 `ttyACM*`와 `video*` 번호에 의존하지 않습니다.
- **Motion safety gate** — 텔레옵은 양팔 calibration, motion 활성화, 현장 작업영역 확인, `START SOARM101` 입력을 모두 요구합니다.
- **하드웨어 단일 소유권** — observation, teleop, recording, 가상 리더 중 한 모드만 serial bus와 카메라를 점유합니다.
- **가상 리더 원격 텔레옵** — 물리 리더 팔 없이, 3D로 그린 팔을 맥 앱이나 아이폰에서 끌어 팔로워를 움직입니다. 목표는 서버의 안전 사다리(절대 관절 한계, 틱당 변화량, 자세 동기화, 부하·전류·추종오차·온도, 워치독)를 통과해야 모터에 닿고, 조작 권한(lease)은 한 시점에 한 기기만 갖습니다. [ADR 0002](ADR/0002-virtual-leader-owner.md)
- **데이터 수집 파이프라인** — episode 성공/재시도 제어가 가능한 로컬 LeRobot dataset을 기록하며, Hub 자동 업로드는 하지 않습니다.
- **운영 문서화** — runbook, failure mode, safety, architecture, ADR로 구현 결정과 현장 절차를 남깁니다.

## 시스템 구조

```text
Browser
  │  loopback HTTP
  ▼
FastAPI console
  ├── read-only hardware doctor
  ├── leader → follower teleoperation (30 FPS, 2° relative-target limit)
  ├── virtual leader (30 Hz in-process owner, lease + safety validator + watchdog)
  │     ├── WebSocket  /api/vleader/stream   ← 3D 뷰어(맥 WKWebView · 아이폰 브라우저)
  │     └── goal relay /api/vleader/goal     ← lerobot-record 안의 Teleoperator
  ├── camera preview workers
  └── LeRobot dataset recorder
          │
          ▼
SO-ARM101 leader / follower + scene / wrist cameras
```

콘솔은 기본적으로 `127.0.0.1`에만 bind합니다. 인증 없는 motion-control API를 LAN에 노출하지 않으며, 원격 접속은 SSH tunnel을 사용합니다.

## 기술 스택

| 영역 | 구성 |
| --- | --- |
| Robot runtime | Python 3.12, [LeRobot](https://github.com/huggingface/lerobot) 0.6.1, Feetech SDK |
| Web console | FastAPI, Uvicorn, Vanilla HTML/CSS/JavaScript |
| Video / data | OpenCV camera input, PyAV / MP4, LeRobot dataset format |
| Deployment | `uv`, systemd user service, udev device alias |
| 검증 | pytest 기반 hardware-free test + read-only bus doctor |

## 빠른 시작

### 1. 설치

```bash
git clone https://github.com/sesepark/soarm101-console.git
cd soarm101-console
uv sync --all-groups
cp config/soarm.env.example config/soarm.env
```

`config/soarm.env`에서 본인 환경의 leader/follower/camera 경로를 설정합니다. 이 파일은 로컬 런타임 설정이며 커밋하지 않습니다.

### 2. Motion 없이 확인

```bash
./scripts/doctor.sh
.venv/bin/pytest -q
```

Doctor는 모터 ID, 모델, firmware, 현재 위치, 전압, torque 상태만 읽습니다. motion command는 전송하지 않습니다.

### 3. 양팔 캘리브레이션

작업영역을 비우고, 현장 관찰자와 전원 차단 수단을 준비한 뒤 실행합니다.

```bash
./scripts/calibrate_follower.sh
./scripts/calibrate_leader.sh
```

각 팔을 안전한 범위의 중간 자세에 둔 뒤 Enter를 누르고, 안내되는 관절을 한 번에 하나씩 안전한 실사용 범위까지 움직입니다. 상세 절차는 [RUNBOOK.md](RUNBOOK.md)를 참고하세요.

### 4. 콘솔 시작

```bash
./scripts/run_web.sh
```

<http://127.0.0.1:8088>을 엽니다. 텔레옵을 활성화하려면 로컬 설정의 `SOARM_ENABLE_MOTION=1`로 변경한 뒤 서비스를 재시작해야 합니다. 이후에도 브라우저에서 현장 확인과 `START SOARM101` 입력을 거쳐야 motion session이 시작됩니다.

## 운영 흐름

| 순서 | 작업 | 보호 장치 |
| --- | --- | --- |
| 1 | **환경 진단** 실행 | 읽기 전용이며 teleop/record 중에는 실행을 막음 |
| 2 | calibration 파일 확인 | Leader/Follower 모두 유효한 motor ID와 range 필요 |
| 3 | **텔레옵 시작** | Motion flag, 현장 확인, 확인 문구 입력 필요 |
| 4 | 현재 모드 중지 | 다음 모드가 시작되기 전에 active hardware owner 해제 |
| 5 | demonstration 기록 | camera role 확인을 추가로 요구 |

## 저장소 구조

```text
src/soarm_console/        FastAPI app, teleop, recording, diagnostics, camera workers
scripts/                  calibration, doctor, web, recording, service installation
config/                   로컬 runtime 설정 template
deploy/                   systemd user service, udev rule
tests/                    hardware-free API 및 safety gate test
ADR/                      Architecture Decision Records
RUNBOOK.md                calibration, teleop, recording 현장 절차
SAFETY.md                 안전 불변조건과 현재 한계
ARCHITECTURE.md           소유권 모델과 향후 확장 방향
```

## 안전 범위와 한계

이 프로젝트는 실험용 로보틱스 시스템이며 **안전 인증 제어 시스템이 아닙니다**. 소프트웨어 정지가 물리 E-stop 또는 전원 차단을 대체한다고 주장하지 않습니다. 작은 동작부터 시작하고, 현장 관찰자를 유지하며, 하드웨어 동작 전 [SAFETY.md](SAFETY.md)와 [FAILURE_MODES.md](FAILURE_MODES.md)를 검토하세요.

## 검증 항목

- Hardware-free test suite: `12 passed`
- 양팔 read-only bus 확인: motor ID 1–6, 전압, firmware, 위치, torque 상태
- Leader/Follower calibration JSON 검증
- 텔레옵 시작 전 브라우저 preflight에서 장치 경로와 calibration gate 확인

## 문서

- [RUNBOOK.md](RUNBOOK.md) — calibration, teleoperation, recording, recovery 절차
- [hardware.md](hardware.md) — USB/camera 식별과 검증된 역할 매핑
- [SAFETY.md](SAFETY.md) — 시스템이 보장하는 것과 보장하지 않는 것
- [ARCHITECTURE.md](ARCHITECTURE.md) — hardware ownership과 향후 policy/VLA 확장
- [PROTOCOL.md](PROTOCOL.md) — 계획 중인 observation/action contract
- [FAILURE_MODES.md](FAILURE_MODES.md) — 예상 장애와 운영자 대응

## 참고 및 감사

로봇 runtime은 [Hugging Face LeRobot](https://github.com/huggingface/lerobot)과 [SO-101 workflow](https://github.com/huggingface/lerobot/blob/main/docs/source/so101.mdx)를 기반으로 합니다. 이 저장소는 해당 하드웨어 workflow 위에 로컬 운영 계층을 구현한 프로젝트이며, Hugging Face와 제휴 또는 보증 관계가 없습니다.
