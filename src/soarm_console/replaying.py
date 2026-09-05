from __future__ import annotations

import json
import os
import threading
import time
from contextlib import nullcontext, suppress
from math import isfinite
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from .datasets import episode_actions
from .diagnostics import MOTORS
from .owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover


RUNTIME_DIR = Path(__file__).parents[2] / "runtime/replay"
CONTROL_PATH = RUNTIME_DIR / "control.json"
STATUS_PATH = RUNTIME_DIR / "status.json"
_STATUS_LOCK = threading.Lock()

#: 재생 배율. 사람이 고를 수 있는 값은 이 셋뿐이고, 기본은 절반이다 — 처음 보는 재생은
#: 느린 편이 낫다. 빠른 쪽은 한 번 보고 나서 고르면 된다.
SPEEDS = (0.25, 0.5, 1.0)
DEFAULT_SPEED = 0.5

#: 정렬 단계. 팔이 지금 서 있는 자리에서 에피소드의 첫 action까지 **관절 공간에서**
#: 보간해 간다. 양 끝이 이미 관절 각도이므로 IK도 경로 계획도 풀 것이 없다 — IK는
#: "이 손끝 자세를 만들려면 관절이 얼마여야 하나"를 푸는 도구이고, 그 답은 이미
#: 데이터셋 안에 들어 있다.
#:
#: MoveIt이 줄 수 있는 것은 보간이 아니라 **충돌 회피 경로 계획**이다. 책상 위에 물건을
#: 두고 그 위를 지나야 하는 날 이 서버에 ROS를 들이는 값을 다시 계산한다. 지금은 아래
#: 60도 제한과 느린 속도, 그리고 옆에 있는 사람이 그 자리를 대신한다.
ALIGN_HZ = 30.0
ALIGN_DEGREES_PER_SECOND = 20.0
ALIGN_PERCENT_PER_SECOND = 25.0
ALIGN_MIN_SECONDS = 2.0
ALIGN_MAX_SECONDS = 8.0

#: 이만큼 떨어져 있으면 **시작하지 않는다.** 관절 하나라도 이 거리를 넘는다는 것은 팔이
#: 그 에피소드와 다른 세계에 있다는 뜻이고, 그때 필요한 것은 느린 보간이 아니라 사람이
#: 팔을 보고 판단하는 일이다.
ALIGN_REFUSE_DISTANCE = 60.0

#: 각도가 아니라 퍼센트로 정규화되는 관절. LeRobot `SOFollower`가 집게만 `RANGE_0_100`을
#: 쓴다(나머지는 `DEGREES`). 데이터셋에 적힌 숫자도 같은 단위다.
PERCENT_JOINTS = frozenset({"gripper"})

#: s-curve(smoothstep)의 최고 속도는 평균 속도의 1.5배다(도함수 `6s(1-s)`의 꼭짓점).
#: 그래서 이동 시간을 "거리 / 20도" 그대로 잡으면 중간에 30°/s가 나온다. 20이라는 숫자가
#: **팔이 내는 가장 빠른 순간**을 뜻하도록 이 배수를 이동 시간에 곱한다 — 상한은 평균이
#: 아니라 첨두에 걸려야 뜻이 있다.
SCURVE_PEAK_FACTOR = 1.5

#: 상태 파일을 다시 쓰는 간격. 제어 루프 안에서 매 틱 파일을 쓰면 그 자체가 지터가 된다.
STATUS_PUBLISH_S = 0.1


class ReplayError(RuntimeError):
    pass


def _write_status(**updates: object) -> None:
    """상태를 원자적으로 갱신한다.

    `recording.py`의 같은 함수와 나란히 둔다. 재생은 자기 `runtime/replay`만 쓰고
    수집의 상태를 건드리지 않는다 — 두 모드는 같은 모양이되 같은 파일이 아니다.
    """
    with _STATUS_LOCK:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        current: dict[str, object] = {}
        try:
            current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        current.update(updates, updated_at=time.time())
        temporary = STATUS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        os.replace(temporary, STATUS_PATH)


# MARK: 순수 함수 — 팔 없이 시험한다


def unit_of(joint: str) -> str:
    return "%" if joint in PERCENT_JOINTS else "°"


def speed_limit_of(joint: str) -> float:
    return ALIGN_PERCENT_PER_SECOND if joint in PERCENT_JOINTS else ALIGN_DEGREES_PER_SECOND


def smoothstep(fraction: float) -> float:
    """3s² - 2s³. 양 끝의 속도가 0이다.

    선형 보간이 아닌 이유가 이것이다. 선형이면 출발하는 순간과 멈추는 순간에 속도가
    계단으로 꺾이고, 그 두 지점이 팔이 가장 크게 흔들리는 자리다.
    """
    step = min(1.0, max(0.0, fraction))
    return step * step * (3.0 - 2.0 * step)


def joint_distances(start: dict[str, float], goal: dict[str, float]) -> dict[str, float]:
    if set(start) != set(goal):
        raise ReplayError(
            "The episode's joints do not match the arm's joints: "
            f"episode {sorted(goal)}, arm {sorted(start)}"
        )
    # NaN/Inf가 섞이면 아래의 60도 비교가 조용히 참을 내지 못하고 전부 통과한다
    # (`nan > 60`은 거짓이다). 거리를 재기 전에 재는 대상부터 확인한다.
    unusable = sorted(
        name for name in goal if not isfinite(goal[name]) or not isfinite(start[name])
    )
    if unusable:
        raise ReplayError(f"These joints do not carry a finite number: {unusable}")
    return {name: abs(goal[name] - start[name]) for name in sorted(goal)}


def alignment_seconds(start: dict[str, float], goal: dict[str, float]) -> float:
    """정렬에 쓸 전체 이동 시간.

    가장 오래 걸리는 관절이 정한다. 관절은 초당 20도, 집게는 초당 25%가 상한이고,
    그 상한은 평균이 아니라 첨두에 건다(`SCURVE_PEAK_FACTOR` 설명 참고).
    """
    distances = joint_distances(start, goal)
    needed = [distance / speed_limit_of(name) for name, distance in distances.items()]
    slowest = max(needed, default=0.0) * SCURVE_PEAK_FACTOR
    return min(ALIGN_MAX_SECONDS, max(ALIGN_MIN_SECONDS, slowest))


def alignment_frames(
    start: dict[str, float], goal: dict[str, float], hz: float = ALIGN_HZ
) -> list[dict[str, float]]:
    """정렬 단계가 한 틱에 하나씩 명령할 자세들. 마지막 원소는 정확히 `goal`이다."""
    seconds = alignment_seconds(start, goal)
    steps = max(1, round(seconds * hz))
    frames = []
    for step in range(1, steps + 1):
        weight = smoothstep(step / steps)
        frames.append({name: start[name] + (goal[name] - start[name]) * weight for name in goal})
    return frames


def alignment_refusal(start: dict[str, float], goal: dict[str, float]) -> str | None:
    """정렬을 시작할 수 없으면 그 이유를, 시작해도 되면 `None`을.

    거절 문구는 **어느 관절이 얼마나 떨어져 있는지**를 적는다. "너무 멀다"만 말하면
    사람은 팔을 어느 쪽으로 옮겨야 할지 모른다.
    """
    try:
        distances = joint_distances(start, goal)
    except ReplayError as exc:
        return str(exc)
    far = sorted(
        ((name, value) for name, value in distances.items() if value > ALIGN_REFUSE_DISTANCE),
        key=lambda item: -item[1],
    )
    if not far:
        return None
    detail = ", ".join(f"{name} {value:.1f}{unit_of(name)}" for name, value in far)
    return (
        "The arm is too far from where this episode starts to align safely "
        f"(limit {ALIGN_REFUSE_DISTANCE:g}° / {ALIGN_REFUSE_DISTANCE:g}%): {detail}. "
        "Move the arm closer to the episode's starting pose, or replay an episode it is already near."
    )


def episode_first_pose(dataset_name: str, episode_index: int) -> dict[str, float]:
    """에피소드가 시작하는 관절 자세. 모터 이름으로 돌려준다(`.pos`를 뗀다).

    시작을 판정하는 60도 검사와 실제로 흘려보내는 값이 같은 코드(`datasets.episode_actions`)
    에서 나와야 한다. 판정과 실행이 서로 다른 곳에서 숫자를 읽으면, 검사를 통과한 것과
    팔에 들어가는 것이 달라질 수 있다.
    """
    episode = episode_actions(dataset_name, episode_index)
    joints = [str(name).removesuffix(".pos") for name in episode["joints"]]
    first = [float(value) for value in episode["action"][0]]
    return dict(zip(joints, first, strict=True))


# MARK: 하드웨어


def build_robot_config(settings: Settings):
    """재생이 쓰는 팔로워 설정.

    `use_degrees`를 여기서 적지 않는 것이 중요하다. 데이터셋에 적힌 숫자의 단위는 그것을
    **찍을 때** 쓴 설정이 정했고(`recording.py`도 이 값을 적지 않는다), 두 곳이 같은
    LeRobot 기본값을 쓰는 동안에만 재생이 녹화와 같은 자세를 뜻한다. 한쪽에만 명시하면
    기본값이 바뀌는 날 두 경로가 조용히 어긋난다.
    """
    from lerobot.robots.so_follower import SO101FollowerConfig

    return SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        # 재생 중에 카메라를 열지 않는다. 재생은 팔을 움직이는 일이고 영상은 이미 있다.
        cameras={},
        # 한 틱에 명령할 수 있는 각도의 상한. 이것이 막혔을 때 서보가 내는 힘의 상한이기도
        # 하다. `effective_max_relative_target`(걸릴 수 없는 값을 `None`으로 바꾸는 것)을
        # 쓰지 않고 설정값을 그대로 넘긴다 — 재생은 카메라를 열지 않으므로 LeRobot이
        # 스텝마다 하는 `Present_Position` 되읽기를 감당할 여유가 있고, 상한이 실제로
        # 걸리는 값으로 낮춰졌을 때 그 자리가 비어 있으면 안 된다.
        max_relative_target=settings.max_relative_target,
        # 재생이 끝나거나 예외가 나면 `finally: robot.disconnect()`가 돈다. 그것이
        # torque-off가 되면 팔이 중력으로 주저앉는다. 해제는 사람이 팔을 받친 뒤
        # 명시적으로만 한다(`SAFETY.md`).
        disable_torque_on_disconnect=False,
    )


def _load_follower_calibration(settings: Settings):
    import draccus
    from lerobot.motors import MotorCalibration

    with settings.follower_calibration.open(encoding="utf-8") as handle:
        return draccus.load(dict[str, MotorCalibration], handle)


def present_position(settings: Settings, *, acquire_owner_lock: bool = True) -> dict[str, float]:
    """팔로워가 **지금** 서 있는 자세. 읽기만 하고 토크도 목표도 건드리지 않는다.

    60도 검사가 이 값을 본다. 검사는 시작 요청을 받은 콘솔 프로세스에서 하고 재생은
    자식 프로세스가 하므로, 그 사이에 팔이 움직일 틈이 이론상 남는다. 그래서 자식도
    붙은 뒤에 같은 검사를 한 번 더 한다 — 400을 내는 것은 여기지만, 팔을 실제로 지키는
    것은 저쪽이다.

    `diagnostics.MOTORS`를 쓰는 이유는 그 표의 정규화 방식이 `SOFollower`와 같기
    때문이다(몸통은 `DEGREES`, 집게는 `RANGE_0_100`). 데이터셋의 숫자와 같은 단위여야
    거리를 뺄 수 있다.
    """
    from lerobot.motors.feetech import FeetechMotorsBus

    error = validate_calibration(settings.follower_calibration)
    if error:
        raise ReplayError(error)
    calibration = _load_follower_calibration(settings)

    def read() -> dict[str, float]:
        bus = FeetechMotorsBus(port=settings.follower_port, motors=MOTORS, calibration=calibration)
        try:
            bus.connect(handshake=False)
            bus.set_baudrate(1_000_000)
            if not bus.is_calibrated:
                raise ReplayError(
                    "The follower's motors do not carry the calibration in "
                    f"{settings.follower_calibration}; positions read now would not mean "
                    "what the dataset means"
                )
            return {
                name: float(value)
                for name, value in bus.sync_read("Present_Position", num_retry=2).items()
            }
        finally:
            if bus.is_connected:
                # 읽으러 왔다가 토크를 끄고 가지 않는다. 팔이 무언가를 들고 있을 수 있다.
                bus.disconnect(disable_torque=False)

    try:
        if not acquire_owner_lock:
            return read()
        with DeviceLockSet.acquire([settings.follower_port], "replay-preflight"):
            return read()
    except DeviceLockError as exc:
        raise ReplayError(f"Another process owns the follower arm: {exc}") from exc
    except ReplayError:
        raise
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 "지금 자세를 모른다"로 올라가야 한다
        raise ReplayError(f"Could not read the follower's present position: {exc}") from exc


def _connect(settings: Settings):
    """팔로워에 붙는다. **붙는 것 자체가 팔을 움직이지 않게** 한다.

    `SOFollower.connect()`를 그대로 부르면 그 안의 `configure()`가 토크를 켜는데, 서보의
    `Goal_Position`에는 지난번에 쓴 값이 그대로 남아 있다. 토크가 걸리는 순간 서보는 그
    값을 향해 **최고 속도로** 달린다. 첫 프레임으로 뛰지 않으려고 정렬 단계를 만들어 놓고
    그 앞에서 팔이 다른 곳으로 뛰면 아무 소용이 없다.

    그래서 `connect()` 대신 그 안에서 하는 일을 직접 하고, 토크가 켜지기 전에 지금 자세를
    목표로 먼저 써 넣는다. 가상 리더 백엔드가 같은 이유로 같은 일을 한다.
    """
    from lerobot.robots.so_follower import SOFollower

    robot = SOFollower(build_robot_config(settings))
    robot.bus.connect()
    try:
        if not robot.bus.is_calibrated:
            if not robot.calibration:
                raise ReplayError(
                    "Follower calibration file is missing; run scripts/calibrate_follower.sh"
                )
            robot.bus.write_calibration(robot.calibration)
        present = robot.bus.sync_read("Present_Position", normalize=False, num_retry=2)
        robot.bus.sync_write("Goal_Position", present, normalize=False, num_retry=2)
        # 여기서 토크가 켜진다 — LeRobot의 동작이다. 위에서 목표를 지금 자리로 박아
        # 두었으므로 팔은 선 자리에서 뻣뻣해진다.
        robot.configure()
    except BaseException:
        with suppress(Exception):
            robot.bus.disconnect(disable_torque=False)
        raise
    return robot


class _StopListener:
    """`runtime/replay/control.json`에 `stop`이 오면 그 자리에서 루프를 벗어나게 한다.

    `recording.py`의 `_GuiControlListener`와 같은 방식이다. 멈춘 뒤에도 토크는 걸어 둔 채
    현재 자세를 유지한다 — 멈추는 것과 힘을 놓는 것은 다른 일이고, 팔이 든 것을
    떨어뜨리면 안 된다.
    """

    def __init__(self) -> None:
        self._stopped = threading.Event()
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def _watch(self) -> None:
        while not self._done.is_set():
            try:
                payload = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
                CONTROL_PATH.unlink(missing_ok=True)
                if str(payload.get("key", "")) == "stop":
                    self._stopped.set()
                    return
            except FileNotFoundError:
                pass
            except (json.JSONDecodeError, OSError):
                CONTROL_PATH.unlink(missing_ok=True)
            self._done.wait(0.05)

    def stop(self) -> None:
        self._done.set()
        self.thread.join(timeout=0.5)


def _align(robot, start: dict[str, float], goal: dict[str, float], listener: _StopListener) -> bool:
    """지금 자세에서 에피소드의 첫 action까지 천천히 간다. 멈춰서 끝났으면 참."""
    from lerobot.utils.robot_utils import precise_sleep

    # 두 값이 같은 곳에서 나와야 한다. 보간이 만든 프레임 수와 틱 간격이 어긋나면
    # 정렬은 계획한 시간이 아니라 다른 시간에 걸쳐 도착한다.
    frames = alignment_frames(start, goal, ALIGN_HZ)
    period = 1.0 / ALIGN_HZ
    total = len(frames)
    _write_status(phase="aligning", frame=0, aligning_seconds_left=round(total * period, 2))
    published = 0.0
    for index, frame in enumerate(frames, start=1):
        if listener.stopped:
            return True
        tick = time.perf_counter()
        robot.send_action({f"{name}.pos": value for name, value in frame.items()})
        now = time.perf_counter()
        if now - published >= STATUS_PUBLISH_S:
            published = now
            _write_status(aligning_seconds_left=round((total - index) * period, 2))
        precise_sleep(max(period - (time.perf_counter() - tick), 0.0))
    _write_status(aligning_seconds_left=0.0)
    return False


def _replay(
    robot,
    joints: list[str],
    frames: list[list[float]],
    fps: float,
    speed: float,
    listener: _StopListener,
) -> bool:
    """`lerobot_replay`의 루프. 멈춰서 끝났으면 참.

    배율은 `precise_sleep`의 간격에만 곱한다. action 값 자체는 건드리지 않는다 — 느리게
    본다는 것은 같은 자세를 더 오래 걸려 지난다는 뜻이지, 다른 자세를 지난다는 뜻이 아니다.
    """
    from lerobot.processor import make_default_robot_action_processor
    from lerobot.utils.robot_utils import precise_sleep

    robot_action_processor = make_default_robot_action_processor()
    period = 1.0 / (fps * speed)
    total = len(frames)
    _write_status(phase="replaying", frame=0, total_frames=total, aligning_seconds_left=0.0)
    published = 0.0
    for index, values in enumerate(frames):
        if listener.stopped:
            _write_status(frame=index)
            return True
        start_episode_t = time.perf_counter()

        action = {f"{name}.pos": float(value) for name, value in zip(joints, values, strict=True)}
        robot_obs = robot.get_observation()
        processed_action = robot_action_processor((action, robot_obs))
        _ = robot.send_action(processed_action)

        now = time.perf_counter()
        if now - published >= STATUS_PUBLISH_S:
            published = now
            _write_status(frame=index + 1)

        dt_s = time.perf_counter() - start_episode_t
        precise_sleep(max(period - dt_s, 0.0))
    _write_status(frame=total)
    return False


def run(settings: Settings, dataset_name: str, episode_index: int, speed: float) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.unlink(missing_ok=True)
    episode = episode_actions(dataset_name, episode_index)
    joints = [str(name).removesuffix(".pos") for name in episode["joints"]]
    frames = [[float(value) for value in row] for row in episode["action"]]
    fps = float(episode["fps"]) or ALIGN_HZ
    _write_status(
        phase="starting",
        dataset=dataset_name,
        episode=episode_index,
        speed=speed,
        frame=0,
        total_frames=len(frames),
        aligning_seconds_left=None,
        error=None,
    )

    listener = _StopListener()
    try:
        robot = _connect(settings)
    except BaseException as exc:
        listener.stop()
        _write_status(phase="error", error=str(exc))
        raise
    try:
        goal = dict(zip(joints, frames[0], strict=True))
        start = {
            name: float(value)
            for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
        }
        # 콘솔이 이미 한 검사를 여기서 한 번 더 한다. 저쪽은 400을 내기 위한 것이고,
        # 이쪽은 팔이 실제로 움직이기 직전의 마지막 자리다.
        refusal = alignment_refusal(start, goal)
        if refusal:
            raise ReplayError(refusal)
        stopped = _align(robot, start, goal, listener)
        if not stopped:
            stopped = _replay(robot, joints, frames, fps, speed, listener)
        _write_status(phase="stopped" if stopped else "complete", aligning_seconds_left=0.0)
    except BaseException as exc:
        _write_status(phase="error", error=str(exc))
        raise
    finally:
        listener.stop()
        # `disable_torque_on_disconnect=False`이므로 여기서 팔은 힘을 놓지 않는다.
        robot.disconnect()


def main() -> None:
    settings = Settings()
    if not settings.motion_enabled:
        raise SystemExit("Refusing to replay: SOARM_ENABLE_MOTION=1 is required")
    error = validate_calibration(settings.follower_calibration)
    if error:
        raise SystemExit(f"Refusing to replay: invalid follower calibration: {error}")
    dataset_name = os.getenv("SOARM_REPLAY_DATASET", "")
    if not dataset_name:
        raise SystemExit("Refusing to replay: SOARM_REPLAY_DATASET is required")
    try:
        episode_index = int(os.getenv("SOARM_REPLAY_EPISODE", "0"))
    except ValueError as exc:
        raise SystemExit(f"Refusing to replay: SOARM_REPLAY_EPISODE is not a number: {exc}") from exc
    try:
        speed = float(os.getenv("SOARM_REPLAY_SPEED", str(DEFAULT_SPEED)))
    except ValueError as exc:
        raise SystemExit(f"Refusing to replay: SOARM_REPLAY_SPEED is not a number: {exc}") from exc
    if speed not in SPEEDS:
        raise SystemExit(f"Refusing to replay: speed must be one of {list(SPEEDS)}")

    try:
        lock_context = (
            nullcontext()
            if inherited_locks_cover([settings.follower_port])
            else DeviceLockSet.acquire([settings.follower_port], "replay")
        )
    except DeviceLockError as exc:
        raise SystemExit(f"Refusing to replay: {exc}") from exc

    with lock_context:
        run(settings, dataset_name, episode_index, speed)


if __name__ == "__main__":
    main()
