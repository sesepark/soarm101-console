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
from .replaying import DEFAULT_SPEED, SPEEDS
from .teleop import TeleopError


class ReplayManager:
    """재생 프로세스를 켜고 끄고 들여다본다.

    `RecordManager`와 같은 모양이다 — 시작/중지/상태/로그가 이미 그 모양으로 앱에
    배선되어 있고, 팔이 움직이는 두 경로가 서로 다른 인터페이스를 갖는 것은 사람이
    급할 때 손이 헛나가는 이유가 된다.

    수집과 다른 것은 두 가지뿐이다. 카메라를 잡지 않고(재생은 영상을 만들지 않는다),
    중지가 신호가 아니라 `control.json`이다 — 재생은 멈춘 자리에서 토크를 걸어 둔 채
    서 있어야 하므로, 루프 스스로 빠져나오는 편이 낫다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None
        self.runtime_dir = Path(__file__).parents[2] / "runtime/replay"
        self.log_path = self.runtime_dir / "replay.log"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def preflight(self) -> list[str]:
        """재생을 막는 것들. 리더 팔은 요구하지 않는다 — 재생에 조작자는 없다."""
        problems: list[str] = []
        if not self.settings.motion_enabled:
            problems.append("SOARM_ENABLE_MOTION=1 is not set")
        error = validate_calibration(self.settings.follower_calibration)
        if error:
            problems.append(f"Invalid follower calibration: {error}")
        if not Path(self.settings.follower_port).exists():
            problems.append(f"Missing follower port: {self.settings.follower_port}")
        return problems

    def start(self, dataset: str, episode: int, speed: float = DEFAULT_SPEED) -> None:
        if speed not in SPEEDS:
            raise TeleopError(f"speed must be one of {list(SPEEDS)}")
        if episode < 0:
            raise TeleopError("episode must not be negative")
        if not dataset.strip():
            raise TeleopError("A dataset name is required")
        with self._lock:
            if self.running:
                raise TeleopError("A replay is already running")
            problems = self.preflight()
            if problems:
                raise TeleopError("; ".join(problems))
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            # 지난 회차가 남긴 중지 명령이 이번 회차를 시작하자마자 끊지 않게 한다.
            (self.runtime_dir / "control.json").unlink(missing_ok=True)
            self._logs.clear()
            env = os.environ.copy()
            env.update(
                {
                    "SOARM_REPLAY_DATASET": dataset.strip(),
                    "SOARM_REPLAY_EPISODE": str(episode),
                    "SOARM_REPLAY_SPEED": f"{speed:g}",
                }
            )
            try:
                owner_locks = DeviceLockSet.acquire([self.settings.follower_port], "replay")
            except DeviceLockError as exc:
                raise TeleopError(str(exc)) from exc
            # 자식도 같은 열린 file description을 물려받는다. parent가 죽어도 자식이 팔을
            # 쥐고 있는 동안 flock이 남아야 한다.
            env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            command = [str(Path(__file__).parents[2] / "scripts/replay.sh")]
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

    def request_stop(self) -> None:
        """`control.json`에 중지를 적는다. 루프가 그것을 보고 그 자리에서 빠져나온다."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        target = self.runtime_dir / "control.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"key": "stop"}), encoding="utf-8")
        os.replace(temporary, target)

    def stop(self, timeout: float = 10.0) -> None:
        """멈춘다. 토크는 걸어 둔 채 팔이 그 자리에 선다.

        신호로 끝내지 않고 먼저 `control.json`으로 부탁하는 이유는, 루프가 스스로
        빠져나와야 상태 파일에 "stopped"를 적고 나갈 수 있기 때문이다. 그래도 나가지
        않으면 SIGINT를 보낸다 — 그 길로 나가도 `disable_torque_on_disconnect=False`라
        팔은 힘을 놓지 않는다.
        """
        process = self._process
        if process is None or process.poll() is not None:
            self._release_locks()
            return
        self.request_stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._release_locks()
                return
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TeleopError("Replay did not stop cleanly after SIGINT") from exc
        self._release_locks()

    def status(self) -> dict[str, object]:
        process = self._process
        runtime = None
        try:
            runtime = json.loads(
                (self.runtime_dir / "status.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {
            "running": self.running,
            "pid": process.pid if self.running and process else None,
            "return_code": process.poll() if process else None,
            "runtime": runtime,
            "logs": list(self._logs)[-100:],
            "log_path": str(self.log_path),
            "speeds": list(SPEEDS),
            "default_speed": DEFAULT_SPEED,
        }

    def _release_locks(self) -> None:
        with self._lock:
            owner_locks, self._owner_locks = self._owner_locks, None
        if owner_locks is not None:
            owner_locks.release()

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
                if handle is not None:
                    # 곧바로 흘려 보낸다. 재생이 중간에 죽어도 그때까지의 줄은 남는다.
                    print(text, file=handle, flush=True)
        finally:
            if handle is not None:
                handle.close()

    def _watch_exit(self, process: subprocess.Popen[str], owner_locks: DeviceLockSet) -> None:
        process.wait()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
        owner_locks.release()
