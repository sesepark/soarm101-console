"""외부 파라미터 모드의 서브프로세스를 소유한다. `policy_manager.py`와 같은 골격.

이 모드를 자식 프로세스로 두는 이유는 다른 넷과 같다. 팔과 카메라의 owner가 하나여야
하고, 그 owner가 죽으면 lock이 커널에서 저절로 풀려야 하기 때문이다. 콘솔 프로세스 안에서
스레드로 돌리면 콘솔이 죽는 순간 팔은 마지막 목표를 향한 채 남는다.
"""

from __future__ import annotations

import json
import os
import threading
import time
import signal
from collections import deque
from pathlib import Path
from . import hubq_client
from .calibration import validate_calibration
from .config import Settings
from .perception import store
from .teleop import TeleopError


CONFIRMATION = "CALIBRATE SOARM101"


class PerceptionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: hubq_client.JobProcess | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._started_at: float | None = None
        self._poses = 0
        self.runtime_dir = store.RUNTIME_DIR

    @property
    def running(self) -> bool:
        if self._process is None:
            try:
                process = hubq_client.active_job("calibration")
            except hubq_client.HubQError:
                process = None
            if process is not None:
                self._process = process
                self._poses = int(process.metadata.get("poses", 0))
                started = process.metadata.get("started_at")
                self._started_at = float(started) if isinstance(started, (int, float)) else None
        return self._process is not None and self._process.poll() is None

    def preflight(self) -> list[str]:
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        if error := validate_calibration(self.settings.follower_calibration):
            problems.append(f"Invalid follower calibration: {error}")
        for label, path in (
            ("follower port", self.settings.follower_port),
            ("scene camera", self.settings.scene_camera),
            ("wrist camera", self.settings.wrist_camera),
        ):
            if not Path(path).exists():
                problems.append(f"Missing {label}: {path}")
        rig = store.load()
        missing = [role for role in store.ROLES if role not in rig.intrinsics]
        if missing:
            problems.append(f"Intrinsics are missing for {', '.join(missing)}")
        return problems

    def start(self, poses: int) -> None:
        if not 10 <= poses <= 40:
            raise TeleopError("poses must be between 10 and 40")
        with self._lock:
            if self.running:
                raise TeleopError("Calibration is already running")
            problems = self.preflight()
            if problems:
                raise TeleopError("; ".join(problems))
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / "status.json").unlink(missing_ok=True)
            self._logs.clear()
            self._poses = poses
            self._started_at = time.time()

            try:
                self._process = hubq_client.start_job(
                    "calibration",
                    "calibration",
                    {
                        "follower": self.settings.follower_port,
                        "scene": self.settings.scene_camera,
                        "wrist": self.settings.wrist_camera,
                    },
                    {"SOARM_CALIB_POSES": str(poses)},
                    {"poses": poses, "started_at": self._started_at},
                    True,
                )
            except hubq_client.HubQError as exc:
                raise TeleopError(str(exc)) from exc

    def stop(self, timeout: float = 60.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            result = hubq_client.stop_job(process, timeout)
        except hubq_client.HubQError as exc:
            if hubq_client.emergency_stop(process, signal.SIGTERM, timeout):
                return
            hubq_client.emergency_stop(process, signal.SIGKILL, 5)
            raise TeleopError("Calibration did not stop cleanly after SIGTERM; it was killed") from exc
        if result.get("state") == "running":
            raise TeleopError("Calibration did not stop cleanly after SIGTERM; it was killed")

    def status(self) -> dict[str, object]:
        running = self.running
        process = self._process
        runtime: dict[str, object] = {}
        try:
            value = json.loads((self.runtime_dir / "status.json").read_text(encoding="utf-8"))
            if isinstance(value, dict):
                runtime = value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        error = runtime.get("error")
        if error is None and process is not None and process.poll() not in (None, 0):
            error = self._logs[-1] if self._logs else f"Calibration exited with code {process.poll()}"
        return {
            "running": running,
            "pid": process.pid if process is not None and running else None,
            "phase": runtime.get("phase", "idle"),
            "pose": runtime.get("pose", 0),
            "total_poses": runtime.get("total_poses", self._poses),
            "detected": runtime.get("detected", {}),
            "started_at": self._started_at,
            "log_tail": list(getattr(process, "logs", self._logs))[-40:],
            "error": error,
        }
