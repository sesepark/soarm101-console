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
    safe_for_motion_start: bool
    models: dict[int, int | None]
    firmware: dict[int, str]
    positions_raw: dict[str, int]
    voltage_raw: dict[str, int]
    torque_enabled: dict[str, int]
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
                role, port, False, False, models, {}, {}, {}, {}, f"Missing motor IDs: {missing}"
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
        healthy = (
            all(model == EXPECTED_MODEL for model in models.values())
            and all(90 <= voltage <= 130 for voltage in voltages.values())
        )
        return ArmDiagnostic(
            role=role,
            port=port,
            healthy=healthy,
            safe_for_motion_start=healthy and all(value == 0 for value in torque.values()),
            models=models,
            firmware=firmware,
            positions_raw=positions,
            voltage_raw=voltages,
            torque_enabled=torque,
        )
    except Exception as exc:
        return ArmDiagnostic(role, port, False, False, {}, {}, {}, {}, {}, str(exc))
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
            error=str(exc),
        )


def run_hardware_doctor(settings: Settings) -> dict[str, object]:
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
