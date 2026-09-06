from __future__ import annotations

import importlib
import json
import os
import time
from math import isfinite
from pathlib import Path

from .config import Settings
from .calibration import validate_calibration
from .models import describe_model, model_dir
from .owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover


RUNTIME_DIR = Path(__file__).parents[2] / "runtime/policy"
STATUS_PATH = RUNTIME_DIR / "status.json"


def _write_status(**values: object) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({**values, "updated_at": time.time()}), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def build_rollout_config(
    settings: Settings,
    run: str,
    step: str,
    task: str,
    fps: float,
    max_seconds: float,
):
    """Construct (but do not execute) the LeRobot rollout configuration."""
    from lerobot.cameras.opencv import OpenCVCameraConfig
    from lerobot.configs import PreTrainedConfig
    from lerobot.robots.so_follower import SO101FollowerConfig
    from lerobot.rollout.configs import BaseStrategyConfig, RolloutConfig
    from lerobot.rollout.inference import RTCInferenceConfig

    model = describe_model(run, step)
    if not model["runnable"]:
        raise ValueError("; ".join(model["problems"]))
    pretrained = model_dir(run, step, must_exist=True) / "pretrained_model"
    policy_type = str(model["policy"])
    importlib.import_module(f"lerobot.policies.{policy_type}.configuration_{policy_type}")
    policy = PreTrainedConfig.from_pretrained(pretrained)
    policy.pretrained_path = str(pretrained)
    cameras = {
        "scene": OpenCVCameraConfig(
            Path(settings.scene_camera), fps=int(fps), width=640, height=480, fourcc="MJPG"
        ),
        "wrist": OpenCVCameraConfig(
            Path(settings.wrist_camera), fps=int(fps), width=640, height=480, fourcc="MJPG"
        ),
    }
    robot = SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        cameras=cameras,
        max_relative_target=settings.policy_max_relative_target,
        disable_torque_on_disconnect=False,
    )
    rename_map = {
        f"observation.images.{role}": feature
        for feature, role in model["camera_map"].items()
    }
    return RolloutConfig(
        robot=robot,
        policy=policy,
        strategy=BaseStrategyConfig(),
        inference=RTCInferenceConfig(),
        fps=fps,
        duration=max_seconds,
        task=task.strip(),
        rename_map=rename_map,
        return_to_initial_position=True,
        display_data=False,
        play_sounds=False,
    )


def main() -> None:
    settings = Settings()
    run = os.getenv("SOARM_POLICY_RUN", "")
    step = os.getenv("SOARM_POLICY_STEP", "")
    task = os.getenv("SOARM_POLICY_TASK", "")
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
        config = build_rollout_config(settings, run, step, task, fps, max_seconds)
        _write_status(phase="starting", run=run, step=step, task=task)
        try:
            lerobot_rollout.rollout(config)
            _write_status(phase="complete", run=run, step=step, task=task)
        except BaseException as exc:
            _write_status(phase="error", run=run, step=step, task=task, error=str(exc))
            raise


if __name__ == "__main__":
    main()
