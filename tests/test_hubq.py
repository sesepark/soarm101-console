from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

import hubq.app as hubq_app
from soarm_console.owner_lock import LOCK_DIR_ENV, DeviceLockSet, read_lock_ledger


def _proc_lock_line(path: Path, pid: int = 4321) -> str:
    details = path.stat()
    return (
        f"1: FLOCK ADVISORY WRITE {pid} "
        f"{os.major(details.st_dev):02x}:{os.minor(details.st_dev):02x}:{details.st_ino} 0 EOF\n"
    )


def test_lock_ledger_does_not_treat_stale_metadata_as_an_owner(
    tmp_path: Path, monkeypatch
) -> None:
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv(LOCK_DIR_ENV, str(lock_dir))
    locks = DeviceLockSet.acquire([tmp_path / "follower"], "replay")
    locks.release()
    proc_locks = tmp_path / "proc-locks"
    proc_locks.write_text("", encoding="utf-8")

    assert read_lock_ledger(proc_locks_path=proc_locks) == [
        {
            "device": str(tmp_path / "follower"),
            "locked": False,
            "owner": None,
            "pid": None,
            "acquired_at": None,
        }
    ]


def test_lock_ledger_reports_only_a_kernel_held_lock(tmp_path: Path, monkeypatch) -> None:
    lock_dir = tmp_path / "locks"
    monkeypatch.setenv(LOCK_DIR_ENV, str(lock_dir))
    locks = DeviceLockSet.acquire([tmp_path / "follower"], "policy")
    lock_path = locks.locks[0].path
    proc_locks = tmp_path / "proc-locks"
    proc_locks.write_text(_proc_lock_line(lock_path), encoding="utf-8")
    try:
        status = read_lock_ledger(proc_locks_path=proc_locks)
    finally:
        locks.release()

    assert status[0]["locked"] is True
    assert status[0]["owner"] == "policy"
    assert status[0]["pid"] == 4321
    assert isinstance(status[0]["acquired_at"], float)


def test_lock_ledger_sees_a_real_kernel_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(LOCK_DIR_ENV, str(tmp_path / "locks"))
    locks = DeviceLockSet.acquire([tmp_path / "scene-camera"], "camera-preview")
    try:
        status = read_lock_ledger()
    finally:
        locks.release()

    assert len(status) == 1
    assert status[0]["locked"] is True
    assert status[0]["owner"] == "camera-preview"


def test_status_endpoint_reports_the_ledger(monkeypatch) -> None:
    ledger = [
        {
            "device": "/dev/ttyACM1",
            "locked": True,
            "owner": "replay",
            "pid": 42,
            "acquired_at": 123.0,
        }
    ]
    monkeypatch.setattr(hubq_app, "read_lock_ledger", lambda: ledger)

    response = TestClient(hubq_app.app).get("/status")

    assert response.status_code == 200
    assert response.json() == {"devices": ledger, "locked": 1}


def test_run_script_binds_hubq_only_to_loopback() -> None:
    script = (Path(__file__).parents[1] / "scripts/run_hubq.sh").read_text(encoding="utf-8")

    assert "--host 127.0.0.1" in script
    assert "0.0.0.0" not in script


def test_units_share_one_explicit_lock_directory() -> None:
    root = Path(__file__).parents[1] / "deploy/systemd"
    console = (root / "soarm-console.service").read_text(encoding="utf-8")
    hubq = (root / "hubq.service").read_text(encoding="utf-8")
    setting = "Environment=SOARM_OWNER_LOCK_DIR=%t/soarm-console/owner-locks"

    assert setting in console
    assert setting in hubq
    assert "/usr/bin/sg dialout" in hubq
    assert "Environment=HUBQ_STATE_DIR=%h/.local/state/hubq" in hubq
    assert "HUBQ_STATE_DIR" not in console
