# ADR 0003: 장치별 Hardware Owner lock

- Status: Accepted
- Date: 2026-09-01
- 관련: ADR 0001, ADR 0002

## Context

ADR 0001은 serial/camera 장치마다 owner가 하나여야 한다고 정했지만, 구현은 한 FastAPI
프로세스 안의 `running` 검사와 HTTP 409뿐이었다. 콘솔 프로세스가 둘 뜨거나 점검 스크립트가
겹치면 이 검사를 공유하지 못한다. 같은 Feetech half-duplex bus에 두 프로세스가 붙으면 읽기
전용 명령도 packet을 전송하므로 status packet이 서로 섞인다.

PID만 적는 파일은 충분하지 않다. 프로세스가 죽은 뒤 PID가 재사용될 수 있고, 종료 중 파일을
지우지 못하면 살아 있는 owner인지 stale 기록인지 구별할 수 없다. 반대로 운영자가 파일을
삭제하는 것을 강제 인계 절차로 만들면, 이전 프로세스가 열린 장치를 계속 쓰는 동안 새 owner가
들어오는 split-brain을 만든다.

## Decision

프로젝트가 여는 각 hardware device에 Linux advisory `flock(LOCK_EX|LOCK_NB)`을 잡는다.
metadata 파일의 존재가 아니라 **열린 file description에 커널 lock이 있는가**가 소유권의
근거다.

### 경로와 장치 이름

기본 디렉터리는 다음 순서로 정한다.

1. `SOARM_OWNER_LOCK_DIR`이 있으면 그 경로
2. `$XDG_RUNTIME_DIR/soarm-console/owner-locks`
3. `/tmp/soarm-console-$UID/owner-locks`

디렉터리는 mode `0700`, 파일은 `0600`이다. 장치 경로는 `resolve(strict=False)`로 정규화한다.
따라서 `/dev/serial/by-id/...`와 그것이 가리키는 `/dev/ttyACM0`은 같은 장치 이름이 된다.
파일명은 정규화한 경로의 SHA-256 앞 20자리와 `.lock`이다. 장치가 여럿이면 정규화한 이름
순서대로 잡아 교착을 피하고, 하나라도 실패하면 이번에 잡은 것을 전부 반납한다.

### 파일 형식

JSON 한 개를 저장한다.

```json
{
  "schema": 1,
  "device": "/dev/ttyACM0",
  "owner": "virtual-leader",
  "pid": 1234,
  "process_start_ticks": 987654,
  "boot_id": "...",
  "hostname": "soarm-server",
  "command": ["..."],
  "acquired_at": 1788220800.0
}
```

PID, `/proc/$PID/stat`의 시작 tick, boot ID는 설명과 조사에 쓰는 metadata다. 이것만 보고
살아 있다고 판정하지 않는다. `flock`을 얻지 못하면 active이고, 얻으면 이전 JSON의 PID가
무엇이든 stale이다. 정상 시작이 lock을 얻은 뒤 metadata를 덮어쓴다. 파일은 반납 후에도
의도적으로 남는다.

### 적용 범위

| 경로 | 잡는 장치 |
|---|---|
| 가상 리더 실물 backend | follower serial |
| 물리 리더 텔레옵 | leader + follower serial |
| 물리 리더 수집 | leader + follower serial + scene/wrist camera |
| 가상 리더 수집 | follower serial + scene/wrist camera |
| camera preview | 해당 camera |
| hardware doctor | 읽는 동안 해당 serial |
| 접촉 문턱 측정 | 측정 동안 follower serial |
| simulated backend | 실물 lock 없음 |

물리 텔레옵과 수집은 console parent가 lock을 먼저 잡고 열린 descriptor를 LeRobot child에
상속한다. parent가 죽고 child만 남아도 child가 장치를 쓰는 동안 커널 lock이 유지된다.
독립적으로 `scripts/record.sh`를 실행하면 recording 프로세스가 같은 lock을 직접 잡는다.

정상 종료와 crash에서는 descriptor close로 lock이 자동 반납된다. 종료 timeout이 난 살아 있는
프로세스의 lock은 반납하지 않는다. lock 획득 실패는 기존 시작 API에서 409로 드러난다.

## 복구

lock 파일을 삭제하거나 바꿔서 인계하지 않는다. 그런 API, `force-unlock` 명령, stale PID를
근거로 다른 프로세스를 죽이는 기능을 만들지 않는다.

1. `lslocks`와 metadata의 PID/command를 함께 보고 owner를 식별한다.
2. 콘솔의 정상 stop 또는 그 프로세스의 SIGINT 절차를 쓴다. 토크는 자동으로 끄지 않는다.
3. 프로세스가 이미 죽었다면 원하는 정상 경로를 다시 시작한다. 커널 lock이 없으므로 새 owner가
   stale metadata를 자동으로 덮어쓴다.
4. lock이 계속 active이면 장치 `fuser` 결과와 해당 PID를 검토한다. 파일만 지우지 않는다.

## Consequences

콘솔 인스턴스, 저장소의 worker와 스크립트 사이 상호배타는 프로세스 경계를 넘어 동작한다.
PID 재사용이나 crash 뒤 남은 파일 때문에 영구적으로 막히지 않고, 인계 실패를 장치를 열기 전에
알 수 있다.

`flock`은 cooperative/advisory다. 이 저장소의 lock 규약을 무시하고 upstream
`.venv/bin/lerobot-teleoperate`나 serial 라이브러리를 직접 실행하는 프로세스를 커널이 막아
주지는 않는다. udev 권한 격리나 broker-only device access 없이는 이 한계를 없앨 수 없다.
따라서 직접 upstream 실행은 지원 운용 경로가 아니며, **그 프로세스까지 막는다고 주장하지
않는다.** 이번 결정에서는 sudo, udev, systemd를 변경하지 않는다.

또한 같은 사용자가 active lock 파일을 직접 unlink하면 advisory lock의 inode를 우회할 수 있다.
그래서 삭제를 복구 절차로 제공하지 않으며, 삭제 후의 상태는 안전한 강제 인계로 간주하지 않는다.

## Alternatives

### PID 파일만 사용

PID 재사용과 crash stale 판정이 모호하고 자동 반납이 없다. 채택하지 않았다.

### lock 파일 삭제를 강제 인계로 사용

기존 owner가 계속 실행 중인지 증명하지 못한 채 새 inode에 lock을 잡게 한다. 채택하지 않았다.

### 장치 `fuser` 검사만 사용

검사와 open 사이 경합이 있고 owner 의도와 인계 metadata가 없다. 장애 조사 보조 수단으로만 쓴다.

### udev로 broker만 장치를 열게 함

비협조 프로세스까지 막는 더 강한 경계지만 사용자/그룹과 서비스 구성을 바꿔야 한다. 별도 사전
검토 없이 이번 범위에 넣지 않았다.
