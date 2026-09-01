# SO-ARM101 운영 Runbook

현재 구현된 기능과 현장 검증 전 절차를 구분한다. 이 프로젝트는 안전 인증 시스템이 아니며,
독립 actuator power cutoff와 물리 E-stop은 없다. 실제 팔을 움직이거나 토크를 거는 단계는
현장에 사람이 있고 팔을 받칠 수 있을 때만 한다.

## 1. 기본 검증

```bash
cd /home/deploy/Project/so-arm-101
uv sync --all-groups
.venv/bin/pytest -q
./scripts/doctor.sh
```

`doctor.sh`는 모터 register를 바꾸지 않고 ID 1–6, model 777, voltage, position, torque 상태를
읽는다. 읽기도 serial packet을 보내므로 다른 owner가 있으면 장치 lock이 거절한다. motion
시작 전 기대값은 두 팔 모두 healthy이고 torque가 꺼진 상태다.

## 2. Observation-only 웹 UI

```bash
grep '^SOARM_ENABLE_MOTION=' config/soarm.env
./scripts/run_web.sh
```

처음 기대값은 `SOARM_ENABLE_MOTION=0`이다. <http://127.0.0.1:8088>에서 확인한다.

1. `환경 진단`이 양쪽 bus 상태를 표시한다.
2. Scene/Wrist preview가 각각 열리고 frame을 받는다.
3. Preview 중지 후 camera owner가 해제된다.
4. calibration이 없으면 teleop/record가 잠긴다.
5. 잘못된 확인 문구는 hardware 접근 전에 HTTP 400으로 거부된다.

MacBook에서 SSH tunnel로 관찰할 때는 Mac에서 실행한다.

```bash
ssh -N -L 8088:127.0.0.1:8088 <계정>@<서버 주소>
```

주소와 계정은 이 저장소가 공개이므로 여기 적지 않는다. Mac 앱을 쓴다면 그 값은
`~/Library/Application Support/SeoulLocalAgent/soarm-console.json`에 있고, 앱이 같은 터널을
스스로 연다.

## 3. Calibration

이 단계부터 실제 팔을 손으로 움직인다. 작업공간, clamp, cable, 전원 차단 수단을 먼저
확인한다.

```bash
./scripts/calibrate_follower.sh
./scripts/calibrate_leader.sh
```

Follower와 Leader를 안전한 가운데 자세에 두고, 안내되는 관절을 하나씩 실사용 범위 안에서
움직인다. 결과 파일은 다음 위치에 생긴다.

```text
~/.cache/huggingface/lerobot/calibration/robots/so_follower/soarm101_follower.json
~/.cache/huggingface/lerobot/calibration/teleoperators/so_leader/soarm101_leader.json
```

웹 backend는 motor name, ID 1–6, range min/max를 검사한다. calibration 변경은 active motion과
분리한다.

## 4. 첫 물리 리더 Teleoperation

1. 두 팔을 대응되는 비슷한 자세로 손으로 맞춘다.
2. 현장에 사람이 있고 follower 주변에 장애물이 없는지 확인한다.
3. `config/soarm.env`의 `SOARM_ENABLE_MOTION=1`을 확인하고 웹 서비스를 재시작한다.
4. 웹 `환경 진단`에서 healthy와 torque disabled를 확인한다.
5. `텔레옵 시작`에서 현장 확인 문구를 사람이 직접 입력한다. UI가 미리 채우지 않는다.
6. 작은 변화로 관절 방향을 하나씩 확인한다.
7. 문제가 있으면 `현재 모드 중지`를 누르고 물리 전원 차단을 준비한다.

물리 리더 경로에는 가상 리더의 lease/watchdog/health trip이 없다. LeRobot의
`max_relative_target=2`와 현장 사람만 남는다. 종료나 예외에서 자동 torque-off하지 않도록
설정되어 있으므로, 프로세스가 멈췄다는 사실을 팔의 무에너지 상태로 해석하지 않는다.

## 5. Dataset recording

1. 작업공간 전체가 보이도록 Scene camera를, gripper와 접촉 지점이 보이도록 Wrist camera를
   고정한다.
2. 두 preview로 역할과 방향을 확인한 뒤 preview를 중지한다.
3. `SOARM_CAMERA_ROLES_CONFIRMED=1`을 확인한다.
4. 수집 확인 문구는 사람이 직접 입력하고 task, episode 수와 시간을 고른다.
5. 성공은 오른쪽, 재시도는 왼쪽, 종료는 Esc에 해당하는 웹 제어로 기록한다.
6. `data/`의 dataset과 `runtime/record/status.json` 상태를 확인한다.

수집 중 serial과 camera owner는 `lerobot-record`다. 가상 리더 수집이면 콘솔은 목표만
중계한다. 이때 부하·전류·온도·추종오차 감지는 **아직 구현되지 않았다**. 조사 결과만
[ADR 0004 초안](ADR/0004-recording-safety-ladder-draft.md)에 있다.

## 6. 가상 리더

가상 리더는 물리 리더 팔 대신 3D viewer의 목표를 받아 follower를 제어한다. 설계는
[ADR 0002](ADR/0002-virtual-leader-owner.md), 장치 소유권은
[ADR 0003](ADR/0003-device-owner-lock.md)을 따른다.

### 6.0 고친 코드는 재시작해야 돈다

당연해 보이지만 한 번 걸렸다. owner lock을 넣고 커밋하고 pytest까지 통과시킨 뒤에도, 돌고
있는 서비스는 **그 전 코드**였다. 밖의 프로세스가 가상 리더가 쥔 팔로워를 그대로 다시 열어
읽었고, 두 프로세스가 같은 버스에 붙는 것을 실제로 만들었다 — lock이 막았어야 할 바로 그
상황이다. 원인은 하나였다: `systemctl --user restart`를 하지 않았다.

커밋 시각과 서비스가 뜬 시각을 비교하면 바로 보인다.

```bash
systemctl --user show soarm-console -p ActiveEnterTimestamp --value
git log -1 --format=%cI
```

앞의 것이 뒤의 것보다 이르면 서비스는 옛 코드다. 고친 뒤에는 반드시:

```bash
systemctl --user restart soarm-console.service
```

lock이 실제로 걸려 있는지는 가상 리더를 켠 상태에서 이렇게 본다.

```bash
PYTHONPATH=src .venv/bin/python -c "
from soarm_console.config import Settings
from soarm_console.owner_lock import DeviceLockSet, DeviceLockError
try:
    with DeviceLockSet.acquire([Settings().follower_port], 'probe'): print('잡혔다 - 막지 못했다')
except DeviceLockError as e: print('거절:', e)"
```

`거절: Device is owned by virtual-leader (pid ...)`가 나와야 한다. `잡혔다`가 나오면 서비스가
옛 코드이거나 lock 경로가 어긋난 것이다.

### 6.1 한 번만 하는 설정과 Tailscale Serve

조작 토큰은 관찰에는 필요 없고 motion 권한 확인에만 쓴다. 실제 값은 응답이나 문서에 넣지
않는다.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# 출력값을 config/soarm.env의 SOARM_MOTION_TOKEN에 저장한 뒤 서비스를 재시작한다.
curl -s http://127.0.0.1:8088/api/vleader | python3 -m json.tool | head -20
```

`preflight`가 비어 있어야 한다. 비어 있지 않으면 그 문장이 시작을 막는 이유다.

아이폰 경로는 Tailscale Serve만 쓰며 Funnel은 쓰지 않는다. 2026-09-01 이 노드에서 아래
명령을 실행했을 때 Serve가 아직 tailnet에 활성화되지 않아 거절되었다.
Serve는 tailnet 안에만 공개되고 HTTPS 인증서를 요구한다는 동작은
[Tailscale 공식 Serve 문서](https://tailscale.com/docs/features/tailscale-serve)를 기준으로 한다.

```bash
tailscale serve --bg --https=443 8088
```

Serve가 아직 켜져 있지 않으면 위 명령이 `Serve is not enabled on your tailnet`과 함께
**활성화 주소를 그 자리에서 출력한다.** 그 주소는 이 tailnet의 특정 node를 가리키므로 여기
적어 두지 않는다 — 이 저장소는 공개이고, 명령을 한 번 돌리면 언제든 다시 나온다. 계정
소유자가 그 주소를 브라우저에서 한 번 열어 승인한 뒤, 서버에서 다시 실행하고 공개된 tailnet
URL을 확인한다.

```bash
tailscale serve --bg --https=443 8088
tailscale serve status
tailscale serve status --json
```

`Available within your tailnet` 아래의 `https://<node>.<tailnet>.ts.net`을 `TAILNET_URL`로
쓴다. 다음 검사는 viewer를 열 뿐 팔을 시작하거나 토크를 걸거나 목표를 보내지 않는다.

```bash
TAILNET_URL='https://<tailscale-serve-status에 표시된 주소>'

# 관찰 화면은 200이어야 한다.
curl -sS -o /dev/null -w 'viewer=%{http_code}\n' "$TAILNET_URL/viewer/"

# application token이 없으면 401이어야 한다.
curl -sS -o /dev/null -w 'token 없음=%{http_code}\n' \
  "$TAILNET_URL/api/vleader/motion-auth"

# 다른 tailnet 장치에서 토큰을 화면에 보이지 않게 입력한다. 이 GET은 motion을 하지 않는다.
read -rsp 'SOARM_MOTION_TOKEN: ' MOTION_TOKEN; echo
printf 'header = "X-SOARM-Motion-Token: %s"\n' "$MOTION_TOKEN" | \
  curl --config - -sS -o /dev/null -w 'token 있음=%{http_code}\n' \
  "$TAILNET_URL/api/vleader/motion-auth"
unset MOTION_TOKEN
```

기대 코드는 차례대로 `200`, `401`, `200`이다. 마지막 200은 토큰이 맞다는 뜻뿐이며 장치 준비,
확인 문구 통과, motion 허가를 뜻하지 않는다. 이 점검 endpoint는 확인 문구와 그 값을 반환하지
않는다. Serve 승인 전이므로 이 tailnet 실검증은 **아직 수행되지 않았다**.

### 6.2 평소 순서

아래 3번부터는 실제 팔 절차다. 이번 변경 작업에서는 실행하지 않았다.

1. `POST /api/vleader/start` — follower serial을 잡고 30Hz로 관찰한다. 토크는 걸지 않는다.
2. 토크가 꺼진 채 팔을 손으로 움직여 3D가 같은 방향으로 도는지 확인한다.
3. 현장 사람이 확인 문구를 직접 입력해 `POST /api/vleader/arm`을 수행한다.
4. 다시 확인 문구를 직접 입력해 `POST /api/vleader/lease`로 조작 권한 하나를 받는다.
5. 작은 범위부터 조작한다. 손을 떼면 팔은 그 자세를 유지한다.
6. `DELETE /api/vleader/lease/{id}`로 반납한다. 팔은 자세를 유지한다.
7. 사람이 팔을 받치고 별도 확인을 거친 torque release 뒤 `POST /api/vleader/stop`으로 루프를
   내린다. 토크가 걸려 있으면 기본 stop은 거절한다.

### 6.3 HOLD/FAULT

```bash
curl -s http://127.0.0.1:8088/api/vleader | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['state'], d['fault'])"
```

- `HOLD`이면 `fault.code`의 원인을 확인한 뒤 사람이 `resume`한다. 이전 동작은 자동 재개하지
  않으며, 다음 명령은 현재 자세 근처에서 다시 시작한다.
- `HARDWARE_ERROR`여도 자동으로 토크를 끄지 않는다. 버스가 돌아왔는지 확인하거나, 사람이
  팔을 받친 뒤 별도 torque release 절차를 따른다.
- 팔이 계속 움직이면 software stop을 믿지 말고 물리 전원을 차단한다.

### 6.4 lease와 장치 owner 확인

```bash
curl -s http://127.0.0.1:8088/api/vleader | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['lease'], d['lease_history'][-3:])"
curl -s -X POST http://127.0.0.1:8088/api/vleader/hold
```

lease는 5초 만에 만료되며 강제로 빼앗는 길은 없다. 장치 owner lock 기본 위치는
`$XDG_RUNTIME_DIR/soarm-console/owner-locks/*.lock`이고, `XDG_RUNTIME_DIR`이 없으면
`/tmp/soarm-console-$UID/owner-locks/*.lock`이다.

```bash
lslocks -o PID,COMMAND,PATH | rg 'soarm-console|owner-locks'
if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
  OWNER_LOCK_DIR="$XDG_RUNTIME_DIR/soarm-console/owner-locks"
else
  OWNER_LOCK_DIR="/tmp/soarm-console-$UID/owner-locks"
fi
for lock in "$OWNER_LOCK_DIR"/*.lock; do
  [ -e "$lock" ] && python3 -m json.tool "$lock"
done
```

active 여부는 JSON의 PID가 아니라 커널 `flock`이 결정한다. crash 뒤 파일이 남아도
정상 시작이 자동으로 stale metadata를 덮어쓴다.

lock 파일을 삭제하거나 수정해 owner를 빼앗지 않는다. active owner이면 정상 stop/SIGINT를
쓰고, 이미 죽었다면 원하는 정상 경로를 다시 시작한다. 계속 막히면 metadata, `lslocks`, 장치
`fuser`를 함께 검토한다. `force-unlock` 경로는 없다.

프로젝트가 제어하는 가상 리더·물리 텔레옵·수집은 프로세스가 달라도 lock으로 상호배타다.
그러나 advisory lock을 무시하고 upstream `lerobot-teleoperate`를 직접 실행한 프로세스까지
OS가 차단하지는 않는다. 직접 upstream 실행은 지원 운용 경로가 아니며, 막힌다고 가정하지
않는다.

### 6.5 하드웨어 없이 시험하기

```bash
SOARM_VL_BACKEND=simulated SOARM_VL_SIM_OBSTACLE=elbow_flex:12 \
  SOARM_MOTION_TOKEN=sim-token SOARM_ENABLE_MOTION=1 \
  .venv/bin/uvicorn soarm_console.app:app --app-dir src --port 8090
```

simulated backend는 실물 serial을 열거나 lock하지 않는다. 접촉 trip과 물러남을 팔 없이 확인할
수 있다.

### 6.6 접촉 문턱 실측

`scripts/measure_contact.py`는 관절별 `Present_Load`, `Present_Current`,
`Present_Temperature`를 10Hz로 읽는다. 스스로 토크를 걸거나 끄지 않는다. `handled`,
`holding`, `contact`는 사람이 현장에 있을 때만 측정하며, 뒤의 두 단계는 이미 토크가 걸린
상태를 요구한다.

| 단계 | 무엇을 재는가 | 사람 | 토크 |
|---|---|---|---|
| `quiescent` | 팔을 건드리지 않은 바닥값 | 불필요 | 꺼짐 |
| `handled` | 사람이 토크가 꺼진 팔을 손으로 움직이는 동안 | 필요 | 꺼짐 |
| `holding` | 자세를 유지하며 중력을 버티는 동안 | 필요 | 걸림 |
| `contact` | 사람이 통제해 팔을 물체에 접촉시키는 동안 | 필요 | 걸림 |

실측할 때는 요약 결과 JSON을 각각 남긴다. 다음 명령은 절차 기록이며 이번 작업에서는 실행하지
않았다.

```bash
mkdir -p runtime/contact
sg dialout -c ".venv/bin/python scripts/measure_contact.py handled --seconds 30 \
  --json runtime/contact/handled.json"

# 현장 절차로 토크를 건 뒤 가상 리더 loop만 내려 serial owner를 반납한다.
sg dialout -c ".venv/bin/python scripts/measure_contact.py holding --seconds 30 \
  --json runtime/contact/holding.json"
# contact는 측정 창 전체에서 사람이 같은 접촉을 유지한 상태로 시작한다.
sg dialout -c ".venv/bin/python scripts/measure_contact.py contact --seconds 30 \
  --json runtime/contact/contact.json"
```

가상 리더가 follower serial을 쥐고 있으면 측정기는 owner lock에서 거절된다. `holding`과
`contact`를 재기 위해 loop만 강제 종료하는 기존 절차는 토크를 그대로 남긴다. 반드시 현장
사람이 팔과 전원 플러그에 닿는 상태에서만 한다.

#### 실측값과 문턱 결정 규칙

`quiescent` (2026-09-01, 25초, 10Hz, 토크 꺼짐, 팔 정지):

```text
관절              부하 min  p50  p95  max  전류 min  p50  p95  max  (mA max)  온도 max
shoulder_pan            0    0    0    0         0    0    0    1         6        36
shoulder_lift           0    0    0    0         0    0    0    0         0        36
elbow_flex              0    0    0    0         0    0    0    1         6        34
wrist_flex              0    0    0    0         0    0    0    0         0        36
wrist_roll              0    0    0    0         0    0    0    1         6        36
gripper                 0    0    0    0         0    0    0    0         0        48
```

남은 칸은 실측 전에는 채우지 않는다.

| 신호 | holding 최대 | contact 최소 | 허용 구간 | 선택값 |
|---|---:|---:|---:|---:|
| `Present_Load` | — 미측정 | — 미측정 | — | 현재 400 |
| `Present_Current` | — 미측정 | — 미측정 | — | 현재 108 |

문턱은 신호별로 반드시 다음을 만족해야 한다.

```text
holding에서 관측한 최대값 < 새 문턱 < contact에서 관측한 최소값
```

contact 시작 전의 비접촉 표본이나 접촉을 푸는 전환 표본이 섞이면 최소값으로 쓸 수 없으므로
그 trial은 버리고 다시 잰다. 접촉 대상 관절과 방향별 유효 trial의 최소값만 비교한다.
정상 holding보다 낮으면 자세 유지 중 오검출하고, contact 최소보다 높으면 접촉을 놓친다.
반복 측정과 대상 관절에서 이 구간이 겹치지 않으면 임의의 중간값을 고르지 않는다. 단일 전역
문턱으로 분리할 수 없다는 결과로 기록하고 관절별 문턱 또는 다른 신호 조합을 다시 설계한다.

`holding`과 `contact`를 재기 전까지 `SOARM_VL_LOAD_TRIP=400`과
`SOARM_VL_CURRENT_TRIP=108`은 여전히 데이터시트와 LeRobot 기본값에서 가져온 **유추값**이다.
quiescent가 0이라는 사실만으로 접촉 검출이 검증되었다고 쓰지 않는다.

### 6.7 온도 문턱 재검토

quiescent에서 팔 관절은 34–36°C였지만 gripper는 48°C였다. 그래서 경고를 55°C에서 58°C로
올렸고, 현재 정지는 65°C다. STS3215 자체 차단은 70°C에서 토크를 끊어 팔을 떨어뜨릴 수 있어,
software 정지가 그보다 먼저 HOLD하기 위해 이 문턱들이 존재한다. 어떤 온도 fault에서도
자동 torque-off하지 않는다.

측정기는 이제 온도의 시작, 끝, 상승량, 최대를 함께 출력한다. holding 뒤 아래 칸을 채운다.

| 관절 | 시작 °C | 끝 °C | 상승 °C/측정시간 | 최대 °C | 판정 |
|---|---:|---:|---:|---:|---|
| shoulder_pan | — | — | — | — | 미측정 |
| shoulder_lift | — | — | — | — | 미측정 |
| elbow_flex | — | — | — | — | 미측정 |
| wrist_flex | — | — | — | — | 미측정 |
| wrist_roll | — | — | — | — | 미측정 |
| gripper | — | — | — | — | 미측정 |

58–65°C의 7°C 간격이 쓸모 있는지는 다음으로 판단한다.

1. 정상 holding 최대와 측정 변동이 58°C 아래에 있어 경고가 상시 켜지지 않는가.
2. 관측된 가장 빠른 상승률에서 58°C 경고 후 65°C HOLD까지 현장 사람이 대응할 시간이 있는가.
3. 65°C HOLD가 70°C 자체 torque 차단보다 충분히 먼저 작동하는가.

실측 전에는 58/65가 검증되었다고 쓰지 않는다. 관절별 열 거동이 달라 전역 scalar가 유효하지
않으면 다음 config 모양을 제안한다. **현재 구현은 이 TOML이나 관절별 override를 읽지 않는다.**

```toml
[virtual_leader.temperature.default]
warn_c = 58
trip_c = 65

[virtual_leader.temperature.joints.gripper]
warn_c = 60  # 예시 모양일 뿐, 실측값이 아니다.
trip_c = 65
```

미지정 관절은 default를 쓰고, 모든 `trip_c`는 70°C보다 낮아야 한다. 실제 숫자는 holding 실측
뒤에만 제안한다.

## 7. 정상 종료

1. 웹에서 `현재 모드 중지`를 누른다.
2. process return과 fault를 확인한다.
3. camera preview를 중지한다.
4. 필요하면 웹 서비스를 종료한다.
5. serial/video owner와 owner lock을 확인한다.

```bash
fuser /dev/ttyACM0 /dev/ttyACM1 /dev/video0 /dev/video2
lslocks -o PID,COMMAND,PATH | rg 'soarm-console|owner-locks'
```

프로세스 종료는 torque-off를 뜻하지 않는다. 팔을 받치고 명시적으로 해제하지 않았다면 토크가
남아 있다고 취급한다.

## 8. 장애 처리

- Leader/Follower 단절: 새 action을 시작하지 말고 active mode를 중지한다.
- Camera 단절: recording을 중지하고 해당 episode를 사용하지 않는다.
- Web/API 단절: process group 상태를 확인한다. 자동 motion 재개는 하지 않는다.
- SIGINT 정지 실패: owner lock을 지우지 않는다. 프로세스와 장치 `fuser`를 조사하고, 실제
  motion이 계속되면 물리 전원을 차단한다.
- SSH 단절: SSH 자체는 heartbeat가 아니다.
- Calibration mismatch: motion gate를 닫고 두 calibration을 재검토한다.
- 어떤 software fault에서도 자동으로 torque를 끄지 않는다. 사람이 팔을 받친 뒤 별도 절차를
  따른다.
