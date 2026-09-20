from __future__ import annotations

import socket
import subprocess
import threading
import time
from typing import Any


_LOCK = threading.Lock()
_CACHED_AT = 0.0
_CACHED: dict[str, Any] = {}


def _number(value: str, cast: type[int] | type[float]) -> int | float | None:
    try:
        return cast(float(value.strip()))
    except ValueError:
        return None


def machine_status(*, cache_seconds: float = 1.0) -> dict[str, Any]:
    """Small, read-only GPU snapshot for the policy screen.

    ``nvidia-smi`` is used instead of importing torch: this endpoint is polled while a
    policy runs, and the web process must not create its own CUDA context.  A short TTL
    also prevents several UI pollers from spawning the command at once.
    """
    global _CACHED_AT, _CACHED
    now = time.monotonic()
    with _LOCK:
        if _CACHED and now - _CACHED_AT < cache_seconds:
            return dict(_CACHED)
        result: dict[str, Any] = {"host": socket.gethostname()}
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            line = completed.stdout.strip().splitlines()[0]
            name, used, total, utilization, temperature, power = [
                field.strip() for field in line.split(",")
            ]
            result["gpu"] = {
                "name": name,
                "memory_used_mib": _number(used, int),
                "memory_total_mib": _number(total, int),
                "utilization_percent": _number(utilization, int),
                "temperature_c": _number(temperature, int),
                "power_w": _number(power, float),
            }
        except (OSError, subprocess.SubprocessError, IndexError, ValueError) as error:
            result["gpu_error"] = str(error)
        _CACHED = result
        _CACHED_AT = now
        return dict(result)
