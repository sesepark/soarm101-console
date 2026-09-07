from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import signal
import threading
import time
from queue import Empty
from math import isfinite
from pathlib import Path

from .config import Settings
from .calibration import validate_calibration
from .models import describe_model, model_dir
from .owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover
from .replaying import (
    ALIGN_FOLLOW_ERROR_DEG,
    ALIGN_FOLLOW_ERROR_PERCENT,
    REPLAY_ALIGNMENT,
    ReplayError,
    _connect,
    align,
    present_position,
)
from .vleader.spec import JOINT_ORDER, SpecError, load_joint_specs


RUNTIME_DIR = Path(__file__).parents[2] / "runtime/policy"
STATUS_PATH = RUNTIME_DIR / "status.json"
ALIGNMENT_ARRIVAL_TIMEOUT_S = 30.0
ALIGNMENT_SETTLE_S = 2.0
# 이 팔에서 잰 정상상태 오차 최대 1.06도의 약 3배이며, 8도 follow-error 경계보다
# 충분히 안쪽이다. 집게도 같은 여유를 주어 도착 판정 단위를 일관되게 유지한다.
ALIGNMENT_TOLERANCE_DEG = 3.0
ALIGNMENT_TOLERANCE_PERCENT = 3.0
ALIGNMENT_SETTLE_POLL_S = 0.1

logger = logging.getLogger(__name__)


def _alignment_distances(
    current: dict[str, float], home: dict[str, float]
) -> dict[str, float]:
    return {name: abs(home[name] - current[name]) for name in home}


def _outside_arrival_tolerance(distances: dict[str, float]) -> dict[str, float]:
    return {
        name: distance
        for name, distance in distances.items()
        if distance
        > (ALIGNMENT_TOLERANCE_PERCENT if name == "gripper" else ALIGNMENT_TOLERANCE_DEG)
    }


def _outside_follow_error(distances: dict[str, float]) -> dict[str, float]:
    return {
        name: distance
        for name, distance in distances.items()
        if distance
        > (ALIGN_FOLLOW_ERROR_PERCENT if name == "gripper" else ALIGN_FOLLOW_ERROR_DEG)
    }


def validate_home(settings: Settings, home: dict[str, float] | None) -> dict[str, float] | None:
    """요청 자세를 팔로워 calibration의 좌표계와 절대 범위에 맞춰 검증한다."""
    if home is None:
        return None
    expected = set(JOINT_ORDER)
    received = set(home)
    if received != expected:
        unknown = sorted(received - expected)
        missing = sorted(expected - received)
        details = []
        if unknown:
            details.append(f"unknown joints: {unknown}")
        if missing:
            details.append(f"missing joints: {missing}")
        raise ValueError("home joints do not match the follower (" + "; ".join(details) + ")")
    try:
        specs = load_joint_specs(settings.follower_calibration)
    except SpecError as exc:
        raise ValueError(str(exc)) from exc
    result: dict[str, float] = {}
    for spec in specs:
        value = float(home[spec.name])
        if not isfinite(value):
            raise ValueError(f"home {spec.name} must be a finite number (received {value})")
        if not spec.contains(value):
            unit = "%" if spec.unit == "percent" else "°"
            raise ValueError(
                f"home {spec.name} {value:g}{unit} is outside calibration range "
                f"{spec.minimum:.1f}~{spec.maximum:.1f}{unit}"
            )
        result[spec.name] = value
    return result


def align_home(
    settings: Settings,
    home: dict[str, float] | None,
    stop_requested: threading.Event,
    *,
    phase: str = "aligning",
) -> bool:
    """정책 기준 자세로 재생과 똑같이 정렬하고, 실제 도착할 때까지 확인한다."""
    if home is None:
        return False
    robot = _connect(settings)
    try:
        current = {
            name: float(value)
            for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
        }
        deadline = time.monotonic() + ALIGNMENT_ARRIVAL_TIMEOUT_S
        published = 0.0

        def publish(index: int, total: int, seconds_left: float) -> None:
            nonlocal published
            now = time.perf_counter()
            if index == 0 or now - published >= 0.1 or index == total:
                published = now
                _write_status(
                    phase=phase,
                    home=home,
                    frame=index,
                    total_frames=total,
                    aligning_seconds_left=seconds_left,
                    error=None,
                )

        while True:
            def should_stop() -> bool:
                return stop_requested.is_set() or time.monotonic() >= deadline

            stopped = align(
                robot,
                current,
                home,
                should_stop=should_stop,
                limits=REPLAY_ALIGNMENT,
                progress=publish,
            )
            if stopped and stop_requested.is_set():
                return True
            current = {
                name: float(value)
                for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
            }
            if stop_requested.is_set():
                return True
            outside = _outside_arrival_tolerance(_alignment_distances(current, home))
            if stopped:
                if not outside:
                    _write_status(alignment_residual={})
                    return False
                detail = ", ".join(
                    f"{name} {distance:.1f}" for name, distance in sorted(outside.items())
                )
                raise ReplayError(
                    f"Policy {phase} did not reach its target within "
                    f"{ALIGNMENT_ARRIVAL_TIMEOUT_S:g}s (remaining: {detail})"
                )

            if time.monotonic() >= deadline:
                if not outside:
                    _write_status(alignment_residual={})
                    return False
                detail = ", ".join(
                    f"{name} {distance:.1f}" for name, distance in sorted(outside.items())
                )
                raise ReplayError(
                    f"Policy {phase} did not reach its target within "
                    f"{ALIGNMENT_ARRIVAL_TIMEOUT_S:g}s (remaining: {detail})"
                )

            # align()의 마지막 프레임이 반환 직전에 목표를 보냈더라도, 목표를 다시 고정한
            # 뒤 서보가 실제로 정착할 시간을 준다. send_action이 없는 최소 Robot 대역은
            # 목표를 고정할 수 없으므로 종전처럼 측정 위치에서 다시 align한다.
            send_action = getattr(robot, "send_action", None)
            if send_action is None:
                if not outside:
                    _write_status(alignment_residual={})
                    return False
                continue
            send_action({f"{name}.pos": value for name, value in home.items()})

            settle_deadline = min(deadline, time.monotonic() + ALIGNMENT_SETTLE_S)
            while True:
                outside = _outside_arrival_tolerance(_alignment_distances(current, home))
                if not outside:
                    _write_status(alignment_residual={})
                    return False
                if stop_requested.is_set():
                    return True
                now = time.monotonic()
                if now >= settle_deadline:
                    break
                if stop_requested.wait(min(ALIGNMENT_SETTLE_POLL_S, settle_deadline - now)):
                    return True
                current = {
                    name: float(value)
                    for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
                }

            distances = _alignment_distances(current, home)
            outside = _outside_arrival_tolerance(distances)
            if not outside:
                _write_status(alignment_residual={})
                return False
            too_far = _outside_follow_error(distances)
            if not too_far:
                residual = {name: round(distance, 3) for name, distance in sorted(outside.items())}
                _write_status(alignment_residual=residual)
                logger.warning(
                    "Policy %s continuing with settled alignment residual: %s",
                    phase,
                    ", ".join(f"{name} {distance:.1f}" for name, distance in residual.items()),
                )
                return False

            # 8도/8%를 넘는 실패만 재접근한다. 이는 도착 허용치의 두 배보다도 커서,
            # 정착으로 해결할 작은 오차에 2초짜리 최소 s-curve를 다시 만들지 않는다.
            if time.monotonic() >= deadline:
                detail = ", ".join(
                    f"{name} {distance:.1f}" for name, distance in sorted(outside.items())
                )
                raise ReplayError(
                    f"Policy {phase} did not reach its target within "
                    f"{ALIGNMENT_ARRIVAL_TIMEOUT_S:g}s (remaining: {detail})"
                )
    finally:
        robot.disconnect()


def _write_status(**values: object) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    temporary = STATUS_PATH.with_suffix(".tmp")
    current.update(values, updated_at=time.time())
    temporary.write_text(json.dumps(current), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


#: RTC가 새 청크를 앞 계획에 이어 붙이도록 유도하는 구간의 길이(프레임).
#:
#: 기본값 10으로 두면 팔이 **0.73초마다 툭툭 끊긴다.** 두 숫자가 어긋나기 때문이다.
#: 큐(`ActionQueue._replace_actions_queue`)는 새 청크의 앞 `real_delay`개를 버리는데,
#: 이 기계에서 잰 `real_delay`는 **19프레임(0.63초)**다(2026-09-06 13:14 rollout에서
#: 병합 52회의 중앙값). 유도가 앞 10개에만 걸리면 그 10개는 버려지는 19개 안에 통째로
#: 들어가고, **실제로 실행되는 부분(19번째부터)에는 연속성 제약이 하나도 남지 않는다.**
#: 그래서 0.73초마다 지금 하던 동작과 무관한 계획으로 갈아타게 되고, 위치 제어 서보에서
#: 위치의 불연속은 곧 충격이다.
#:
#: 25를 고른 것은 두 값 사이여서다: **19 < 25 < 30**.
#: - 19보다 커야 버려지는 구간을 덮어서 이음매가 유도 안으로 들어온다.
#: - 30(`RTCInferenceConfig.queue_threshold`)보다는 작아야 한다. 추론은 큐가 30개 밑으로
#:   내려갈 때 시작하면서 그때 남은 것을 앞 계획으로 넘기는데, 그것이 이 값보다 짧으면
#:   `_normalize_prev_actions_length`가 **0으로 채운다.** 정규화된 공간에서 0은 "움직이지
#:   않음"이 아니라 데이터셋의 평균 자세라, 엉뚱한 자리로 유도하게 된다.
RTC_EXECUTION_HORIZON = 25


def _robot_config(settings: Settings, fps: float):
    from lerobot.cameras.opencv import OpenCVCameraConfig
    from lerobot.robots.so_follower import SO101FollowerConfig

    # These names are the dataset names. Remote inference deliberately sends scene/wrist unchanged;
    # the checkpoint's saved preprocessor owns the rename to policy-specific feature names.
    cameras = {
        "scene": OpenCVCameraConfig(
            Path(settings.scene_camera), fps=int(fps), width=640, height=480, fourcc="MJPG"
        ),
        "wrist": OpenCVCameraConfig(
            Path(settings.wrist_camera), fps=int(fps), width=640, height=480, fourcc="MJPG"
        ),
    }
    return SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        cameras=cameras,
        max_relative_target=settings.policy_max_relative_target,
        disable_torque_on_disconnect=False,
    )


def _inference_config(policy_type: str):
    """이 정책에 맞는 추론 방식.

    RTC는 아무 정책이나 쓸 수 있는 것이 아니다. `predict_action_chunk`가 `inference_delay`와
    `prev_chunk_left_over`를 받아야 하고(SmolVLA·pi0 계열은 받고 **ACT는 받지 않는다**),
    받지 않는 정책에 RTC를 걸면 lerobot이 rollout을 시작하기도 전에 `ValueError`를 낸다.
    콘솔은 지금까지 정책 종류와 무관하게 RTC를 걸고 있었으므로, ACT 체크포인트를 팔에
    올리는 길은 **반드시 실패했을 것이다.** 종류를 보고 고른다.
    """
    from lerobot.rollout.inference import RTCInferenceConfig, SyncInferenceConfig

    try:
        from lerobot.policies.factory import get_policy_class
        from lerobot.policies.rtc.configuration_rtc import RTCConfig

        chunk = get_policy_class(policy_type).predict_action_chunk
        inspect.signature(chunk).bind(
            object(), object(), inference_delay=0, prev_chunk_left_over=None
        )
    except (TypeError, ValueError, KeyError, ImportError, AttributeError):
        return SyncInferenceConfig()
    return RTCInferenceConfig(rtc=RTCConfig(execution_horizon=RTC_EXECUTION_HORIZON))


def build_rollout_config(
    settings: Settings,
    run: str,
    step: str,
    task: str,
    fps: float,
    max_seconds: float,
):
    """Construct (but do not execute) the LeRobot rollout configuration."""
    from lerobot.configs import PreTrainedConfig
    from lerobot.rollout.configs import BaseStrategyConfig, RolloutConfig

    model = describe_model(run, step)
    if not model["runnable"]:
        raise ValueError("; ".join(model["problems"]))
    pretrained = model_dir(run, step, must_exist=True) / "pretrained_model"
    policy_type = str(model["policy"])
    importlib.import_module(f"lerobot.policies.{policy_type}.configuration_{policy_type}")
    policy = PreTrainedConfig.from_pretrained(pretrained)
    policy.pretrained_path = str(pretrained)
    robot = _robot_config(settings, fps)
    rename_map = {
        f"observation.images.{role}": feature
        for feature, role in model["camera_map"].items()
    }
    return RolloutConfig(
        robot=robot,
        policy=policy,
        strategy=BaseStrategyConfig(),
        inference=_inference_config(policy_type),
        fps=fps,
        duration=max_seconds,
        task=task.strip(),
        rename_map=rename_map,
        return_to_initial_position=False,
        display_data=False,
        play_sounds=False,
    )


class FailSafeRobotClient:
    """RobotClient adapter that turns the first broken RPC into an immediate hold."""

    def __init__(self, config):
        import grpc
        from lerobot.async_inference.robot_client import RobotClient

        self.client = RobotClient(config)
        self.client.policy_config.rename_map = dict(config.checkpoint_rename_map)
        self.failure: str | None = None
        original_stub = self.client.stub
        owner = self

        class WatchedStub:
            def __getattr__(self, name):
                call = getattr(original_stub, name)

                def watched(*args, **kwargs):
                    try:
                        return call(*args, **kwargs)
                    except grpc.RpcError as exc:
                        owner.abort(f"Remote policy connection failed: {exc.code().name}")
                        raise

                return watched

        self.client.stub = WatchedStub()
        self.client.channel.subscribe(self._channel_state, try_to_connect=True)

    def _channel_state(self, state) -> None:
        import grpc

        if state in {grpc.ChannelConnectivity.TRANSIENT_FAILURE, grpc.ChannelConnectivity.SHUTDOWN}:
            self.abort(f"Remote policy connection became {state.name}")

    def abort(self, reason: str) -> None:
        if self.failure is None:
            self.failure = reason
        self.client.shutdown_event.set()
        with self.client.action_queue_lock:
            while True:
                try:
                    self.client.action_queue.get_nowait()
                except Empty:
                    break

    def run(self, task: str, stop_requested: threading.Event, max_seconds: float) -> None:
        if not self.client.start():
            raise RuntimeError(self.failure or "Could not connect to the remote policy server")
        receiver = threading.Thread(target=self.client.receive_actions, kwargs={"verbose": True}, daemon=True)
        receiver.start()

        def watchdog() -> None:
            if stop_requested.wait(max_seconds):
                self.client.shutdown_event.set()
            else:
                self.client.shutdown_event.set()

        timer = threading.Thread(target=watchdog, daemon=True)
        timer.start()
        try:
            self.client.control_loop(task=task, verbose=True)
        finally:
            self.client.stop()
            receiver.join(timeout=5)
        if self.failure:
            raise RuntimeError(self.failure)


def build_remote_client_config(
    settings: Settings,
    policy_type: str,
    pretrained_path: str,
    task: str,
    fps: float,
    rename_map: dict[str, str],
):
    from lerobot.async_inference.configs import RobotClientConfig

    config = RobotClientConfig(
        policy_type=policy_type,
        pretrained_name_or_path=pretrained_path,
        robot=_robot_config(settings, fps),
        actions_per_chunk=50,
        task=task.strip(),
        server_address=f"127.0.0.1:{settings.remote_policy_port}",
        policy_device="cuda",
        client_device="cpu",
        fps=int(fps),
        chunk_size_threshold=0.5,
        aggregate_fn_name="weighted_average",
    )
    # LeRobot 0.6.1's server always applies the incoming rename override, even when it is empty.
    # Re-send the checkpoint's own saved map so the server does not erase it. The client still emits
    # scene/wrist and performs no local renaming.
    config.checkpoint_rename_map = dict(rename_map)
    return config


def main() -> None:
    settings = Settings()
    run = os.getenv("SOARM_POLICY_RUN", "")
    step = os.getenv("SOARM_POLICY_STEP", "")
    task = os.getenv("SOARM_POLICY_TASK", "")
    raw_home = os.getenv("SOARM_POLICY_HOME", "")
    remote_path = os.getenv("SOARM_REMOTE_POLICY_PATH", "")
    remote_type = os.getenv("SOARM_REMOTE_POLICY_TYPE", "")
    raw_rename_map = os.getenv("SOARM_REMOTE_RENAME_MAP", "")
    try:
        fps = float(os.getenv("SOARM_POLICY_FPS", "30"))
        max_seconds = float(os.getenv("SOARM_POLICY_MAX_SECONDS", "120"))
    except ValueError as exc:
        raise SystemExit(f"Refusing policy rollout: invalid numeric setting: {exc}") from exc
    if not settings.motion_enabled:
        raise SystemExit("Refusing policy rollout: SOARM_ENABLE_MOTION=1 is required")
    if not settings.camera_roles_confirmed:
        raise SystemExit("Refusing policy rollout: SOARM_CAMERA_ROLES_CONFIRMED=1 is required")
    if error := validate_calibration(settings.follower_calibration):
        raise SystemExit(f"Refusing policy rollout: invalid follower calibration: {error}")
    if not task.strip():
        raise SystemExit("Refusing policy rollout: task is required")
    if not isfinite(fps) or not isfinite(max_seconds) or fps <= 0 or max_seconds <= 0:
        raise SystemExit("Refusing policy rollout: fps and max_seconds must be positive")
    if not isfinite(settings.policy_max_relative_target) or settings.policy_max_relative_target <= 0:
        raise SystemExit(
            "Refusing policy rollout: SOARM_POLICY_MAX_RELATIVE_TARGET must be positive and finite"
        )
    try:
        decoded_home = json.loads(raw_home) if raw_home else None
        if decoded_home is not None and not isinstance(decoded_home, dict):
            raise ValueError("home is not an object")
        home = validate_home(settings, decoded_home)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit(f"Refusing policy rollout: invalid home: {exc}") from exc
    devices = [settings.follower_port, settings.scene_camera, settings.wrist_camera]
    try:
        lock_context = (
            DeviceLockSet.acquire(devices, "policy")
            if not inherited_locks_cover(devices)
            else None
        )
    except DeviceLockError as exc:
        raise SystemExit(f"Refusing policy rollout: {exc}") from exc

    from contextlib import nullcontext
    from lerobot.scripts import lerobot_rollout

    with lock_context or nullcontext():
        stop_requested = threading.Event()
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_requested.set())
        phase = "aligning" if home is not None else "running"
        _write_status(
            phase=phase,
            home=home or {},
            run=run,
            step=step,
            task=task,
            error=None,
            alignment_residual={},
        )
        try:
            stopped = align_home(settings, home, stop_requested, phase="aligning")
            if stopped:
                _write_status(phase="aligning", home=home or {}, run=run, step=step, task=task)
                return
            # home이 없으면 LeRobot이 예전에 기억하던 것과 같은 자리를, rollout이 팔을
            # 연결하기 직전에 읽는다. 이미 owner lock을 갖고 있으므로 다른 모드가 사이에
            # 끼어 관절값을 바꿀 수 없다.
            return_home = home or present_position(settings, acquire_owner_lock=False)
            if remote_path:
                try:
                    rename_map = json.loads(raw_rename_map)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid remote checkpoint rename map: {exc}") from exc
                if not isinstance(rename_map, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in rename_map.items()
                ):
                    raise ValueError("Invalid remote checkpoint rename map")
                config = build_remote_client_config(
                    settings, remote_type, remote_path, task, fps, rename_map
                )
            else:
                config = build_rollout_config(settings, run, step, task, fps, max_seconds)
            if stop_requested.is_set():
                return
            phase = "running"
            _write_status(phase=phase, home=home or {}, run=run, step=step, task=task)
            if not remote_path:
                signal.signal(signal.SIGTERM, previous_sigterm)
            remote_failed = False
            try:
                # LeRobot의 ProcessSignalHandler가 running 중 SIGTERM을 받아 rollout을
                # teardown한다. 자체 복귀는 꺼져 있으므로 teardown 뒤 아래로 내려온다.
                if remote_path:
                    FailSafeRobotClient(config).run(task, stop_requested, max_seconds)
                else:
                    lerobot_rollout.rollout(config)
            except BaseException:
                remote_failed = bool(remote_path)
                raise
            finally:
                # A broken remote connection is an emergency hold, not a request for another
                # autonomous movement. Keep the last commanded position and report the failure.
                if not remote_failed:
                    phase = "returning"
                    return_stop_requested = threading.Event()
                    signal.signal(
                        signal.SIGTERM,
                        lambda _signum, _frame: return_stop_requested.set(),
                    )
                    _write_status(
                        phase=phase,
                        home=home or {},
                        run=run,
                        step=step,
                        task=task,
                        alignment_residual={},
                    )
                    try:
                        align_home(
                            settings,
                            return_home,
                            return_stop_requested,
                            phase="returning",
                        )
                    except BaseException as exc:
                        raise RuntimeError(f"Policy return to initial position failed: {exc}") from exc
                    _write_status(phase=phase, home=home or {}, run=run, step=step, task=task)
        except BaseException as exc:
            _write_status(
                phase=phase,
                home=home or {},
                run=run,
                step=step,
                task=task,
                error=str(exc),
            )
            raise
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
