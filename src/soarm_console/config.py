from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


#: 이 팔에서 가장 넓은 관절의 폭. `max_relative_target`은 한 스텝의 이동량 상한이므로,
#: 가장 넓은 관절의 폭보다 큰 상한은 어느 관절에서도 걸릴 수 없다.
#:
#: 숫자는 팔로워 calibration에서 계산한 관절 폭 가운데 가장 큰 것이다(`/api/status`의
#: `virtual_leader.spec`에서 잰 값, 2026-09-05): shoulder_pan 236.4, shoulder_lift 209.8,
#: elbow_flex 193.8, wrist_flex 205.4, wrist_roll 360.0, gripper 100.0(퍼센트).
#: wrist_roll이 한 바퀴를 다 돌므로 360이 상한이다.
#:
#: 한때 여기에 정규화 폭 200을 적어 두었는데 그것은 틀린 값이었다. 여섯 중 넷이 200을
#: 넘으므로, 예컨대 250을 넣으면 wrist_roll의 360도 점프를 실제로 자를 수 있는데도 상한이
#: 조용히 꺼졌다. 안전 클램프에서 틀리려면 상한을 남기는 쪽으로 틀려야 한다.
WIDEST_JOINT_SPAN = 360.0


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
            "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:11.4:1.0-video-index0",
        )
    )
    wrist_camera: str = field(
        default_factory=lambda: os.getenv(
            "SOARM_WRIST_CAMERA",
            "/dev/v4l/by-path/pci-0000:00:14.0-usb-0:5:1.0-video-index0",
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

    # 학습이 도는 기계. 주소와 계정은 이 저장소가 공개이므로 여기 적지 않고 `config/soarm.env`에
    # 둔다 — serial 포트나 카메라 경로와 달리 이것은 다른 기계로 가는 길이다.
    #
    # tailnet(MagicDNS) 이름을 쓰는 것을 권한다. mDNS의 `*.local`은 같은 LAN에서만 풀리고,
    # IPv6 링크로컬로 풀려 SSH가 조용히 타임아웃하는 일도 있다.
    spark_host: str = field(default_factory=lambda: os.getenv("SOARM_SPARK_HOST", ""))
    spark_user: str = field(default_factory=lambda: os.getenv("SOARM_SPARK_USER", ""))
    spark_dataset_root: str = field(
        default_factory=lambda: os.getenv("SOARM_SPARK_DATASET_ROOT", "data/soarm")
    )
    spark_output_root: str = field(
        default_factory=lambda: os.getenv("SOARM_SPARK_OUTPUT_ROOT", "outputs")
    )

    @property
    def effective_max_relative_target(self) -> float | None:
        """LeRobot에 넘길 상한. 걸릴 수 없는 값은 `None`으로 바꿔서 넘긴다.

        `None`이 아니기만 하면 LeRobot의 `send_action`은 목표를 자르기 위해 스텝마다
        `Present_Position`을 한 번 더 읽는다. LeRobot 자신이 그 자리에 "/!\ Slower fps
        expected due to reading from the follower"라고 적어 두었다. 상한이 가장 넓은 관절의
        폭보다 크면 그 읽기는 아무것도 자르지 못하므로, 30Hz 루프에서 카메라 두 대와 시리얼
        대역을 다투는 순수한 낭비가 된다.

        상한을 끄는 것이 아니라, 이미 꺼져 있는 상한이 물리던 비용만 없앤다.
        """
        if self.max_relative_target >= WIDEST_JOINT_SPAN:
            return None
        return self.max_relative_target

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
