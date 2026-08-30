from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings


class TeleopError(RuntimeError):
    pass


class TeleopManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=300)
        self._lock = threading.Lock()

    def command(self) -> list[str]:
        cfg = self.settings
        return [
            str(cfg.lerobot_teleoperate),
            "--robot.type=so101_follower",
            f"--robot.port={cfg.follower_port}",
            f"--robot.id={cfg.follower_id}",
            f"--robot.max_relative_target={cfg.max_relative_target:g}",
            "--teleop.type=so101_leader",
            f"--teleop.port={cfg.leader_port}",
            f"--teleop.id={cfg.leader_id}",
            "--fps=30",
            "--display_data=false",
        ]

    def preflight(self) -> list[str]:
        cfg = self.settings
        problems: list[str] = []
        if not cfg.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        for label, path in (
            ("leader port", Path(cfg.leader_port)),
            ("follower port", Path(cfg.follower_port)),
            ("lerobot-teleoperate", cfg.lerobot_teleoperate),
        ):
            if not path.exists():
                problems.append(f"Missing {label}: {path}")
        for calibration in (cfg.leader_calibration, cfg.follower_calibration):
            problem = validate_calibration(calibration)
            if problem:
                problems.append(problem)
        return problems

    def start(self) -> None:
        with self._lock:
            if self.running:
                raise TeleopError("Teleoperation is already running")
            problems = self.preflight()
            if problems:
                raise TeleopError("; ".join(problems))
            self._logs.clear()
            self._process = subprocess.Popen(
                self.command(),
                cwd=Path(__file__).parents[2],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            threading.Thread(target=self._collect_logs, daemon=True).start()

    def stop(self, timeout: float = 8.0) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TeleopError(
                "Teleoperation did not stop after SIGINT; use the physical power cutoff if motion continues"
            ) from exc

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, object]:
        process = self._process
        return {
            "running": self.running,
            "pid": process.pid if self.running and process else None,
            "return_code": process.poll() if process else None,
            "logs": list(self._logs)[-80:],
        }

    def _collect_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._logs.append(line.rstrip())
