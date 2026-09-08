from __future__ import annotations

import threading
import signal
from collections import deque
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from . import hubq_client


class TeleopError(RuntimeError):
    pass


class TeleopManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: hubq_client.JobProcess | None = None
        self._logs: deque[str] = deque(maxlen=300)
        self._lock = threading.Lock()

    def preflight(self) -> list[str]:
        cfg = self.settings
        problems: list[str] = []
        if not cfg.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        for label, path in (
            ("leader port", Path(cfg.leader_port)),
            ("follower port", Path(cfg.follower_port)),
            # 예전에는 `lerobot-teleoperate` 바이너리를 찾았다. 이제 텔레옵은 우리
            # 모듈이므로 그것을 돌릴 인터프리터가 있는지를 본다.
            ("python interpreter", Path(__file__).parents[2] / ".venv/bin/python"),
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
            try:
                self._process = hubq_client.start_job(
                    "teleop",
                    "physical-leader-teleop",
                    {
                        "leader": self.settings.leader_port,
                        "follower": self.settings.follower_port,
                    },
                    {},
                    {},
                    True,
                )
            except hubq_client.HubQError as exc:
                raise TeleopError(str(exc)) from exc

    def stop(self, timeout: float = 8.0) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
        try:
            result = hubq_client.stop_job(process, timeout)
        except hubq_client.HubQError as exc:
            if hubq_client.emergency_stop(process, signal.SIGINT, timeout):
                return
            raise TeleopError(
                "Teleoperation did not stop after SIGINT; use the physical power cutoff if motion continues"
            ) from exc
        if result.get("state") == "running":
            raise TeleopError(
                "Teleoperation did not stop after SIGINT; use the physical power cutoff if motion continues"
            )

    @property
    def running(self) -> bool:
        if self._process is None:
            try:
                self._process = hubq_client.active_job("teleop")
            except hubq_client.HubQError:
                return False
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, object]:
        running = self.running
        process = self._process
        return {
            "running": running,
            "pid": process.pid if running and process else None,
            "return_code": process.poll() if process else None,
            "logs": list(getattr(process, "logs", self._logs))[-80:],
        }
