from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    leader_port: str = field(
        default_factory=lambda: os.getenv(
            "SOARM_LEADER_PORT",
                "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90149286-if00",
        )
    )
    follower_port: str = field(
        default_factory=lambda: os.getenv(
            "SOARM_FOLLOWER_PORT",
                "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B90147327-if00",
        )
    )
    scene_camera: str = field(
        default_factory=lambda: os.getenv(
            "SOARM_SCENE_CAMERA",
            "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:7:1.0-video-index0",
        )
    )
    wrist_camera: str = field(
        default_factory=lambda: os.getenv(
            "SOARM_WRIST_CAMERA",
            "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:8:1.0-video-index0",
        )
    )
    leader_id: str = field(default_factory=lambda: os.getenv("SOARM_LEADER_ID", "soarm101_leader"))
    follower_id: str = field(
        default_factory=lambda: os.getenv("SOARM_FOLLOWER_ID", "soarm101_follower")
    )
    motion_enabled: bool = field(
        default_factory=lambda: os.getenv("SOARM_ENABLE_MOTION", "0") == "1"
    )
    camera_roles_confirmed: bool = field(
        default_factory=lambda: os.getenv("SOARM_CAMERA_ROLES_CONFIRMED", "0") == "1"
    )
    max_relative_target: float = field(
        default_factory=lambda: float(os.getenv("SOARM_MAX_RELATIVE_TARGET", "2"))
    )

    @property
    def calibration_root(self) -> Path:
        configured = os.getenv("HF_LEROBOT_CALIBRATION")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".cache/huggingface/lerobot/calibration"

    @property
    def leader_calibration(self) -> Path:
        return self.calibration_root / "teleoperators/so_leader" / f"{self.leader_id}.json"

    @property
    def follower_calibration(self) -> Path:
        return self.calibration_root / "robots/so_follower" / f"{self.follower_id}.json"

    @property
    def lerobot_teleoperate(self) -> Path:
        override = os.getenv("LEROBOT_TELEOPERATE")
        if override:
            return Path(override).expanduser()
        return Path(__file__).parents[2] / ".venv/bin/lerobot-teleoperate"
