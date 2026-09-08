from __future__ import annotations

import json
import os
import threading
import time
import signal
from collections import deque
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from . import hubq_client
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
        self._process: hubq_client.JobProcess | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self.runtime_dir = Path(__file__).parents[2] / "runtime/replay"
        self.log_path = self.runtime_dir / "replay.log"

    @property
    def running(self) -> bool:
        if self._process is None:
            try:
                self._process = hubq_client.active_job("replay")
            except hubq_client.HubQError:
                return False
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
                self._process = hubq_client.start_job(
                    "replay",
                    "replay",
                    {"follower": self.settings.follower_port},
                    {key: value for key, value in env.items() if key.startswith("SOARM_REPLAY_")},
                    {"dataset": dataset.strip(), "episode": episode, "speed": speed},
                    True,
                )
            except hubq_client.HubQError as exc:
                raise TeleopError(str(exc)) from exc

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
            return
        self.request_stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._release_locks()
                return
            time.sleep(0.05)
        try:
            result = hubq_client.stop_job(process, timeout)
        except hubq_client.HubQError as exc:
            if hubq_client.emergency_stop(process, signal.SIGINT, timeout):
                return
            raise TeleopError("Replay did not stop cleanly after SIGINT") from exc
        if result.get("state") == "running":
            raise TeleopError("Replay did not stop cleanly after SIGINT")

    def status(self) -> dict[str, object]:
        running = self.running
        process = self._process
        runtime = None
        try:
            runtime = json.loads(
                (self.runtime_dir / "status.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {
            "running": running,
            "pid": process.pid if running and process else None,
            "return_code": process.poll() if process else None,
            "runtime": runtime,
            "logs": list(getattr(process, "logs", self._logs))[-100:],
            "log_path": str(self.log_path),
            "speeds": list(SPEEDS),
            "default_speed": DEFAULT_SPEED,
        }
