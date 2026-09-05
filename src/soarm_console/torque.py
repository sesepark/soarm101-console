from __future__ import annotations

from typing import Any

from lerobot.motors.feetech import FeetechMotorsBus

from .config import Settings
from .diagnostics import MOTORS
from .owner_lock import DeviceLockError, DeviceLockSet


class TorqueError(RuntimeError):
    pass


# 토크를 푸는 것은 팔을 떨어뜨리는 일이다. `SAFETY.md`가 토크 해제를 모든 고장의 기본
# 동작으로 삼지 말라고 적어 둔 이유이고, 텔레옵과 수집이 `disable_torque_on_disconnect=False`로
# 도는 이유이기도 하다. 그래서 이 모듈은 **사람이 명시적으로 부를 때만** 하는 한 가지 일만 한다.
#
# 그런데 그 설계 때문에 생기는 막다른 길이 하나 있었다. 이전 세션이 남긴 토크가 켜져 있으면
# 다음 텔레옵이 `safe_for_motion_start`에서 거절되는데, 콘솔에는 그것을 풀 방법이 없었다.
# 가상 리더의 `/api/vleader/torque/release`는 가상 리더가 돌고 있을 때만 쓸 수 있다.
# 이 모듈이 그 자리를 채운다.
ROLES = ("leader", "follower")


def _port(settings: Settings, role: str) -> str:
    if role == "leader":
        return settings.leader_port
    if role == "follower":
        return settings.follower_port
    raise TorqueError("Unknown arm")


def release(settings: Settings, role: str) -> dict[str, Any]:
    """한 팔의 토크를 푼다.

    푼 뒤의 상태를 모터에서 다시 읽어 돌려준다. 껐다고 말하는 것과 꺼진 것을 확인하는 것은
    다르고, 팔이 떨어지는 동작에서는 후자여야 한다.
    """
    if role not in ROLES:
        raise TorqueError("Unknown arm")
    port = _port(settings, role)
    try:
        with DeviceLockSet.acquire([port], "torque-release"):
            bus = FeetechMotorsBus(port=port, motors=MOTORS)
            bus.connect(handshake=False)
            bus.set_baudrate(1_000_000)
            try:
                bus.disable_torque()
                after = {
                    name: bus.read("Torque_Enable", name, normalize=False, num_retry=2)
                    for name in MOTORS
                }
            finally:
                # 끊으면서 토크를 건드리지 않는다. 방금 푼 것을 다시 걸 이유도, 이미 꺼진
                # 것을 또 끌 이유도 없다.
                bus.disconnect(disable_torque=False)
    except DeviceLockError as exc:
        raise TorqueError(
            "Another process owns the arm; stop the running mode first"
        ) from exc
    except TorqueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 팔 이름과 함께 올라가야 한다
        raise TorqueError(f"Could not release torque on the {role} arm: {exc}") from exc

    still_on = sorted(name for name, value in after.items() if value)
    return {
        "arm": role,
        "released": not still_on,
        "torque_enabled": after,
        "still_enabled": still_on,
    }
