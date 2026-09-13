"""Worker-side mobile lease guard; independent of the console and HUBq lifetimes.

SOARM_MOBILE_JOB_RECORD is a HUBq-provided path to its atomically replaced job
record. Only mobile physical teleoperation receives it. No motion limits change.
"""
from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def lease_alive(path: Path, now: float) -> bool:
    try:
        record = json.loads(path.read_text())
        deadline = record["lease_expires_at"]
        return (record["state"] in {"starting", "running"}
                and record.get("expiry_stop_requested_at") is None
                and isinstance(deadline, (float, int))
                and math.isfinite(deadline) and deadline > now)
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def mobile_watchdog():
    raw_path = os.getenv("SOARM_MOBILE_JOB_RECORD")
    if not raw_path:
        yield
        return
    path = Path(raw_path)
    if not lease_alive(path, time.time()):
        raise SystemExit("Refusing to start: mobile teleoperation lease has ended")
    stopped = threading.Event()
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupt_once(signum, frame):
        # HUBq and this worker can notice the same expiry simultaneously. A
        # second SIGINT must not interrupt disconnect()/owner-lock cleanup.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt_once)

    def watch():
        while not stopped.wait(0.2):
            if not lease_alive(path, time.time()):
                print("Mobile teleoperation lease ended; stopping and retaining follower torque", flush=True)
                os.kill(os.getpid(), signal.SIGINT)
                return

    watcher = threading.Thread(target=watch, name="mobile-teleop-watchdog", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stopped.set()
        watcher.join(timeout=1)
        signal.signal(signal.SIGINT, previous_handler)
