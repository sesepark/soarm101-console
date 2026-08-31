# SO-ARM101 Operations Runbook

현재 구현된 로컬 제어 기능과 향후 network 기능을 구분한다. 로컬 UI에는 observation-only
gate, 단일 teleop/record mode, read-only doctor, camera 단일 소유, calibration 검증,
SIGINT 정지가 있다.

`PROTOCOL.md`의 network lease, heartbeat, watchdog, HOLD는 **가상 리더 경로에서는
구현되어 있다**(아래 6절). 기존 물리 리더 텔레옵 경로에는 없다.

## 1. 기본 검증

```bash
cd /home/deploy/Project/so-arm-101
uv sync --all-groups
.venv/bin/pytest -q
./scripts/doctor.sh
```

`doctor.sh`는 모터 상태를 읽기만 한다. 기대 결과는 arm별 ID 1–6, model 777, 적정 voltage,
그리고 motion 시작 전 torque disabled다.

웹 서비스는 `sg dialout`으로 실행된다. 셸 doctor는 성공하지만 웹 doctor만 port open에
실패하면 `systemctl --user restart soarm-console.service` 후 서비스 child의 그룹을 확인한다.

## 2. Observation-only 웹 UI

```bash
grep '^SOARM_ENABLE_MOTION=' config/soarm.env
./scripts/run_web.sh
```

기대값은 `SOARM_ENABLE_MOTION=0`이다. <http://127.0.0.1:8088>에서 다음을 확인한다.

MacBook에서 접속할 때는 Mac 터미널에서 다음을 먼저 실행한다.

```bash
ssh -N -L 8088:127.0.0.1:8088 deploy@192.168.0.20
```

터널이 유지되는 동안 Mac의 `127.0.0.1:8088`이 서버 웹 UI로 전달된다.

1. `환경 진단`이 양쪽 bus 상태를 표시한다.
2. Scene/Wrist preview가 각각 열리고 동시에 frame을 수신한다.
3. Preview 중지 후 camera owner가 해제된다.
4. Calibration이 없으면 teleop/record가 잠긴다.
5. 잘못된 확인 문구는 hardware 접근 전에 HTTP 400으로 거부된다.

## 3. Calibration

이 단계부터 실제 arm을 손으로 움직인다. 작업공간, clamp, cable, 전원 차단 수단을 먼저 확인한다.

```bash
./scripts/calibrate_follower.sh
./scripts/calibrate_leader.sh
```

Follower와 Leader의 가운데 자세를 잡고 각 관절을 순서대로 유효 범위 안에서 움직인다.
생성될 파일은 다음과 같다.

```text
~/.cache/huggingface/lerobot/calibration/robots/so_follower/soarm101_follower.json
~/.cache/huggingface/lerobot/calibration/teleoperators/so_leader/soarm101_leader.json
```

웹 backend는 JSON motor name, ID 1–6, 필수 field, range min/max를 검사한다.

## 4. 첫 Teleoperation

1. 두 팔을 대응되는 비슷한 자세로 손으로 맞춘다.
2. 현장에 사람이 있고 follower 주변에 장애물이 없는지 확인한다.
3. `config/soarm.env`에서 `SOARM_ENABLE_MOTION=1`로 변경한다.
4. 웹 서비스를 재시작한다.
5. 웹 `환경 진단`에서 bus healthy와 torque disabled를 확인한다.
6. `텔레옵 시작` → 현장 checkbox → `START SOARM101`을 입력한다.
7. 작은 leader 변화로 방향과 joint mapping을 하나씩 확인한다.
8. 문제가 있으면 `현재 모드 중지` 후 물리 전원 차단을 준비한다.

Follower command에는 기본 `max_relative_target=2`가 적용된다. 이는 독립 E-stop이 아니다.

## 5. Dataset recording

1. Scene을 작업공간 전체, Wrist를 gripper와 접촉 지점이 보이게 고정한다.
2. 두 preview로 역할과 방향을 확인한다.
3. `config/soarm.env`에서 `SOARM_CAMERA_ROLES_CONFIRMED=1`로 변경한다.
4. 웹 서비스를 재시작한다.
5. 단일 task 문장, episode 수, episode 시간을 입력한다.
6. `RECORD SOARM101` 확인 후 새 local session을 시작한다.
7. 성공은 `성공 저장`, 실패는 `실패 폐기`, 종료는 `전체 수집 종료`를 사용한다.
8. `data/`의 metadata, Parquet, MP4, episode count를 검토한다.
9. 검토가 끝난 dataset만 별도 명령으로 Hugging Face Hub에 upload한다.

웹 camera preview와 recording은 같은 장치를 동시에 열 수 없다. Record 시작 시 preview를
중지하며, camera가 다른 process에 점유돼 있으면 `scripts/record.sh`가 거부한다.

## 6. 정상 종료

1. 웹에서 `현재 모드 중지`를 누른다.
2. Log에서 process return을 확인한다.
3. Camera preview를 중지한다.
4. 필요하면 웹 서비스를 종료한다.
5. Serial/video owner가 없는지 확인한다.

```bash
fuser /dev/ttyACM0 /dev/ttyACM1 /dev/video0 /dev/video2
```

## 7. 장애 처리

- Leader/Follower 단절: 새 action을 시작하지 말고 active mode를 중지한다.
- Camera 단절: dataset recording을 중지하고 해당 episode를 사용하지 않는다.
- Web/API 단절: systemd가 process group을 정리하는지 확인한다.
- SIGINT 정지 실패: 독립 power cutoff를 사용한다. 자동 강제 재개하지 않는다.
- SSH 단절: SSH 자체는 heartbeat가 아니다. 현재 network control은 구현되지 않았다.
- Calibration mismatch: motion gate를 다시 닫고 두 calibration을 재검토한다.


## 6. 가상 리더 (물리 리더 팔 없는 원격 텔레옵)

3D로 그린 팔을 맥 앱이나 아이폰에서 끌면 팔로워가 따라온다. 설계 근거는
[ADR 0002](ADR/0002-virtual-leader-owner.md), 문턱값과 미확인 항목은 맥 앱 저장소의
`docs/원격_텔레옵_안전.md`에 있다.

### 6.1 한 번만 하는 설정

```bash
# 조작 권한 토큰. 관찰에는 필요 없고 팔을 움직이는 요청에만 붙는다.
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# config/soarm.env 에 SOARM_MOTION_TOKEN=<값> 을 넣고
systemctl --user restart soarm-console
curl -s http://127.0.0.1:8088/api/vleader | python3 -m json.tool | head -20
```

`preflight`가 비어 있어야 시작할 수 있다. 비어 있지 않으면 그 문장이 이유다.

아이폰에서 쓰려면 Tailscale Serve를 켠다. tailnet 안에서만 열리고 funnel은 쓰지 않는다.

```bash
tailscale serve --bg --https=443 8088
tailscale serve status
```

`Serve is not enabled on your tailnet`이 나오면 안내된 주소를 브라우저에서 한 번 열어
켠 뒤 다시 실행한다.

### 6.2 평소 순서

1. `POST /api/vleader/start` — 팔로워 serial을 잡고 30Hz 관찰. **토크는 걸지 않는다.**
2. 토크를 걸기 전에 팔을 손으로 움직여 3D가 같은 방향으로 도는지 본다.
3. `POST /api/vleader/arm` + `MOVE SOARM101` — 토크를 건다.
4. `POST /api/vleader/lease` — 조작 권한 하나. 동시에 하나만 발급된다.
5. 조작. 손을 떼면 팔은 그 자리에 선다.
6. `DELETE /api/vleader/lease/{id}` — 반납. 팔은 자세를 유지한다.
7. `POST /api/vleader/stop` — 루프를 내린다. **토크가 걸려 있으면 거절한다.**

### 6.3 멈췄을 때

```bash
curl -s http://127.0.0.1:8088/api/vleader | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['state'], d['fault'])"
```

- `HOLD` + `fault.code` — 왜 멈췄는지가 그 안에 있다. 원인을 확인한 뒤
  `POST /api/vleader/resume`. **이전 동작을 이어서 하지 않는다** — 다음 명령은 현재 자세
  근처에서 시작해야 한다.
- `FAULT` + `HARDWARE_ERROR` — serial이 흔들렸다. 토크는 그대로 두었다. 팔을 받치고
  `POST /api/vleader/torque/release` + `RELEASE TORQUE SOARM101`로 내리거나,
  버스가 돌아왔으면 `resume`.
- **팔이 계속 움직이면** 소프트웨어를 믿지 말고 전원 플러그를 뽑는다. 독립 차단 수단은 없다.

### 6.4 누가 쥐고 있는지 모를 때

```bash
curl -s http://127.0.0.1:8088/api/vleader | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['lease'], d['lease_history'][-3:])"
curl -s -X POST http://127.0.0.1:8088/api/vleader/hold   # 토큰도 리스도 필요 없다
```

리스는 5초 만에 만료되므로, 쥔 쪽이 사라졌다면 기다리면 풀린다. 강제로 빼앗는 길은 없다.

### 6.5 상호배타 확인

가상 리더가 도는 동안 `lerobot-teleoperate`와 `lerobot-record`(물리 리더)는 409로 막힌다.
그 반대도 같다. 셋이 겹치면 같은 serial에 두 프로세스가 말을 걸어 status packet이 깨진다 —
개발 중에 실제로 한 번 보았고, 그 상태의 로그는
`Failed to read 'Present_Voltage' on id_=1 ... Incorrect status packet!`이다.

### 6.6 하드웨어 없이 시험하기

```bash
SOARM_VL_BACKEND=simulated SOARM_VL_SIM_OBSTACLE=elbow_flex:12 \
  SOARM_MOTION_TOKEN=sim-token SOARM_ENABLE_MOTION=1 \
  .venv/bin/uvicorn soarm_console.app:app --app-dir src --port 8090
```

흉내 백엔드는 serial을 열지 않는다. `SOARM_VL_SIM_OBSTACLE`로 지정한 각도에서 관절이 막혀
접촉 트립과 물러남을 팔 없이 걸어 볼 수 있다.
