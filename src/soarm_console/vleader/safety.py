from __future__ import annotations

import math
import os
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
    FOLLOWING_ERROR = "FOLLOWING_ERROR"
    OVER_TEMPERATURE = "OVER_TEMPERATURE"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_RELEASED = "LEASE_RELEASED"
    OPERATOR_HOLD = "OPERATOR_HOLD"
    HARDWARE_ERROR = "HARDWARE_ERROR"


TRIP_KOREAN = {
    Trip.OVERLOAD: "부하가 계속 높습니다 — 무언가에 닿았을 수 있습니다",
    Trip.OVERCURRENT: "전류가 계속 높습니다 — 무언가에 닿았을 수 있습니다",
    Trip.FOLLOWING_ERROR: "목표와 실제 위치가 계속 벌어집니다 — 팔이 막혀 있습니다",
    Trip.OVER_TEMPERATURE: "모터가 뜨겁습니다",
    Trip.COMMAND_TIMEOUT: "명령이 끊겼습니다",
    Trip.LEASE_EXPIRED: "조작 권한이 만료되었습니다",
    Trip.LEASE_RELEASED: "조작 권한을 반납했습니다",
    Trip.OPERATOR_HOLD: "사람이 정지를 눌렀습니다",
    Trip.HARDWARE_ERROR: "하드웨어에서 오류가 났습니다",
}


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


@dataclass(frozen=True)
class VLeaderSettings:
    """안전 사다리의 문턱값.

    전부 config로 뺀 이유는 SAFETY.md가 이 값들을 불변조건이 아니라 `DEFAULT` 정책으로
    분류하기 때문이다. 기본값은 보수적으로 잡았고, 근거와 아직 실측하지 못한 것은
    `docs/원격_텔레옵_안전.md`에 적어 두었다.
    """

    #: 제어 루프 주기. 30Hz는 기존 `lerobot-teleoperate --fps=30`과 같은 값이라,
    #: 물리 리더로 하던 것과 같은 속도로 돈다.
    hz: int = field(default_factory=lambda: _env_int("SOARM_VL_HZ", 30))
    #: 한 틱에 허용하는 최대 변화. LeRobot의 `max_relative_target`과 같은 뜻이고 같은
    #: 단위(도)다. 서버는 이 값으로 먼저 자르고, LeRobot이 `send_action` 안에서 한 번 더
    #: 자른다 — 두 겹이 겹쳐 있는 것이 맞다. 하나는 우리 것이고 하나는 라이브러리 것이라
    #: 한쪽을 고쳐도 다른 쪽이 남는다.
    step_deg: float = field(default_factory=lambda: _env_float("SOARM_VL_STEP_DEG", 2.0))
    step_percent: float = field(default_factory=lambda: _env_float("SOARM_VL_STEP_PERCENT", 3.0))
    #: 명령이 이만큼 끊기면 HOLD. 30Hz에서 아홉 틱쯤이다. 무선 구간이 잠깐 끊겼다고
    #: 매번 멈추지 않으면서, 조작하던 사람이 손을 떼면 곧바로 서는 길이.
    command_timeout_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_COMMAND_TIMEOUT_MS", 300)
    )
    #: 명령 하나가 스스로 주장할 수 있는 유효기간의 상한. 이보다 긴 값을 실어 보내도
    #: 이 값으로 자른다. 클라이언트가 "10초 동안 유효"라고 우기며 끊긴 뒤에도 마지막
    #: 명령을 살려 두는 길을 막는다(SAFETY.md 불변조건 6).
    command_valid_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_COMMAND_VALID_MS", 500)
    )
    lease_ttl_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_LEASE_TTL_MS", 5000))
    heartbeat_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_HEARTBEAT_MS", 1000))
    #: 리스를 잡은 직후 첫 명령이 실제 자세에서 이보다 멀면 거절한다.
    sync_tolerance_deg: float = field(
        default_factory=lambda: _env_float("SOARM_VL_SYNC_TOLERANCE_DEG", 6.0)
    )
    sync_tolerance_percent: float = field(
        default_factory=lambda: _env_float("SOARM_VL_SYNC_TOLERANCE_PERCENT", 10.0)
    )
    #: STS3215 `Present_Load` raw. 0~1000이 0~100%에 대응한다. LeRobot이 집게에만
    #: `Max_Torque_Limit=500`을 써 두었으므로 그 절반보다 낮은 자리에 우리 문턱을 둔다.
    load_trip: int = field(default_factory=lambda: _env_int("SOARM_VL_LOAD_TRIP", 400))
    load_trip_ms: int = field(default_factory=lambda: _env_int("SOARM_VL_LOAD_TRIP_MS", 300))
    #: `Present_Current` raw. STS3215의 눈금은 6.5mA이므로 108이면 약 0.7A다.
    current_trip: int = field(default_factory=lambda: _env_int("SOARM_VL_CURRENT_TRIP", 108))
    current_trip_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_CURRENT_TRIP_MS", 300)
    )
    #: 목표와 실제가 이만큼 벌어진 채 이 시간이 지나면 막힌 것으로 본다.
    following_error_deg: float = field(
        default_factory=lambda: _env_float("SOARM_VL_FOLLOW_ERROR_DEG", 12.0)
    )
    following_error_ms: int = field(
        default_factory=lambda: _env_int("SOARM_VL_FOLLOW_ERROR_MS", 400)
    )
    #: 실측(2026-09-01, 토크 끄고 25초): 팔 관절은 34~36°C, **집게는 48°C**였다. 움직이지
    #: 않고 쉬는 중에도 그렇다. 처음에 55°C로 잡았더니 집게는 평소에도 경고에 가까웠고,
    #: 그런 경고는 진짜로 뜨거워졌을 때 아무도 믿지 않게 만든다. 정지 문턱(65°C)과의
    #: 간격은 그대로 두면서 집게의 평상시보다 10°C 위로 올렸다.
    temperature_warn_c: int = field(
        default_factory=lambda: _env_int("SOARM_VL_TEMP_WARN_C", 58)
    )
    #: STS3215의 자체 보호는 70°C에서 토크를 끊는다 — 그러면 팔이 떨어진다. 그보다
    #: 먼저, 떨어뜨리지 않는 방식으로 우리가 멈춘다.
    temperature_trip_c: int = field(
        default_factory=lambda: _env_int("SOARM_VL_TEMP_TRIP_C", 65)
    )
    #: 접촉으로 걸렸을 때 최근 경로를 따라 물러나는 양.
    retreat_deg: float = field(default_factory=lambda: _env_float("SOARM_VL_RETREAT_DEG", 4.0))

    def step_limit(self, spec: JointSpec) -> float:
        return self.step_percent if spec.unit == "percent" else self.step_deg

    def sync_tolerance(self, spec: JointSpec) -> float:
        return self.sync_tolerance_percent if spec.unit == "percent" else self.sync_tolerance_deg

    def as_dict(self) -> dict[str, object]:
        return {
            "hz": self.hz,
            "step_deg": self.step_deg,
            "step_percent": self.step_percent,
            "command_timeout_ms": self.command_timeout_ms,
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
            "following_error_ms": self.following_error_ms,
            "temperature_warn_c": self.temperature_warn_c,
            "temperature_trip_c": self.temperature_trip_c,
            "retreat_deg": self.retreat_deg,
        }


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

    def clamp_step(
        self, targets: dict[str, float], present: dict[str, float], scale: float = 1.0
    ) -> tuple[dict[str, float], list[str]]:
        """틱당 변화량 상한.

        거절이 아니라 **자른다**. 화면에서 손가락을 빠르게 끌면 목표는 늘 현재보다 멀리
        있게 마련이고, 그때마다 거절하면 조작 자체가 되지 않는다. 대신 자른 관절을 함께
        돌려주어 화면이 "지금 최대 속도로 따라가는 중"이라고 말할 수 있게 한다.

        `scale`은 **지난 명령 이후 흐른 시간**이다. 이것이 없으면 상한이 "명령당"이 되어,
        초당 300번 보내는 클라이언트가 초당 600도를 움직일 수 있다. 시간으로 나눠 두면
        메시지를 몇 번에 나눠 보내든 속도의 상한은 `step × hz`로 같다.
        """
        clamped: dict[str, float] = {}
        limited: list[str] = []
        for name, value in targets.items():
            spec = self.specs[name]
            current = present.get(name, value)
            cap = self.settings.step_limit(spec) * max(0.02, min(1.0, scale))
            delta = max(-cap, min(cap, value - current))
            if abs(value - current) > cap + 1e-9:
                limited.append(name)
            clamped[name] = spec.clamp(current + delta)
        return clamped, limited


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
    ) -> tuple[str, str, str] | None:
        """걸렸으면 `(code, joint, 사람이 읽을 문장)`, 아니면 `None`."""
        settings = self.settings
        for name in self.specs:
            celsius = temperature.get(name)
            if celsius is not None and celsius >= settings.temperature_trip_c:
                return (
                    Trip.OVER_TEMPERATURE,
                    name,
                    f"{self.specs[name].label} 모터가 {celsius:.0f}°C입니다 (정지 문턱 {settings.temperature_trip_c}°C)",
                )
        for name in self.specs:
            value = abs(load.get(name, 0.0))
            if self._sustained((name, Trip.OVERLOAD), value >= settings.load_trip, now, settings.load_trip_ms):
                return (
                    Trip.OVERLOAD,
                    name,
                    f"{self.specs[name].label}의 부하가 {settings.load_trip_ms}ms 넘게 {value:.0f}(문턱 {settings.load_trip})입니다",
                )
        for name in self.specs:
            value = abs(current.get(name, 0.0))
            if self._sustained((name, Trip.OVERCURRENT), value >= settings.current_trip, now, settings.current_trip_ms):
                return (
                    Trip.OVERCURRENT,
                    name,
                    f"{self.specs[name].label}의 전류가 {settings.current_trip_ms}ms 넘게 {value * 6.5:.0f}mA입니다",
                )
        for name, spec in self.specs.items():
            if name not in goal or name not in present:
                self._since.pop((name, Trip.FOLLOWING_ERROR), None)
                continue
            gap = abs(goal[name] - present[name])
            if self._sustained(
                (name, Trip.FOLLOWING_ERROR),
                gap >= settings.following_error_deg,
                now,
                settings.following_error_ms,
            ):
                return (
                    Trip.FOLLOWING_ERROR,
                    name,
                    f"{spec.label}이(가) 목표에서 {gap:.1f} 벌어진 채 {settings.following_error_ms}ms 지났습니다",
                )
        return None

    def warnings(self, temperature: dict[str, float]) -> list[dict[str, object]]:
        """멈출 정도는 아니지만 화면이 말해야 하는 것."""
        lines = []
        for name, celsius in temperature.items():
            if celsius >= self.settings.temperature_warn_c and name in self.specs:
                lines.append(
                    {
                        "joint": name,
                        "code": Trip.OVER_TEMPERATURE,
                        "message": f"{self.specs[name].label} {celsius:.0f}°C",
                    }
                )
        return lines
