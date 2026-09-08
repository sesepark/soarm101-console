from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hubq.jobs import JobConflict, JobError, JobRegistry, load_kinds
from soarm_console import hubq_client
from soarm_console.owner_lock import LOCK_DIR_ENV, read_lock_ledger


def _kind(
    directory: Path,
    command: list[str],
    *,
    kill_after_timeout: bool = True,
    limit_seconds: float | None = None,
) -> None:
    directory.mkdir()
    (directory / "teleop.json").write_text(
        json.dumps(
            {
                "command": command,
                "owners": ["physical-leader-teleop"],
                "device_roles": ["leader", "follower"],
                "motion": True,
                "already_running": "Teleoperation is already running",
                "log": str(directory.parent / "probe.log"),
                "stop_signal": "SIGTERM",
                "stop_timeout": 2,
                "kill_after_timeout": kill_after_timeout,
                "limit_seconds": limit_seconds,
            }
        ),
        encoding="utf-8",
    )


def test_five_deployed_kinds_declare_commands_devices_and_motion() -> None:
    kinds = load_kinds()

    assert set(kinds) == {"teleop", "record", "replay", "policy", "calibration"}
    assert all(spec.command and spec.device_roles for spec in kinds.values())
    assert all(spec.motion for spec in kinds.values())
    assert kinds["teleop"].limit_seconds == 3600
    assert kinds["record"].limit_seconds == 3600
    assert all(
        kinds[name].limit_seconds is None for name in {"replay", "policy", "calibration"}
    )


def test_expired_job_gets_its_graceful_signal(tmp_path: Path, monkeypatch) -> None:
    kinds = tmp_path / "kinds"
    marker = tmp_path / "stopped"
    _kind(
        kinds,
        ["/bin/sh", "-c", f"trap 'touch {marker}; exit 0' TERM; while :; do sleep 0.02; done"],
        limit_seconds=0.05,
    )
    monkeypatch.setenv(LOCK_DIR_ENV, str(tmp_path / "locks"))
    registry = JobRegistry(tmp_path / "state", kinds)
    job = registry.start(
        kind="teleop",
        owner="physical-leader-teleop",
        devices={"leader": str(tmp_path / "leader"), "follower": str(tmp_path / "follower")},
        env={},
        metadata={},
        confirmed=True,
    )

    time.sleep(0.08)
    assert registry.expire_due() == [job["id"]]
    assert marker.exists()
    assert registry.describe(str(job["id"]))["state"] != "running"


def test_heartbeat_renews_an_expiring_job(tmp_path: Path, monkeypatch) -> None:
    kinds = tmp_path / "kinds"
    _kind(
        kinds,
        ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 0.02; done"],
        limit_seconds=0.12,
    )
    monkeypatch.setenv(LOCK_DIR_ENV, str(tmp_path / "locks"))
    registry = JobRegistry(tmp_path / "state", kinds)
    job = registry.start(
        kind="teleop",
        owner="physical-leader-teleop",
        devices={"leader": str(tmp_path / "leader"), "follower": str(tmp_path / "follower")},
        env={},
        metadata={},
        confirmed=True,
    )
    try:
        for _ in range(4):
            time.sleep(0.05)
            assert registry.heartbeat() == 1
            assert registry.expire_due() == []
        assert registry.describe(str(job["id"]))["state"] == "running"
    finally:
        registry.stop(str(job["id"]))


def test_job_inherits_locks_and_reconcile_finds_it_after_registry_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    kinds = tmp_path / "kinds"
    locks = tmp_path / "locks"
    leader = tmp_path / "leader"
    follower = tmp_path / "follower"
    leader.touch()
    follower.touch()
    monkeypatch.setenv(LOCK_DIR_ENV, str(locks))
    _kind(
        kinds,
        [
            "/bin/sh",
            "-c",
            "trap 'exit 0' TERM; while :; do sleep 0.1; done",
        ],
    )
    first = JobRegistry(state, kinds)
    job = first.start(
        kind="teleop",
        owner="physical-leader-teleop",
        devices={"leader": str(leader), "follower": str(follower)},
        env={},
        metadata={"proof": True},
        confirmed=True,
    )
    try:
        assert job["state"] == "running"
        assert sum(bool(item["locked"]) for item in read_lock_ledger()) == 2
        with pytest.raises(JobConflict):
            first.start(
                kind="teleop",
                owner="physical-leader-teleop",
                devices={"leader": str(leader), "follower": str(follower)},
                env={},
                metadata={},
                confirmed=True,
            )

        second = JobRegistry(state, kinds)
        recovered = second.describe(str(job["id"]))
        assert recovered["state"] == "running"
        assert recovered["reconciled"] is True
        assert recovered["pid"] == job["pid"]
        assert recovered["metadata"] == {"proof": True}
        second.stop(str(job["id"]))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(item["locked"] for item in read_lock_ledger()):
            time.sleep(0.05)
        assert not any(item["locked"] for item in read_lock_ledger())
    finally:
        try:
            os.killpg(int(job["pid"]), 9)
        except ProcessLookupError:
            pass


def test_console_managers_do_not_launch_or_acquire_owner_locks() -> None:
    root = Path(__file__).parents[1] / "src/soarm_console"
    managers = [
        "teleop.py",
        "record_manager.py",
        "replay_manager.py",
        "policy_manager.py",
        "perception_manager.py",
    ]

    for name in managers:
        source = (root / name).read_text(encoding="utf-8")
        assert "subprocess.Popen" not in source
        assert "DeviceLockSet.acquire" not in source


def test_motion_kind_cannot_start_without_a_fresh_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kinds = tmp_path / "kinds"
    _kind(kinds, ["/bin/true"])
    monkeypatch.setenv(LOCK_DIR_ENV, str(tmp_path / "locks"))
    registry = JobRegistry(tmp_path / "state", kinds)

    with pytest.raises(JobError, match="Motion confirmation is required"):
        registry.start(
            kind="teleop",
            owner="physical-leader-teleop",
            devices={"leader": str(tmp_path / "leader"), "follower": str(tmp_path / "follower")},
            env={},
            metadata={},
        )


def test_kind_can_preserve_the_old_no_sigkill_stop_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kinds = tmp_path / "kinds"
    _kind(
        kinds,
        ["/bin/sh", "-c", "trap '' TERM; while :; do sleep 0.1; done"],
        kill_after_timeout=False,
    )
    monkeypatch.setenv(LOCK_DIR_ENV, str(tmp_path / "locks"))
    registry = JobRegistry(tmp_path / "state", kinds)
    job = registry.start(
        kind="teleop",
        owner="physical-leader-teleop",
        devices={"leader": str(tmp_path / "leader"), "follower": str(tmp_path / "follower")},
        env={},
        metadata={},
        confirmed=True,
    )
    try:
        time.sleep(0.1)
        result = registry.stop(str(job["id"]), timeout=0.1)
        assert result["state"] == "running"
    finally:
        os.killpg(int(job["pid"]), 9)


def test_console_treats_a_missing_record_for_a_dead_process_as_exited(monkeypatch) -> None:
    process = hubq_client.JobProcess(
        {"id": "gone", "pid": 999_999_999, "state": "running", "metadata": {}, "logs": []}
    )
    monkeypatch.setattr(
        hubq_client,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(hubq_client.HubQError("gone")),
    )

    assert process.poll() == -1
