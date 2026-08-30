from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from .teleop import TeleopError


class RecordManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self.runtime_dir = Path(__file__).parents[2] / "runtime/record"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def preflight(self) -> list[str]:
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        if not self.settings.camera_roles_confirmed:
            problems.append("SOARM_CAMERA_ROLES_CONFIRMED=1 is not set")
        for role, path in (
            ("leader", self.settings.leader_calibration),
            ("follower", self.settings.follower_calibration),
        ):
            error = validate_calibration(path)
            if error:
                problems.append(f"Invalid {role} calibration: {error}")
        return problems

    def start(self, task: str, episodes: int, episode_seconds: int) -> None:
        if not 1 <= episodes <= 1000:
            raise TeleopError("episodes must be between 1 and 1000")
        if not 5 <= episode_seconds <= 300:
            raise TeleopError("episode_seconds must be between 5 and 300")
        if not task.strip():
            raise TeleopError("A task description is required")
        with self._lock:
            if self.running:
                raise TeleopError("Recording is already running")
            problems = self.preflight()
            if problems:
                raise TeleopError("; ".join(problems))
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._logs.clear()
            env = os.environ.copy()
            env.update(
                {
                    "SOARM_TASK": task.strip(),
                    "SOARM_NUM_EPISODES": str(episodes),
                    "SOARM_EPISODE_SECONDS": str(episode_seconds),
                }
            )
            command = [str(Path(__file__).parents[2] / "scripts/record.sh")]
            self._process = subprocess.Popen(
                command,
                cwd=Path(__file__).parents[2],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            threading.Thread(target=self._collect_logs, daemon=True).start()

    def control(self, key: str) -> None:
        if key not in {"right", "left", "esc"}:
            raise TeleopError("Unknown recording control")
        if not self.running:
            raise TeleopError("Recording is not running")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        target = self.runtime_dir / "control.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"key": key}), encoding="utf-8")
        os.replace(temporary, target)

    def stop(self, timeout: float = 10.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TeleopError("Recording did not stop cleanly after SIGINT") from exc

    def status(self) -> dict[str, object]:
        process = self._process
        status_path = self.runtime_dir / "status.json"
        runtime = None
        try:
            runtime = json.loads(status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {
            "running": self.running,
            "pid": process.pid if self.running and process else None,
            "return_code": process.poll() if process else None,
            "runtime": runtime,
            "logs": list(self._logs)[-100:],
        }

    def _collect_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._logs.append(line.rstrip())
