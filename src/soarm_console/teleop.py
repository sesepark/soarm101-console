from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections import deque
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from .owner_lock import DeviceLockError, DeviceLockSet


class TeleopError(RuntimeError):
    pass


class TeleopManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=300)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None

    def command(self) -> list[str]:
        cfg = self.settings
        limit = cfg.effective_max_relative_target
        command = [
            str(cfg.lerobot_teleoperate),
            "--robot.type=so101_follower",
            f"--robot.port={cfg.follower_port}",
            f"--robot.id={cfg.follower_id}",
        ]
        # 걸릴 수 없는 상한은 아예 넘기지 않는다. 넘기면 LeRobot이 스텝마다 팔로워를
        # 한 번 더 읽고, 그 왕복은 자르지도 못할 값을 위해 치르는 값이다.
        if limit is not None:
            command.append(f"--robot.max_relative_target={limit:g}")
        command += [
            # 종료나 예외가 곧 torque-off가 되면 팔이 떨어진다. 해제는 사람이 팔을
            # 받친 상태에서 별도 절차로만 한다.
            "--robot.disable_torque_on_disconnect=false",
            "--teleop.type=so101_leader",
            f"--teleop.port={cfg.leader_port}",
            f"--teleop.id={cfg.leader_id}",
            "--fps=30",
            "--display_data=false",
        ]
        return command

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
            try:
                owner_locks = DeviceLockSet.acquire(
                    [self.settings.leader_port, self.settings.follower_port],
                    "physical-leader-teleop",
                )
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            try:
                self._process = subprocess.Popen(
                    self.command(),
                    cwd=Path(__file__).parents[2],
                    env=os.environ.copy(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                    # console parent가 죽고 LeRobot child만 남는 경우에도 child가 장치를
                    # 쓰는 동안 커널 lock을 유지한다.
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

    def stop(self, timeout: float = 8.0) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                owner_locks, self._owner_locks = self._owner_locks, None
                if owner_locks is not None:
                    owner_locks.release()
                return
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TeleopError(
                "Teleoperation did not stop after SIGINT; use the physical power cutoff if motion continues"
            ) from exc
        else:
            with self._lock:
                owner_locks, self._owner_locks = self._owner_locks, None
            if owner_locks is not None:
                owner_locks.release()

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

    def _watch_exit(self, process: subprocess.Popen[str], owner_locks: DeviceLockSet) -> None:
        process.wait()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
        owner_locks.release()
