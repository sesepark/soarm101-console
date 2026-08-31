from __future__ import annotations

import os
import re
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot.configs.dataset import DatasetRecordConfig
from lerobot.robots.so_follower import SO101FollowerConfig
from lerobot.scripts import lerobot_record
from lerobot.scripts.lerobot_record import RecordConfig
from lerobot.teleoperators.so_leader import SO101LeaderConfig
from lerobot.utils.keyboard_input import apply_recording_control

from .calibration import validate_calibration
from .config import Settings
from .vleader import teleoperator as teleoperator_module
from .vleader.teleoperator import SOArmVirtualLeaderConfig


RUNTIME_DIR = Path(__file__).parents[2] / "runtime/record"
CONTROL_PATH = RUNTIME_DIR / "control.json"
STATUS_PATH = RUNTIME_DIR / "status.json"


def _write_status(**updates: object) -> None:
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


class _GuiControlListener:
    def __init__(self, events: dict[str, bool]):
        self.events = events
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stopped.is_set():
            try:
                payload = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
                CONTROL_PATH.unlink(missing_ok=True)
                key = str(payload.get("key", ""))
                if key in {"right", "left", "esc"}:
                    apply_recording_control(key, self.events)
                    _write_status(last_control=key)
            except FileNotFoundError:
                pass
            except (json.JSONDecodeError, OSError):
                CONTROL_PATH.unlink(missing_ok=True)
            self.stopped.wait(0.05)

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=0.5)


def _init_gui_listener():
    events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
    # 가상 리더 teleoperator가 이 표를 본다. 조작이 오래 끊기면 스스로 에피소드를
    # 끝내야 하는데, LeRobot은 teleoperator에게 이 표를 넘겨주지 않는다.
    teleoperator_module.RECORDING_EVENTS = events
    return _GuiControlListener(events), events


def build_record_config(
    settings: Settings, task: str, dataset_name: str, teleop_source: str = "leader"
) -> RecordConfig:
    if not task.strip():
        raise ValueError("SOARM_TASK must describe one task")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", dataset_name):
        raise ValueError("SOARM_DATASET_NAME must contain only letters, numbers, '_' or '-'")

    root = Path(__file__).parents[2] / "data" / dataset_name
    cameras = {
        "scene": OpenCVCameraConfig(
            Path(settings.scene_camera), fps=30, width=640, height=480, fourcc="MJPG"
        ),
        "wrist": OpenCVCameraConfig(
            Path(settings.wrist_camera), fps=30, width=640, height=480, fourcc="MJPG"
        ),
    }
    robot = SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        cameras=cameras,
        max_relative_target=settings.max_relative_target,
    )
    if teleop_source == "virtual":
        # 물리 리더 팔이 없다. 관절 목표는 콘솔이 검증해 들고 있는 것을 가져온다.
        teleop = SOArmVirtualLeaderConfig(id="soarm101_virtual_leader")
    else:
        teleop = SO101LeaderConfig(port=settings.leader_port, id=settings.leader_id)
    dataset = DatasetRecordConfig(
        repo_id=f"local/{dataset_name}",
        single_task=task.strip(),
        root=root,
        fps=30,
        episode_time_s=int(os.getenv("SOARM_EPISODE_SECONDS", "30")),
        reset_time_s=int(os.getenv("SOARM_RESET_SECONDS", "15")),
        num_episodes=int(os.getenv("SOARM_NUM_EPISODES", "10")),
        video=True,
        push_to_hub=False,
        tags=["so101", "teleoperation", "local"],
        no_stamp=True,
    )
    return RecordConfig(
        robot=robot,
        teleop=teleop,
        dataset=dataset,
        display_data=False,
        play_sounds=False,
    )


def main() -> None:
    settings = Settings()
    if not settings.camera_roles_confirmed:
        raise SystemExit("Refusing record: SOARM_CAMERA_ROLES_CONFIRMED=1 is required")
    if not settings.motion_enabled:
        raise SystemExit("Refusing to record: SOARM_ENABLE_MOTION=1 is required")
    # 가상 리더로 찍을 때는 리더 팔의 calibration을 요구하지 않는다. 그 팔이 없는 것이
    # 이 경로의 존재 이유다. 팔로워 쪽은 어느 경로에서도 반드시 있어야 한다.
    teleop_source = os.getenv("SOARM_TELEOP_SOURCE", "leader")
    required = (
        [("follower", settings.follower_calibration)]
        if teleop_source == "virtual"
        else [("leader", settings.leader_calibration), ("follower", settings.follower_calibration)]
    )
    calibration_errors = [
        f"{role}: {error}" for role, path in required if (error := validate_calibration(path))
    ]
    if calibration_errors:
        raise SystemExit(f"Refusing record: invalid calibration: {calibration_errors}")
    missing = [path for _, path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Refusing to record: missing calibration files: {missing}")
    task = os.getenv("SOARM_TASK", "")
    default_name = "soarm101_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dataset_name = os.getenv("SOARM_DATASET_NAME", default_name)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.unlink(missing_ok=True)
    _write_status(phase="starting", dataset_name=dataset_name, task=task, teleop=teleop_source)
    lerobot_record.init_keyboard_listener = _init_gui_listener
    try:
        _write_status(phase="recording")
        lerobot_record.record(
            build_record_config(settings, task, dataset_name, teleop_source=teleop_source)
        )
        _write_status(phase="complete")
    except BaseException:
        _write_status(phase="error")
        raise


if __name__ == "__main__":
    main()
