from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import signal
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .calibration import validate_calibration
from .config import Settings
from . import hubq_client
from .teleop import TeleopError
from .v4l2_controls import apply_recording_controls


#: 기록 루프가 목표 fps를 못 지킬 때 LeRobot이 내는 문장. 데이터셋의 `timestamp`는
#: `frame_index / fps`로 합성된 값이라(LeRobot `dataset_writer`), 루프가 느렸어도 파케이나
#: 영상에는 흔적이 남지 않는다. 시간축이 조용히 늘어난 데이터가 스스로 30Hz라고 말하게
#: 되는데, 그 사실을 알려 주는 것은 이 경고뿐이다.
SLOW_LOOP_MARKER = "Record loop is running slower"
RECORDING_EPISODE_MARKER = re.compile(r"\bRecording episode \d+\b")

#: `recording.py`가 데이터셋 이름에 허용하는 것과 같은 모양. 상태 파일에서 읽은 이름을
#: 경로로 쓰기 전에 다시 본다.
_DATASET_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")

#: 수집이 받아들이는 조작. `esc`와 `abort`는 둘 다 회차를 끝내지만 결과가 다르다 —
#: `abort`는 찍던 회를 버리고, `esc`는 저장한다. `recording._GuiControlListener`가
#: 그 차이를 만든다.
CONTROLS = ("right", "left", "esc", "abort")


#: `soarm_quality.json`에서 이어 찍기 때 **더해야** 하는 세는 값들. 비율은 여기 없다 —
#: 비율은 더하는 것이 아니라 합쳐진 세는 값에서 다시 계산한다.
_COUNTED_QUALITY_KEYS = ("total_frames", "sensor_read_failures", "episodes_aborted")
_COUNTED_QUALITY_MAPS = ("camera_stale_frames", "sensor_implausible")


def _merge_session_quality(
    session: dict[str, object], previous: dict[str, object]
) -> dict[str, object]:
    """이번 실행이 센 값에 지난 실행의 값을 더한다.

    `soarm_quality.json` 하나가 데이터셋 전체를 말해야 한다. 이어 찍기에서 마지막 실행의
    수만 남기면 앞 회차들의 프레임이 세어지지 않은 채 사라진다.

    `camera_stale_pct`는 더하지 않고 합쳐진 프레임 수에서 **다시 계산한다** — 비율의 합은
    비율이 아니고, 회차마다 프레임 수가 다르면 평균도 답이 아니다.

    읽기 시간의 p50·p99는 이번 실행의 값 그대로다. 지난 실행의 표본은 남아 있지 않고,
    두 백분위수를 더하거나 평균 내는 것은 어느 쪽도 실제로 일어난 시간이 아니다.
    """
    merged: dict[str, object] = dict(session)
    for key in _COUNTED_QUALITY_KEYS:
        merged[key] = _as_int(session.get(key)) + _as_int(previous.get(key))
    current_seconds = _as_float(session.get("total_seconds"))
    previous_seconds = _as_float(previous.get("total_seconds"))
    if previous_seconds <= 0.0:
        previous_frames = _as_int(previous.get("total_frames"))
        previous_hz = _as_float(previous.get("loop_hz"))
        if previous_frames > 0 and previous_hz > 0.0:
            # `total_seconds`를 쓰기 전 품질 파일도 이어 찍을 수 있어야 한다.
            previous_seconds = previous_frames / previous_hz
    merged["total_seconds"] = current_seconds + previous_seconds
    for key in _COUNTED_QUALITY_MAPS:
        current = session.get(key)
        current = current if isinstance(current, dict) else {}
        earlier = previous.get(key)
        earlier = earlier if isinstance(earlier, dict) else {}
        merged[key] = {
            name: _as_int(current.get(name)) + _as_int(earlier.get(name))
            for name in {*current, *earlier}
        }
    total = _as_int(merged.get("total_frames"))
    stale = merged.get("camera_stale_frames")
    stale = stale if isinstance(stale, dict) else {}
    merged["camera_stale_pct"] = {
        name: (100.0 * _as_int(count) / total) if total else 0.0
        for name, count in stale.items()
    }
    return merged


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def preview_path(runtime_dir: Path, role: str) -> Path:
    """수집 중 스냅숏이 놓이는 자리.

    쓰는 쪽(`recording._PreviewWriter`)과 내주는 쪽(`app`)이 같은 이름을 봐야 하므로
    이름을 만드는 자리를 하나만 둔다.
    """
    return runtime_dir / f"preview-{role}.jpg"


class RecordManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: hubq_client.JobProcess | None = None
        self._logs: deque[str] = deque(maxlen=400)
        self._lock = threading.Lock()
        self._watching_job_id: str | None = None
        self._camera_controls: dict[str, dict[str, object]] = {}
        self._slow_loop_warnings = 0
        self._ignore_first_slow_loop_warning = False
        self._resumed = False
        #: 이 회를 시작하기 직전의 하드웨어 진단. `app`이 넘겨 준다 — 가상 리더로 찍을
        #: 때는 진단을 돌리지 않으므로 `None`이다. `soarm_provenance.json`에 함께 적힌다.
        self._doctor: dict[str, object] | None = None
        # The app owns the virtual-leader object; the manager only signals that
        # a relay-backed recording has relinquished the follower.
        self.on_virtual_exit: Callable[[], None] | None = None
        self.runtime_dir = Path(__file__).parents[2] / "runtime/record"
        self.log_path = self.runtime_dir / "record.log"

    @property
    def running(self) -> bool:
        if self._process is None:
            self._adopt_active()
        return self._process is not None and self._process.poll() is None

    def _adopt_active(self) -> None:
        try:
            process = hubq_client.active_job("record")
        except hubq_client.HubQError:
            return
        if process is None:
            return
        self._process = process
        metadata = process.metadata
        self._resumed = bool(metadata.get("resumed", False))
        controls = metadata.get("camera_controls")
        if isinstance(controls, dict):
            self._camera_controls = controls
        doctor = metadata.get("doctor")
        self._doctor = doctor if isinstance(doctor, dict) else None
        self._start_watcher(str(metadata.get("teleop_source", "leader")))

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
        self,
        task: str,
        episodes: int,
        episode_seconds: int,
        teleop_source: str = "leader",
        dataset: str | None = None,
        resume: bool = False,
        doctor: dict[str, object] | None = None,
    ) -> None:
        """수집 자식을 띄운다.

        `resume`이면 `dataset`이 가리키는 기존 데이터셋에 회차를 이어 붙인다. 그 데이터셋이
        실제로 이어 찍어도 되는 것인지 — 있는가, 과제가 같은가 — 는 `app`이 요청을 받는
        자리에서 본다. 여기서는 이름과 깃발을 자식에게 넘기는 일만 한다.
        """
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
            self._ignore_first_slow_loop_warning = False
            env = os.environ.copy()
            env.update(
                {
                    "SOARM_TASK": task.strip(),
                    "SOARM_NUM_EPISODES": str(episodes),
                    "SOARM_EPISODE_SECONDS": str(episode_seconds),
                    "SOARM_TELEOP_SOURCE": teleop_source,
                }
            )
            # 이어 찍기가 아니면 이름은 자식이 짓는다(`recording.default_dataset_name`).
            # 환경에서 지워 두지 않으면 지난 실행의 이름이 남아 다른 회차가 같은 폴더로
            # 들어간다 — parent의 환경을 복사해 오기 때문이다.
            env.pop("SOARM_DATASET_NAME", None)
            env.pop("SOARM_RESUME", None)
            if resume:
                if dataset is None:
                    raise TeleopError("Resuming needs the name of the dataset to append to")
                env["SOARM_DATASET_NAME"] = dataset
                env["SOARM_RESUME"] = "1"
            self._resumed = resume
            # LeRobot OpenCVCamera가 장치를 열기 전에 넣어야 한다. V4L2 컨트롤은 장치에
            # 남으므로 여기서 닫은 뒤 record child가 열어도 되며, 지원하지 않는 컨트롤은
            # 카메라 교체 시 생길 수 있으므로 경고만 남기고 수집은 계속한다.
            camera_controls = {
                "scene": apply_recording_controls(self.settings.scene_camera),
                "wrist": apply_recording_controls(self.settings.wrist_camera),
            }
            self._camera_controls = camera_controls
            self._doctor = doctor
            try:
                device_map = {
                    "follower": self.settings.follower_port,
                    "scene": self.settings.scene_camera,
                    "wrist": self.settings.wrist_camera,
                }
                if teleop_source == "leader":
                    device_map["leader"] = self.settings.leader_port
                self._process = hubq_client.start_job(
                    "record",
                    f"record-{teleop_source}",
                    device_map,
                    {key: value for key, value in env.items() if key.startswith("SOARM_")},
                    {
                        "teleop_source": teleop_source,
                        "resumed": resume,
                        "camera_controls": camera_controls,
                        "doctor": doctor,
                    },
                    True,
                )
            except hubq_client.HubQError as exc:
                raise TeleopError(str(exc)) from exc
            self._start_watcher(teleop_source)

    def preview_path(self, role: str) -> Path:
        return preview_path(self.runtime_dir, role)

    def control(self, key: str) -> None:
        if key not in CONTROLS:
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
        try:
            result = hubq_client.stop_job(process, timeout)
        except hubq_client.HubQError as exc:
            if hubq_client.emergency_stop(process, signal.SIGINT, timeout):
                return
            raise TeleopError("Recording did not stop cleanly after SIGINT") from exc
        if result.get("state") == "running":
            raise TeleopError("Recording did not stop cleanly after SIGINT")

    def status(self) -> dict[str, object]:
        running = self.running
        process = self._process
        status_path = self.runtime_dir / "status.json"
        runtime = None
        try:
            runtime = json.loads(status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        logs = list(getattr(process, "logs", self._logs))
        self._slow_loop_warnings = 0
        self._ignore_first_slow_loop_warning = False
        for line in logs:
            self._observe_log_line(line)
        return {
            "running": running,
            "pid": process.pid if running and process else None,
            "return_code": process.poll() if process else None,
            "runtime": runtime,
            "logs": logs[-100:],
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

    def _materialize_log(self, lines: list[str]) -> None:
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )
        except OSError:
            pass

    def _observe_log_line(self, text: str) -> None:
        """Count genuine slow ticks, excluding LeRobot's episode-transition tick."""
        if RECORDING_EPISODE_MARKER.search(text):
            self._ignore_first_slow_loop_warning = True
        if SLOW_LOOP_MARKER not in text:
            return
        if self._ignore_first_slow_loop_warning:
            self._ignore_first_slow_loop_warning = False
            return
        self._slow_loop_warnings += 1

    def _archive_log(self) -> None:
        """끝난 로그와 이번 실행의 품질 요약을 데이터셋 폴더 안에 남긴다.

        `spark.push_dataset`는 데이터셋 폴더째 학습 서버로 보낸다. 둘이 그 안에 있어야
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
        self._write_quality(target, runtime)
        self._append_provenance(target, runtime)

    def _write_quality(self, target: Path, runtime: dict[str, object]) -> None:
        """`soarm_quality.json` — 이 데이터가 어떻게 찍혔는지 한 장.

        데이터셋 자신은 이것을 말하지 못한다. `timestamp`는 `frame_index / fps`로 합성된
        값이라 루프가 느렸어도 파케이는 30Hz라고 적혀 있고, 정지 장면과 멈춘 카메라는
        영상에서 구별되지 않는다. 그래서 찍는 동안 세어 둔 값을 여기 적어 함께 보낸다.

        이어 찍기면 지난 실행의 경고 수에 이번 것을 더한다. 파일 하나가 데이터셋 전체를
        말해야 하므로, 마지막 실행만 남기면 앞 회차들이 조용해진다.

        `camera_stale_pct`는 **세션 전체**의 값이다. 이름은 그대로 두고 뜻만 바꿨다 —
        앱이 이 키를 읽고, 그 자리에서 물어보는 것도 "이 데이터가 어떻게 찍혔나"이기
        때문이다. 예전 값은 `_LoopRateMonitor`의 3초 창이라 회차가 끝나는 순간만 말했고,
        실제로 `test4_20260905_1459`는 0.0으로 적혔지만 파케이를 세면 2.54·2.90%였다.
        """
        path = target / "soarm_quality.json"
        warnings = self._slow_loop_warnings
        previous: dict[str, object] = {}
        if self._resumed:
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                warnings += int(previous.get("slow_loop_warnings", 0))
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                previous = {}
        session = runtime.get("session_quality")
        session = session if isinstance(session, dict) else {}
        merged = _merge_session_quality(session, previous if self._resumed else {})
        frames = _as_int(merged.get("total_frames"))
        seconds = _as_float(merged.get("total_seconds"))
        quality = {
            # status의 값은 최근 3초 창이다. 품질 파일은 저장·정리를 뺀 모든 기록 구간의
            # 프레임 수와 wall time으로 데이터셋 전체의 실제 속도를 말한다.
            "loop_hz": frames / seconds if frames > 0 and seconds > 0.0 else runtime.get("loop_hz"),
            "slow_loop_warnings": warnings,
            "recorded_at": time.time(),
            **merged,
        }
        try:
            path.write_text(json.dumps(quality), encoding="utf-8")
        except OSError:
            pass

    def _append_provenance(self, target: Path, runtime: dict[str, object]) -> None:
        """`soarm_provenance.json` — 이 회가 어떤 조건에서 찍혔는지, 회마다 한 항목씩.

        **배열에 덧붙인다.** 한 데이터셋에 여러 번 이어 찍으면 조건이 회마다 다를 수
        있고(카메라 컨트롤이 안 걸린 회, 진단이 다른 회, 다른 커밋으로 찍은 회), 마지막
        것만 남기면 앞의 회차들이 무슨 조건에서 찍혔는지 답할 곳이 사라진다.

        자식과 부모가 아는 것이 다르다. 시작 epoch·커밋·calibration 해시·fps는 자식이
        상태 파일에 적어 두고, 카메라 컨트롤 되읽기와 시작 진단은 여기 부모에게만 있다.
        둘을 여기서 합쳐 한 항목으로 쓴다 — 파일 하나를 두 프로세스가 쓰지 않는다.
        """
        provenance = runtime.get("provenance")
        if not isinstance(provenance, dict):
            return
        entry = dict(provenance)
        entry["camera_controls"] = self._camera_controls
        entry["doctor"] = self._doctor
        path = target / "soarm_provenance.json"
        history: list[object] = []
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                history = existing
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        history.append(entry)
        try:
            path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except (OSError, TypeError):
            pass

    def _start_watcher(self, teleop_source: str) -> None:
        process = self._process
        if process is None or self._watching_job_id == process.job_id:
            return
        self._watching_job_id = process.job_id
        threading.Thread(
            target=self._watch_exit, args=(process, teleop_source), daemon=True
        ).start()

    def _watch_exit(
        self, process: hubq_client.JobProcess, teleop_source: str = "leader"
    ) -> None:
        process.wait()
        self._materialize_log(process.logs)
        self._slow_loop_warnings = 0
        self._ignore_first_slow_loop_warning = False
        for line in process.logs:
            self._observe_log_line(line)
        self._archive_log()
        if teleop_source == "virtual" and self.on_virtual_exit is not None:
            try:
                self.on_virtual_exit()
            except Exception as exc:
                # Cleanup failure must remain visible, but it cannot strand the
                # recording's owner lock or kill this watcher before it finishes.
                self._logs.append(f"Could not stop virtual leader relay: {exc}")
