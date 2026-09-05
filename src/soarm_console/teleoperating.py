"""물리 리더 텔레옵. `lerobot-teleoperate` 바이너리 대신 이 모듈이 자식으로 돈다.

바꾼 이유는 두 가지다. **팔로워가 붙는 순간 튀지 않게** 하려면 토크가 켜지기 전에
`Goal_Position`을 지금 자리로 옮겨야 하고, **루프의 첫 틱에 뛰지 않게** 하려면 두 팔의
자세를 먼저 맞춰야 한다. 둘 다 CLI 플래그로 부탁할 수 있는 일이 아니다.

`recording.py`와 같은 모양으로 만든다 — 설정을 파이썬으로 짓고, LeRobot의 클래스와
루프를 그대로 쓰되 그 앞뒤에만 손을 댄다. 루프 자체(`lerobot_teleoperate.teleop_loop`)와
처리 파이프라인은 upstream(`lerobot_teleoperate.teleoperate`)이 만드는 방식을 그대로
따른다. 여기서 다시 쓰는 순간 텔레옵과 수집이 서로 다른 것을 뜻하게 된다.
"""

from __future__ import annotations

from contextlib import nullcontext

from lerobot.processor import make_default_processors
from lerobot.robots.so_follower import SO101FollowerConfig, SOFollower
from lerobot.scripts import lerobot_teleoperate
from lerobot.teleoperators.so_leader import SO101LeaderConfig, SOLeader

from .calibration import validate_calibration
from .config import Settings
from .follower_start import align_follower_to_leader, install_safe_follower_start
from .owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover


#: LeRobot의 기본 텔레옵 주기는 60Hz지만, 수집이 30Hz로 찍으므로 여기도 30으로 둔다.
#: 두 경로에서 팔이 다르게 움직이면 "텔레옵으로 해 보고 그대로 찍는다"가 성립하지 않는다.
FPS = 30


class TeleoperationError(RuntimeError):
    pass


def build_robot_config(settings: Settings) -> SO101FollowerConfig:
    """텔레옵이 쓰는 팔로워 설정. 수집(`recording.build_record_config`)과 같은 값이다.

    카메라는 열지 않는다 — 텔레옵은 기록하지 않고, 콘솔의 프리뷰가 이미 두 대를 쥐고 있다.
    """
    return SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        cameras={},
        max_relative_target=settings.effective_max_relative_target,
        # 종료나 예외가 곧 torque-off가 되면 팔이 떨어진다. 해제는 사람이 팔을 받친
        # 상태에서 별도 절차로만 한다.
        disable_torque_on_disconnect=False,
    )


def build_teleop_config(settings: Settings) -> SO101LeaderConfig:
    return SO101LeaderConfig(port=settings.leader_port, id=settings.leader_id)


def connect_without_prompting(device, role: str) -> None:
    """붙되 사람에게 묻지 않는다.

    `connect()`의 기본값은 모터에 적힌 calibration이 파일과 다를 때 `calibrate()`를
    부르고, 그 안의 첫 줄이 `input()`이다. 이 프로세스에는 tty가 없으므로 그 자리는 곧
    `EOFError`이고, 수집·텔레옵이 이유 없이 죽는 길이었다. 사람이 ENTER를 눌렀을 때
    일어나는 일 — 파일의 값을 모터에 쓰는 것 — 을 직접 한다.

    `configure()`를 한 번 더 부르는 이유는 순서 때문이다. `connect(calibrate=False)`는
    calibration을 쓰기 전에 이미 `configure()`를 돌렸고, 그 안에서 토크가 켜졌다.
    새 calibration을 모터에 적은 뒤 다시 부르면 우리 `configure()`가 그 앞에서
    `Goal_Position`을 지금 자리로 옮기므로, 팔은 선 자리에서 뻣뻣해진 채로 남는다.
    """
    device.connect(calibrate=False)
    if device.bus.is_calibrated:
        return
    if not device.calibration:
        raise TeleoperationError(
            f"{role} calibration file is missing; run scripts/calibrate_{role}.sh"
        )
    device.bus.write_calibration(device.calibration)
    device.configure()


def run(settings: Settings) -> None:
    """붙고, 자세를 맞추고, LeRobot의 루프에 들어간다."""
    install_safe_follower_start()

    robot = SOFollower(build_robot_config(settings))
    teleop = SOLeader(build_teleop_config(settings))

    # 리더를 먼저 붙인다. upstream이 그렇게 하고, 팔로워가 붙은 채 오래 놀지 않게 하려는
    # 것이다 — 노는 동안 펌웨어 워치독이 돌 수 있다.
    connect_without_prompting(teleop, "leader")
    try:
        connect_without_prompting(robot, "follower")

        teleop_action_processor, robot_action_processor, robot_observation_processor = (
            make_default_processors()
        )

        # 루프 앞에서 팔로워를 리더의 지금 자세까지 걸어간다. 이것을 하지 않으면 첫 틱이
        # 리더 자세를 그대로 보내고, 두 팔이 다르면 팔로워가 그 차이만큼 한 번에 뛴다.
        align_follower_to_leader(robot, teleop, log=print)

        lerobot_teleoperate.teleop_loop(
            teleop=teleop,
            robot=robot,
            fps=FPS,
            teleop_action_processor=teleop_action_processor,
            robot_action_processor=robot_action_processor,
            robot_observation_processor=robot_observation_processor,
            display_data=False,
            duration=None,
        )
    except KeyboardInterrupt:
        # 멈추는 것과 힘을 놓는 것은 다르다. `disable_torque_on_disconnect=False`이므로
        # 아래의 `disconnect()`에서 팔은 마지막 자세를 유지한 채 선다.
        pass
    finally:
        if robot.is_connected:
            robot.disconnect()
        if teleop.is_connected:
            teleop.disconnect()


def main() -> None:
    settings = Settings()
    if not settings.motion_enabled:
        raise SystemExit("Refusing to teleoperate: SOARM_ENABLE_MOTION=1 is required")
    for role, path in (
        ("leader", settings.leader_calibration),
        ("follower", settings.follower_calibration),
    ):
        error = validate_calibration(path)
        if error:
            raise SystemExit(f"Refusing to teleoperate: invalid {role} calibration: {error}")
        if not path.exists():
            raise SystemExit(f"Refusing to teleoperate: missing calibration file: {path}")

    devices = [settings.leader_port, settings.follower_port]
    try:
        lock_context = (
            nullcontext()
            if inherited_locks_cover(devices)
            else DeviceLockSet.acquire(devices, "physical-leader-teleop")
        )
    except DeviceLockError as exc:
        raise SystemExit(f"Refusing to teleoperate: {exc}") from exc

    with lock_context:
        run(settings)


if __name__ == "__main__":
    main()
