from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .calibration import validate_calibration
from .config import Settings
from .models import describe_model
from .owner_lock import DeviceLockError, DeviceLockSet
from .teleop import TeleopError


_RTC_LATENCY = re.compile(r"RTC inference latency=([0-9.]+)s")
_ACTUAL_FPS = re.compile(r"running slower \(([0-9.]+) Hz\)")


class PolicyManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None
        self._run: str | None = None
        self._step: str | None = None
        self._task: str | None = None
        self._started_at: float | None = None
        self._expires_at: float | None = None
        self._fps_target: float | None = None
        self._fps_actual: float | None = None
        self._chunk_seconds: float | None = None
        self._chunks: int | None = None
        self._camera_map: dict[str, str] = {}
        self._home: dict[str, float] = {}
        self._phase = "running"
        self.other_mode_problem: Callable[[], str | None] | None = None
        self.runtime_dir = Path(__file__).parents[2] / "runtime/policy"
        self.log_path = self.runtime_dir / "policy.log"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def preflight(self, run: str | None = None, step: str | None = None) -> list[str]:
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        if not self.settings.camera_roles_confirmed:
            problems.append("SOARM_CAMERA_ROLES_CONFIRMED=1 is not set")
        if (
            not math.isfinite(self.settings.policy_max_relative_target)
            or self.settings.policy_max_relative_target <= 0
        ):
            problems.append("SOARM_POLICY_MAX_RELATIVE_TARGET must be a positive finite number")
        error = validate_calibration(self.settings.follower_calibration)
        if error:
            problems.append(f"Invalid follower calibration: {error}")
        for label, path in (
            ("follower port", self.settings.follower_port),
            ("scene camera", self.settings.scene_camera),
            ("wrist camera", self.settings.wrist_camera),
        ):
            if not Path(path).exists():
                problems.append(f"Missing {label}: {path}")
        if self.other_mode_problem is not None and (problem := self.other_mode_problem()):
            problems.append(problem)
        if run is not None and step is not None:
            try:
                model = describe_model(run, step)
            except (FileNotFoundError, ValueError) as exc:
                problems.append(str(exc))
            else:
                problems.extend(str(problem) for problem in model["problems"])
        return problems

    def start(
        self,
        run: str,
        step: str,
        task: str,
        fps: float,
        max_seconds: float,
        home: dict[str, float] | None = None,
    ) -> None:
        if not task.strip():
            raise TeleopError("A task description is required")
        if not 1 <= fps <= 60:
            raise TeleopError("fps must be between 1 and 60")
        if not 1 <= max_seconds <= 600:
            raise TeleopError("max_seconds must be between 1 and 600")
        with self._lock:
            if self.running:
                raise TeleopError("A policy rollout is already running")
            problems = self.preflight(run, step)
            if problems:
                raise TeleopError("; ".join(problems))
            model = describe_model(run, step)
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / "status.json").unlink(missing_ok=True)
            self._logs.clear()
            self._fps_actual = None
            self._chunk_seconds = None
            self._chunks = None
            now = time.time()
            self._run, self._step, self._task = run, step, task.strip()
            self._started_at, self._expires_at = now, now + max_seconds
            self._fps_target = fps
            self._camera_map = dict(model["camera_map"])
            self._home = dict(home or {})
            self._phase = "aligning" if home is not None else "running"
            env = os.environ.copy()
            env.update(
                {
                    "SOARM_POLICY_RUN": run,
                    "SOARM_POLICY_STEP": step,
                    "SOARM_POLICY_TASK": task.strip(),
                    "SOARM_POLICY_FPS": f"{fps:g}",
                    "SOARM_POLICY_MAX_SECONDS": f"{max_seconds:g}",
                    "SOARM_POLICY_HOME": json.dumps(home) if home is not None else "",
                }
            )
            devices = [
                self.settings.follower_port,
                self.settings.scene_camera,
                self.settings.wrist_camera,
            ]
            try:
                owner_locks = DeviceLockSet.acquire(devices, "policy")
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            project_root = Path(__file__).parents[2]
            command = [str(project_root / ".venv/bin/python"), "-m", "soarm_console.policying"]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=Path(__file__).parents[2],
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
            threading.Thread(target=self._watch_exit, args=(self._process, owner_locks), daemon=True).start()

    def stop(self, timeout: float = 40.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self._release_locks()
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Rollout gets the full timeout to run teardown and let the console return the arm. A stuck
            # child cannot be left commanding hardware indefinitely, so SIGKILL is the
            # final safety cutoff only after that graceful path failed.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            self._release_locks()
            raise TeleopError("Policy rollout did not stop cleanly after SIGTERM; it was killed") from exc
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
        runtime_phase = runtime.get("phase")
        if runtime_phase in {"aligning", "running", "returning"}:
            self._phase = str(runtime_phase)
        if error is None and process is not None and process.poll() not in (None, 0):
            error = self._logs[-1] if self._logs else f"Policy process exited with code {process.poll()}"
        return {
            "running": self.running,
            "run": self._run,
            "step": self._step,
            "task": self._task,
            "started_at": self._started_at,
            "expires_at": self._expires_at,
            "fps_target": self._fps_target,
            "fps_actual": self._fps_actual,
            "chunk_seconds": self._chunk_seconds,
            "chunks": self._chunks,
            "camera_map": dict(self._camera_map),
            "home": dict(self._home),
            "phase": self._phase,
            "max_relative_target": self.settings.policy_max_relative_target,
            "inference": "rtc",
            "log_tail": list(self._logs)[-100:],
            "error": error,
        }

    def _collect_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            handle = self.log_path.open("w", encoding="utf-8")
        except OSError:
            handle = None
        try:
            for line in process.stdout:
                text = line.rstrip()
                self._logs.append(text)
                if match := _RTC_LATENCY.search(text):
                    self._chunk_seconds = float(match.group(1))
                    self._chunks = (self._chunks or 0) + 1
                if match := _ACTUAL_FPS.search(text):
                    self._fps_actual = float(match.group(1))
                if handle is not None:
                    print(text, file=handle, flush=True)
        finally:
            if handle is not None:
                handle.close()

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
