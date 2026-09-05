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
from .owner_lock import DeviceLockError, DeviceLockSet
from .teleop import TeleopError
from .v4l2_controls import apply_recording_controls


class RecordManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None
        self._camera_controls: dict[str, dict[str, object]] = {}
        self.runtime_dir = Path(__file__).parents[2] / "runtime/record"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def preflight(self, teleop_source: str = "leader") -> list[str]:
        """수집을 막는 것들.

        가상 리더로 찍을 때는 리더 팔의 calibration을 요구하지 않는다 — 그 팔이 없는 것이
        이 경로의 존재 이유다. 팔로워 쪽은 어느 경로에서도 있어야 한다.
        """
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        if not self.settings.camera_roles_confirmed:
            problems.append("SOARM_CAMERA_ROLES_CONFIRMED=1 is not set")
        required = (
            [("follower", self.settings.follower_calibration)]
            if teleop_source == "virtual"
            else [
                ("leader", self.settings.leader_calibration),
                ("follower", self.settings.follower_calibration),
            ]
        )
        for role, path in required:
            error = validate_calibration(path)
            if error:
                problems.append(f"Invalid {role} calibration: {error}")
        return problems

    def start(
        self, task: str, episodes: int, episode_seconds: int, teleop_source: str = "leader"
    ) -> None:
        if not 1 <= episodes <= 1000:
            raise TeleopError("episodes must be between 1 and 1000")
        if not 5 <= episode_seconds <= 300:
            raise TeleopError("episode_seconds must be between 5 and 300")
        if not task.strip():
            raise TeleopError("A task description is required")
        with self._lock:
            if self.running:
                raise TeleopError("Recording is already running")
            problems = self.preflight(teleop_source)
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
                    "SOARM_TELEOP_SOURCE": teleop_source,
                }
            )
            devices = [
                self.settings.follower_port,
                self.settings.scene_camera,
                self.settings.wrist_camera,
            ]
            if teleop_source == "leader":
                devices.append(self.settings.leader_port)
            try:
                owner_locks = DeviceLockSet.acquire(
                    devices, f"record-{teleop_source}"
                )
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            # record child도 같은 열린 file description을 물려받는다. parent가 죽어도 child가
            # 장치를 쓰는 동안 flock이 남아야 한다.
            env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            # LeRobot OpenCVCamera가 장치를 열기 전에 넣어야 한다. V4L2 컨트롤은 장치에
            # 남으므로 여기서 닫은 뒤 record child가 열어도 되며, 지원하지 않는 컨트롤은
            # 카메라 교체 시 생길 수 있으므로 경고만 남기고 수집은 계속한다.
            camera_controls = {
                "scene": apply_recording_controls(self.settings.scene_camera),
                "wrist": apply_recording_controls(self.settings.wrist_camera),
            }
            self._camera_controls = camera_controls
            command = [str(Path(__file__).parents[2] / "scripts/record.sh")]
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=Path(__file__).parents[2],
                    env=env,
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
            with self._lock:
                owner_locks, self._owner_locks = self._owner_locks, None
            if owner_locks is not None:
                owner_locks.release()
            return
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TeleopError("Recording did not stop cleanly after SIGINT") from exc
        else:
            with self._lock:
                owner_locks, self._owner_locks = self._owner_locks, None
            if owner_locks is not None:
                owner_locks.release()

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

    def camera_controls(self, role: str) -> dict[str, object] | None:
        """Values read back immediately before the collection process opened the cameras."""
        with self._lock:
            state = self._camera_controls.get(role)
            if state is None:
                return None
            return {
                "values": dict(state["values"]),
                "failures": list(state["failures"]),
            }

    def _collect_logs(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._logs.append(line.rstrip())

    def _watch_exit(self, process: subprocess.Popen[str], owner_locks: DeviceLockSet) -> None:
        process.wait()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
        owner_locks.release()
