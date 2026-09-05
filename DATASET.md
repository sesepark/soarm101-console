# 수집한 데이터가 무엇을 담는가

이 콘솔이 찍은 LeRobot 데이터셋의 열 구성, 값을 고치지 않는 이유, 회차마다 함께 남기는
품질·출처 기록, 그리고 수집 루프가 회 사이에서 하는 일을 적어 둔 문서임. README에서 갈라져
나온 것이라 결론만 필요하면 그쪽을 먼저 보면 됨.

---

## 데이터셋이 담는 열

LeRobot v3 features. 아래 열 개 열이 `observation.state`·`action`·영상 옆에 **더해짐**.
값은 전부 `float32`이고 매 프레임(30Hz) 채워짐. `<motor>`는
`shoulder_pan`·`shoulder_lift`·`elbow_flex`·`wrist_flex`·`wrist_roll`·`gripper` 순서이며,
이는 `observation.state`의 이름 순서와 같음.

| 열 | shape | names | 출처와 단위 |
| --- | --- | --- | --- |
| `observation.state` | [6] | `<motor>.pos` | *(기존)* 관절 위치. **바뀌지 않음** |
| `observation.load` | [6] | `<motor>.load` | `Present_Load`(60). 부호 포함 −1000..1000 |
| `observation.velocity` | [6] | `<motor>.vel` | `Present_Velocity`(58). 부호 포함, 서보 눈금/s |
| `observation.temperature` | [6] | `<motor>.temp` | `Present_Temperature`(63). °C |
| `observation.voltage` | [6] | `<motor>.volt` | `Present_Voltage`(62) ÷ 10. V |
| `observation.servo_status` | [6] | `<motor>.status` | `Status`(65) 바이트 그대로. 과부하·과열 비트. **`0x20`(=32)은 과부하 플래그이고, 집게에서는 파지 구간에 정상적으로 나타남** — `test4_20260905_1459`에서 44프레임·19프레임 연속으로 섰고 그 구간은 집게가 물체를 물고 있던 때임. 고장이 아님 |
| `observation.servo_moving` | [6] | `<motor>.moving` | `Moving`(66). 0/1 |
| `observation.current` | [6] | `<motor>.current` | `Present_Current`(69). **이 펌웨어(3.9)에서는 0이나 1로만 읽힘**(2026-09-05 확인). 그래도 남김 — 값이 없다는 사실 자체가 기록임 |
| `observation.wall_time` | [1] | `since_start` | 이 프레임의 `Present_Position`을 읽은 시각. **수집 프로세스 시작 epoch으로부터의 초**임(float32에 epoch을 그대로 넣으면 30Hz 프레임이 전부 같은 시각이 됨). 기준 epoch은 `soarm_provenance.json`의 `started_at` |
| `observation.camera_fresh` | [2] | `scene`, `wrist` | 이 틱이 카메라에서 **새** 프레임을 받았는가. 0/1 |
| `observation.sensor_read_ok` | [1] | `read_ok` | 이 프레임의 서보 블록 읽기가 성공했는가. `1.0`이면 이 행의 값들은 이 프레임에서 새로 읽은 것이고, `0.0`이면 읽기가 실패해 **직전 값을 그대로 다시 쓴** 행임. 값은 고치지 않고 이 사실만 적음 |

**부호는 2의 보수가 아니라 부호-크기(sign-magnitude)임.** 부하는 비트 10이, 속도는 비트 15가
부호이고, 푸는 것은 `MotorsBus._decode_sign`(가상 리더가 부하를 읽을 때 지나는 바로 그 코드)에
맡김. 두 곳이 다르게 풀면 같은 힘이 다른 숫자가 되기 때문임.

서보 읽기는 **틱당 블록 하나**임(주소 56~70의 15바이트를 한 번에 읽어 우리가 쪼갠다).
레지스터마다 따로 읽으면 버스 왕복이 틱당 일곱 번이 되어 30Hz가 흔들리고, 늘어난 시간축은
`timestamp`가 합성값이라 파일에 남지도 않음.

읽기가 실패한 틱은 마지막으로 성공한 값을 한 번 더 씀 — `validate_frame`은 열이 하나만 비어도
회차를 통째로 막으므로 값을 뺄 수는 없고, 버스 패킷 하나가 깨졌다고 30초짜리 시연을 잃는 편이
훨씬 나쁘기 때문임. **그 행이 되풀이라는 사실은 `observation.sensor_read_ok`에 남음.** 몇 번
그랬는지는 `/api/status`의 `recording.runtime`에 `extras_read_failures`로, 블록 읽기가 30Hz
예산에서 가져간 몫은 `extras_read_ms`로 실림.

열이 없는 옛 데이터셋에 이어 찍기는 400으로 거절함(`Dataset was recorded without the sensor
columns; start a new dataset`) — 열 구성이 바뀌면 `extras_schema`가 올라가고(지금 2), 스키마가
다른 데이터셋도 같은 이유로 거절됨. `GET /api/status`의 `capabilities`에 `sensor_extras`가 실림.

## 값은 원본 그대로 저장하고, 걸러 내는 것은 분석 단계에서 함

**어떤 열에도 clamp·대체·보간을 넣지 않음.** 물리적으로 불가능해 보이는 값도 서보가 내준
그대로 파케이에 들어감.

그렇게 하는 이유는 이렇다. `test4_20260905_1459`(1,104프레임, 6,624 판독)에는 말이 되지 않는
값이 6개 있었음 — 예를 들어 frame 297의 `elbow_flex.temp`가 150°C인데 앞뒤 프레임은 36°C이고,
같은 프레임의 다른 필드는 멀쩡함. 패킷이 통째로 밀린 것이 아니라 **한 바이트가 어긋난** 모양임.
그런데 팔을 세워 둔 채 900프레임(5,400 판독)을 읽으면 그런 값이 0건이고, 움직이며 찍은 6건은
부하·속도가 평균보다 높은 프레임에 몰림(움직이는 관절 3.50개 대 2.29개). 즉 온도계의 잡음이
아니라 **모터가 전류를 쓰는 동안 serial 판독이 어긋나는** 것임.

여기서 온도와 전압만 고치면 어떻게 되는지가 요점임. 같은 손상이 부하나 속도 바이트에 나면 그
값은 여전히 그럴듯한 숫자여서 어떤 문턱으로도 잡히지 않음. 고친 두 열만 깨끗해 보이고, 정작
연구가 쓰려는 열(부하로 간접 촉각을 추정하는 것이 이 데이터의 목적임)의 손상은 그대로 남은 채
**감춰짐.** 게다가 고친 값은 되돌릴 수 없음 — 시연은 다시 찍을 수 없기 때문임.

그래서 값을 고치는 대신 **값에 대한 사실**을 남김. `observation.sensor_read_ok`가 그 행이 새
판독인지 되풀이인지 말하고, 세어 본 결과는 `soarm_quality.json`에 감: `sensor_read_failures`,
`sensor_implausible`(필드별 개수), 그리고 **무슨 기준으로 세었는지**
(`sensor_implausible_thresholds`, 지금은 온도 `0 < t ≤ 100 °C`·전압 `5.0 ≤ v ≤ 15.0 V`).
기준을 함께 적는 이유는 원본이 그대로 남아 있으므로 나중에 다른 기준으로 다시 셀 수 있기
때문임 — 그때 이 파일이 "예전에는 무엇을 세었나"에 답함.

## 이 데이터가 어떻게 찍혔는지 — `soarm_quality.json`

회차가 끝나면 데이터셋 폴더에 함께 놓임. `camera_stale_pct`는 **세션 전체**의 값임: 그 회의 총
프레임 가운데 몇 장이 카메라에서 새 프레임을 받지 못했는가. 함께 실리는
`camera_stale_frames`(정수)와 `total_frames`가 그 분모와 분자임.

키 이름은 그대로 두고 뜻만 바꿨음(앱이 이 키를 읽는다). 예전 값은 `_LoopRateMonitor`의 3초
창에서 와서 회차가 끝나는 **순간**만 말했고, 그래서 `test4_20260905_1459`는 0.0으로 적혔지만
같은 데이터셋의 `observation.camera_fresh`를 세면 scene 2.54%·wrist 2.90%(어느 쪽이든 5.16%)
였음. 지금은 파케이에 들어가는 바로 그 행에서 세므로, 이 값은 나중에 파케이를 다시 세어 얻는
값과 반드시 같음. 이어 찍으면 세는 값들은 더해지고 비율은 합쳐진 수에서 다시 계산됨 — 비율의
합은 비율이 아니기 때문임.

블록 읽기에 든 시간은 `sensor_block_read_ms_p50`·`_p99`로 남음(백분위수는 보간하지 않고 실제로
일어난 값 가운데 고른다. 정지 상태 실측 2.17 / 2.38ms).

### 관련 API

- `GET /api/datasets`, `GET /api/datasets/{name}` — `extras`에 이 데이터셋이 담은 열의 마지막
  마디 목록(`["load","velocity",…,"camera_fresh","sensor_read_ok"]`). 열이 없는 옛 데이터셋은 `[]`임.
- `GET /api/datasets/{name}/episodes/{i}/trajectory` — 기존 `fps`·`frames`·`joints`·`state`·
  `action`에 더해, 열이 있으면 `load`·`velocity`(frames × 6), `camera_fresh`(frames × 2),
  `camera_keys`(`["scene","wrist"]`), `wall_time`(frames × 1), `sensor_read_ok`(frames × 1).
  없으면 키 자체가 빠짐.

## 회 단위 provenance

수집 세션마다 `data/<dataset>/soarm_provenance.json`의 배열에 항목이 **하나 덧붙음**(이어 찍으면
늘어난다). 담기는 것: `started_at`(위 `wall_time`의 기준 epoch), `server_commit`, `lerobot` 버전,
팔로워·리더 calibration의 `sha256`, 되읽은 `camera_controls`, 시작 시 `doctor` 진단,
`episode_seconds`, `reset_seconds`, `fps`, `extras_schema`(지금 2 — `observation.sensor_read_ok`가
생기며 올랐다).

calibration 해시를 남기는 이유는 그것이 **데이터를 읽는 자 자체**이기 때문임 — 다시 잰
calibration으로 찍은 회차는 앞 회차와 다른 좌표계에 있는데, 그 사실은 데이터셋 안에서 전혀
보이지 않음.

## 회 사이에 누른 키

`record_loop`가 돌지 않는 동안 도착한 `right`·`left`는 **적용하지 않고 버림.** 그런 키를
`events`에 적으면 LeRobot의 다음 루프가 첫 반복 맨 앞에서 그것을 읽어 **다음 회차를 0프레임으로**
끝냄. 실제로 `test3_20260905_1413`이 그렇게 죽었음(2026-09-05 14:14 UTC): 회 0이 끝나고 정리
15초가 저절로 끝난 직후, 영상을 굽는 11초 동안 누른 ⏎가 회 1을 0프레임으로 만들었고, 그 빈 회를
저장하다 `validate_episode_buffer`가 `ValueError`를 내 남은 여덟 회가 통째로 사라졌음. 사람은 회
하나를 넘기려던 것이지 다음 회를 지우려던 것이 아니었음. 버린 키는 `/api/status`의
`recording.runtime.last_control_ignored`에 `{key, reason: "no loop running", at}`로 실려, 화면이
"버려졌다"고 말할 수 있음.

`esc`와 `abort`는 이때도 받음 — 저장 중에 "끝내기"를 눌렀으면 그 뜻은 분명하기 때문임. 다만
`stop_recording`만 세우고 `exit_early`는 세우지 않음. 둘 다 세우면 `record()`는 다음 회를
0프레임으로 한 번 더 열고 끝남. `stop_recording` 하나면 저장이 끝난 뒤 `while` 조건에서 조용히
나감. 루프가 시작하는 자리에서 `exit_early`를 한 번 비우는 것이 마지막 방어임 — 리스너를 거치지
않고 이 표를 만지는 길이 따로 있기 때문임(가상 리더가 조작이 끊기면 그렇게 한다).

빈 회차는 어떤 이유로 생기든 **저장하지 않고 건너뜀.** `writer.episode_buffer["size"]`가 0이면
`clear_episode_buffer()`만 부르고 `empty_episodes_skipped`를 하나 올림. `episodes_saved`는 그대로임
— 데이터셋에 들어간 것이 없기 때문임. 프레임 수를 셀 수 없으면 저장함: 모르는 것을 0으로 읽으면
멀쩡한 회를 버리기 때문임.

## 영상은 찍으면서 굽는다

`streaming_encoding=True`, `encoder_threads=2`로 찍음. 끄면 LeRobot은 프레임마다 PNG를 쓰고
`save_episode()`에서 그것을 통째로 인코딩하는데, 그동안 텔레옵 루프가 서 있고 그 정지가 위
"회 사이에 누른 키" 문제의 원인이었음. 켜면 프레임이 들어오는 즉시 별 스레드가 굽는다.

640×480 두 대·30Hz·900프레임(30초)을 합성 프레임으로 흘려 넣고 잰 값임(2026-09-05, 12스레드
기계, load 1.9):

| | 넣는 속도 | `save_episode()` | `add_frame` p50 / p99 |
|---|---|---|---|
| `streaming_encoding=False` | 29.90Hz | **7.45초** | 0.46 / 1.03ms |
| `streaming_encoding=True`, `encoder_threads=2` | 29.80Hz | **0.41초** | 0.76 / 1.53ms |
| `streaming_encoding=True`, `encoder_threads=1` | 29.85Hz | 0.52초 | 0.64 / 1.37ms |

저장이 7.45초에서 0.41초로 줄고 넣는 속도는 사실상 그대로임(0.1Hz 차이는 30Hz 페이싱 루프의
잡음 안이다). `add_frame`이 틱당 0.3ms를 더 쓰는데 33ms 예산에서 1%임.

합성 장면이라 실제 촬영과 정확히 같지는 않음 — 같은 회차를 PNG로 쌓아 구울 때 실물 로그에 남은
값은 11초였음. **실물에서 확인할 것**: 한 회 찍고 `loop_hz`가 29.9 그대로인지,
`camera_stale_pct`가 전과 같은지. 떨어지면 `encoder_threads=1`이 첫 번째 손잡이이고, 그래도
떨어지면 streaming을 끄고 `save_episode`의 `parallel_encoding`(카메라 두 대를 병렬로 굽는 것,
이미 기본 `True`)만 남김 — 그때는 저장 대기가 돌아오므로 화면이 `saving_seconds_estimate`(직전
저장에 걸린 초)로 "약 N초"를 그림.

## 카메라 프레임률을 어디서 재는가

프리뷰는 V4L2의 기본 버퍼 큐를 유지해 두 카메라를 함께 볼 때도 30fps에 가까운 처리량을 보존함.
고른 프레임률보다 빨리 오는 프레임은 콘솔이 솎아 내는데, 이때 다음에 내보낼 시각을 **지난 예정
시각에 주기를 더해** 정함. 방금 내보낸 시각으로 다시 맞추면, 요청한 값이 장치가 실제로 내주는
속도와 가까울 때 프레임의 3분의 1이 사라짐 — 지터로 한 주기보다 조금 일찍 온 프레임이 문턱에
걸려 버려지고, 그러면 다음 프레임을 한 주기 더 기다리기 때문임.

640×480 두 대를 함께 열고 잰 값임(2026-09-05): 장치는 인코딩 없이 30.1fps를 주는데, 예정 시각을
다시 맞출 때 프리뷰로 나간 것은 18.7·19.0fps였고 주기를 더하면 29.2·29.4fps임(남은 차이는 JPEG
인코딩 몫). 목표가 장치보다 한참 낮은 `절약`(2fps)이나 `보통`(8fps)에서는 원래 나지 않던 일이고,
`전체`(30fps)에서만 났음.

**fps는 어디서 재는지를 함께 적어야 함** — 여기 적은 30.1fps는 장치가 내주는 원본을 디코드 없이
센 값이고, 코드 주석과 `tests/test_cameras.py`가 쓰는 26.8fps는 같은 흐름을 디코드까지 마친 뒤
센 값임. 어긋난 측정이 아니라 파이프라인의 서로 다른 지점이고, 그 사이의 차이가 CPU 디코드 몫임.

수집 중에는 루프가 카메라에서 **새 프레임을 실제로 몇 장 받았는지**가 `/api/status`의
`recording.runtime`에 카메라별 `camera_fresh_hz`와 `camera_stale_pct`로 실림(`loop_hz`와 같은 3초
창, 1초 주기). LeRobot의 `read_latest()`는 블로킹이 아니라서 새 프레임이 아직 없으면 버퍼에 있던
것을 그대로 다시 돌려주므로, 돌려받은 ndarray의 버퍼 주소가 직전과 같으면 그 틱은 새 프레임을
받지 못한 것으로 셈.

**픽셀을 비교하거나 찍힌 영상에서 세면 안 됨** — 움직이지 않는 장면은 서로 다른 두 번의 촬영인데도
같은 값을 내고, AV1 인코더는 움직임 없는 두 프레임을 하나로 합치므로 카메라가 같은 프레임을 두 번
준 것과 구별되지 않음(실제로 그 둘을 혼동해 없는 문제를 쫓은 적이 있음, 2026-09-05). 데이터셋의
`timestamp`는 언제나 `frame_index / fps`로 합성되어 파일에는 흔적이 남지 않으니, 원천에서 세는
수밖에 없음.
