# SO-ARM101 Console

> SO-ARM101 leader–follower 팔을 위한 로컬 우선 운영 콘솔입니다. 브라우저 하나에서 하드웨어 상태 확인, 캘리브레이션, 안전 게이트가 있는 텔레옵, LeRobot 호환 데이터 수집을 수행합니다.

이 프로젝트는 실제 운영에서 필요한 안정적인 장치 식별, serial bus 단일 소유권, calibration 검증, 카메라 lifecycle, 명시적인 motion 시작 절차를 구현하는 데 초점을 둡니다.

## 주요 기능

- **브라우저 기반 운영** — FastAPI 콘솔에서 하드웨어 상태, 카메라 프리뷰, 텔레옵, 데이터 수집을 제어합니다.
- **안정적인 장치 식별** — serial은 `/dev/serial/by-id`, 카메라는 `/dev/v4l/by-path`를 사용해 변하는 `ttyACM*`와 `video*` 번호에 의존하지 않습니다.
- **Motion safety gate** — 텔레옵은 양팔 calibration, motion 활성화, 현장 작업영역 확인, `START SOARM101` 입력을 모두 요구합니다.
- **하드웨어 단일 소유권** — 프로젝트가 제어하는 observation, teleop, recording, 가상 리더 경로는 장치별 `flock`으로 serial bus와 카메라를 배타 점유합니다. lock을 무시하는 외부 프로세스까지 OS가 차단하는 것은 아닙니다. [ADR 0003](ADR/0003-device-owner-lock.md)
- **가상 리더 원격 텔레옵** — 물리 리더 팔 없이, 3D로 그린 팔을 맥 앱이나 아이폰에서 끌어 팔로워를 움직입니다. 목표는 서버의 안전 사다리(절대 관절 한계, 틱당 변화량, 자세 동기화, 부하·전류·추종오차·온도, 워치독)를 통과해야 모터에 닿고, 조작 권한(lease)은 한 시점에 한 기기만 갖습니다. [ADR 0002](ADR/0002-virtual-leader-owner.md)
- **폰에서 쓰는 조작 화면** — 서버 주소를 폰에서 열면 조작 화면(`/viewer/?host=web`)으로 옵니다. 홈 화면 앱으로 설치되고, 카메라·3D·상태·권한 네 탭에 정지 버튼이 늘 붙어 있습니다. **폰에서는 조작 방식이 `끝점` 하나이고 아래 조작판이 없습니다** — 관절 슬라이더 여섯 줄은 393×852 화면에서 267px을 가져가 카메라를 92px짜리 띠로 눌렀습니다. 사람이 직접 정하는 넷(앞뒤·손목 굽힘·손목 회전·집게)은 3D 위에 뜨는 타일 넷이 맡고, 넷 다 누른 채 좌우로 끄는 같은 몸짓입니다. 그 결과 카메라는 294px(폭에 정확히 4:3이라 640×480이 잘리지 않습니다), 3D는 392px을 씁니다. 화면 위의 **영상 받기**는 어느 탭에서든 `끔`·`절약`(320×240, 2fps, 약 55MB/시간)·`보통`(640×480, 8fps)·`전체`(640×480, 30fps, 약 3GB/시간)를 고를 수 있고, 처음 여는 폰은 `절약`으로 시작합니다. `끔`은 프레임을 숨기는 것이 아니라 MJPEG 연결과 두 카메라 preview worker를 닫으므로 모바일 데이터를 쓰지 않습니다.
- **데이터 수집 파이프라인** — episode 성공/재시도 제어가 가능한 로컬 LeRobot dataset을 기록하며, Hub 자동 업로드는 하지 않습니다.
- **카메라 프레임률과 수집 색 설정** — 프리뷰는 V4L2의 기본 버퍼 큐를 유지해 두 카메라를 함께 볼 때도 30fps에 가까운 처리량을 보존합니다. 고른 프레임률보다 빨리 오는 프레임은 콘솔이 솎아 내는데, 이때 다음에 내보낼 시각을 **지난 예정 시각에 주기를 더해** 정합니다. 방금 내보낸 시각으로 다시 맞추면, 요청한 값이 장치가 실제로 내주는 속도와 가까울 때 프레임의 3분의 1이 사라집니다 — 지터로 한 주기보다 조금 일찍 온 프레임이 문턱에 걸려 버려지고, 그러면 다음 프레임을 한 주기 더 기다리기 때문입니다. 640×480 두 대를 함께 열고 잰 값입니다(2026-09-05): 장치는 인코딩 없이 30.1fps를 주는데, 예정 시각을 다시 맞출 때 프리뷰로 나간 것은 18.7·19.0fps였고 주기를 더하면 29.2·29.4fps입니다(남은 차이는 JPEG 인코딩 몫입니다). **fps는 어디서 재는지를 함께 적어야 합니다** — 여기 적은 30.1fps는 장치가 내주는 원본을 디코드 없이 센 값이고, 코드 주석과 `tests/test_cameras.py`가 쓰는 26.8fps는 같은 흐름을 디코드까지 마친 뒤 센 값입니다. 어긋난 측정이 아니라 파이프라인의 서로 다른 지점이고, 그 사이의 차이가 CPU 디코드 몫입니다. 목표가 장치보다 한참 낮은 `절약`(2fps)이나 `보통`(8fps)에서는 원래 나지 않던 일이고, `전체`(30fps)에서만 났습니다. 오래 멈췄다 재개할 때 밀린 예정 시각을 몰아서 내보내지 않도록 현재 시각으로 한 번 당깁니다. 수집 직전에는 LeRobot이 카메라를 열기 전에 60Hz 전원 주파수, 고정 4600K 화이트 밸런스, dynamic-framerate 해제를 직접 적용하되 노출은 자동으로 둡니다. 실제 장치에서 되읽은 값과 지원하지 않는 컨트롤은 `/api/status`의 `cameras.{scene,wrist}.recording_controls`에 표시됩니다.
- **학습 서버 연동** — 수집한 데이터셋을 DGX Spark로 보내고, 학습된 체크포인트를 되받습니다. 전송은
  `.incoming`에 다 받은 뒤 제자리로 옮기므로 끊긴 전송이 멀쩡한 데이터셋처럼 보이지 않고, 남은 조각에서
  이어받습니다. 학습 자체는 콘솔이 띄우지 않고 명령을 만들어 줍니다 — 몇 시간 도는 일이 웹 요청의 수명에
  묶이면 안 되기 때문입니다. [TRAINING.md](TRAINING.md)
- **토크 해제** — 텔레옵과 수집은 끊길 때 토크를 끄지 않습니다(팔이 떨어지는 고장이 팔이 버티는 고장보다
  나쁩니다). 그 대가로 이전 세션이 남긴 토크 때문에 다음 텔레옵이 거절되는데, `POST /api/torque/release`가
  그것을 명시적으로 푸는 유일한 자리입니다. 모션 토큰과 `RELEASE TORQUE SOARM101`을 요구하고, 모드가
  도는 동안에는 거절합니다.
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

데이터 수집 중 `/api/status`의 `recording.runtime`은 현재 에피소드의 시작 시각·제한 시간·0부터 시작하는 번호와 최근 수 초의 실제 `loop_hz`를 제공하며, 회차 사이에는 `phase=resetting`과 빈 시작 시각을 내보냅니다. 수집 후에는 `GET /api/datasets/{name}/episodes/{episode_index}/trajectory`로 `meta/info.json`에 기록된 관절 순서 그대로 follower state와 action을 받을 수 있고, 20,000프레임을 넘는 회차는 SSH 터널에 큰 응답을 밀어 넣지 않도록 거절합니다.

## 기술 스택

| 영역 | 구성 |
| --- | --- |
| Robot runtime | Python 3.12, [LeRobot](https://github.com/huggingface/lerobot) 0.6.1, Feetech SDK |
| Web console | FastAPI, Uvicorn, Vanilla HTML/CSS/JavaScript |
| 3D 뷰어 | three.js r160(자체 호스팅), 직접 쓴 URDF 로더, 수치 야코비안 역기구학 |
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
src/soarm_console/static/ 데스크톱 콘솔 페이지와 3D 조작 화면(`viewer/`, 맥·폰 공용)
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

- Hardware-free test suite: `.venv/bin/pytest -q`로 현재 전체 suite를 확인
- 양팔 read-only bus 확인: motor ID 1–6, 전압, firmware, 위치, torque 상태
- Leader/Follower calibration JSON 검증
- 텔레옵 시작 전 브라우저 preflight에서 장치 경로와 calibration gate 확인

## 문서

- [TRAINING.md](TRAINING.md) — 학습 서버 구성, 배치 크기 실측, 전송 파이프라인, 실패 문구
- `scripts/validate_dataset.py` — 녹화한 데이터셋의 형식·타임스탬프·언어 지시 검사
- [RUNBOOK.md](RUNBOOK.md) — calibration, teleoperation, recording, recovery 절차
- [hardware.md](hardware.md) — USB/camera 식별과 검증된 역할 매핑
- [SAFETY.md](SAFETY.md) — 시스템이 보장하는 것과 보장하지 않는 것
- [ARCHITECTURE.md](ARCHITECTURE.md) — hardware ownership과 향후 policy/VLA 확장
- [PROTOCOL.md](PROTOCOL.md) — observation/action contract (가상 리더 경로에서 구현되어 돌고 있음)
- [FAILURE_MODES.md](FAILURE_MODES.md) — 예상 장애와 운영자 대응

## 참고 및 감사

로봇 runtime은 [Hugging Face LeRobot](https://github.com/huggingface/lerobot)과 [SO-101 workflow](https://github.com/huggingface/lerobot/blob/main/docs/source/so101.mdx)를 기반으로 합니다. 이 저장소는 해당 하드웨어 workflow 위에 로컬 운영 계층을 구현한 프로젝트이며, Hugging Face와 제휴 또는 보증 관계가 없습니다.
