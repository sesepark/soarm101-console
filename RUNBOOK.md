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

`doctor.sh`는 모터 register를 바꾸지 않고 ID 1–6, model 777, voltage, position, torque,
temperature를 읽는다. 읽기도 serial packet을 보내므로 다른 owner가 있으면 장치 lock이
거절한다. motion 시작 전 기대값은 두 팔 모두 `healthy`이고, **torque가 켜져 있어도 된다**
(2026-09-05). 텔레옵과 수집은 팔이 떨어지지 않도록 토크를 켠 채 끝나므로, 켜져 있는 것이
정상적으로 끝낸 세션 다음의 모습이다. 자세한 이유는 `SAFETY.md`의 "진단이 더 이상 토크를
묻지 않는다"에 있다.

### 1.1 영상 인코딩에 필요한 시스템 라이브러리

이 기계에는 `libavdevice.so.58`이 없어 `torchcodec`이 로드되지 않는다. 동작 자체는
`pyav` 폴백으로 돌지만, `record.log`가 100줄짜리 트레이스백으로 시작해 정작 읽어야 할
경고를 밀어낸다. 사람이 한 번 설치한다:

```bash
sudo apt install ffmpeg
```

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

1. 현장에 사람이 있고 follower 주변에 장애물이 없는지 확인한다.
2. `config/soarm.env`의 `SOARM_ENABLE_MOTION=1`을 확인하고 웹 서비스를 재시작한다.
3. 웹 `환경 진단`에서 `healthy`를 확인한다. 토크 상태는 이제 시작을 막지 않는다.
4. `텔레옵 시작`에서 현장 확인 문구를 사람이 직접 입력한다. UI가 미리 채우지 않는다.
5. **팔로워가 리더의 지금 자세까지 스스로 걸어간다**(최대 6초). 그동안 리더를 움직이지
   않는다 — 목표는 시작하는 순간의 리더 자세로 한 번만 잡는다. 걸어가는 도중 무언가
   잘못되면 `현재 모드 중지`가 그 자리에서 세운다(토크는 유지된다).
6. 작은 변화로 관절 방향을 하나씩 확인한다.
7. 문제가 있으면 `현재 모드 중지`를 누르고 물리 전원 차단을 준비한다.

두 팔을 손으로 맞춰 두지 않아도 된다(2026-09-05). 예전에는 1번이 "두 팔을 비슷한 자세로
손으로 맞춘다"였는데, LeRobot의 첫 틱이 리더 자세를 그대로 보내므로 그 맞춤이 사람의
책임이었다. 지금은 시작 정렬이 그 일을 하고, 사람이 하던 맞춤보다 정확하다.

물리 리더 경로에는 가상 리더의 lease/watchdog/health trip이 없다. LeRobot의
`max_relative_target`과 현장 사람만 남는다. 종료나 예외에서 자동 torque-off하지 않도록
설정되어 있으므로, 프로세스가 멈췄다는 사실을 팔의 무에너지 상태로 해석하지 않는다.

텔레옵 자식은 `scripts/teleoperate.sh` → `soarm_console.teleoperating`이다. 로그는
`/api/status`의 `teleoperation.logs`에 실리고, 정렬이 시작될 때
`Walking the follower to the leader's pose over N.Ns (furthest joint <이름> <거리>)` 한 줄을
남긴다.

## 5. Dataset recording

1. 작업공간 전체가 보이도록 Scene camera를, gripper와 접촉 지점이 보이도록 Wrist camera를
   고정한다.
2. 두 preview로 역할과 방향을 확인한 뒤 preview를 중지한다.
3. `SOARM_CAMERA_ROLES_CONFIRMED=1`을 확인한다.
4. 수집 확인 문구는 사람이 직접 입력하고 task, episode 수와 시간을 고른다.
5. 첫 회차 앞에서 팔로워가 리더 자세까지 걸어간다(`phase="aligning"`). 그동안 리더를
   움직이지 않는다.
6. 성공은 오른쪽, 재시도는 왼쪽, 저장하고 종료는 Esc, **찍던 회를 버리고 종료는 `abort`**에
   해당하는 웹 제어로 기록한다.
7. `data/`의 dataset과 `runtime/record/status.json` 상태를 확인한다.

### 5.1 회차를 버리는 것과 저장하고 끝내는 것

`esc`는 루프를 빠져나온 뒤 `save_episode()`가 그대로 돌아 **찍다 만 회를 저장한다.**
`data/soarm101_20260905_092024`의 2회차가 그렇게 남았다 — 82프레임, 2.7초. 잘못된 회차를
남기지 않고 끝내려면 `abort`를 쓴다.

이미 들어 있는 회차를 꺼내려면:

```bash
curl -X DELETE http://127.0.0.1:8088/api/datasets/<name>/episodes/<index>
```

제자리 편집이므로 LeRobot이 원본을 `data/<name>_old`로 옮겼다가 콘솔이 그것을
`data/.trash/`로 보낸다. 수집이나 재생이 도는 동안에는 409다. 데이터셋 전체를 지우는
`DELETE /api/datasets/<name>`도 `data/.trash/<name>-<시각>`으로 **옮기기만** 한다 —
디스크를 실제로 비우는 것은 사람이 `rm -rf data/.trash`로 한다.

### 5.2 이어 찍기

```bash
curl -X POST http://127.0.0.1:8088/api/recording/start \
  -H 'content-type: application/json' \
  -d '{"confirmation":"RECORD SOARM101","task":"<기존과 똑같은 과제>",
       "episodes":5,"episode_seconds":30,"dataset":"<name>","resume":true}'
```

과제 문자열이 데이터셋에 적힌 것과 **정확히** 같아야 한다. 다르면 400
`Dataset task does not match: dataset has […], request has […]`다.

### 5.3 수집 중에 카메라가 무엇을 보고 있나

수집이 도는 동안 콘솔의 MJPEG 프리뷰는 꺼진다 — 장치를 쥔 것은 record 자식이다. 대신
찍는 쪽이 보는 프레임이 5Hz로 내려온다:

```bash
curl -s http://127.0.0.1:8088/api/recording/preview/scene.jpg -o /tmp/scene.jpg
```

3초보다 오래된 그림은 404다. 404가 계속 나오는데 수집은 돌고 있다면 그 카메라가 프레임을
주지 못하고 있다는 뜻이므로 `/api/status`의 `recording.runtime.camera_stale_pct`를 본다.

수집이 끝나면 `data/<name>/record.log`와 `data/<name>/soarm_quality.json`이 남는다. 후자에는
마지막 회차의 `loop_hz`, 카메라별 stale 비율, 이번 실행에서 센 느린 루프 경고 수가 들어 있고,
`GET /api/datasets`가 그것을 함께 실어 보낸다.

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

### 6.6b 실물에서 잰 값 — 부하로는 접촉을 알 수 없다

2026-09-01, 그리퍼만 실제로 움직여 재었다(다른 관절은 사람이 있어야 하므로 재지 못했다).

| 상황 | `Present_Load` | `Present_Current` |
|---|---|---|
| 쉬는 중 (토크 꺼짐) | 0 | 0 |
| 자세 유지 (토크 걸림, 안착 자세) | 0 | 0 |
| 자유롭게 움직이는 중 | 24 ~ 100 | 1 ~ 4 |
| 턱이 맞닿아 막힘 | 48 ~ 128 | 1 ~ 6 |

**자유 이동과 막힘의 구간이 겹친다.** 문턱 400은 어느 쪽에도 가깝지 않으므로, 이 관절에서
접촉을 부하로 알아내는 일은 일어나지 않는다.

설계에서 따라 나오는 결과다. 틱당 변화량 상한 때문에 목표는 실제보다 최대 한 틱만 앞설 수
있고, 위치 P 게인이 16이므로 서보가 장애물에 주는 힘은 그 위치 오차에 비례해 작다. 팔이
세게 밀지 못하게 만들어 두었으니 부하도 크게 읽힐 수 없다 — 안전 장치 둘이 서로를 무디게
만든 셈이다.

그래서 접촉 감지의 주된 수단을 **막힘 검사**로 옮겼다: 사람이 요청한 값(틱당 상한으로 자르기
전의 것)과 실제가 벌어진 채, 최근 1초 동안 실제가 `SOARM_VL_STALL_EPSILON`(0.6)만큼도
움직이지 않으면 닿은 것으로 본다. 예전 검사는 **자른** 목표와 실제를 비교했는데 그 둘은
매 틱 다시 붙으므로 정의상 벌어지지 않았다 — 있었지만 발화할 수 없는 검사였다.

실물 확인:

```
RETREATING → HOLD · FOLLOWING_ERROR · gripper
"집게를 1.2% 더 보내라는 명령이 이어지는데 400ms 넘게 제자리입니다 — 무언가에 닿았습니다"
```

부하·전류 문턱은 남겨 두되 이 팔에서 검증되지 않았다. `shoulder_lift`처럼 팔 전체를 드는
관절에서는 숫자가 다를 수 있고, 그것은 사람이 있을 때 `holding`/`contact`로 재야 한다.

### 6.6d 끝단에 닿으면 벌어질 자리가 없다

6.6b의 막힘 검사에는 구멍이 하나 있었다. **사람이 요청한 값과 실제의 벌어짐**을 보는데,
기계적 끝단에서는 그 벌어짐이 자랄 수 없다. 집게를 0%로 계속 보내도 팔은 1.6%에 서고,
남은 벌어짐 1.6%는 문턱(`SOARM_VL_FOLLOW_ERROR_PERCENT`, 2.0)에 닿지 않는다.

2026-09-01 실측. 집게를 0%로 20초 동안 계속 보냈다:

```
최고 부하 120 · 최고 전류 5 · 최고 온도 46
20초 동안 아무것도 걸리지 않았다
```

즉 부하 120으로 미는 상태가 20초 동안 유지됐고 사다리 어느 칸에도 걸리지 않았다. 남은
보호는 온도(65°C)뿐이었는데, 같은 구간에서 온도는 20초에 2°C 올랐다 — 문턱까지 3분쯤
걸린다는 뜻이다. 모터를 지키기에는 느리다.

그래서 칸을 하나 더 놓았다(`STALLED`). **셋을 함께** 본다:

1. 목표가 실제보다 `SOARM_VL_STALL_EPSILON`(0.6) 이상 앞서 있다 — 밀고 있다.
2. 실제가 최근 창에서 그만큼도 움직이지 않았다 — 서 있다.
3. 부하가 `SOARM_VL_STALL_LOAD`(80) 이상이다 — 힘을 쓰고 있다.

이 셋이 `SOARM_VL_STALL_LOAD_MS`(500) 넘게 이어지면 막힌 것으로 본다. 1번이 있어야 하는
이유는 **가만히 자세를 버티는 것**과 구별하기 위해서다. 버틸 때는 목표가 실제와 같은
자리에 있다. 2번이 있어야 하는 이유는 자유 이동(부하 24~100)과 막힘(48~128)의 부하
구간이 겹치기 때문이다 — 겹치지 않는 것은 **움직이는가**이다.

같은 시험을 다시 돌린 결과:

```
걸린 시각 0.62s · 상태 RETREATING
"집게를 밀고 있는데 500ms 넘게 제자리이고 부하가 84입니다 — 무언가에 막혀 있습니다"
끝난 뒤: 상태 HOLD · 집게 현재 4.99% (물러남 뒤) 부하 20
```

자유 이동에서 헛걸리지 않는지도 확인했다. 집게를 5% → 60%로 여는 동안 부하가 104까지
올라갔지만(문턱 80보다 높다) 걸리지 않았다 — 움직이고 있었기 때문이다.

### 6.6e 물러남에는 끝이 있어야 한다

2026-09-01, 사람이 없는 상태에서 `shoulder_lift`가 `RETREATING`으로 **53초 넘게** 서
있었다. 부하는 -100으로 계속 밀고 있었고 위치는 44.22°에서 조금도 움직이지 않았다.

```
0: state=RETREATING 명령나이=46169 shoulder_lift 현재  44.22 목표  40.29 부하  -100
7: state=RETREATING 명령나이=53507 shoulder_lift 현재  44.22 목표  40.29 부하  -100
```

물러남은 목표에 닿을 때까지 이어지는데, 닿지 못하는 자리가 있다. 걸린 방향의 반대편에도
무언가가 있으면 — 책상 위에서 위로 밀다 걸리면 아래는 책상이다 — 팔은 물러날 곳이 없다.
게다가 물러나는 동안에는 관측 정지(막힘·과열)를 **보지 않으므로**, 6.6d에서 넣은 칸도
이것을 끊지 못했다.

`SOARM_VL_RETREAT_MS`(1500)를 두었다. 그 안에 빠져나오지 못하면 지금 자리에 그대로
세운다. 세우는 것은 언제나 할 수 있다. 이유 문장에 "물러나려 했지만 …ms 안에 빠져나오지
못해 그 자리에서 세웁니다"가 붙는다.

**밖에서 박았을 때의 순서** (실물로 확인했다):

1. `정지`. 토큰도 리스도 필요 없다. 미는 것이 멈춘다.
2. `조작 권한 받기` 한 번. 멈춘 이유를 게이트에서 읽고 확인하면 그 멈춤이 함께 풀린다.
3. **부딪힌 반대 방향으로** 뺀다. 첫 명령은 팔의 지금 자세에서 시작하므로 튀지 않는다.

집게로 끝까지 재현했다: 끝단에 박아 4.86초에 걸림 → 물러남 뒤 HOLD → 다시 권한을 받아
1.39%에서 44.67%까지 걸림 없이 빠져나왔다.

### 6.6f `step_deg`는 속도가 아니라 **토크**도 정한다

`SOARM_VL_STEP_DEG`는 한 틱에 목표가 **실제 위치로부터** 얼마나 앞설 수 있는지의 상한이다.
이 값 하나가 두 가지를 동시에 정한다.

* **최대 속도** = `step × hz`. 2.0이면 60°/s, 5.0이면 150°/s.
* **서보가 보는 위치 오차** = 최대 `step`. STS3215는 위치 P 제어이고 LeRobot이 연결할 때
  `P_Coefficient=16`(I=0, D=32)을 써 넣으므로, 서보가 내는 힘은 이 오차에 비례한다.
  **목표를 가까이 두면 팔이 약해진다.**

2026-09-01 실측이 이것을 그대로 보여 줬다. `step_deg=2.0`에서 어깨 들기를 위로 미는 동안
부하는 언제나 **정확히 100**(1000 중, 약 10%)에서 멈췄고 팔은 들리지 않았다. 막힌 것이
아니라 2°짜리 오차가 낼 수 있는 힘이 거기까지였다. 같은 명령이 `step_deg=5.0`에서는
부하 **236**을 냈고 팔이 실제로 올라오기 시작했다.

참고값(STS3215, 7.4V 판): 스톨 토크 19.5kg·cm@6V, 정격 6.5kg·cm, 무부하 속도
0.238s/60° = **252°/s**, 스톨 전류 2A, 12비트 자기 엔코더(4096) = **0.088°/눈금**.
LeRobot 계열 텔레옵에서 `max_relative_target`을 **5도**로 두는 것이 흔하고, 익숙해지면
풀라고 안내한다. 우리 기본값 2.0은 그보다 2.5배 조인 값이었다.

그래서 `config/soarm.env`에 `SOARM_VL_STEP_DEG=5.0`을 두었다. 150°/s는 서보의 무부하
속도(252°/s)보다 낮고, 오차 5°는 P=16에서 약 24% 듀티다.

### 6.6g 밖에서 팔을 세워 올린 기록 (2026-09-01)

팔이 책상에 누운 채 어떤 관절도 말을 듣지 않는 상태에서, 사람이 옆에 없이 되살린 순서다.

1. **중력과 무관한 축부터 건드려 본다.** `shoulder_pan`을 2° 밀었더니 3초 동안 0.00°에
   부하 −24 — *막힌 것이 아니라 밀지 않고 있었다*. 목표를 2.5° 앞에 두자 움직였다.
   작은 목표는 정지 마찰조차 이기지 못한다.
2. **어깨를 직접 드는 것은 안 된다.** 부하 100(step 2.0) / 236(step 5.0)에서 서고, 한 번에
   0.2~1.4°씩만 기어올랐다.
3. **지렛대를 줄인다.** `elbow_flex`를 접었더니 10°를 **막힘 없이** 다 갔고, 그 직후
   어깨가 저절로 1.3° 올라오면서 유지 부하가 236 → 60으로 떨어졌다.
4. **팔꿈치 접기 → 어깨 들기를 번갈아** 반복. 어깨 43.9° → **76.1°**, 팔꿈치 −26° →
   **−87.8°**. 팔이 책상에서 떨어져 접힌 자세로 섰고, 유지 부하는 어깨 72 · 팔꿈치 20.
5. 온도는 전 구간 33~36°C였다(정지 문턱 65°C). 스무 번 넘게 밀어붙였는데도 그렇다 —
   틱당 상한이 있는 한 서보는 전력으로 밀 수 없다.

**교훈: 밖에서 팔이 눌러앉으면 어깨를 들려고 하지 말고 팔꿈치부터 접는다.**

### 6.6h 토크를 풀어도 팔은 떨어지지 않는다

2026-09-02 실측. 접힌 자세(어깨 -67.8°, 팔꿈치 31.9°)에서 토크를 **걸었다가 풀고** 6초를
지켜봤다:

```
관절별 이동: 전부 +0.00°   가장 많이 움직인 값 0.00°
```

팔로워는 1/345 감속비라 사실상 스스로 잠긴다. 이 프로젝트가 처음부터 적어 온 "토크를
끄면 팔이 떨어진다"는 **이 하드웨어에서는 대체로 사실이 아니다.**

다만 조건이 있다. 같은 날, 어깨를 중력이 두는 자리보다 **위로 버티게 해 둔** 상태
(26.2°, 유지 부하 100)에서 콘솔이 재시작되며 토크가 빠지자 어깨가 **43.3°로 17° 주저앉았다.**
즉 규칙은 이렇다 — *팔이 스스로 쉴 수 있는 자세면 그대로 있고, 힘으로 버티고 있던 자세면
그 차이만큼 내려앉는다.*

화면 문구를 그에 맞게 고쳤다. 틀린 경고는 두 번째부터 아무도 읽지 않는다.

`SAFETY.md`의 "사고 시 자동 torque-off 금지"는 그대로 둔다. 근거가 "팔이 떨어진다" 하나만
있는 것은 아니다 — 토크가 없는 팔은 부딪힌 물체에 밀려 어디로든 갈 수 있고, 다시 걸 때
자세를 새로 맞춰야 한다. 다만 그 조항의 이유에서 "떨어진다"는 빼는 것이 정직하다.

### 6.6i 물리 리더 텔레옵의 속도 상한

`SOARM_MAX_RELATIVE_TARGET`은 팔로워가 **한 틱에** 리더 쪽으로 따라갈 수 있는 각도다.
LeRobot은 리더의 절대 관절값을 그대로 팔로워의 목표로 쓰므로(차이값이 아니다), 이 값이
곧 따라가는 속도의 상한이 된다.

2였을 때 30Hz에서 60°/s다. 사람이 리더를 그보다 빠르게 움직이는 것은 어렵지 않고, 그러면
팔로워가 뒤처졌다가 손을 멈춘 뒤에도 계속 따라온다. LeRobot 자체 기본값은 **상한 없음
(`max_relative_target=None`)**이고, 계열 문서에서 흔히 쓰는 값은 5도다. 8로 올렸다 —
가상 리더의 `SOARM_VL_STEP_DEG`와 같은 자리에 맞춘 값이다.

### 6.6c 판독값은 튄다

45°C로 안정된 집게가 **한 번** 89°C로 읽혔고 그때 팔이 멈췄다. 57·63·64°C 같은 단발 오독도
여러 번 보았다. 온도 트립에 연속 초과(`SOARM_VL_TEMP_TRIP_MS`, 500ms)를 요구하도록 고쳤다.
부하·전류·위치도 같은 성질을 가지므로, **한 번의 숫자로 판단하는 코드를 새로 만들지 않는다.**

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

### 6.8 관찰은 팔을 뻣뻣하게 만들지 않는다

`start`는 읽기다. 끝나고 나서 상태가 `SAFE`(토크 꺼짐)여야 한다. `READY`로 나온다면
누군가 이미 토크를 걸어 둔 팔을 이어받은 것이고, 그때만 그렇다.

한동안 그렇지 않았다. LeRobot의 `SOFollower.connect()`는 안에서 `configure()`를 부르고,
그 안의 `with bus.torque_disabled():`가 **빠져나오면서 토크를 켠다.** 그래서 관찰만
시작해도 팔이 뻣뻣해졌고, 확인을 요구하는 `arm` 게이트를 지나지 않고도 팔이 명령을 받을 수
있는 상태가 되었다. 지금은 `connect()`를 통째로 부르지 않고 그 안의 일을 직접 하면서,
붙기 전의 토크 상태를 기억해 원래대로 돌려놓는다. 확인하는 방법:

```bash
curl -sX POST 127.0.0.1:8088/api/vleader/start >/dev/null
curl -s 127.0.0.1:8088/api/vleader | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["state"], d["torque_enabled"])'
# SAFE False   ← 이래야 한다
```

### 6.9 내리기를 거절당한 뒤

토크가 걸린 채 `stop`을 부르면 409로 거절한다. 정상이다 — 아무도 보지 않는 곳에 토크가
걸린 팔을 남기지 않는다. 거절당한 뒤에도 `running`은 `true`여야 한다. `false`인데 다음
`start`가 `Device is owned by virtual-leader (pid …)`로 막힌다면, 그 pid는 십중팔구
콘솔 자기 자신이다. 서비스를 재시작하는 것 말고는 방법이 없다:

```bash
systemctl --user restart soarm-console
```

이 상태를 만들던 원인(참조를 먼저 버리고 그다음에 내리려 했다)은 고쳤다. 다시 나타난다면
`VirtualLeaderService.stop()`의 순서를 먼저 본다.

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
