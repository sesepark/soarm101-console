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
        """텔레옵 자식을 띄우는 명령.

        `lerobot-teleoperate` 바이너리가 아니라 `scripts/teleoperate.sh`를 가리킨다.
        그 스크립트가 `soarm_console.teleoperating`을 돌리고, 그쪽이 붙는 순간의 목표
        동기화와 루프 앞의 자세 정렬을 한 뒤 LeRobot의 루프에 들어간다. 설정값은 이제
        CLI 플래그가 아니라 그 모듈이 `Settings`에서 직접 읽는다 — 값이 두 군데에 적히면
        한쪽만 고쳐지는 날이 온다.
        """
        return [str(Path(__file__).parents[2] / "scripts/teleoperate.sh")]

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
                owner_locks = DeviceLockSet.acquire(
                    [self.settings.leader_port, self.settings.follower_port],
                    "physical-leader-teleop",
                )
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            env = os.environ.copy()
            # 텔레옵 자식은 이제 우리 모듈이고, 그 모듈도 lock을 확인한다. parent가 이미
            # 쥔 것을 자식이 다시 잡을 수는 없으므로, 물려받은 descriptor를 알려 준다 —
            # `record_manager`가 record child에게 하는 것과 같다.
            env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            try:
                self._process = subprocess.Popen(
                    self.command(),
                    cwd=Path(__file__).parents[2],
                    env=env,
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
