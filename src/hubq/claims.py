from __future__ import annotations

from dataclasses import dataclass

from soarm_console.owner_lock import canonical_device, read_lock_ledger


KINDS = {
    "camera-config",
    "camera-preview",
    "dataset-delete",
    "hardware-doctor",
    "intrinsics",
    "physical-leader-teleop",
    "policy",
    "record-leader",
    "record-virtual",
    "replay",
    "replay-preflight",
    "rig-calibration",
    "torque-release",
    "virtual-leader",
}


def owner_kind(owner: object) -> str:
    value = str(owner or "unknown")
    if value.startswith("record-"):
        return "recording"
    return {
        "physical-leader-teleop": "teleoperation",
        "calibration": "rig-calibration",
    }.get(value, value)


@dataclass(frozen=True)
class ClaimDecision:
    allowed: bool
    detail: str | None
    conflicts: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "detail": self.detail,
            "conflicts": self.conflicts,
        }


_MESSAGES: dict[str, dict[str, str]] = {
    "physical-leader-teleop": {
        "teleoperation": "Teleoperation is already running",
        "recording": "Stop recording before teleoperation",
        "replay": "Stop the replay before teleoperation: the follower has one owner",
        "rig-calibration": "Stop the camera calibration before teleoperation: the follower has one owner",
        "policy": "Stop the policy before teleoperation: the follower has one owner",
        "virtual-leader": "Stop the virtual leader before physical-leader teleoperation: the follower has one owner",
    },
    "record-leader": {
        "recording": "Recording is already running",
        "teleoperation": "Stop teleoperation before recording",
        "replay": "Stop the replay before recording: the follower has one owner",
        "rig-calibration": "Stop the camera calibration before recording: the follower and cameras have one owner",
        "policy": "Stop the policy before recording: the follower and cameras have one owner",
        "virtual-leader": "Stop the virtual leader before recording with the physical leader",
    },
    "record-virtual": {
        "recording": "Recording is already running",
        "teleoperation": "Stop teleoperation before recording",
        "replay": "Stop the replay before recording: the follower has one owner",
        "rig-calibration": "Stop the camera calibration before recording: the follower and cameras have one owner",
        "policy": "Stop the policy before recording: the follower and cameras have one owner",
    },
    "replay": {
        "replay": "Stop the replay that is already running",
        "rig-calibration": "Stop the camera calibration before replaying: the follower has one owner",
        "policy": "Stop the policy before replaying: the follower has one owner",
        "recording": "Stop recording before replaying: the follower has one owner",
        "teleoperation": "Stop teleoperation before replaying: the follower has one owner",
        "virtual-leader": "Stop the virtual leader before replaying: the follower has one owner",
    },
    "policy": {
        "policy": "A policy rollout is already running",
    },
    "rig-calibration": {
        "rig-calibration": "Calibration is already running",
    },
    "virtual-leader": {
        "virtual-leader": "Virtual leader is already running",
        "policy": "Stop the policy before starting the virtual leader: the follower has one owner",
    },
}


def _is_relevant(kind: str, holder: str) -> bool:
    # These console-owned operations deliberately preserve their narrower existing rules.
    if kind == "camera-config":
        return holder == "recording"
    if kind == "dataset-delete":
        return holder in {"recording", "replay", "policy"}
    if kind == "intrinsics":
        return holder in {"recording", "policy", "rig-calibration"} or holder == "unknown"
    # A preview lock belongs to the console camera worker. Jobs that need the cameras stop that
    # worker after the claim and before taking their own locks.
    if holder == "camera-preview" and kind in {
        "intrinsics",
        "policy",
        "record-leader",
        "record-virtual",
        "rig-calibration",
    }:
        return False
    # Virtual recording intentionally hands the live virtual leader into relay mode, releasing its
    # follower lock before RecordManager acquires the same device.
    if kind == "record-virtual" and holder == "virtual-leader":
        return False
    return True


def _message(kind: str, conflict: dict[str, object]) -> str:
    holder = owner_kind(conflict.get("owner"))
    if kind == "camera-config":
        return "Recording fixes every camera at 640x480@30"
    if kind == "dataset-delete":
        return "Cannot delete while recording or replaying"
    if kind == "hardware-doctor":
        return "Cannot inspect serial buses during an active mode"
    if kind == "torque-release":
        return "Stop the running mode before releasing torque"
    if kind == "replay-preflight":
        return "Stop the running mode before reading the follower: it has one owner"
    if kind == "intrinsics":
        return "Stop the running mode before collecting board views"
    if kind == "policy" and holder != "policy":
        return "Stop the running mode before starting a policy"
    if kind == "rig-calibration" and holder != "rig-calibration":
        return "Stop the running mode before starting calibration"
    if message := _MESSAGES.get(kind, {}).get(holder):
        return message
    owner = conflict.get("owner") or "unknown"
    pid = conflict.get("pid") if conflict.get("pid") is not None else "?"
    return f"Device is owned by {owner} (pid {pid}): {conflict['device']}"


def decide_claim(kind: str, devices: list[str]) -> ClaimDecision:
    if kind not in KINDS:
        raise ValueError(f"Unknown job kind: {kind}")
    requested = {canonical_device(device) for device in devices}
    if not requested:
        raise ValueError("A claim needs at least one device")
    conflicts = [
        entry
        for entry in read_lock_ledger()
        if entry.get("locked")
        and entry.get("device") in requested
        and _is_relevant(kind, owner_kind(entry.get("owner")))
    ]
    if not conflicts:
        return ClaimDecision(True, None, [])
    conflict = conflicts[0]
    return ClaimDecision(False, _message(kind, conflict), conflicts)
