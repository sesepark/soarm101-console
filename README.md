# SO-ARM101 Console

> SO-ARM101 leader–follower 팔을 위한 로컬 우선 운영 콘솔입니다. 브라우저 하나에서 하드웨어 상태 확인, 캘리브레이션, 안전 게이트가 있는 텔레옵, LeRobot 호환 데이터 수집을 수행합니다.

이 프로젝트는 실제 운영에서 필요한 안정적인 장치 식별, serial bus 단일 소유권, calibration 검증, 카메라 lifecycle, 명시적인 motion 시작 절차를 구현하는 데 초점을 둡니다.

## 주요 기능

- **브라우저 기반 운영** — FastAPI 콘솔에서 하드웨어 상태, 카메라 프리뷰, 텔레옵, 데이터 수집을 제어합니다.
- **안정적인 장치 식별** — serial은 `/dev/serial/by-id`, 카메라는 `/dev/v4l/by-path`를 사용해 변하는 `ttyACM*`와 `video*` 번호에 의존하지 않습니다.
- **Motion safety gate** — 텔레옵은 양팔 calibration, motion 활성화, 현장 작업영역 확인, `START SOARM101` 입력을 모두 요구합니다. **토크가 걸려 있는지는 더 이상 묻지 않습니다**(2026-09-05): 텔레옵과 수집은 팔이 떨어지지 않도록 토크를 켠 채 끝나므로, 정상적으로 끝낸 세션 다음의 시작이 반드시 거절되고 있었습니다. 게이트가 시키던 "진단 → 토크 해제 → 팔 처짐"이야말로 다음 시작에서 팔이 튀는 원인이었습니다. 지금 시작을 막는 것은 모터가 답하지 않거나 전압이 범위 밖일 때뿐이고, 문구가 어느 팔의 무엇인지를 적습니다. "다른 프로세스가 이미 팔을 쥐고 있는가"는 `flock` owner lock이 맡습니다.
- **부드러운 시작(soft start)** — 텔레옵과 수집이 시작할 때 팔이 튀던 두 자리를 없앴습니다. 붙는 순간에는 토크가 켜지기 전에 `Goal_Position`을 지금 자세로 옮깁니다(STS3215는 토크가 걸리는 순간 남아 있던 옛 목표를 향해 최고 속도로 갑니다). 루프에 들어가기 전에는 팔로워를 리더의 **지금** 자세까지 s-curve로 걸어갑니다(첨두 40°/s, 집게 50%/s, 1–6초) — LeRobot의 첫 틱은 리더 자세를 그대로 보내므로, 두 팔이 다르면 팔로워가 그 차이만큼 한 번에 뜁니다. 수집 중에는 그동안 `phase="aligning"`이 나갑니다. 가상 리더 경로에서는 하지 않습니다(`start_relay`가 자세를 이어 줍니다). 이를 위해 텔레옵 자식이 `lerobot-teleoperate` 바이너리에서 `soarm_console.teleoperating`으로 바뀌었고, 루프 자체는 여전히 LeRobot의 `teleop_loop`입니다.
- **하드웨어 단일 소유권** — 프로젝트가 제어하는 observation, teleop, recording, 가상 리더 경로는 장치별 `flock`으로 serial bus와 카메라를 배타 점유합니다. lock을 무시하는 외부 프로세스까지 OS가 차단하는 것은 아닙니다. [ADR 0003](ADR/0003-device-owner-lock.md)
- **가상 리더 원격 텔레옵** — 물리 리더 팔 없이, 3D로 그린 팔을 맥 앱이나 아이폰에서 끌어 팔로워를 움직입니다. 목표는 서버의 안전 사다리(절대 관절 한계, 틱당 변화량, 자세 동기화, 부하·전류·추종오차·온도, 워치독)를 통과해야 모터에 닿고, 조작 권한(lease)은 한 시점에 한 기기만 갖습니다. [ADR 0002](ADR/0002-virtual-leader-owner.md)
- **폰에서 쓰는 조작 화면** — 서버 주소를 폰에서 열면 조작 화면(`/viewer/?host=web`)으로 옵니다. 홈 화면 앱으로 설치되고, 카메라·3D·상태·권한 네 탭에 정지 버튼이 늘 붙어 있습니다. **폰에서는 조작 방식이 `끝점` 하나이고 아래 조작판이 없습니다** — 관절 슬라이더 여섯 줄은 393×852 화면에서 267px을 가져가 카메라를 92px짜리 띠로 눌렀습니다. 사람이 직접 정하는 넷(앞뒤·손목 굽힘·손목 회전·집게)은 3D 위에 뜨는 타일 넷이 맡고, 넷 다 누른 채 좌우로 끄는 같은 몸짓입니다. 그 결과 카메라는 294px(폭에 정확히 4:3이라 640×480이 잘리지 않습니다), 3D는 392px을 씁니다. 화면 위의 **영상 받기**는 어느 탭에서든 `끔`·`절약`(320×240, 2fps, 약 55MB/시간)·`보통`(640×480, 8fps)·`전체`(640×480, 30fps, 약 3GB/시간)를 고를 수 있고, 처음 여는 폰은 `절약`으로 시작합니다. `끔`은 프레임을 숨기는 것이 아니라 MJPEG 연결과 두 카메라 preview worker를 닫으므로 모바일 데이터를 쓰지 않습니다.
- **데이터 수집 파이프라인** — episode 성공/재시도 제어가 가능한 로컬 LeRobot dataset을 기록하며, Hub 자동 업로드는 하지 않습니다. 조작은 넷입니다: `right`(회차 조기 종료), `left`(다시 찍기), `esc`(저장하고 끝), **`abort`(찍던 회를 버리고 끝)**. `esc`는 루프를 빠져나온 뒤 `save_episode()`가 그대로 돌아 찍다 만 회를 저장하는데, 실제로 82프레임 2.7초짜리 조각이 온전한 시연인 척 남은 적이 있어 `abort`를 따로 두었습니다. 회차 사이 정리 구간과 저장(인코딩) 구간이 각각 `phase=resetting`·`saving`으로 나가고, 저장된 회차 수는 `episodes_saved`로 실립니다 — 정리 15초 뒤 인코딩 8초 동안 화면이 아무 말도 하지 않아 사람이 수집이 죽은 줄 알던 자리입니다.
- **이어 찍기와 데이터셋 정리** — `POST /api/recording/start`에 `{dataset, resume: true}`를 주면 기존 데이터셋에 회차를 이어 붙입니다(LeRobot의 `LeRobotDataset.resume` 경로). 과제가 다르면 400으로 거절합니다 — 데이터셋 하나는 학습 한 번의 단위이고, 섞인 데이터는 파케이를 열기 전에는 섞였다는 사실조차 보이지 않습니다. 회차 하나는 `DELETE /api/datasets/{name}/episodes/{index}`가 `lerobot-edit-dataset`으로 들어내고, 데이터셋 자체는 `DELETE /api/datasets/{name}`이 `data/.trash/`로 **옮기기만** 합니다. 새 데이터셋 이름은 UTC가 아니라 **로컬 시각**으로 짓고, 과제가 ASCII면 그것을 앞에 둡니다(`pick_and_place_20260905_1820`).
- **수집 중 스냅숏과 품질 기록** — 수집이 도는 동안 카메라를 쥔 것은 record 자식이라 콘솔의 MJPEG 프리뷰는 꺼집니다. 그래서 찍는 쪽이 보는 프레임을 5Hz로 `GET /api/recording/preview/{scene|wrist}.jpg`에 내려놓습니다(인코딩은 별 스레드에서 하므로 30Hz 루프를 막지 않고, 3초보다 오래된 그림은 404입니다 — 멈춘 카메라의 마지막 장면을 계속 보여 주면 화면은 아무 일도 없다는 듯 그것을 보여 줍니다). 수집이 끝나면 `data/<name>/soarm_quality.json`에 `loop_hz`·`camera_stale_pct`·`slow_loop_warnings`가 남고, `GET /api/datasets`가 그것과 과제 목록을 함께 실어 보냅니다.
- **카메라 프레임률과 수집 색 설정** — 프리뷰는 V4L2의 기본 버퍼 큐를 유지해 두 카메라를 함께 볼 때도 30fps에 가까운 처리량을 보존합니다. 고른 프레임률보다 빨리 오는 프레임은 콘솔이 솎아 내는데, 이때 다음에 내보낼 시각을 **지난 예정 시각에 주기를 더해** 정합니다. 방금 내보낸 시각으로 다시 맞추면, 요청한 값이 장치가 실제로 내주는 속도와 가까울 때 프레임의 3분의 1이 사라집니다 — 지터로 한 주기보다 조금 일찍 온 프레임이 문턱에 걸려 버려지고, 그러면 다음 프레임을 한 주기 더 기다리기 때문입니다. 640×480 두 대를 함께 열고 잰 값입니다(2026-09-05): 장치는 인코딩 없이 30.1fps를 주는데, 예정 시각을 다시 맞출 때 프리뷰로 나간 것은 18.7·19.0fps였고 주기를 더하면 29.2·29.4fps입니다(남은 차이는 JPEG 인코딩 몫입니다). **fps는 어디서 재는지를 함께 적어야 합니다** — 여기 적은 30.1fps는 장치가 내주는 원본을 디코드 없이 센 값이고, 코드 주석과 `tests/test_cameras.py`가 쓰는 26.8fps는 같은 흐름을 디코드까지 마친 뒤 센 값입니다. 어긋난 측정이 아니라 파이프라인의 서로 다른 지점이고, 그 사이의 차이가 CPU 디코드 몫입니다. 목표가 장치보다 한참 낮은 `절약`(2fps)이나 `보통`(8fps)에서는 원래 나지 않던 일이고, `전체`(30fps)에서만 났습니다. 오래 멈췄다 재개할 때 밀린 예정 시각을 몰아서 내보내지 않도록 현재 시각으로 한 번 당깁니다. 수집 직전에는 LeRobot이 카메라를 열기 전에 60Hz 전원 주파수, 고정 4600K 화이트 밸런스, dynamic-framerate 해제를 직접 적용하되 노출은 자동으로 둡니다. 실제 장치에서 되읽은 값과 지원하지 않는 컨트롤은 `/api/status`의 `cameras.{scene,wrist}.recording_controls`에 표시됩니다. 수집 중에는 루프가 카메라에서 **새 프레임을 실제로 몇 장 받았는지**가 `/api/status`의 `recording.runtime`에 카메라별 `camera_fresh_hz`와 `camera_stale_pct`로 실립니다(`loop_hz`와 같은 3초 창, 1초 주기). LeRobot의 `read_latest()`는 블로킹이 아니라서 새 프레임이 아직 없으면 버퍼에 있던 것을 그대로 다시 돌려주므로, 돌려받은 ndarray의 버퍼 주소가 직전과 같으면 그 틱은 새 프레임을 받지 못한 것으로 셉니다 — `camera_stale_pct`가 그 비율(%)이고 `camera_fresh_hz`는 남은 비율에 `loop_hz`를 곱한 값이라, 30Hz 루프에서 15Hz만 나오면 카메라가 절반을 흘리고 있다는 뜻입니다. **픽셀을 비교하거나 찍힌 영상에서 세면 안 됩니다** — 움직이지 않는 장면은 서로 다른 두 번의 촬영인데도 같은 값을 내고, AV1 인코더는 움직임 없는 두 프레임을 하나로 합치므로 카메라가 같은 프레임을 두 번 준 것과 구별되지 않습니다(실제로 그 둘을 혼동해 없는 문제를 쫓은 적이 있습니다, 2026-09-05). 데이터셋의 `timestamp`는 언제나 `frame_index / fps`로 합성되어 파일에는 흔적이 남지 않으니, 원천에서 세는 수밖에 없습니다.
- **서보 판독값을 함께 남긴다** — 수집이 매 프레임(30Hz) 위치 말고도 부하·속도·온도·전압·상태 바이트·이동 플래그·전류와, 그 프레임의 위치를 읽은 시각, 카메라별 새 프레임 여부를 **별도 열**로 남깁니다(아래 [데이터셋이 담는 열](#데이터셋이-담는-열)). 목적은 나중에 모터 부하로 간접 촉각을 추정하는 연구이고, 시연은 다시 찍을 수 없으므로 읽지 않으면 사라지는 값들입니다. `observation.state`는 관절 위치 여섯 그대로 두므로 지금까지 학습한 정책과 사전학습 정규화 통계는 그대로입니다 — 정책은 자기가 모르는 열을 지나갑니다. 서보 읽기는 **틱당 블록 하나**입니다(주소 56~70의 15바이트를 한 번에 읽어 우리가 쪼갭니다). 레지스터마다 따로 읽으면 버스 왕복이 틱당 일곱 번이 되어 30Hz가 흔들리고, 늘어난 시간축은 `timestamp`가 합성값이라 파일에 남지도 않습니다. 부호를 푸는 것은 가상 리더가 쓰는 `MotorsBus._decode_sign` 그대로입니다 — 두 곳이 다르게 풀면 같은 힘이 다른 숫자가 됩니다. 열이 없는 옛 데이터셋에 이어 찍기는 400으로 거절합니다(`Dataset was recorded without the sensor columns; start a new dataset`). `GET /api/status`의 `capabilities`에 `sensor_extras`가 실립니다.
- **학습 서버 연동** — 수집한 데이터셋을 DGX Spark로 보내고, 학습된 체크포인트를 되받습니다. 전송은
  `.incoming`에 다 받은 뒤 제자리로 옮기므로 끊긴 전송이 멀쩡한 데이터셋처럼 보이지 않고, 남은 조각에서
  이어받습니다. **학습은 콘솔이 띄우되 품지 않습니다** — `POST /api/spark/train`이 원격 tmux 세션
  `train-<run>` 안에서 `lerobot-train`을 시작하므로, 콘솔이 재시작돼도 학습은 그대로 돕니다.
  `GET /api/spark/runs`가 실행별로 스텝·loss·로그 끝 다섯 줄·죽은 이유를 함께 돌려주고,
  `POST /api/spark/runs/{run}/stop`이 멈춥니다. 실행 이름에 시각이 들어가는 이유는 LeRobot이
  `output_dir`이 이미 있으면 거절하기 때문입니다 — 고정 이름이던 때는 같은 데이터셋의 두 번째 학습이
  반드시 실패했고, 그 실패가 tmux 안에만 남았습니다. [TRAINING.md](TRAINING.md)
- **토크 해제** — 텔레옵과 수집은 끊길 때 토크를 끄지 않습니다(팔이 떨어지는 고장이 팔이 버티는 고장보다
  나쁩니다). 남은 토크가 다음 시작을 막지는 이제 않지만, 팔을 손으로 옮기거나 보관 자세로 내릴 때는
  풀어야 합니다. `POST /api/torque/release`가 그것을 명시적으로 푸는 유일한 자리이며, 모션 토큰과
  `RELEASE TORQUE SOARM101`을 요구하고 모드가 도는 동안에는 거절합니다. **팔을 받친 상태에서만**
  누릅니다.
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

`/api/status`의 `capabilities`가 이 서버가 답할 수 있는 것들의 이름을 싣습니다 — `abort`, `resume`, `preview`, `quality`, `delete`, `train`, `replay_preview`, `soft_start`. 맥 앱은 이 목록에 이름이 있을 때만 해당 기능을 켭니다. 화면이 서버보다 앞서 나가면 사람은 눌리지 않는 단추를 보게 되고, 서버가 앞서 나가면 새 기능이 아무에게도 보이지 않습니다.

데이터 수집 중 `/api/status`의 `recording.runtime`은 현재 에피소드의 시작 시각·제한 시간·0부터 시작하는 번호와 최근 수 초의 실제 `loop_hz`를 제공하며, 회차 사이에는 `phase=resetting`과 정리 구간의 시작 시각·길이를, 저장 중에는 `phase=saving`을 내보냅니다. 수집 후에는 `GET /api/datasets/{name}/episodes/{episode_index}/trajectory`로 `meta/info.json`에 기록된 관절 순서 그대로 follower state와 action을 받을 수 있고, 20,000프레임을 넘는 회차는 SSH 터널에 큰 응답을 밀어 넣지 않도록 거절합니다.

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
| 3 | **텔레옵 시작** | Motion flag, 현장 확인, 확인 문구 입력 필요. 팔로워가 리더 자세까지 걸어간 뒤 루프가 시작됨 |
| 4 | 현재 모드 중지 | 다음 모드가 시작되기 전에 active hardware owner 해제 |
| 5 | demonstration 기록 | camera role 확인을 추가로 요구. 실패한 회차는 `abort`로 버림 |
| 6 | 데이터셋 정리 | 지우기는 `data/.trash`로 옮기기만 하고, 수집·재생 중에는 거절 |
| 7 | 학습 시작 | 원격 tmux 세션에서 돌고, GPU가 하나라 동시 실행은 409 |

## 데이터셋이 담는 열

LeRobot v3 features. 아래 아홉 열이 `observation.state`·`action`·영상 옆에 **더해집니다**.
값은 전부 `float32`이고 매 프레임(30Hz) 채워집니다. `<motor>`는
`shoulder_pan`·`shoulder_lift`·`elbow_flex`·`wrist_flex`·`wrist_roll`·`gripper` 순서이며,
이는 `observation.state`의 이름 순서와 같습니다.

| 열 | shape | names | 출처와 단위 |
| --- | --- | --- | --- |
| `observation.state` | [6] | `<motor>.pos` | *(기존)* 관절 위치. **바뀌지 않습니다** |
| `observation.load` | [6] | `<motor>.load` | `Present_Load`(60). 부호 포함 −1000..1000 |
| `observation.velocity` | [6] | `<motor>.vel` | `Present_Velocity`(58). 부호 포함, 서보 눈금/s |
| `observation.temperature` | [6] | `<motor>.temp` | `Present_Temperature`(63). °C |
| `observation.voltage` | [6] | `<motor>.volt` | `Present_Voltage`(62) ÷ 10. V |
| `observation.servo_status` | [6] | `<motor>.status` | `Status`(65) 바이트 그대로. 과부하·과열 비트 |
| `observation.servo_moving` | [6] | `<motor>.moving` | `Moving`(66). 0/1 |
| `observation.current` | [6] | `<motor>.current` | `Present_Current`(69). **이 펌웨어(3.9)에서는 0이나 1로만 읽힙니다**(2026-09-05 확인). 그래도 남깁니다 — 값이 없다는 사실 자체가 기록입니다 |
| `observation.wall_time` | [1] | `since_start` | 이 프레임의 `Present_Position`을 읽은 시각. **수집 프로세스 시작 epoch으로부터의 초**입니다(float32에 epoch을 그대로 넣으면 30Hz 프레임이 전부 같은 시각이 됩니다). 기준 epoch은 `soarm_provenance.json`의 `started_at` |
| `observation.camera_fresh` | [2] | `scene`, `wrist` | 이 틱이 카메라에서 **새** 프레임을 받았는가. 0/1 |

**부호는 2의 보수가 아니라 부호-크기(sign-magnitude)입니다.** 부하는 비트 10이, 속도는
비트 15가 부호이고, 푸는 것은 `MotorsBus._decode_sign`(가상 리더가 부하를 읽을 때 지나는
바로 그 코드)에 맡깁니다.

읽기가 실패한 틱은 마지막으로 성공한 값을 한 번 더 씁니다 — `validate_frame`은 열이 하나만
비어도 회차를 통째로 막으므로 값을 뺄 수는 없고, 버스 패킷 하나가 깨졌다고 30초짜리 시연을
잃는 편이 훨씬 나쁩니다. 몇 번 그랬는지는 `/api/status`의 `recording.runtime`에
`extras_read_failures`로, 블록 읽기가 30Hz 예산에서 가져간 몫은 `extras_read_ms`로 실립니다.

### API

- `GET /api/datasets`, `GET /api/datasets/{name}` — `extras`에 이 데이터셋이 담은 열의 마지막
  마디 목록(`["load","velocity",…,"camera_fresh"]`). 열이 없는 옛 데이터셋은 `[]`입니다.
- `GET /api/datasets/{name}/episodes/{i}/trajectory` — 기존 `fps`·`frames`·`joints`·`state`·
  `action`에 더해, 열이 있으면 `load`·`velocity`(frames × 6), `camera_fresh`(frames × 2),
  `camera_keys`(`["scene","wrist"]`), `wall_time`(frames × 1). 없으면 키 자체가 빠집니다.

### 회 단위 provenance

수집 세션마다 `data/<dataset>/soarm_provenance.json`의 배열에 항목이 **하나 덧붙습니다**
(이어 찍으면 늘어납니다). 담기는 것: `started_at`(위 `wall_time`의 기준 epoch),
`server_commit`, `lerobot` 버전, 팔로워·리더 calibration의 `sha256`, 되읽은
`camera_controls`, 시작 시 `doctor` 진단, `episode_seconds`, `reset_seconds`, `fps`,
`extras_schema`. calibration 해시를 남기는 이유는 그것이 **데이터를 읽는 자 자체**이기
때문입니다 — 다시 잰 calibration으로 찍은 회차는 앞 회차와 다른 좌표계에 있는데, 그 사실은
데이터셋 안에서 전혀 보이지 않습니다.

## 저장소 구조

```text
src/soarm_console/        FastAPI app, teleop, recording, diagnostics, camera workers
src/soarm_console/static/ 데스크톱 콘솔 페이지와 3D 조작 화면(`viewer/`, 맥·폰 공용)
scripts/                  calibration, doctor, web, teleoperation, recording, service installation
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
