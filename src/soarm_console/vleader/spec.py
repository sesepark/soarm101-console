from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from ..calibration import EXPECTED_MOTORS


# STS3215의 위치 해상도에서 1을 뺀 값. LeRobot의 `model_resolution_table`이 쓰는 것과 같은
# 숫자이고, 정규화 공식이 이 값을 그대로 쓰므로 여기서도 같은 값을 써야 한다.
TICKS = 4095

# 관절 순서. 화면에 늘어놓는 순서이자 `lerobot-record`가 기록하는 순서다. calibration
# 파일의 키 순서에 맡기면 JSON을 손으로 고칠 때마다 화면 순서가 바뀐다.
JOINT_ORDER: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)

LABELS = {
    "shoulder_pan": "어깨 회전",
    "shoulder_lift": "어깨 들기",
    "elbow_flex": "팔꿈치",
    "wrist_flex": "손목 굽힘",
    "wrist_roll": "손목 회전",
    "gripper": "집게",
}

# URDF의 관절 이름. `renesas-rdk/so_arm101_description`은 LeRobot의 모터 이름을 그대로
# 쓰므로 표가 항등식이다. 그래도 표로 남기는 이유는, 다른 URDF로 갈아탈 때 고칠 자리가
# 여기 하나여야 하기 때문이다.
URDF_JOINT = {name: name for name in JOINT_ORDER}

# 집게만 단위가 다르다. LeRobot의 SOFollower는 팔 관절 다섯 개를 도(degree)로, 집게를
# 0~100 퍼센트로 정규화한다(`MotorNormMode.RANGE_0_100`이 집게에만 하드코딩되어 있다).
# URDF의 gripper 관절은 0~1.74533 rad(= 0~100°)이므로 퍼센트를 그 구간에 그대로 편다.
GRIPPER_OPEN_RAD = 1.74533


class SpecError(RuntimeError):
    pass


@dataclass(frozen=True)
class JointSpec:
    """관절 하나의 계약.

    `minimum`/`maximum`은 **calibration 파일에서 계산한 값**이다. 추정값이 아니다 —
    SAFETY.md의 불변조건 4가 요구하는 것이 정확히 이것이고, 서버는 이 범위 밖의 목표를
    받지 않는다. LeRobot의 `_unnormalize()`가 도 단위에서는 값을 자르지 않으므로
    (RANGE_M100_100과 달리 clamp가 없다) 이 검사를 우리가 하지 않으면 범위 밖 각도가
    그대로 raw tick으로 바뀌어 모터에 실린다.
    """

    name: str
    label: str
    unit: str  # "deg" | "percent"
    minimum: float
    maximum: float
    urdf_joint: str
    # 화면과 URDF의 회전 방향이 반대로 보이면 여기만 -1로 뒤집는다. 실물을 보며 확인해야
    # 알 수 있는 값이라 config로 빼 두었다.
    urdf_sign: float = 1.0

    @property
    def span(self) -> float:
        return self.maximum - self.minimum

    def clamp(self, value: float) -> float:
        return min(self.maximum, max(self.minimum, value))

    def contains(self, value: float) -> bool:
        # 부동소수 오차로 한계 바로 위의 값이 거절되지 않도록 아주 작은 여유만 둔다.
        return self.minimum - 1e-6 <= value <= self.maximum + 1e-6

    def to_radians(self, value: float) -> float:
        """3D 뷰어가 쓸 URDF 관절 각도."""
        if self.unit == "percent":
            return self.urdf_sign * (value / 100.0) * GRIPPER_OPEN_RAD
        return self.urdf_sign * math.radians(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "unit": self.unit,
            "min": round(self.minimum, 4),
            "max": round(self.maximum, 4),
            "urdf_joint": self.urdf_joint,
            "urdf_sign": self.urdf_sign,
            # 뷰어가 도/퍼센트를 라디안으로 바꾸는 계수. 계산식을 클라이언트마다 다시 쓰면
            # 맥과 폰이 서로 다른 그림을 그리게 된다.
            "radians_per_unit": round(
                (GRIPPER_OPEN_RAD / 100.0) if self.unit == "percent" else math.radians(1.0), 8
            ),
        }


def _limits_from_calibration(motor: str, entry: dict[str, object]) -> tuple[float, float, str]:
    """calibration 한 줄을 LeRobot이 쓰는 단위의 절대 한계로.

    LeRobot의 `MotorsBus._normalize()`를 그대로 뒤집은 것이다. 도 단위는
    `(raw - mid) * 360 / 4095`이므로 범위는 0을 가운데 둔 대칭 구간이 되고, 퍼센트 단위는
    정의상 0~100이다. 공식을 옮겨 적는 대신 계산해 두는 이유는, calibration을 다시 잡으면
    범위가 달라지는데 그때 이 숫자가 저절로 따라와야 하기 때문이다.
    """
    try:
        low = float(entry["range_min"])
        high = float(entry["range_max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecError(f"Calibration range is unreadable for {motor}") from exc
    if high <= low:
        raise SpecError(f"Calibration range is invalid for {motor}")
    if motor == "gripper":
        return 0.0, 100.0, "percent"
    half = (high - low) / 2.0 * 360.0 / TICKS
    return -half, half, "deg"


def load_joint_specs(calibration_path: Path, signs: dict[str, float] | None = None) -> list[JointSpec]:
    """팔로워 calibration에서 관절 계약을 만든다."""
    try:
        payload = json.loads(Path(calibration_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"Cannot read follower calibration: {calibration_path}") from exc
    if not isinstance(payload, dict) or set(payload) != set(EXPECTED_MOTORS):
        raise SpecError(f"Follower calibration does not describe an SO-101: {calibration_path}")

    signs = signs or {}
    specs: list[JointSpec] = []
    for name in JOINT_ORDER:
        minimum, maximum, unit = _limits_from_calibration(name, payload[name])
        specs.append(
            JointSpec(
                name=name,
                label=LABELS[name],
                unit=unit,
                minimum=minimum,
                maximum=maximum,
                urdf_joint=URDF_JOINT[name],
                urdf_sign=float(signs.get(name, 1.0)),
            )
        )
    return specs
