from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HubQError(RuntimeError):
    pass


class HubQConflict(HubQError):
    pass


def _detail(raw: bytes, fallback: str) -> str:
    try:
        body = json.loads(raw)
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return fallback


def claim(kind: str, devices: list[str]) -> None:
    base_url = os.getenv("HUBQ_URL", "http://127.0.0.1:8094").rstrip("/")
    request = Request(
        f"{base_url}/claim",
        data=json.dumps({"kind": kind, "devices": devices}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=1.0) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        detail = _detail(exc.read(), f"HUBq returned HTTP {exc.code}")
        if exc.code == 409:
            raise HubQConflict(detail) from exc
        raise HubQError(detail) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise HubQError(f"HUBq is unavailable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("allowed") is not True:
        raise HubQError("HUBq returned an invalid claim response")


def _request(
    method: str, path: str, body: dict[str, object] | None = None, *, timeout: float = 3.0
) -> object:
    base_url = os.getenv("HUBQ_URL", "http://127.0.0.1:8094").rstrip("/")
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = _detail(exc.read(), f"HUBq returned HTTP {exc.code}")
        if exc.code == 409:
            raise HubQConflict(detail) from exc
        raise HubQError(detail) from exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise HubQError(f"HUBq is unavailable: {exc}") from exc


class JobProcess:
    """Small Popen-shaped handle backed by HUBq's durable job record."""

    stdout = None

    def __init__(self, record: dict[str, Any]):
        self.job_id = str(record["id"])
        self.pid = int(record["pid"])
        self._record = record

    @property
    def metadata(self) -> dict[str, Any]:
        value = self._record.get("metadata")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def logs(self) -> list[str]:
        value = self._record.get("logs")
        return [str(line) for line in value] if isinstance(value, list) else []

    def refresh(self) -> dict[str, Any]:
        value = _request("GET", f"/jobs/{self.job_id}")
        if not isinstance(value, dict):
            raise HubQError("HUBq returned an invalid job response")
        self._record = value
        return value

    def poll(self) -> int | None:
        try:
            record = self.refresh()
        except HubQError:
            # A scheduler restart must not make the console forget a still-live child.
            # The durable identity check remains HUBq's job; this is only the short outage path.
            if self._record.get("state") == "running" and Path(f"/proc/{self.pid}").exists():
                return None
            # The process is gone as well. Treat a missing durable record as an unknown exit
            # rather than turning the console's public status endpoint into a 500.
            return -1
        if record.get("state") == "running":
            return None
        code = record.get("return_code")
        return int(code) if isinstance(code, int) else -1

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.job_id, timeout)
            time.sleep(0.1)


def start_job(
    kind: str,
    owner: str,
    devices: dict[str, str],
    env: dict[str, str],
    metadata: dict[str, object],
    confirmed: bool,
    sidecars: dict[str, list[str]] | None = None,
) -> JobProcess:
    value = _request(
        "POST",
        "/jobs",
        {
            "kind": kind,
            "owner": owner,
            "devices": devices,
            "env": env,
            "metadata": metadata,
            "sidecars": sidecars or {},
            "confirmed": confirmed,
        },
        timeout=10.0,
    )
    if not isinstance(value, dict) or value.get("state") != "running":
        raise HubQError("HUBq returned an invalid job response")
    return JobProcess(value)


def active_job(kind: str) -> JobProcess | None:
    value = _request("GET", f"/jobs?kind={kind}&active=true")
    if not isinstance(value, list):
        raise HubQError("HUBq returned an invalid job list")
    return JobProcess(value[0]) if value else None


def stop_job(process: JobProcess, timeout: float | None = None) -> dict[str, Any]:
    body: dict[str, object] = {}
    if timeout is not None:
        body["timeout"] = timeout
    value = _request("POST", f"/jobs/{process.job_id}/stop", body, timeout=(timeout or 60) + 6)
    if not isinstance(value, dict):
        raise HubQError("HUBq returned an invalid job response")
    process._record = value
    return value


def register(name: str, owner: str, devices: list[str]) -> None:
    value = _request("POST", f"/registrations/{name}", {"owner": owner, "devices": devices})
    if not isinstance(value, dict) or value.get("owner") != owner:
        raise HubQError("HUBq returned an invalid registration response")


def unregister(name: str) -> None:
    _request("DELETE", f"/registrations/{name}")


def emergency_stop(process: JobProcess, stop_signal: int, timeout: float) -> bool:
    """Safety escape hatch when HUBq is unreachable; never gate a stop on the scheduler."""
    try:
        os.killpg(process.pid, stop_signal)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not Path(f"/proc/{process.pid}").exists():
            return True
        time.sleep(0.05)
    return False
