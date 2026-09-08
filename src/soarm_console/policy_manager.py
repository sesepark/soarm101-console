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
from .datasets import DatasetError
from .owner_lock import DeviceLockError, DeviceLockSet
from .spark import (
    SparkError,
    describe_remote_model,
    ensure_policy_side,
    policy_tunnel_command,
    stop_policy_side,
)
from .teleop import TeleopError


_RTC_LATENCY = re.compile(r"RTC inference latency=([0-9.]+)s")
_REMOTE_LATENCY = re.compile(r"Network latency \(server->client\): ([0-9.]+)ms")
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
        #: 이 시행에 허락된 초. 적재가 끝난 뒤 시한을 다시 세는 데 쓴다.
        self._max_seconds: float | None = None
        #: 프로세스를 띄운 시각. 적재가 끝나면 `_started_at`은 옮겨지므로 따로 든다.
        self._launched_at: float | None = None
        #: 이 체크포인트를 지난번에 올리는 데 걸린 초. 없으면 처음 올리는 것이다.
        self._expected_load_seconds: float | None = None
        #: 이번에 실제로 걸린 초.
        self._load_seconds: float | None = None
        self._fps_actual: float | None = None
        self._chunk_seconds: float | None = None
        self._chunks: int | None = None
        self._camera_map: dict[str, str] = {}
        self._home: dict[str, float] = {}
        self._phase = "running"
        self._remote = False
        self._remote_side_active = False
        self._tunnel: subprocess.Popen[str] | None = None
        self.other_mode_problem: Callable[[], str | None] | None = None
        self.runtime_dir = Path(__file__).parents[2] / "runtime/policy"
        self.log_path = self.runtime_dir / "policy.log"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def preflight(
        self, run: str | None = None, step: str | None = None, *, remote: bool = False
    ) -> list[str]:
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
        if remote and (not self.settings.spark_host or not self.settings.spark_user):
            problems.append("SOARM_SPARK_HOST and SOARM_SPARK_USER are required for remote inference")
        if not remote and run is not None and step is not None:
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
        remote: bool = False,
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
            if not remote:
                # A naturally completed remote trial leaves its side job alive for the next trial.
                # Switching back to local inference is the end of that session, so wake training now.
                self._stop_remote_resources(stop_side=True)
            problems = self.preflight(run, step, remote=remote)
            if problems:
                raise TeleopError("; ".join(problems))
            if remote:
                try:
                    model = describe_remote_model(self.settings, run, step)
                except (SparkError, DatasetError) as exc:
                    raise TeleopError(str(exc)) from exc
            else:
                model = describe_model(run, step)
            if not model["runnable"]:
                raise TeleopError("; ".join(model["problems"]))
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            (self.runtime_dir / "status.json").unlink(missing_ok=True)
            self._logs.clear()
            self._fps_actual = None
            self._chunk_seconds = None
            self._chunks = None
            now = time.time()
            self._run, self._step, self._task = run, step, task.strip()
            self._started_at, self._expires_at = now, now + max_seconds
            self._max_seconds = max_seconds
            self._launched_at = now
            self._load_seconds = None
            self._expected_load_seconds = self._remembered_load_seconds(run, step, remote)
            self._fps_target = fps
            self._camera_map = dict(model["camera_map"])
            self._home = dict(home or {})
            self._phase = "aligning" if home is not None else "running"
            self._remote = remote
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
            if remote:
                env.update(
                    {
                        "SOARM_REMOTE_POLICY_PATH": str(model["source"]),
                        "SOARM_REMOTE_POLICY_TYPE": str(model["policy"]),
                        "SOARM_REMOTE_RENAME_MAP": json.dumps(model["rename_map"]),
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
                if remote:
                    try:
                        ensure_policy_side(self.settings)
                    except SparkError as exc:
                        raise TeleopError(str(exc)) from exc
                    self._remote_side_active = True
                    self._start_tunnel()
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
                self._stop_remote_resources(stop_side=remote)
                raise
            self._owner_locks = owner_locks
            threading.Thread(target=self._collect_logs, daemon=True).start()
            threading.Thread(target=self._watch_exit, args=(self._process, owner_locks), daemon=True).start()

    def stop(self, timeout: float = 40.0) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            self._release_locks()
            self._stop_remote_resources(stop_side=True)
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
            # 무엇을 죽였는지에 따라 사람이 할 일이 정반대다. 팔을 움직이던 롤아웃을
            # 강제로 끊은 것이라면 팔이 어중간한 자세로 남았을 수 있어 지켜봐야 하고,
            # 정책을 아직 올리는 중이었다면 팔은 한 번도 움직인 적이 없다. 둘을 같은
            # 문장으로 말해서 "물리 전원을 차단하세요"를 읽게 하지 않는다.
            if self._phase in {"aligning", "loading"}:
                raise TeleopError(
                    "Policy rollout was killed before it moved the arm; the arm did not move"
                ) from exc
            raise TeleopError(
                "Policy rollout did not stop cleanly after SIGTERM; it was killed"
            ) from exc
        self._release_locks()
        self._stop_remote_resources(stop_side=True)

    #: 체크포인트마다 지난번 적재가 몇 초 걸렸는지. 화면이 "얼마나 기다리면 되는지"를
    #: 모델 이름으로 분기해 적지 않고 여기서 읽는다.
    #:
    #: 값을 코드에 박지 않는 이유는 정확도가 아니라 **조건이 늘어나기 때문이다.** 같은
    #: π0.5도 곁다리가 처음 뜬 회는 130초, 그 뒤로는 2초다. 모델이 늘 때마다, 캐시가
    #: 켜질 때마다 분기를 더하는 구조는 반드시 어느 하나가 낡은 채로 남는다.
    LOAD_TIMES_NAME = "load_seconds.json"

    def _load_times_path(self) -> Path:
        return self.runtime_dir / self.LOAD_TIMES_NAME

    @staticmethod
    def _load_key(run: str, step: str, remote: bool) -> str:
        # 같은 체크포인트라도 어디서 올리는지에 따라 완전히 다른 시간이다.
        return f"{run}/{step}#{'remote' if remote else 'local'}"

    def _read_load_times(self) -> dict[str, float]:
        try:
            value = json.loads(self._load_times_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            key: float(seconds)
            for key, seconds in value.items()
            if isinstance(key, str) and isinstance(seconds, (int, float)) and seconds > 0
        }

    def _remembered_load_seconds(self, run: str, step: str, remote: bool) -> float | None:
        return self._read_load_times().get(self._load_key(run, step, remote))

    def _remember_load_seconds(self, seconds: float) -> None:
        """가장 최근 값 하나만 든다. 평균을 내면 캐시가 더워진 뒤에도 첫 회가 계속 섞인다."""
        if not self._run or not self._step or seconds <= 0:
            return
        times = self._read_load_times()
        times[self._load_key(self._run, self._step, self._remote)] = round(seconds, 1)
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._load_times_path().with_suffix(".tmp")
            temporary.write_text(json.dumps(times, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self._load_times_path())
        except OSError:
            # 기억하지 못해도 이번 시행은 그대로 돈다. 화면이 "아직 모른다"고 말할 뿐이다.
            pass

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
        alignment_residual = runtime.get("alignment_residual", {})
        runtime_phase = runtime.get("phase")
        if runtime_phase in {"aligning", "loading", "running", "returning"}:
            # 적재가 끝나 팔이 실제로 움직이기 시작한 순간이 시행의 시작이다. 프로세스를
            # 띄운 순간부터 세면 π0.5는 시한(120초)의 대부분을 체크포인트 읽는 데 쓰고,
            # 화면의 남은 시간은 팔이 서 있는 동안 줄어든다.
            if runtime_phase == "running" and self._phase == "loading":
                self._started_at = time.time()
                self._expires_at = self._started_at + (self._max_seconds or 0)
                if self._launched_at is not None:
                    self._load_seconds = self._started_at - self._launched_at
                    self._remember_load_seconds(self._load_seconds)
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
            "inference": "remote" if self._remote else "rtc",
            # 지난번에 이 체크포인트를 올리는 데 걸린 초, 그리고 이번에 실제로 걸린 초.
            # 화면은 앞의 것으로 기다리는 사람에게 얼마나 남았는지 말한다.
            "expected_load_seconds": self._expected_load_seconds,
            "load_seconds": self._load_seconds,
            "remote": self._remote,
            "log_tail": list(self._logs)[-100:],
            "error": error,
            "alignment_residual": alignment_residual,
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
                if match := _REMOTE_LATENCY.search(text):
                    self._chunk_seconds = float(match.group(1)) / 1000
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
        returncode = process.wait()
        with self._lock:
            if self._process is process and self._owner_locks is owner_locks:
                self._owner_locks = None
                # Keep cleanup and the next start mutually exclusive. Otherwise an old watch thread
                # can close the tunnel belonging to a rollout started just after poll() saw it exit.
                self._stop_remote_resources(stop_side=returncode != 0)
        owner_locks.release()

    def _start_tunnel(self) -> None:
        try:
            tunnel = subprocess.Popen(
                policy_tunnel_command(self.settings),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise TeleopError(f"Could not start the remote policy tunnel: {exc}") from exc
        time.sleep(0.2)
        if tunnel.poll() is not None:
            detail = tunnel.stderr.read().strip() if tunnel.stderr is not None else ""
            raise TeleopError(f"Could not start the remote policy tunnel: {detail or tunnel.returncode}")
        self._tunnel = tunnel

    def _stop_remote_resources(self, *, stop_side: bool) -> None:
        tunnel, self._tunnel = self._tunnel, None
        if tunnel is not None and tunnel.poll() is None:
            try:
                os.killpg(tunnel.pid, signal.SIGTERM)
                tunnel.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                if tunnel.poll() is None:
                    os.killpg(tunnel.pid, signal.SIGKILL)
                    tunnel.wait(timeout=5)
        if stop_side and self._remote_side_active:
            try:
                stop_policy_side(self.settings)
            except SparkError as exc:
                self._logs.append(f"Could not stop remote policy side job: {exc}")
            else:
                self._remote_side_active = False
