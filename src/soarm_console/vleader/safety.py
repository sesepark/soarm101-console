from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

from .spec import JointSpec


class Reject:
    """거절 사유 코드.

    앞의 아홉 개는 PROTOCOL.md에 이미 적혀 있는 것을 그대로 쓴다. 뒤의 것들은 이 구현에서
    새로 생긴 사유이고, PROTOCOL.md가 허용한 대로 추가한 것이다 — 클라이언트는 모르는
    코드를 일반 거절로 처리하므로, 오래된 클라이언트가 새 코드를 만나도 조용히 무시하지 않고
    거절로 본다.
    """

    NO_ACTIVE_LEASE = "NO_ACTIVE_LEASE"
    WRONG_AUTHORITY = "WRONG_AUTHORITY"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    EXPIRED_COMMAND = "EXPIRED_COMMAND"
    DUPLICATE_SEQUENCE = "DUPLICATE_SEQUENCE"
    INVALID_SHAPE = "INVALID_SHAPE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    OUTSIDE_ABSOLUTE_LIMIT = "OUTSIDE_ABSOLUTE_LIMIT"
    HARDWARE_NOT_READY = "HARDWARE_NOT_READY"
    # --- 이 구현에서 추가한 것 ---
    #: 리스를 막 잡은 뒤 첫 명령이 실제 자세에서 너무 멀다. 이것이 없으면 가상 리더의
    #: 기본 자세로 팔이 튄다.
    POSE_NOT_SYNCED = "POSE_NOT_SYNCED"
    #: 지금 HOLD/FAULT다. 사람이 원인을 확인하고 다시 시작해야 한다.
    NOT_ACCEPTING_MOTION = "NOT_ACCEPTING_MOTION"


KOREAN = {
    Reject.NO_ACTIVE_LEASE: "지금 이 연결에는 조작 권한(리스)이 없습니다",
    Reject.WRONG_AUTHORITY: "다른 기기가 조작 권한을 쥐고 있습니다",
    Reject.STALE_OBSERVATION: "너무 오래된 관측을 근거로 한 명령입니다",
    Reject.EXPIRED_COMMAND: "명령의 유효기간이 지났습니다",
    Reject.DUPLICATE_SEQUENCE: "이미 처리한 순번입니다",
    Reject.INVALID_SHAPE: "명령의 형식이 맞지 않습니다",
    Reject.NON_FINITE_VALUE: "숫자가 아닌 값이 들어 있습니다",
    Reject.OUTSIDE_ABSOLUTE_LIMIT: "관절의 절대 한계를 벗어났습니다",
    Reject.HARDWARE_NOT_READY: "하드웨어가 준비되지 않았습니다",
    Reject.POSE_NOT_SYNCED: "첫 명령이 팔의 현재 자세에서 너무 멉니다",
    Reject.NOT_ACCEPTING_MOTION: "지금은 자세 유지(HOLD) 중이라 새 동작을 받지 않습니다",
}


class Trip:
    """접촉·과열처럼 명령이 아니라 **관측**이 걸어 내는 정지 사유."""

    OVERLOAD = "OVERLOAD"
    OVERCURRENT = "OVERCURRENT"
    STALLED = "STALLED"
    FOLLOWING_ERROR = "FOLLOWING_ERROR"
    OVER_TEMPERATURE = "OVER_TEMPERATURE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_RELEASED = "LEASE_RELEASED"
    OPERATOR_HOLD = "OPERATOR_HOLD"
    HARDWARE_ERROR = "HARDWARE_ERROR"


#: **조작하던 사람이 사라져서** 선 것들.
#:
#: 팔에 일어난 일이 아니라 조작면에 일어난 일이다. 읽을 현장이 없고, 확인할 것도 없다 —
#: 앞 사람이 반납했거나, 리스가 만료됐거나, 스트림이 끊겼다는 뜻뿐이다. 그래서 새 사람이
#: **확인 체크와 함께** 조작 권한을 받으면 이것들은 그 자리에서 풀린다.
#:
#: 나머지(접촉·과부하·과열·사람이 누른 정지·하드웨어 오류)는 풀리지 않는다. 그것들은
#: 팔에 일어난 일이고, 다음 사람이 **읽어야 하는** 이유다. 권한을 새로 받는 것으로 조용히
#: 지워지면 멈춘 이유를 아무도 보지 않은 채 다시 움직이게 된다.
OPERATOR_GONE = frozenset(
    {Trip.LEASE_RELEASED, Trip.LEASE_EXPIRED, Trip.COMMAND_TIMEOUT}
)

TRIP_KOREAN = {
    Trip.OVERLOAD: "부하가 계속 높습니다 — 무언가에 닿았을 수 있습니다",
    Trip.STALLED: "밀고 있는데 움직이지 않습니다 — 무언가에 막혀 있습니다",
    Trip.OVERCURRENT: "전류가 계속 높습니다 — 무언가에 닿았을 수 있습니다",
    Trip.FOLLOWING_ERROR: "목표와 실제 위치가 계속 벌어집니다 — 팔이 막혀 있습니다",
    Trip.OVER_TEMPERATURE: "모터가 뜨겁습니다",
    Trip.COMMAND_TIMEOUT: "명령이 끊겼습니다",
    Trip.LEASE_EXPIRED: "조작 권한이 만료되었습니다",
    Trip.LEASE_RELEASED: "조작 권한을 반납했습니다",
    Trip.OPERATOR_HOLD: "사람이 정지를 눌렀습니다",
    Trip.HARDWARE_ERROR: "하드웨어에서 오류가 났습니다",
}


def object_particle(word: str) -> str:
    """`을`인지 `를`인지.

    `을(를)`로 적어 두면 읽는 사람이 괄호를 골라 읽어야 한다. 이 문장은 팔이 멈췄을 때
    화면에 뜨는 문장이고, 그때는 한 글자라도 덜 걸리는 편이 낫다. 한글 음절의 받침은
    코드포인트에서 바로 나온다 — (코드 - 0xAC00) % 28 이 0이면 받침이 없다.
    """
    last = word.strip()[-1:] if word.strip() else ""
    if not last or not ("가" <= last <= "힣"):
        return "을"
    return "를" if (ord(last) - 0xAC00) % 28 == 0 else "을"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, default)))
    except (TypeError, ValueError):
        return default


#: 서보 위치 눈금 하나가 몇 도인가. STS3215는 한 바퀴를 4096으로 센다.
#:
#: `Goal_Velocity`(주소 46)의 단위가 바로 이 눈금/초다. 2026-09-02에 집게로 실측했다 —
#: 200을 써 넣으면 21°/s, 500이면 47°/s, 1000이면 93°/s, 2000이면 150°/s에서 천장에
#: 닿았다(이 팔의 무부하 속도). 0은 "제한 없음"이고 공장 기본값이다.
DEGREES_PER_TICK = 360.0 / 4096.0

#: 집게의 0~100%가 몇 도에 해당하는가. calibration 범위(1656~3100 눈금)에서 나온다.
#: 퍼센트로 말한 속도를 서보의 눈금/초로 바꿀 때 쓴다.
GRIPPER_DEGREES_PER_PERCENT = (3100 - 1656) / 100.0 * DEGREES_PER_TICK


@dataclass(frozen=True)
class VLeaderSettings:
    """안전 사다리의 문턱값.

    전부 config로 뺀 이유는 SAFETY.md가 이 값들을 불변조건이 아니라 `DEFAULT` 정책으로
    분류하기 때문이다. 기본값은 실측에 맞춰 잡았고, 아직 재지 못한 것은
    `docs/원격_텔레옵_안전.md`에 적어 두었다.

    ## 속도와 힘은 이제 서로 다른 값이 정한다

    예전에는 `step_deg` 하나가 둘을 함께 정했다. 목표를 실제 위치보다 한 틱에 몇 도까지
    앞세울 수 있는지가 상한이었고, 그 값이 곧 최대 속도(step × hz)이면서 동시에 서보가
    보는 위치 오차 — 즉 **힘** — 이었다. STS3215가 위치 P 제어이고 LeRobot이 P=16을 써
    넣기 때문이다. 그래서 안전하게 낮추면 어깨가 팔을 들지 못했고(2°에서 실제로 그랬다),
    들 수 있게 올리면 속도까지 함께 올라갔다. 두 요구가 한 손잡이에 묶여 있었다.

    이제는 나눈다.

    - **속도**는 서보 자신이 지킨다. `Goal_Velocity` 레지스터에 써 넣으면 서보 안의 궤적
      생성기가 목표까지 그 속도로 미끄러진다. 명령을 얼마나 자주 보내는지와 무관하고,
      위치 오차와도 무관하다.
    - **힘**은 목표가 실제보다 앞설 수 있는 거리(`lead_deg`)가 정한다. 자유롭게 움직이는
      동안 이 거리는 서보의 추종오차만큼밖에 벌어지지 않으므로 힘은 저절로 작다. 무언가에
      막혔을 때만 이 거리까지 벌어지고, 그때 나오는 힘이 이 값의 뜻이다.

    나누고 나서 얻은 것: 상한을 낮춰도 팔이 약해지지 않고, 힘을 올려도 팔이 빨라지지 않는다.
    """

    #: 제어 루프 주기. 30Hz는 기존 `lerobot-teleoperate --fps=30`과 같은 값이다.
    hz: int = field(default_factory=lambda: _env_int("SOARM_VL_HZ", 30))
    #: 팔 관절의 최대 속도. 서보의 `Goal_Velocity`로 내려간다.
    #:
    #: 이 팔의 천장은 실측 150°/s다(집게, 무부하). 기본값을 그 아래에 두는 이유는 속도가
    #: 곧 부딪혔을 때의 운동에너지이기 때문이지, 명령이 그보다 빠를 수 없어서가 아니다.
    max_deg_per_s: float = field(
        default_factory=lambda: _env_float("SOARM_VL_MAX_DEG_PER_S", 90.0)
    )
    #: 집게의 최대 속도. 단위가 퍼센트라 따로 둔다. 100%가 약 127°이므로 118%/s가 천장이다.
    max_percent_per_s: float = field(
        default_factory=lambda: _env_float("SOARM_VL_MAX_PERCENT_PER_S", 110.0)
    )
    #: 목표가 실제 위치보다 앞설 수 있는 거리 = **막혔을 때 내는 힘**.
    #:
    #: 실측(2026-09-01): 오차 2°에서 부하 100 — 어깨가 팔을 전혀 들지 못했다. 5°에서 236,
    #: 팔이 올라오기 시작했다. 8°에서 304. 즉 부하는 오차에 거의 비례하고, 팔을 들려면
    #: 최소 5°쯤이 필요하다. 12°는 그 두 배를 조금 넘는 여유이고, 막혔을 때의 부하는
    #: 서보 자체 보호(Overload_Torque=80%, 즉 800)에 한참 못 미치는 자리에 온다.
    lead_deg: float = field(default_factory=lambda: _env_float("SOARM_VL_LEAD_DEG", 12.0))
    #: 집게의 같은 값. 집게는 LeRobot이 `Max_Torque_Limit=500`으로 묶어 두어 더 약하다.
    lead_percent: float = field(
        default_factory=lambda: _env_float("SOARM_VL_LEAD_PERCENT", 12.0)
    )
    #: 명령이 이만큼 끊기면 **목표를 지금 자리에 붙인다.** 팔은 선다. 아직 HOLD는 아니다.
    #:
    #: 예전에는 이 자리가 곧바로 HOLD였다. 30Hz에서 아홉 틱이면 무선 구간이 한 번
    #: 흔들리기에 충분한 시간이라, 폰으로 조작하면 아무 잘못 없이 멈추고 그때마다
    #: `확인하고 계속`을 눌러야 했다. 멈추는 것과 **사람에게 확인을 요구하는 것**은
    #: 다른 일이다. 끊기면 즉시 서되, 확인은 정말로 사라졌을 때만 요구한다.
    command_timeout_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_COMMAND_TIMEOUT_MS", 300)
    )
    #: 그 침묵이 이만큼 이어지면 그때 HOLD. 조작하던 쪽이 정말로 사라진 것이다.
    command_hold_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_COMMAND_HOLD_MS", 1500)
    )
    #: 명령 하나가 스스로 주장할 수 있는 유효기간의 상한. 이보다 긴 값을 실어 보내도
    #: 이 값으로 자른다(SAFETY.md 불변조건 6).
    command_valid_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_COMMAND_VALID_MS", 500)
    )
    lease_ttl_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_LEASE_TTL_MS", 5000))
    heartbeat_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_HEARTBEAT_MS", 1000))
    #: 리스를 잡은 직후 첫 명령이 실제 자세에서 이보다 멀면 거절한다.
    #:
    #: 속도를 서보가 지키게 되면서 이 값의 뜻이 달라졌다. 예전에는 먼 첫 목표가 곧 빠른
    #: 출발이었지만, 이제 10°를 건너뛰어도 팔은 정해진 속도로 미끄러질 뿐이다. 그래서
    #: 6°에서 10°로 넓혔다 — 좁은 창은 권한을 받을 때마다 화면이 조용히 거절당하는
    #: 이유였고, 막아 주던 위험은 이제 다른 자리에서 막힌다.
    sync_tolerance_deg: float = field(
        default_factory=lambda: _env_float("SOARM_VL_SYNC_TOLERANCE_DEG", 10.0)
    )
    sync_tolerance_percent: float = field(
        default_factory=lambda: _env_float("SOARM_VL_SYNC_TOLERANCE_PERCENT", 15.0)
    )
    #: STS3215 `Present_Load` raw(0~1000). 서보 자신은 80%(=800)에서 토크를 20%로 떨어뜨린다
    #: (`Overload_Torque`/`Protective_Torque`, 실측으로 읽었다). 우리는 그보다 먼저,
    #: 떨어뜨리지 않는 방식으로 멈춘다.
    load_trip: int = field(default_factory=lambda: _env_int("SOARM_VL_LOAD_TRIP", 550))
    load_trip_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_LOAD_TRIP_MS", 400))
    #: `Present_Current`. **이 하드웨어에서는 쓰지 않는다.**
    #:
    #: 실측에서 여섯 관절 모두, 자세를 버틸 때도 막혀서 물러날 때도 판독값이 0~3칸에
    #: 머물렀다. 부하가 300을 넘는 순간에도 그랬다. 즉 이 레지스터는 이 팔에서 힘을
    #: 말해 주지 않는다. 걸릴 수 없는 검사를 사다리에 남겨 두면 화면은 보호가 한 겹 더
    #: 있다고 말하게 되고, 그것은 틀린 안심이다. 0이면 검사하지 않는다.
    current_trip: int = field(default_factory=lambda: _env_int("SOARM_VL_CURRENT_TRIP", 0))
    current_trip_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_CURRENT_TRIP_MS", 300)
    )
    #: 목표와 실제가 이만큼 벌어진 채 팔이 서 있으면 막힌 것으로 본다.
    #:
    #: 목표가 **절대 자세**가 되면서 이 검사가 비로소 제대로 선다. 예전에는 목표가 매 틱
    #: 실제 위치에 다시 붙어 정의상 벌어지지 않았고(그래서 "사람이 요청한 값"을 따로
    #: 들고 다녀야 했다), 지금은 목표가 사람이 말한 그 자리에 그대로 있다.
    #:
    #: 문턱은 `lead_deg`보다 조금 작아야 한다. 막히면 벌어짐은 `lead_deg`에서 멈추므로,
    #: 그보다 크게 잡으면 영영 걸리지 않는다.
    following_error_deg: float = field(
        default_factory=lambda: _env_float("SOARM_VL_FOLLOW_ERROR_DEG", 8.0)
    )
    following_error_percent: float = field(
        default_factory=lambda: _env_float("SOARM_VL_FOLLOW_ERROR_PERCENT", 8.0)
    )
    #: 이 창 동안 실제 위치가 이만큼도 움직이지 않았으면 서 있는 것으로 본다.
    stall_epsilon: float = field(
        default_factory=lambda: _env_float("SOARM_VL_STALL_EPSILON", 0.6)
    )
    #: 막힌 채로 **밀고 있는가**. 목표가 앞서 있고, 팔은 서 있고, 부하가 높다.
    #:
    #: 이 칸은 기계적 끝단을 위한 것이다. 끝단에서는 벌어짐이 자랄 자리가 없어 위의
    #: 추종오차 검사가 걸리지 않는다 — 집게를 끝까지 닫으면 남은 벌어짐이 1.6%뿐이다.
    #:
    #: 문턱을 부하만으로 정할 수 없다는 것은 실측이 말해 주었다. 팔 관절이 정지 마찰을
    #: 이기고 **움직이기 시작하는 순간** 부하가 96~144까지 오르는데, 그것은 진짜로 막혔을
    #: 때와 겹친다. 가르는 것은 **얼마나 오래 서 있는가**다.
    stall_load: int = field(default_factory=lambda: _env_int("SOARM_VL_STALL_LOAD", 200))
    stall_load_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_STALL_LOAD_MS", 1200)
    )
    following_error_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_FOLLOW_ERROR_MS", 600)
    )
    #: 실측(2026-09-01, 토크 끄고 25초): 팔 관절은 34~36°C, **집게는 48°C**였다. 쉬는
    #: 중에도 그렇다. 55°C로 잡았더니 집게는 평소에도 경고에 가까웠고, 그런 경고는 진짜로
    #: 뜨거워졌을 때 아무도 믿지 않게 만든다.
    temperature_warn_c: int = field(
        default_factory=lambda: _env_int("SOARM_VL_TEMP_WARN_C", 58)
    )
    #: STS3215의 자체 보호는 70°C에서 토크를 끊는다 — 그러면 힘으로 버티던 자세만큼
    #: 팔이 주저앉는다. 그보다 먼저, 떨어뜨리지 않는 방식으로 우리가 멈춘다.
    temperature_trip_c: int = field(
        default_factory=lambda: _env_int("SOARM_VL_TEMP_TRIP_C", 65)
    )
    #: 온도도 연속 초과를 요구한다. 45°C로 안정된 집게가 **한 번** 89°C로 읽히고 팔이
    #: 선 적이 있다. 튀지 않는 것은 온도이지 판독값이 아니다.
    temperature_trip_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_TEMP_TRIP_MS", 500)
    )
    #: 관절이 자기 끝에 닿았다고 볼 거리.
    #:
    #: 끝에 닿아 서 있는 것은 **고장이 아니라 기하학**이다. 집게를 끝까지 닫으면 팔은
    #: 거기서 서고, 그것이 정상이다. 그런데 목표를 계속 그 너머로 보내면 서보는 영원히
    #: 조금씩 밀고 있게 된다 — 실측(2026-09-02)에서 집게가 1.25%에 선 채 부하 84로
    #: 6초 내내 밀었고, 사다리의 어느 칸에도 걸리지 않았다. 걸리지 않는 것이 맞다:
    #: 부하 84는 아무것도 부수지 않는다. 다만 모터는 그동안 계속 뜨거워진다.
    #:
    #: 그래서 세우는 대신 **미는 것을 그만둔다.** 끝에 닿아 서 있으면 그 관절의 목표를
    #: 지금 자리에 붙여 두고, 화면에는 "끝까지 갔습니다"라고 적는다. 사람에게 확인을
    #: 요구하지 않는다 — 집게를 끝까지 닫을 때마다 `확인하고 계속`을 눌러야 한다면
    #: 그것은 보호가 아니라 방해다.
    #:
    #: 장애물과는 다르다. 장애물은 관절 **가운데**에서 팔을 세우고, 그때는 목표가
    #: `lead`만큼 앞서므로 서보가 세게 민다. 그쪽은 아래 추종오차·막힘이 잡아 세운다.
    limit_epsilon: float = field(
        default_factory=lambda: _env_float("SOARM_VL_LIMIT_EPSILON", 2.0)
    )
    #: 접촉으로 걸렸을 때 밀던 방향의 반대로 물러나는 양.
    retreat_deg: float = field(default_factory=lambda: _env_float("SOARM_VL_RETREAT_DEG", 4.0))
    #: 물러나는 데 줄 수 있는 시간. **물러남에는 반드시 끝이 있어야 한다** — 걸린 방향의
    #: 반대편에도 무언가가 있으면 팔은 물러날 곳이 없고, 그 자리에서 계속 밀게 된다.
    retreat_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_RETREAT_MS", 1500))

    # MARK: 단위가 다른 관절을 같은 식으로 다루기

    def lead(self, spec: JointSpec) -> float:
        return self.lead_percent if spec.unit == "percent" else self.lead_deg

    def speed(self, spec: JointSpec) -> float:
        """이 관절의 최대 속도. 단위는 그 관절의 단위/초다."""
        return self.max_percent_per_s if spec.unit == "percent" else self.max_deg_per_s

    def ticks_per_second(self, spec: JointSpec) -> int:
        """서보의 `Goal_Velocity`에 써 넣을 값. 0은 제한 없음이므로 최소 1로 올린다."""
        per_unit = (
            GRIPPER_DEGREES_PER_PERCENT if spec.unit == "percent" else 1.0
        ) / DEGREES_PER_TICK
        return max(1, min(4000, int(round(self.speed(spec) * per_unit))))

    def following_error(self, spec: JointSpec) -> float:
        return self.following_error_percent if spec.unit == "percent" else self.following_error_deg

    def sync_tolerance(self, spec: JointSpec) -> float:
        return self.sync_tolerance_percent if spec.unit == "percent" else self.sync_tolerance_deg

    def as_dict(self) -> dict[str, object]:
        return {
            "hz": self.hz,
            "max_deg_per_s": self.max_deg_per_s,
            "max_percent_per_s": self.max_percent_per_s,
            "lead_deg": self.lead_deg,
            "lead_percent": self.lead_percent,
            # 옛 이름. 이 값이 속도를 정하던 시절의 클라이언트가 아직 읽을 수 있으므로
            # 남겨 두되, 뜻은 지금의 `lead_*`와 같다.
            "step_deg": self.lead_deg,
            "step_percent": self.lead_percent,
            "command_timeout_ms": self.command_timeout_ms,
            "command_hold_ms": self.command_hold_ms,
            "command_valid_ms": self.command_valid_ms,
            "lease_ttl_ms": self.lease_ttl_ms,
            "heartbeat_ms": self.heartbeat_ms,
            "sync_tolerance_deg": self.sync_tolerance_deg,
            "sync_tolerance_percent": self.sync_tolerance_percent,
            "load_trip": self.load_trip,
            "load_trip_ms": self.load_trip_ms,
            "current_trip": self.current_trip,
            "current_trip_ms": self.current_trip_ms,
            "following_error_deg": self.following_error_deg,
            "following_error_percent": self.following_error_percent,
            "stall_epsilon": self.stall_epsilon,
            "limit_epsilon": self.limit_epsilon,
            "stall_load": self.stall_load,
            "stall_load_ms": self.stall_load_ms,
            "following_error_ms": self.following_error_ms,
            "temperature_warn_c": self.temperature_warn_c,
            "temperature_trip_c": self.temperature_trip_c,
            "temperature_trip_ms": self.temperature_trip_ms,
            "retreat_deg": self.retreat_deg,
            "retreat_ms": self.retreat_ms,
        }


#: 사람이 고르는 세 가지 조작감.
#:
#: 숫자를 직접 고르라고 하면 아무도 고를 수 없다. `lead_deg`가 12여야 하는지 15여야
#: 하는지는 이 팔을 만들어 본 사람도 재 보기 전에는 모르고, 쓰는 사람에게 물을 일은
#: 더더욱 아니다. 그래서 화면에는 **한 줄로 설명되는 세 가지**만 두고, 숫자는 여기서
#: 함께 움직인다. 속도와 힘과 민감도는 서로 짝이 맞아야 하는 값들이라 따로 고르면
#: 어긋나기 쉽다 — 빠르게 움직이면서 예민하게 멈추면 정상 조작 중에 자꾸 선다.
PROFILES: dict[str, dict[str, float]] = {
    "gentle": {
        "max_deg_per_s": 45.0,
        "max_percent_per_s": 60.0,
        "lead_deg": 8.0,
        "lead_percent": 8.0,
        "following_error_deg": 6.0,
        "following_error_percent": 6.0,
        "following_error_ms": 500,
        "stall_load": 160,
        "stall_load_ms": 1000,
        "load_trip": 450,
    },
    "normal": {
        "max_deg_per_s": 90.0,
        "max_percent_per_s": 110.0,
        "lead_deg": 12.0,
        "lead_percent": 12.0,
        "following_error_deg": 8.0,
        "following_error_percent": 8.0,
        "following_error_ms": 600,
        "stall_load": 200,
        "stall_load_ms": 1200,
        "load_trip": 550,
    },
    "quick": {
        "max_deg_per_s": 140.0,
        "max_percent_per_s": 118.0,
        "lead_deg": 18.0,
        "lead_percent": 16.0,
        "following_error_deg": 12.0,
        "following_error_percent": 10.0,
        "following_error_ms": 700,
        "stall_load": 260,
        "stall_load_ms": 1400,
        "load_trip": 650,
    },
}

PROFILE_KOREAN = {
    "gentle": ("조심", "천천히 움직이고 조금만 막혀도 섭니다. 좁은 곳에서 다루거나 무언가를 집을 때."),
    "normal": ("보통", "평소 조작에 맞춘 값입니다. 팔을 들 만큼 힘이 있고, 책상에 닿으면 곧 섭니다."),
    "quick": ("빠름", "크게 움직일 때. 힘도 속도도 커지므로 팔 주변이 비어 있을 때만 쓰세요."),
}


def profile_of(settings: VLeaderSettings) -> str | None:
    """지금 값이 어느 조작감인가. 손으로 하나만 바꿔 두었으면 `None`."""
    for name, values in PROFILES.items():
        if all(abs(float(getattr(settings, key)) - float(value)) < 1e-6 for key, value in values.items()):
            return name
    return None


class RejectError(Exception):
    """검사 한 칸에서 떨어졌다. `code`는 클라이언트가 읽는 기계용 사유다."""

    def __init__(self, code: str, detail: str = "", joint: str | None = None):
        super().__init__(detail or KOREAN.get(code, code))
        self.code = code
        self.joint = joint
        self.detail = detail or KOREAN.get(code, code)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "joint": self.joint, "message": self.detail}


class CommandValidator:
    """명령 하나에 대한 검사 사다리.

    순서가 곧 설계다. 형식 → 재생 방지 → 권한 → 유효기간 → 절대 한계 → 변화량 → 자세
    동기화 순으로, **거절이 싼 것부터** 본다. 뒤로 갈수록 하드웨어의 지금 상태를 알아야
    답할 수 있는 검사이고, 앞의 검사에서 이미 떨어질 명령을 그런 검사까지 끌고 갈 이유가
    없다. 부하·전류·추종오차·온도·워치독은 명령 하나로는 판단할 수 없으므로 여기 없고,
    제어 루프 쪽(`TripDetector`)에 있다.
    """

    def __init__(self, specs: list[JointSpec], settings: VLeaderSettings):
        self.specs = {spec.name: spec for spec in specs}
        self.settings = settings

    def validate(
        self,
        payload: object,
        *,
        present: dict[str, float],
        needs_sync: bool,
    ) -> dict[str, float]:
        """목표 관절값을 돌려준다. 어디선가 떨어지면 `RejectError`."""
        # 1. 형식과 유한성.
        if not isinstance(payload, dict) or not payload:
            raise RejectError(Reject.INVALID_SHAPE, "관절 값이 사전 형태가 아닙니다")
        unknown = set(payload) - set(self.specs)
        if unknown:
            raise RejectError(
                Reject.INVALID_SHAPE, f"모르는 관절 이름: {', '.join(sorted(unknown))}"
            )
        targets: dict[str, float] = {}
        for name, raw in payload.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise RejectError(Reject.INVALID_SHAPE, f"{name}: 숫자가 아닙니다", joint=name)
            value = float(raw)
            if not math.isfinite(value):
                raise RejectError(Reject.NON_FINITE_VALUE, f"{name}: {raw}", joint=name)
            targets[name] = value

        # 2. 절대 한계. calibration에서 읽은 값이고, 넘으면 자르지 않고 거절한다 —
        #    조용히 잘라 주면 화면이 도달했다고 말하는 자리와 팔이 선 자리가 달라진다.
        for name, value in targets.items():
            spec = self.specs[name]
            if not spec.contains(value):
                raise RejectError(
                    Reject.OUTSIDE_ABSOLUTE_LIMIT,
                    f"{spec.label}: {value:.1f}는 허용 범위 {spec.minimum:.1f}~{spec.maximum:.1f} 밖입니다",
                    joint=name,
                )

        # 3. 리스를 막 잡은 직후의 첫 명령. 여기서 걸러 내지 않으면 가상 리더의 기본
        #    자세로 팔이 튄다 — 리스를 잡을 때 뷰어를 현재 자세로 맞추는 것이 먼저다.
        if needs_sync:
            for name, value in targets.items():
                spec = self.specs[name]
                current = present.get(name)
                if current is None:
                    raise RejectError(Reject.HARDWARE_NOT_READY, f"{spec.label}의 현재 위치를 아직 읽지 못했습니다", joint=name)
                tolerance = self.settings.sync_tolerance(spec)
                if abs(value - current) > tolerance:
                    raise RejectError(
                        Reject.POSE_NOT_SYNCED,
                        f"{spec.label}: 첫 명령 {value:.1f}가 현재 {current:.1f}에서 {abs(value - current):.1f} 떨어져 있습니다 (허용 {tolerance:.1f})",
                        joint=name,
                    )
        return targets

    def clamp_lead(
        self, targets: dict[str, float], present: dict[str, float]
    ) -> tuple[dict[str, float], list[str]]:
        """목표가 실제 위치보다 앞설 수 있는 거리를 자른다.

        **이것은 속도 제한이 아니다.** 속도는 서보의 `Goal_Velocity`가 지킨다. 여기서
        자르는 것은 서보가 보는 위치 오차이고, 위치 P 제어에서 그것은 곧 **힘**이다.
        막히지 않은 관절에서는 이 거리가 서보의 추종오차만큼밖에 벌어지지 않으므로 이
        자르기는 아무 일도 하지 않는다. 무언가에 막혔을 때만 일한다.

        거절이 아니라 자르는 이유는 그대로다. 화면에서 손가락을 빠르게 끌면 목표는 늘
        현재보다 멀리 있게 마련이고, 그때마다 거절하면 조작 자체가 되지 않는다. 대신
        자른 관절을 함께 돌려주어 화면이 "따라가는 중"이라고 말할 수 있게 한다.

        예전에는 여기에 `scale`(지난 명령 이후 흐른 시간)이 있었다. 이 자르기가 속도를
        정하던 시절에는 그것이 필요했다 — 없으면 초당 300번 보내는 클라이언트가 초당
        600도를 움직였다. 지금은 속도가 명령 빈도와 무관하므로 그 보정도 필요 없다.
        오히려 없어서 좋아진 것이 있다: 명령이 드문드문 오는 느린 연결에서도 팔이
        느려지지 않는다. 폰이 10Hz로만 보내도 서보는 정해진 속도로 목표까지 간다.
        """
        clamped: dict[str, float] = {}
        limited: list[str] = []
        for name, value in targets.items():
            spec = self.specs[name]
            current = present.get(name, value)
            cap = self.settings.lead(spec)
            delta = max(-cap, min(cap, value - current))
            if abs(value - current) > cap + 1e-9:
                limited.append(name)
            clamped[name] = spec.clamp(current + delta)
        return clamped, limited

    def at_end_stop(
        self, name: str, target: float, present: float, moved: float | None
    ) -> bool:
        """이 관절이 자기 끝에 닿아 선 채로 더 밀리고 있는가.

        셋이 함께여야 한다. **끝 근처에 있고**, **더 그쪽으로 가라는 목표를 받고 있고**,
        **움직이지 않는다.** 하나라도 빠지면 정상 조작과 구별되지 않는다 — 끝을 향해
        가는 중인 관절은 움직이고 있고, 끝에 서 있어도 목표가 돌아섰으면 미는 것이 아니다.
        """
        spec = self.specs.get(name)
        if spec is None or moved is None:
            return False
        margin = self.settings.limit_epsilon
        low = present - spec.minimum <= margin and target < present
        high = spec.maximum - present <= margin and target > present
        if not (low or high):
            return False
        return moved < self.settings.stall_epsilon


class TripDetector:
    """관측이 걸어 내는 정지.

    부하·전류·추종오차는 한 번 튀었다고 멈추지 않는다. 모터가 방향을 바꾸는 순간이나
    통신이 한 번 흔들린 값 때문에 팔이 서면, 정작 진짜로 막혔을 때 사람이 그 경고를 믿지
    않게 된다. 그래서 전부 **연속 초과 시간**으로 본다. 온도만 예외로 즉시 본다 —
    온도는 튀지 않고, 한 번 뜨거워지면 식는 데 시간이 걸린다.
    """

    def __init__(self, specs: list[JointSpec], settings: VLeaderSettings):
        self.specs = {spec.name: spec for spec in specs}
        self.settings = settings
        self._since: dict[tuple[str, str], float] = {}

    def reset(self) -> None:
        self._since.clear()

    def _sustained(self, key: tuple[str, str], breached: bool, now: float, window_ms: int) -> bool:
        if not breached:
            self._since.pop(key, None)
            return False
        first = self._since.setdefault(key, now)
        return (now - first) * 1000.0 >= window_ms

    def inspect(
        self,
        *,
        now: float,
        present: dict[str, float],
        goal: dict[str, float],
        load: dict[str, float],
        current: dict[str, float],
        temperature: dict[str, float],
        requested: dict[str, float] | None = None,
        moved: dict[str, float] | None = None,
    ) -> tuple[str, str, str] | None:
        """걸렸으면 `(code, joint, 사람이 읽을 문장)`, 아니면 `None`."""
        settings = self.settings
        for name in self.specs:
            celsius = temperature.get(name)
            if celsius is None:
                continue
            if self._sustained(
                (name, Trip.OVER_TEMPERATURE),
                celsius >= settings.temperature_trip_c,
                now,
                settings.temperature_trip_ms,
            ):
                return (
                    Trip.OVER_TEMPERATURE,
                    name,
                    f"{self.specs[name].label} 모터가 {settings.temperature_trip_ms}ms 넘게 "
                    f"{celsius:.0f}°C입니다 (정지 문턱 {settings.temperature_trip_c}°C)",
                )
        for name in self.specs:
            value = abs(load.get(name, 0.0))
            if self._sustained((name, Trip.OVERLOAD), value >= settings.load_trip, now, settings.load_trip_ms):
                return (
                    Trip.OVERLOAD,
                    name,
                    f"{self.specs[name].label}의 부하가 {settings.load_trip_ms}ms 넘게 {value:.0f}(문턱 {settings.load_trip})입니다",
                )
        # 전류 문턱이 0이면 이 칸은 없다. 이 하드웨어에서 `Present_Current`가 힘을
        # 말해 주지 않는다는 것이 실측으로 드러났고(부하 300에서도 판독값 0~3),
        # 걸릴 수 없는 검사를 남겨 두면 화면이 보호가 한 겹 더 있다고 말하게 된다.
        for name in self.specs if settings.current_trip > 0 else ():
            value = abs(current.get(name, 0.0))
            if self._sustained((name, Trip.OVERCURRENT), value >= settings.current_trip, now, settings.current_trip_ms):
                return (
                    Trip.OVERCURRENT,
                    name,
                    f"{self.specs[name].label}의 전류가 {settings.current_trip_ms}ms 넘게 {value * 6.5:.0f}mA입니다",
                )
        # 막힌 채 **밀고 있는** 관절. 위의 부하 검사(문턱 400)는 실물에서 걸린 적이 없고,
        # 아래 추종오차 검사는 사람이 요청한 값과의 벌어짐을 보므로 기계적 끝단에서는
        # 벌어질 자리가 없어 걸리지 않는다. 이 칸이 그 사이를 메운다.
        for name, spec in self.specs.items():
            if name not in goal or name not in present:
                self._since.pop((name, Trip.STALLED), None)
                continue
            pushing = abs(goal[name] - present[name]) >= settings.stall_epsilon
            standing = (moved or {}).get(name, float("inf")) < settings.stall_epsilon
            heavy = abs(load.get(name, 0.0)) >= settings.stall_load
            if self._sustained(
                (name, Trip.STALLED), pushing and standing and heavy, now, settings.stall_load_ms
            ):
                return (
                    Trip.STALLED,
                    name,
                    f"{spec.label}를 밀고 있는데 {settings.stall_load_ms}ms 넘게 제자리이고 "
                    f"부하가 {abs(load.get(name, 0.0)):.0f}입니다 — 무언가에 막혀 있습니다",
                )

        # 막힌 관절을 찾는 자리.
        #
        # 처음에는 **틱당 잘린 목표**와 실제의 차이를 봤다. 그 차이는 자라지 않는다 —
        # 잘린 목표는 매 틱 실제 위치에 다시 붙기 때문이다. 실물에서 집게를 끝까지 닫아
        # 턱이 맞닿게 했을 때 부하는 48~64였고 자유롭게 움직일 때(최대 88)와 구별되지
        # 않았다. 즉 부하로도, 잘린 목표로도 "막혔다"를 알 수 없었다.
        #
        # 알 수 있는 것은 **사람이 계속 요청하는데 팔이 그 자리에 서 있다**는 사실이다.
        # 두 조건을 함께 본다. 요청만 보면 빠르게 끌 때(요청이 앞서가고 팔이 따라오는 중)
        # 걸리고, 정지만 보면 아무도 조작하지 않을 때 걸린다.
        requested = requested or goal
        moved = moved or {}
        for name, spec in self.specs.items():
            if name not in requested or name not in present:
                self._since.pop((name, Trip.FOLLOWING_ERROR), None)
                continue
            gap = abs(requested[name] - present[name])
            standing = moved.get(name, float("inf")) < settings.stall_epsilon
            if self._sustained(
                (name, Trip.FOLLOWING_ERROR),
                gap >= settings.following_error(spec) and standing,
                now,
                settings.following_error_ms,
            ):
                unit = "%" if spec.unit == "percent" else "°"
                return (
                    Trip.FOLLOWING_ERROR,
                    name,
                    f"{spec.label}{object_particle(spec.label)} {gap:.1f}{unit} 더 보내라는 명령이 이어지는데 "
                    f"{settings.following_error_ms}ms 넘게 제자리입니다 — 무언가에 닿았습니다",
                )
        return None

    def warnings(self, temperature: dict[str, float]) -> list[dict[str, object]]:
        """멈출 정도는 아니지만 화면이 말해야 하는 것.

        경고도 한 번 튄 값으로는 뜨지 않는다. 깜빡였다 사라지는 경고는 읽는 사람에게
        아무것도 알려 주지 않으면서 다음 경고의 무게만 깎는다.
        """
        lines = []
        for name, celsius in temperature.items():
            if name not in self.specs:
                continue
            warm = self._sustained(
                (name, "warn-temperature"),
                celsius >= self.settings.temperature_warn_c,
                time.monotonic(),
                self.settings.temperature_trip_ms,
            )
            if warm:
                lines.append(
                    {
                        "joint": name,
                        "code": Trip.OVER_TEMPERATURE,
                        "message": f"{self.specs[name].label} {celsius:.0f}°C",
                    }
                )
        return lines


#: 프로필이 옮기는 값마다 대응하는 환경변수. `load_settings`가 "사람이 직접 적어 둔 값"과
#: "프로필이 정한 값"을 가르는 데 쓴다.
PROFILE_ENV = {
    "max_deg_per_s": "SOARM_VL_MAX_DEG_PER_S",
    "max_percent_per_s": "SOARM_VL_MAX_PERCENT_PER_S",
    "lead_deg": "SOARM_VL_LEAD_DEG",
    "lead_percent": "SOARM_VL_LEAD_PERCENT",
    "following_error_deg": "SOARM_VL_FOLLOW_ERROR_DEG",
    "following_error_percent": "SOARM_VL_FOLLOW_ERROR_PERCENT",
    "following_error_ms": "SOARM_VL_FOLLOW_ERROR_MS",
    "stall_load": "SOARM_VL_STALL_LOAD",
    "stall_load_ms": "SOARM_VL_STALL_LOAD_MS",
    "load_trip": "SOARM_VL_LOAD_TRIP",
}


def load_settings() -> VLeaderSettings:
    """시작할 때의 정책 한 벌.

    `SOARM_VL_PROFILE`이 있으면 그 조작감을 깔되, **env에 직접 적혀 있는 값은 건드리지
    않는다.** 두 곳이 같은 값을 말할 수 있으므로 어느 쪽이 이기는지 정해 두어야 하고,
    손으로 적은 쪽이 이기는 편이 맞다 — 프로필은 고르는 것이고 env는 재 본 뒤 못 박는
    것이다.
    """
    base = VLeaderSettings()
    name = os.getenv("SOARM_VL_PROFILE", "").strip().lower()
    if name not in PROFILES:
        return base
    values = {
        key: value
        for key, value in PROFILES[name].items()
        if os.getenv(PROFILE_ENV.get(key, "")) in (None, "")
    }
    if not values:
        return base
    from dataclasses import replace as _replace

    return _replace(base, **values)
