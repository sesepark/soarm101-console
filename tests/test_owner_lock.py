from __future__ import annotations

import json
from pathlib import Path

import pytest

from soarm_console.owner_lock import (
    DeviceLock,
    DeviceLockError,
    DeviceLockSet,
    inherited_locks_cover,
)


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SOARM_OWNER_LOCK_DIR", str(tmp_path / "locks"))


def test_one_device_has_only_one_cooperating_owner(tmp_path: Path):
    device = tmp_path / "follower"
    device.touch()
    first = DeviceLock.acquire(device, "virtual-leader")
    try:
        with pytest.raises(DeviceLockError) as conflict:
            DeviceLock.acquire(device, "physical-leader-teleop")
        assert conflict.value.holder["owner"] == "virtual-leader"
        assert conflict.value.holder["pid"] > 0
    finally:
        first.release()


def test_aliases_of_the_same_device_share_a_lock(tmp_path: Path):
    device = tmp_path / "ttyACM0"
    device.touch()
    alias = tmp_path / "by-id-follower"
    alias.symlink_to(device)
    lock = DeviceLock.acquire(alias, "first")
    try:
        with pytest.raises(DeviceLockError):
            DeviceLock.acquire(device, "second")
    finally:
        lock.release()


def test_stale_metadata_is_replaced_only_by_a_normal_acquire(tmp_path: Path):
    device = tmp_path / "follower"
    first = DeviceLock.acquire(device, "old-owner")
    path = first.path
    first.release()
    assert json.loads(path.read_text(encoding="utf-8"))["owner"] == "old-owner"

    second = DeviceLock.acquire(device, "new-owner")
    try:
        assert json.loads(path.read_text(encoding="utf-8"))["owner"] == "new-owner"
    finally:
        second.release()


def test_partial_lock_set_is_rolled_back_on_conflict(tmp_path: Path):
    first_device = tmp_path / "a"
    second_device = tmp_path / "b"
    occupied = DeviceLock.acquire(second_device, "occupied")
    try:
        with pytest.raises(DeviceLockError):
            DeviceLockSet.acquire([first_device, second_device], "candidate")
        available = DeviceLock.acquire(first_device, "after-rollback")
        available.release()
    finally:
        occupied.release()


def test_record_child_can_validate_inherited_descriptors(tmp_path: Path, monkeypatch):
    devices = [tmp_path / "follower", tmp_path / "camera"]
    locks = DeviceLockSet.acquire(devices, "record-virtual")
    try:
        monkeypatch.setenv("SOARM_OWNER_LOCK_FDS", locks.inherited_spec)
        assert inherited_locks_cover(devices)
        assert not inherited_locks_cover([*devices, tmp_path / "unexpected"])
    finally:
        locks.release()
