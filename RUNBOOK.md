# SO-ARM101 Operations Runbook

현재 구현된 로컬 제어 기능과 향후 network 기능을 구분한다. 로컬 UI에는 observation-only
gate, 단일 teleop/record mode, read-only doctor, camera 단일 소유, calibration 검증,
SIGINT 정지가 있다. `PROTOCOL.md`의 network lease, heartbeat, 독립 watchdog/HOLD는 아직
구현되지 않았다.

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
