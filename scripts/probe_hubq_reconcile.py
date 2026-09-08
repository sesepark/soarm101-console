#!/usr/bin/env python3
"""Launch a harmless durable HUBq-shaped job for service-restart verification.

This intentionally uses the deployed job state directory but a temporary calibration kind whose
command only sleeps. It never opens a robot or camera device. After this launcher exits, the child
alone holds three probe locks; the real HUBq service can then be restarted and must reconcile it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from hubq.jobs import JobRegistry


def main() -> None:
    state_dir = Path(os.getenv("HUBQ_STATE_DIR", Path.home() / ".local/state/hubq"))
    probe_dir = state_dir / "reconcile-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    devices = {}
    for role in ("follower", "scene", "wrist"):
        path = probe_dir / role
        path.touch()
        devices[role] = str(path)
    with tempfile.TemporaryDirectory(prefix="hubq-kinds-") as raw_kinds:
        kinds_dir = Path(raw_kinds)
        (kinds_dir / "calibration.json").write_text(
            json.dumps(
                {
                    "command": [
                        ".venv/bin/python",
                        "-c",
                        (
                            "import os,signal,time; "
                            "signal.signal(signal.SIGTERM, lambda *_: os._exit(0)); "
                            "print('HUBq reconcile probe ready', flush=True); "
                            "time.sleep(3600)"
                        ),
                    ],
                    "owners": ["calibration"],
                    "device_roles": ["follower", "scene", "wrist"],
                    "motion": False,
                    "already_running": "Calibration is already running",
                    "log": str(probe_dir / "probe.log"),
                    "stop_signal": "SIGTERM",
                    "stop_timeout": 5,
                    "kill_after_timeout": True,
                }
            ),
            encoding="utf-8",
        )
        job = JobRegistry(state_dir, kinds_dir).start(
            kind="calibration",
            owner="calibration",
            devices=devices,
            env={},
            metadata={"probe": True},
        )
    print(json.dumps(job))


if __name__ == "__main__":
    main()
