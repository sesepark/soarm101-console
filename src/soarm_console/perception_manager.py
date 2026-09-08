"""외부 파라미터 모드의 서브프로세스를 소유한다. `policy_manager.py`와 같은 골격.

이 모드를 자식 프로세스로 두는 이유는 다른 넷과 같다. 팔과 카메라의 owner가 하나여야
하고, 그 owner가 죽으면 lock이 커널에서 저절로 풀려야 하기 때문이다. 콘솔 프로세스 안에서
스레드로 돌리면 콘솔이 죽는 순간 팔은 마지막 목표를 향한 채 남는다.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from .calibration import validate_calibration
from .config import Settings
from .owner_lock import DeviceLockError, DeviceLockSet
from .perception import store
from .teleop import TeleopError


CONFIRMATION = "CALIBRATE SOARM101"


class PerceptionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None
        self._started_at: float | None = None
        self._poses = 0
        self.runtime_dir = store.RUNTIME_DIR

    @property
    def running(self) -> bool:
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

            env = os.environ.copy()
            env["SOARM_CALIB_POSES"] = str(poses)
            devices = [
                self.settings.follower_port,
                self.settings.scene_camera,
                self.settings.wrist_camera,
            ]
            try:
                owner_locks = DeviceLockSet.acquire(devices, "calibration")
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            project_root = Path(__file__).parents[2]
            command = [
                str(project_root / ".venv/bin/python"),
                "-m",
                "soarm_console.perception.calibrating",
            ]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=project_root,
                    env={**env, "PYTHONPATH": str(project_root / "src")},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    pass_fds=owner_locks.file_descriptors,
                )
            except BaseException:
                owner_locks.release()
                raise
            self._owner_locks = owner_locks
            threading.Thread(target=self._collect_logs, daemon=True).start()
            threading.Thread(
                target=self._watch_exit, args=(self._process, owner_locks), daemon=True
            ).start()

    def stop(self, timeout: float = 60.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self._release_locks()
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # 자식은 시작 자세로 되돌아가는 시간까지 다 받는다. 팔을 아무 데나 두고 끝내는
            # 것보다 조금 기다리는 편이 낫다. 그래도 안 서면 그때가 마지막 안전선이다.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            self._release_locks()
            raise TeleopError("Calibration did not stop cleanly after SIGTERM; it was killed") from exc
        self._release_locks()

    def status(self) -> dict[str, object]:
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
            "running": self.running,
            "pid": process.pid if process is not None and self.running else None,
            "phase": runtime.get("phase", "idle"),
            "pose": runtime.get("pose", 0),
            "total_poses": runtime.get("total_poses", self._poses),
            "detected": runtime.get("detected", {}),
            "started_at": self._started_at,
            "log_tail": list(self._logs)[-40:],
            "error": error,
        }

    def _collect_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._logs.append(line.rstrip())

    def _release_locks(self) -> None:
        with self._lock:
            owner_locks, self._owner_locks = self._owner_locks, None
        if owner_locks is not None:
            owner_locks.release()

    def _watch_exit(self, process: subprocess.Popen[str], owner_locks: DeviceLockSet) -> None:
        process.wait()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
        owner_locks.release()
