from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus

from .config import Settings
from .owner_lock import DeviceLockError, DeviceLockSet


MOTORS = {
    "shoulder_pan": Motor(1, "sts3215", MotorNormMode.DEGREES),
    "shoulder_lift": Motor(2, "sts3215", MotorNormMode.DEGREES),
    "elbow_flex": Motor(3, "sts3215", MotorNormMode.DEGREES),
    "wrist_flex": Motor(4, "sts3215", MotorNormMode.DEGREES),
    "wrist_roll": Motor(5, "sts3215", MotorNormMode.DEGREES),
    "gripper": Motor(6, "sts3215", MotorNormMode.RANGE_0_100),
}
EXPECTED_IDS = set(range(1, 7))
EXPECTED_MODEL = 777


@dataclass
class ArmDiagnostic:
    role: str
    port: str
    healthy: bool
    #: `healthy`와 같은 값이다. 남겨 두는 이유는 이름을 부르는 화면들이 있기 때문이고,
    #: 뜻이 하나뿐이라는 것은 `safe_for_motion_start is healthy`가 지킨다.
    #:
    #: 한때 여기에 "여섯 모터 전부 `Torque_Enable == 0`"이 더 붙어 있었다. 그것을 뺀
    #: 이유는 `run_hardware_doctor`에 적어 두었다.
    safe_for_motion_start: bool
    models: dict[int, int | None]
    firmware: dict[int, str]
    positions_raw: dict[str, int]
    voltage_raw: dict[str, int]
    #: 지금 토크가 걸려 있는가. 시작을 막지는 않지만 화면은 이 값을 본다 — 맥 앱의
    #: `토크 해제` 단추가 나타나는 근거가 여기다.
    torque_enabled: dict[str, int]
    #: 모터별 `Present_Temperature`(°C). STS3215는 과열로 스스로 토크를 끊는다. 팔이
    #: 이유 없이 힘을 잃었을 때 물어볼 곳이 여기여야 한다.
    temperature: dict[str, int]
    error: str | None = None


def _inspect_arm_unlocked(role: str, port: str) -> ArmDiagnostic:
    """Read motor state without writing motor registers or changing torque."""
    bus = FeetechMotorsBus(port=port, motors=MOTORS)
    try:
        bus.connect(handshake=False)
        # This configures the host UART only; it does not change a motor register.
        bus.set_baudrate(1_000_000)
        models = {motor_id: bus.ping(motor_id, num_retry=2) for motor_id in EXPECTED_IDS}
        responsive = {motor_id for motor_id, model in models.items() if model is not None}
        if responsive != EXPECTED_IDS:
            missing = sorted(EXPECTED_IDS - responsive)
            return ArmDiagnostic(
                role, port, False, False, models, {}, {}, {}, {}, {},
                f"Missing motor IDs: {missing}",
            )

        firmware = bus._read_firmware_version(sorted(responsive), raise_on_error=False)
        positions = bus.sync_read("Present_Position", normalize=False, num_retry=2)
        voltages = {
            name: bus.read("Present_Voltage", name, normalize=False, num_retry=2)
            for name in MOTORS
        }
        torque = {
            name: bus.read("Torque_Enable", name, normalize=False, num_retry=2)
            for name in MOTORS
        }
        temperature = {
            name: bus.read("Present_Temperature", name, normalize=False, num_retry=2)
            for name in MOTORS
        }
        healthy = (
            all(model == EXPECTED_MODEL for model in models.values())
            and all(90 <= voltage <= 130 for voltage in voltages.values())
        )
        return ArmDiagnostic(
            role=role,
            port=port,
            healthy=healthy,
            safe_for_motion_start=healthy,
            models=models,
            firmware=firmware,
            positions_raw=positions,
            voltage_raw=voltages,
            torque_enabled=torque,
            temperature=temperature,
        )
    except Exception as exc:
        return ArmDiagnostic(role, port, False, False, {}, {}, {}, {}, {}, {}, str(exc))
    finally:
        if bus.is_connected:
            bus.disconnect(disable_torque=False)


def inspect_arm(role: str, port: str, *, acquire_owner_lock: bool = True) -> ArmDiagnostic:
    """장치를 예약한 뒤 read-only 진단한다.

    lock을 이미 가진 owner의 시작 절차만 `acquire_owner_lock=False`를 쓴다. 진단은 motor
    register를 쓰지 않지만 serial packet은 보내므로, 다른 owner와 동시에 붙을 수 없다.
    """
    if not acquire_owner_lock:
        return _inspect_arm_unlocked(role, port)
    try:
        with DeviceLockSet.acquire([port], "hardware-doctor"):
            return _inspect_arm_unlocked(role, port)
    except DeviceLockError as exc:
        return ArmDiagnostic(
            role=role,
            port=port,
            healthy=False,
            safe_for_motion_start=False,
            models={},
            firmware={},
            positions_raw={},
            voltage_raw={},
            torque_enabled={},
            temperature={},
            error=str(exc),
        )


def run_hardware_doctor(settings: Settings) -> dict[str, object]:
    """두 팔을 읽기만 해서 살핀다. **토크가 걸려 있는지는 시작을 막지 않는다.**

    한때 막았다. 여섯 모터 전부 `Torque_Enable == 0`이어야 텔레옵과 수집이 시작됐다.
    그런데 그 두 경로는 팔이 떨어지지 않도록 `disable_torque_on_disconnect=False`로
    끝나므로 팔로워 토크는 켜진 채 남는다 — 즉 정상적으로 끝낸 세션 다음의 시작은
    **반드시** 거절이었다. 사람은 세션마다 진단을 돌리고 토크를 풀어 팔을 떨어뜨린 뒤
    다시 시작해야 했다.

    더 나쁜 것은 그 절차가 물리적으로 해로웠다는 것이다. 토크를 풀면 팔이 중력으로
    처지고, 다음 `connect()` 안의 `configure()`가 토크를 다시 걸 때 서보는 남아 있던 옛
    목표를 향해 최고 속도로 달린다. 게이트가 요구한 일이 게이트가 막으려던 사고를
    만들고 있었다. 지금은 `follower_start.sync_goal_to_present`가 그 튐을 없앤다.

    게이트의 원래 목적 — 다른 프로세스가 이미 팔을 쥐고 있지 않은가 — 는 지금
    `owner_lock`의 flock이 맡는다(ADR 0003). 그쪽이 훨씬 정확하다: 토크는 지난 세션의
    흔적일 뿐이지만 flock은 지금 살아 있는 소유자를 가리킨다.
    """
    arms = {
        "leader": inspect_arm("leader", settings.leader_port),
        "follower": inspect_arm("follower", settings.follower_port),
    }
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "healthy": all(arm.healthy for arm in arms.values()),
        "safe_for_motion_start": all(arm.safe_for_motion_start for arm in arms.values()),
        "arms": {name: asdict(arm) for name, arm in arms.items()},
    }


def doctor_failure(report: dict[str, object]) -> str:
    """진단이 왜 통과하지 못했는지를 한 줄로. 지나갔으면 빈 문자열.

    "Hardware doctor did not pass"만 적어 보내면 사람은 다음에 무엇을 볼지 알 수 없다.
    어느 팔의 무엇이 문제인지까지 적는다 — 맥 앱은 이 문장을 그대로 표에 올린다.
    """
    arms = report.get("arms")
    if not isinstance(arms, dict):
        return "no arms were inspected"
    problems: list[str] = []
    for name, arm in arms.items():
        if not isinstance(arm, dict) or arm.get("healthy"):
            continue
        if arm.get("error"):
            problems.append(f"{name} {arm['error']}")
            continue
        models = arm.get("models") or {}
        wrong = sorted(
            str(motor) for motor, model in models.items() if model != EXPECTED_MODEL
        )
        if wrong:
            problems.append(f"{name} motors {', '.join(wrong)} did not answer as sts3215")
        voltages = arm.get("voltage_raw") or {}
        low = sorted(
            f"{motor} {value / 10:.1f}V"
            for motor, value in voltages.items()
            if not 90 <= value <= 130
        )
        if low:
            problems.append(f"{name} supply voltage out of range: {', '.join(low)}")
        if not wrong and not low:
            problems.append(f"{name} did not pass")
    return "; ".join(problems)
