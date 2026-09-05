# 학습 환경 및 DGX Spark 연동

이 문서는 SO-ARM101로 녹화한 데이터셋을 DGX Spark에서 학습하고 결과를 다시 가져오는 경로를
기록한다. 녹화는 이 콘솔이 도는 HUB에서, 학습은 Spark에서 한다.

마지막 실측: 2026-09-05

## 역할 분담

```text
[SO-ARM101 leader/follower + 카메라 2대]
        │ USB
        ▼
HUB (이 콘솔)                      Spark (학습)
Ubuntu 22.04 · x86_64              DGX OS 7.5 · aarch64
i7-8700 · GTX 1660 6GB             GB10 · 121GB 통합메모리
녹화 · 텔레옵 · 추론                학습
        └──────── tailnet ────────┘
```

제어와 녹화는 GPU를 거의 쓰지 않으므로 팔이 물린 HUB에서 그대로 한다. GPU가 오래 필요한
학습만 Spark로 보낸다. 두 기계는 같은 tailnet에 있어 집 안에서도 밖에서도 같은 이름으로
닿는다.

## 왜 tailnet 이름을 쓰는가

`spark_host`는 비어 있고 `config/soarm.env`의 `SOARM_SPARK_HOST`에서 온다. `<이름>.local`(mDNS)은 같은 LAN에서만 풀리고
밖에서는 풀리지 않는다. tailnet MagicDNS 이름은 어디서나 같은 기계를 가리키므로, 설정을
집과 밖에서 바꿔 끼울 필요가 없다.

`.local` 이름은 IPv6 링크로컬로 풀릴 때가 있고, 그때 SSH가 조용히 타임아웃한다. 자동화
경로에서는 tailnet 이름을 쓴다.

## Spark 소프트웨어 스택

| 항목 | 값 |
| --- | --- |
| OS | DGX OS 7.5.0 (Ubuntu 24.04.4 LTS) |
| 커널 | 6.17.0-1032-nvidia / aarch64 |
| GPU | NVIDIA GB10, compute capability **sm_121** |
| 드라이버 | 580.173.02 / CUDA 13.0 |
| venv | `~/venvs/lerobot` (Python 3.12) |
| torch | 2.11.0+cu130 |
| lerobot | 0.6.1 `[dataset,feetech,training]` |

### 두 기계의 torch 빌드가 다른 이유

같은 `cu130`이라도 아키텍처별로 컴파일 타깃이 다르다.

- Spark(aarch64): `sm_80, sm_90, sm_100, sm_110, sm_120`
- HUB(x86_64): `sm_75, sm_80, sm_86, sm_90, sm_100, sm_120`

aarch64 빌드에는 Turing(`sm_75`)이 없다. 서버용 빌드라 소비자 GPU를 담지 않는다. 그래서
HUB의 GTX 1660은 x86_64 빌드로만 돌아간다. 반대로 GB10은 `sm_121`인데 어느 빌드에도
`sm_121`이 없다 — 같은 Blackwell 계열인 `sm_120` 바이너리가 하위 호환으로 실행된다.
일반 연산은 문제없지만, 커스텀 CUDA 커널을 직접 컴파일하는 패키지는 재빌드가 필요할 수 있다.

데이터셋과 체크포인트는 parquet·mp4·safetensors라 torch 빌드에 묶이지 않는다. 두 기계
사이를 오가는 것은 전부 이 세 형식뿐이므로 빌드가 달라도 문제가 없다.

### FFmpeg

DGX OS 기본 이미지에는 FFmpeg가 없다. LeRobot은 `torchcodec`으로 데이터셋의 mp4를 읽으므로
`libavcodec`/`libavformat`/`libavutil`이 없으면 학습이 시작되지 않는다. `apt install ffmpeg`
한 번으로 해결된다(6.1.1 확인, torchcodec은 4–8을 지원).

## 배치 크기 실측

ACT(52M 파라미터), `lerobot/svla_so101_pickplace`, 각 100 step.

| batch | step/s | **samples/s** | 메모리 | data_s | peak W | 100 step 소요 |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 3.97 | **32** | 3.73 GB | 0.001 | 45.6 | 37 s |
| 32 | 1.09 | **35** | 13.10 GB | 0.002 | 48.1 | 106 s |
| 64 | 0.55 | **35** | 25.57 GB | 0.004 | 56.6 | 201 s |
| 128 | 0.27 | **34** | 49.05 GB | 0.053 | 64.6 | 394 s |
| 256 | — | — | — | — | 50.9 | **900 s 안에 실패** |

### 읽는 법

**처리량이 batch 8에서 이미 포화한다.** 배치를 16배로 키워도 초당 샘플 수는 32에서 35로
움직일 뿐이다. 메모리만 선형으로 늘고 속도는 그대로다. ACT가 52M로 작아 배치를 키워도
GB10을 더 채우지 못한다.

따라서 **배치는 학습 시간을 줄이는 수단이 아니다.** 수렴 특성을 위해 고르는 값이다. 큰
배치가 필요하다면 그 이유는 gradient noise이지 속도가 아니다.

`data_s`가 batch 128에서 0.053으로 뛴다. 이 지점부터는 데이터 로딩도 무시할 수 없어지므로
`--num_workers`를 함께 봐야 한다.

### 통합메모리의 함정

batch 256은 실패했는데, 실패하는 방식이 일반 GPU와 다르다.

GB10은 GPU 전용 메모리가 따로 없고 시스템 RAM 121GB를 공유한다. 배치가 커져 학습이 약
100GB를 잡으면 OS가 쓸 메모리가 남지 않아 기계 전체가 스래싱에 빠진다. 실측에서 **ICMP는
정상 응답(8/8, 3.9ms)했지만 SSH는 60초 타임아웃으로도 배너 교환을 하지 못했다** — 커널은
살아 있는데 유저스페이스가 굶은 상태다.

일반 GPU라면 `CUDA out of memory` 예외 하나로 끝날 상황이 여기서는 원격 접속 자체를 잃는
것으로 나타난다. 자동화에서 배치를 탐색할 때는 반드시 상한과 타임아웃을 걸어야 한다.
실측 스크립트가 `timeout 900`을 걸어 두었기에 15분 뒤 스스로 회복했다.

`nvidia-smi`도 이 구조 때문에 `memory.used`/`memory.total`을 `[N/A]`로 답한다. GPU 메모리를
읽는 코드는 이 값이 없을 수 있다고 보아야 한다.

### 권장값

**batch 32–64.** 처리량은 batch 8과 같고, 메모리 여유(13–26GB)가 충분해 다른 작업과 함께
돌려도 안전하다. 128은 49GB를 잡아 이득 없이 여유만 줄이고, 256은 기계를 잃는다.

## 콘솔 API

`src/soarm_console/spark.py`가 SSH와 rsync를 감싼다. 셸을 거치지 않고 인자 리스트로만
실행하며, 데이터셋 이름은 `datasets.py`의 규칙을 그대로 재사용해 검사한다.

| 엔드포인트 | 하는 일 |
| --- | --- |
| `GET /api/spark` | 도달 여부, GPU, 남은 디스크 |
| `GET /api/spark/datasets` | Spark에 올라가 있는 데이터셋 목록 |
| `POST /api/spark/datasets/{name}` | 녹화한 데이터셋 하나를 Spark로 전송 |
| `GET /api/spark/runs` | 학습 실행별 체크포인트 **와 진행 상황** |
| `POST /api/spark/train` | `{dataset, policy}` — 학습을 tmux 안에서 띄운다 |
| `POST /api/spark/runs/{run}/stop` | 도는 학습에 Ctrl-C를 보낸다 |
| `POST /api/spark/runs/{run}/{step}` | 체크포인트의 `pretrained_model`을 회수 |
| `GET /api/spark/train-command` | 사람이 터미널에 붙여 넣을 학습 명령(그대로 남겨 둔다) |

### 콘솔이 학습을 띄우되 품지는 않는다 (2026-09-05)

`POST /api/spark/train`이 학습을 시작한다. 다만 콘솔이 학습 **프로세스**를 갖지는 않는다 —
원격 tmux 세션 `train-<run>` 안에서 돌므로, 콘솔이 `systemctl --user restart`로 다시
시작돼도 학습은 그대로 돈다. 콘솔이 하는 일은 띄우는 것과 읽는 것뿐이다.

예전에는 명령 문자열을 만들어 주고 사람이 붙여 넣었다. 그 방식에는 조용한 함정이 있었다:
`output_dir`이 `outputs/<dataset>`으로 고정이었고, LeRobot은 그 폴더가 이미 있으면
`FileExistsError`로 거절한다(`configs/train.py`). 즉 **같은 데이터셋의 두 번째 학습은 반드시
실패**했고, 그 실패는 tmux 안에서 일어나므로 화면에는 "시작했다"만 남았다. 지금은 실행 이름에
시각이 들어간다 — `<dataset>__<policy>__<YYYYMMDD_HHMM>`.

정책은 둘만 받는다. 이 팔에서 실제로 돌려 본 것이 둘이기 때문이다.

| `policy` | 플래그 | steps | batch_size | save_freq |
| --- | --- | --- | --- | --- |
| `act` | `--policy.type=act` | 100,000 | 64 | 20,000 |
| `smolvla` | `--policy.path=lerobot/smolvla_base` | 20,000 | 32 | 5,000 |

SmolVLA는 이미 배운 것을 옮겨 오므로 스텝이 훨씬 적다. 대신 **첫 실행은 HF에서 기반 모델을
내려받느라 오래 걸린다** — 진행이 한동안 멈춰 보이는 것은 그 때문이다.

시작 전에 두 가지를 원격에서 한 번의 왕복으로 확인한다. `<root>/<dataset>/meta/info.json`이
없으면 404 `Dataset is not on the training machine`(먼저 전송해야 한다), 살아 있는 `train-*`
세션이 하나라도 있으면 409 `Training is already running: <run>`이다. 후자는 GPU가 하나이기
때문이고, 무엇이 돌고 있는지를 문구에 적어 사람이 멈출지 기다릴지 고를 수 있게 한다.

### 진행은 로그에서 읽는다

시작 직후 `outputs/<run>/soarm_train.json`에 `{dataset, policy, steps, batch_size, started_at}`
을 남기고, 명령 자체는 `2>&1 | tee outputs/<run>/train.log`로 끝난다. `GET /api/spark/runs`의
각 실행에 실리는 `training`은 그 둘과 `tmux has-session`을 합친 것이다:

```json
{"running": true, "step": 20000, "steps": 100000, "loss": 0.187,
 "policy": "act", "started_at": 1757060000.0, "updated_at": 1757063600.0,
 "log_tail": ["…"], "error": null}
```

읽는 쪽에서 조심할 것이 하나 있다. LeRobot은 step을 `format_big_number`로 줄여 찍으므로
로그에는 `step:20K`로 나온다 — 숫자만 집으면 20이 되고, 화면은 10만 스텝 학습이 0.02%
진행됐다고 말한다. 접미사를 되돌려 읽는다. `running`이 아닌데 `step < steps`이면 로그에서
`Traceback`이나 `Error`가 든 마지막 줄을 골라 `error`에 담는다 — 끝나지 않았는데 세션이
없다는 것은 무언가 죽었다는 뜻이고, 사람이 tmux에 붙지 않고도 이유를 볼 수 있어야 한다.

목록은 `checkpoints/last` 심볼릭 링크를 건너뛴다(`os.path.islink`). 따라가면 같은 체크포인트가
두 번 나와 화면이 있지도 않은 회수를 센다. 체크포인트가 아직 하나도 없는 실행도 싣는다 —
ACT의 첫 저장은 2만 스텝 뒤이고, 그때까지 목록에서 사라져 있으면 사람은 학습이 시작됐는지조차
알 수 없다.

이 전부를 원격 파이썬 스크립트 하나 안에서 한다. 실행마다 ssh를 왕복하면 tailnet 너머에서
화면이 눈에 띄게 굼떠진다.

### 멈추는 것

`POST /api/spark/runs/{run}/stop`은 `tmux send-keys C-c`를 먼저 보낸다. 사람이 tmux에 붙어
눌렀을 때와 같은 길이고, LeRobot은 그 신호를 받고 정리한 뒤 나간다. 2초 뒤에도 살아 있으면
`kill-session`한다. 어느 쪽이든 **이미 디스크에 쓰인 체크포인트는 그대로 남는다.**

### 전송은 원자적이다

받는 자리는 최종 위치가 아니라 `<root>/.incoming/<name>`이고, 다 받은 뒤에 제자리로 옮긴다.

이렇게 하지 않으면 끊긴 전송이 조용히 거짓말을 한다. 최종 위치에 바로 받다가 끊기면
`meta/info.json`만 도착한 디렉터리가 남는데, 목록은 그것을 **에피소드 50개짜리 멀쩡한
데이터셋으로 읽고** 화면은 `전송됨`이라 말한다. 그 상태로 학습을 걸면 영상이 없어 실패한다.
실측으로 재현한 뒤 고친 동작이다.

`--partial`이 남긴 조각은 `.incoming` 안에 남으므로 다시 누르면 이어받는다. 6.2GB 전송을
중간에 끊고 다시 시작했을 때 344MB 지점부터 이어받아 완료하는 것을 확인했다.

### 전송 속도와 블로킹

`POST /api/spark/datasets/{name}`은 전송이 끝날 때까지 요청을 붙들고 있다. 실측:

| 경로 | 6.2GB 소요 | 속도 |
| --- | --- | --- |
| 맥이 집에 있을 때 (LAN 터널) | 62초 | 108 MB/s |
| 맥이 집 밖에 있을 때 (tailnet 터널) | 64초 | 104 MB/s |

**두 경우가 거의 같은 이유가 중요하다.** 데이터는 콘솔 서버와 학습 서버 사이 LAN으로
흐르고, 맥의 터널은 HTTP 요청 하나만 나른다. 맥이 어디에 있든 전송 속도는 집 안의 두
기계 사이 속도이며, 밖에 있다고 느려지지 않는다.

이 속도라면 50GB도 8분이다. 그래도 블로킹이 옳은 설계는 아니다 — 진행률을 보여 줄 수
없고, 앱을 닫으면 요청이 끊긴다(전송 자체는 `.incoming`에 남아 다음에 이어받는다).
전송이 상시 몇 분을 넘기기 시작하면 녹화가 그렇듯 백그라운드 작업과 상태 엔드포인트로
옮겨야 한다.

### 실패는 원인을 말한다

rsync와 ssh는 원인을 첫 줄에 찍고 마지막 줄에는 요약을 남긴다. 마지막 줄을 그대로 올리면
`rsync error: unexplained error (code 255)` 같은 아무것도 설명하지 않는 문장이 화면에 뜬다.
`_explain()`이 아는 원인(디스크 부족, 이름 해석 실패, 로그인 거절, 연결 끊김 등)을 먼저
찾고, 없으면 요약 줄을 피해 원인에 가까운 줄을 고른다.

문장은 영어로 둔다. 서버가 내는 `detail`을 화면의 말로 옮기는 자리는 맥 앱의
`SOArmServerText`에 이미 있고, 그 규칙을 깨면 같은 문구가 두 곳에서 관리된다. 끊긴 전송에
대해서는 "다시 누르면 받다 만 곳부터 이어받습니다"까지 함께 말한다 — 사람이 다음에 무엇을
할지가 실패 문구의 목적이다.

## 설정

| 환경변수 | 기본값 |
| --- | --- |
| `SOARM_SPARK_HOST` | `<학습서버>` |
| `SOARM_SPARK_USER` | `<계정>` |
| `SOARM_SPARK_DATASET_ROOT` | `data/soarm` |
| `SOARM_SPARK_OUTPUT_ROOT` | `outputs` |

HUB의 `deploy` 공개키가 Spark의 `authorized_keys`에 등록되어 있어야 한다.

## 절차

전송·학습·회수가 모두 콘솔 API 하나씩이다.

```bash
# 1. 데이터셋을 Spark로
curl -X POST http://127.0.0.1:8088/api/spark/datasets/<name>

# 2. 학습 시작 — 돌려주는 `run`이 이후의 이름이다
curl -X POST http://127.0.0.1:8088/api/spark/train \
  -H 'content-type: application/json' \
  -d '{"dataset": "<name>", "policy": "act"}'

# 3. 진행 상황 (각 실행의 `training`을 본다)
curl -s http://127.0.0.1:8088/api/spark/runs | jq '.[].training'

# 4. 멈추려면
curl -X POST http://127.0.0.1:8088/api/spark/runs/<run>/stop

# 5. 끝나면 체크포인트 회수
curl -X POST http://127.0.0.1:8088/api/spark/runs/<run>/<step>
```

터미널에서 직접 돌리고 싶으면 `GET /api/spark/train-command`가 여전히 명령 문자열을 준다.
다만 그쪽은 `output_dir`이 데이터셋 이름으로 고정이라 두 번째 학습에서 `FileExistsError`가
난다 — 그때는 `--output_dir`을 손으로 바꾼다.

원격에서 직접 볼 일이 있으면:

```bash
ssh <계정>@<학습서버> 'tmux capture-pane -pt train-<run> | tail -20'
ssh <계정>@<학습서버> 'tail -f outputs/<run>/train.log'
```

회수한 것은 `checkpoints/<run>/<step>/`에 놓인다. `training_state`는 가져오지 않는다 —
추론에 쓰이지 않는데 optimizer 상태까지 있어 훨씬 크고, 재개는 Spark에서 하는 것이 맞다.

## 알려진 제약

- **`sm_121` 네이티브 커널이 없다.** `sm_120` 바이너리의 하위 호환으로 돈다. ACT 학습은
  실측으로 문제없었지만, flash-attention처럼 커널을 직접 컴파일하는 패키지는 별도 확인이
  필요하다.
- **GR00T N1.5는 지원이 끝났다.** N1.7로 가야 하며 LeRobot Dataset v3.0 형식을 요구한다.
  `lerobot-train --policy.type`이 `groot`, `pi0`, `pi05`, `smolvla` 등을 이미 받는다.
- **추론 FPS를 녹화 FPS에 맞춰야 한다.** ACT 같은 액션 청크 정책은 시간 일관성이 성공률에
  직결된다. 30fps로 녹화했다면 추론도 30Hz로 돌린다.
