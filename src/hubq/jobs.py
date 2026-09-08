from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Deliberate boundary exception: HUBq and its children share this one lock contract.
from soarm_console.owner_lock import DeviceLockError, DeviceLockSet


PROJECT_ROOT = Path(__file__).parents[2]
KINDS_DIR = Path(__file__).with_name("kinds")


class JobError(RuntimeError):
    pass


class JobConflict(JobError):
    pass


@dataclass(frozen=True)
class KindSpec:
    name: str
    command: tuple[str, ...]
    owners: tuple[str, ...]
    device_roles: tuple[str, ...]
    optional_device_roles: tuple[str, ...]
    sidecars: tuple[str, ...]
    motion: bool
    already_running: str
    log: Path
    stop_signal: int
    stop_timeout: float
    kill_after_timeout: bool


def load_kinds(directory: Path = KINDS_DIR) -> dict[str, KindSpec]:
    result: dict[str, KindSpec] = {}
    for path in sorted(directory.glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        try:
            command = tuple(str(part) for part in body["command"])
            owners = tuple(str(owner) for owner in body["owners"])
            roles = tuple(str(role) for role in body["device_roles"])
            optional = tuple(str(role) for role in body.get("optional_device_roles", []))
            sidecars = tuple(str(name) for name in body.get("sidecars", []))
            stop_signal = int(getattr(signal, str(body["stop_signal"])))
            stop_timeout = float(body["stop_timeout"])
            already_running = str(body["already_running"])
            kill_after_timeout = bool(body["kill_after_timeout"])
            raw_log = Path(str(body["log"]))
            log = raw_log if raw_log.is_absolute() else PROJECT_ROOT / raw_log
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise JobError(f"Invalid HUBq kind file: {path.name}") from exc
        if not command or not owners or not roles or stop_timeout <= 0:
            raise JobError(f"Invalid HUBq kind file: {path.name}")
        result[path.stem] = KindSpec(
            name=path.stem,
            command=command,
            owners=owners,
            device_roles=roles,
            optional_device_roles=optional,
            sidecars=sidecars,
            motion=bool(body.get("motion", True)),
            already_running=already_running,
            log=log,
            stop_signal=stop_signal,
            stop_timeout=stop_timeout,
            kill_after_timeout=kill_after_timeout,
        )
    return result


def _process_start_ticks(pid: int) -> int | None:
    try:
        # comm may contain spaces and parentheses, so split only after its final ')'.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[19])  # field 22 overall; fields here start at field 3.
    except (FileNotFoundError, OSError, IndexError, ValueError):
        return None


def _alive(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    ticks = record.get("process_start_ticks")
    return isinstance(pid, int) and isinstance(ticks, int) and _process_start_ticks(pid) == ticks


class JobRegistry:
    def __init__(self, state_dir: Path | None = None, kinds_dir: Path = KINDS_DIR):
        self.state_dir = state_dir or Path(
            os.getenv("HUBQ_STATE_DIR", Path.home() / ".local/state/hubq")
        )
        self.jobs_dir = self.state_dir / "jobs"
        self.logs_dir = self.state_dir / "logs"
        self.kinds = load_kinds(kinds_dir)
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, DeviceLockSet] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self.reconcile()

    def _write(self, record: dict[str, Any]) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        target = self.jobs_dir / f"{record['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    def reconcile(self) -> None:
        """Match durable job records to the exact Linux processes that created them."""
        with self._lock:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.jobs_dir.glob("*.json")):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                    continue
                if record.get("state") == "running" and not _alive(record):
                    record["state"] = "lost"
                    record["finished_at"] = time.time()
                    record["return_code"] = None
                    self._write(record)
                elif record.get("state") == "running":
                    record["reconciled"] = True
                    self._write(record)
                self._records[record["id"]] = record

    def _spec(self, kind: str) -> KindSpec:
        try:
            return self.kinds[kind]
        except KeyError as exc:
            raise JobError(f"Unknown job kind: {kind}") from exc

    def _validate_devices(self, spec: KindSpec, devices: dict[str, str]) -> list[str]:
        keys = set(devices)
        required = set(spec.device_roles)
        allowed = required | set(spec.optional_device_roles)
        if not required <= keys or not keys <= allowed or any(not value for value in devices.values()):
            raise JobError(
                f"{spec.name} devices must contain {', '.join(spec.device_roles)}"
            )
        return list(devices.values())

    def start(
        self,
        *,
        kind: str,
        owner: str,
        devices: dict[str, str],
        env: dict[str, str],
        metadata: dict[str, Any],
        sidecars: dict[str, list[str]] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        spec = self._spec(kind)
        if spec.motion and not confirmed:
            raise JobError("Motion confirmation is required")
        if owner not in spec.owners:
            raise JobError(f"Owner {owner} is not valid for {kind}")
        device_paths = self._validate_devices(spec, devices)
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise JobError("Job environment must contain strings")
        sidecars = sidecars or {}
        if not set(sidecars) <= set(spec.sidecars) or any(
            not command or not all(isinstance(part, str) for part in command)
            for command in sidecars.values()
        ):
            raise JobError(f"Invalid sidecars for {kind}")
        with self._lock:
            self._refresh_all()
            if any(item["state"] == "running" and item["kind"] == kind for item in self._records.values()):
                raise JobConflict(spec.already_running)
            try:
                owner_locks = DeviceLockSet.acquire(device_paths, owner)
            except DeviceLockError as exc:
                raise JobConflict(str(exc)) from exc
            job_id = uuid.uuid4().hex
            log_path = spec.log
            log_path.parent.mkdir(parents=True, exist_ok=True)
            child_env = os.environ.copy()
            child_env.update(env)
            child_env["SOARM_OWNER_LOCK_FDS"] = owner_locks.inherited_spec
            child_env.setdefault("PYTHONPATH", str(PROJECT_ROOT / "src"))
            command = [str(PROJECT_ROOT / part) if index == 0 and "/" in part and not Path(part).is_absolute() else part for index, part in enumerate(spec.command)]
            record: dict[str, Any] = {
                "id": job_id,
                "kind": kind,
                "owner": owner,
                "devices": devices,
                "command": command,
                "motion": spec.motion,
                "metadata": metadata,
                "state": "starting",
                "pid": None,
                "pgid": None,
                "process_start_ticks": None,
                "started_at": time.time(),
                "finished_at": None,
                "return_code": None,
                "log_path": str(log_path),
                "reconciled": False,
                "sidecars": [],
            }
            self._write(record)
            sidecar_processes: list[subprocess.Popen[bytes]] = []
            try:
                with log_path.open("wb", buffering=0) as output:
                    for name, sidecar_command in sidecars.items():
                        sidecar = subprocess.Popen(
                            sidecar_command,
                            cwd=PROJECT_ROOT,
                            env=child_env,
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                        sidecar_processes.append(sidecar)
                        record["sidecars"].append(
                            {
                                "name": name,
                                "pid": sidecar.pid,
                                "process_start_ticks": _process_start_ticks(sidecar.pid),
                            }
                        )
                    if sidecar_processes:
                        time.sleep(0.2)
                        failed = next((item for item in sidecar_processes if item.poll() is not None), None)
                        if failed is not None:
                            raise OSError(f"Policy tunnel exited with code {failed.returncode}")
                    process = subprocess.Popen(
                        command,
                        cwd=PROJECT_ROOT,
                        env=child_env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        pass_fds=owner_locks.file_descriptors,
                    )
                    owner_locks.mark_inherited_owner(process.pid, command)
            except BaseException:
                for sidecar in sidecar_processes:
                    if sidecar.poll() is None:
                        os.killpg(sidecar.pid, signal.SIGTERM)
                owner_locks.release()
                record["state"] = "failed_to_start"
                record["finished_at"] = time.time()
                self._write(record)
                raise
            record.update(
                state="running",
                pid=process.pid,
                pgid=os.getpgid(process.pid),
                process_start_ticks=_process_start_ticks(process.pid),
            )
            self._records[job_id] = record
            self._locks[job_id] = owner_locks
            self._processes[job_id] = process
            self._write(record)
            threading.Thread(target=self._watch, args=(job_id, process), daemon=True).start()
            return self.describe(job_id)

    def _watch(self, job_id: str, process: subprocess.Popen[bytes]) -> None:
        return_code = process.wait()
        with self._lock:
            record = self._records[job_id]
            if record["state"] == "running":
                record["state"] = "exited"
                record["finished_at"] = time.time()
                record["return_code"] = return_code
                self._write(record)
            self._stop_sidecars(record)
            locks = self._locks.pop(job_id, None)
            self._processes.pop(job_id, None)
        if locks is not None:
            locks.release()

    def _refresh(self, record: dict[str, Any]) -> None:
        if record["state"] == "running" and not _alive(record):
            record["state"] = "lost"
            record["finished_at"] = time.time()
            self._stop_sidecars(record)
            self._write(record)

    @staticmethod
    def _stop_sidecars(record: dict[str, Any]) -> None:
        for sidecar in record.get("sidecars", []):
            if not isinstance(sidecar, dict) or not _alive(sidecar):
                continue
            try:
                os.killpg(int(sidecar["pid"]), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _refresh_all(self) -> None:
        for record in self._records.values():
            self._refresh(record)

    def describe(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                record = self._records[job_id]
            except KeyError as exc:
                raise JobError(f"No such job: {job_id}") from exc
            self._refresh(record)
            result = dict(record)
            result["logs"] = self._tail(Path(record["log_path"]))
            return result

    def list(self, *, kind: str | None = None, active: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_all()
            records = [
                self.describe(job_id)
                for job_id, record in self._records.items()
                if (kind is None or record["kind"] == kind)
                and (not active or record["state"] == "running")
            ]
            return sorted(records, key=lambda item: float(item["started_at"]), reverse=True)

    def stop(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            record = self.describe(job_id)
            if record["state"] != "running":
                return record
            spec = self._spec(str(record["kind"]))
            pid = int(record["pid"])
            if not _alive(record):
                return self.describe(job_id)
            os.killpg(pid, spec.stop_signal)
        deadline = time.monotonic() + (spec.stop_timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            result = self.describe(job_id)
            if result["state"] != "running":
                if result["state"] == "lost":
                    with self._lock:
                        stored = self._records[job_id]
                        stored["state"] = "stopped"
                        self._write(stored)
                    return self.describe(job_id)
                return result
            time.sleep(0.05)
        if _alive(record) and not spec.kill_after_timeout:
            return self.describe(job_id)
        if _alive(record):
            os.killpg(pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 5
        while time.monotonic() < kill_deadline and _alive(record):
            time.sleep(0.05)
        result = self.describe(job_id)
        if result["state"] == "lost":
            with self._lock:
                stored = self._records[job_id]
                stored["state"] = "stopped"
                self._write(stored)
            return self.describe(job_id)
        return result

    @staticmethod
    def _tail(path: Path, lines: int = 400) -> list[str]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                chunks: list[bytes] = []
                newlines = 0
                while position > 0 and newlines <= lines:
                    size = min(8192, position)
                    position -= size
                    handle.seek(position)
                    chunk = handle.read(size)
                    chunks.append(chunk)
                    newlines += chunk.count(b"\n")
            return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return []
