from __future__ import annotations

import json
from pathlib import Path


EXPECTED_MOTORS = {
    "shoulder_pan": 1,
    "shoulder_lift": 2,
    "elbow_flex": 3,
    "wrist_flex": 4,
    "wrist_roll": 5,
    "gripper": 6,
}
REQUIRED_FIELDS = {"id", "drive_mode", "homing_offset", "range_min", "range_max"}


def validate_calibration(path: Path) -> str | None:
    if not path.is_file():
        return f"Missing calibration: {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"Invalid calibration JSON {path}: {exc}"
    if not isinstance(payload, dict):
        return f"Invalid calibration object: {path}"
    if set(payload) != set(EXPECTED_MOTORS):
        return f"Calibration motor names do not match SO-101: {path}"
    for motor, expected_id in EXPECTED_MOTORS.items():
        item = payload.get(motor)
        if not isinstance(item, dict) or not REQUIRED_FIELDS.issubset(item):
            return f"Calibration fields are incomplete for {motor}: {path}"
        if item["id"] != expected_id:
            return f"Calibration ID mismatch for {motor}: {path}"
        if item["range_min"] >= item["range_max"]:
            return f"Calibration range is invalid for {motor}: {path}"
    return None

