from __future__ import annotations

import json
import os
import re
import shutil
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


#: 기록 루프가 목표 fps를 못 지킬 때 LeRobot이 내는 문장. 데이터셋의 `timestamp`는
#: `frame_index / fps`로 합성된 값이라(LeRobot `dataset_writer`), 루프가 느렸어도 파케이나
#: 영상에는 흔적이 남지 않는다. 시간축이 조용히 늘어난 데이터가 스스로 30Hz라고 말하게
#: 되는데, 그 사실을 알려 주는 것은 이 경고뿐이다.
SLOW_LOOP_MARKER = "Record loop is running slower"

#: `recording.py`가 데이터셋 이름에 허용하는 것과 같은 모양. 상태 파일에서 읽은 이름을
#: 경로로 쓰기 전에 다시 본다.
_DATASET_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")


class RecordManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._owner_locks: DeviceLockSet | None = None
        self._camera_controls: dict[str, dict[str, object]] = {}
        self._slow_loop_warnings = 0
        self.runtime_dir = Path(__file__).parents[2] / "runtime/record"
        self.log_path = self.runtime_dir / "record.log"

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
            self._slow_loop_warnings = 0
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
            # 이 회차가 30Hz를 지켰는지. 0이 아니면 데이터가 주장하는 fps와 실제로 찍힌
            # 속도가 다르다는 뜻이고, 그 데이터로 배운 정책은 시연보다 빠르게 움직인다.
            "slow_loop_warnings": self._slow_loop_warnings,
            "log_path": str(self.log_path),
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
        # 메모리에만 담으면 회차가 끝나는 순간 증거가 사라진다. 수집이 정말 30Hz로
        # 돌았는지는 데이터셋 파일만 봐서는 알 수 없으므로(SLOW_LOOP_MARKER 설명 참고),
        # 이 출력은 디스크에 남아야 한다.
        try:
            handle = self.log_path.open("w", encoding="utf-8")
        except OSError:
            handle = None
        try:
            for line in process.stdout:
                text = line.rstrip()
                self._logs.append(text)
                if SLOW_LOOP_MARKER in text:
                    self._slow_loop_warnings += 1
                if handle is not None:
                    # 곧바로 흘려 보낸다. 수집이 중간에 죽어도 그때까지의 경고는 남는다.
                    print(text, file=handle, flush=True)
        finally:
            if handle is not None:
                handle.close()

    def _archive_log(self) -> None:
        """끝난 로그를 데이터셋 폴더 안으로 옮긴다.

        `spark.push_dataset`는 데이터셋 폴더째 학습 서버로 보낸다. 로그가 그 안에 있어야
        나중에 학습 서버에서 "이 데이터가 정말 30Hz로 찍혔나"를 데이터만 보고 답할 수 있다.
        """
        try:
            runtime = json.loads((self.runtime_dir / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        name = runtime.get("dataset_name")
        if not isinstance(name, str) or not _DATASET_NAME.fullmatch(name):
            return
        target = Path(__file__).parents[2] / "data" / name
        if not target.is_dir():
            return
        try:
            shutil.copyfile(self.log_path, target / "record.log")
        except OSError:
            pass

    def _watch_exit(self, process: subprocess.Popen[str], owner_locks: DeviceLockSet) -> None:
        process.wait()
        self._archive_log()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
        owner_locks.release()
