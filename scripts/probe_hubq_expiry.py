#!/usr/bin/env python3
"""Create a harmless short-lived HUBq teleop-shaped job for expiry proofs.

The child opens no robot or camera. It only holds two regular-file probe locks and exits cleanly
when HUBq sends the teleop kind's SIGINT. Restart the real HUBq after running this script so it
reconciles the durable record; the production console heartbeat can then renew it, or its absence
can let the short deadline expire.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from hubq.jobs import JobRegistry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-seconds", type=float, default=15)
    args = parser.parse_args()
    state_dir = Path(os.getenv("HUBQ_STATE_DIR", Path.home() / ".local/state/hubq"))
    probe_dir = state_dir / "expiry-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    devices: dict[str, str] = {}
    for role in ("leader", "follower"):
        path = probe_dir / role
        path.touch()
        devices[role] = str(path)
    with tempfile.TemporaryDirectory(prefix="hubq-expiry-kinds-") as raw_kinds:
        kinds_dir = Path(raw_kinds)
        marker = probe_dir / "gracefully-stopped"
        marker.unlink(missing_ok=True)
        (kinds_dir / "teleop.json").write_text(
            json.dumps(
                {
                    "command": [
                        ".venv/bin/python",
                        "-c",
                        (
                            "import os,pathlib,signal,time; "
                            f"marker=pathlib.Path({str(marker)!r}); "
                            "signal.signal(signal.SIGINT, "
                            "lambda *_: (marker.write_text('SIGINT\\n'), os._exit(0))); "
                            "print('HUBq expiry probe ready', flush=True); "
                            "time.sleep(3600)"
                        ),
                    ],
                    "owners": ["physical-leader-teleop"],
                    "device_roles": ["leader", "follower"],
                    "motion": False,
                    "already_running": "Teleoperation is already running",
                    "log": str(probe_dir / "probe.log"),
                    "stop_signal": "SIGINT",
                    "stop_timeout": 8,
                    "kill_after_timeout": False,
                    "limit_seconds": args.limit_seconds,
                }
            ),
            encoding="utf-8",
        )
        job = JobRegistry(state_dir, kinds_dir).start(
            kind="teleop",
            owner="physical-leader-teleop",
            devices=devices,
            env={},
            metadata={"probe": True},
        )
    print(json.dumps(job))


if __name__ == "__main__":
    main()
