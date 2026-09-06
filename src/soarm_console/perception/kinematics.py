"""URDF 정기구학. 뷰어가 쓰는 것과 **같은 URDF 파일**을 읽는다.

`placo`를 들이지 않는 이유가 있다. LeRobot의 `RobotKinematics`가 그것을 요구하지만,
이 팔의 관절은 전부 국소 Z축 회전이라 4×4 동차변환을 순서대로 곱하는 것이 전부다.
의존성이 없으면 하드웨어도 시뮬레이터도 없이 단위 시험이 돌고, 같은 URDF를 읽는
뷰어의 `urdf-mini.js`와 값을 맞춰 볼 수도 있다.

**이것이 왜 카메라 코드에 있나.** 고정 카메라의 자리를 구할 때 우리는 자를 대지 않고
팔에게 묻는다(작업지시 §1.5). 그러려면 "관절값이 이럴 때 그리퍼는 베이스 기준 어디인가"를
답할 것이 필요하고, 그것이 이 파일이다.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np


#: 뷰어와 같은 파일. 두 곳이 다른 URDF를 읽으면 화면과 계산이 다른 팔을 그린다.
URDF_PATH = Path(__file__).resolve().parents[1] / "static/viewer/urdf/so101.urdf"

#: 정기구학의 끝. URDF가 이 이름으로 손끝 프레임을 따로 두고 있고, LeRobot의
#: `RobotKinematics`도 같은 것을 기본값으로 쓴다.
TIP_LINK = "gripper_frame_link"
ROOT_LINK = "base_link"


class KinematicsError(RuntimeError):
    pass


def rotation_from_rpy(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """URDF의 `rpy`를 회전 행렬로. 고정축 X→Y→Z 순서이므로 `Rz·Ry·Rx`다.

    순서를 뒤집으면 90°가 섞인 관절에서 축이 통째로 바뀐다. 이 URDF는 거의 모든 관절
    원점에 ±90°가 들어 있어서, 순서를 틀리면 팔이 전혀 다른 방향으로 뻗는다.
    """
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def rotation_about(axis: np.ndarray, angle: float) -> np.ndarray:
    """축 하나를 중심으로 도는 회전 (로드리게스)."""
    norm = float(np.linalg.norm(axis))
    if norm < 1e-12:
        return np.eye(3)
    k = axis / norm
    cross = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(angle) * cross + (1 - math.cos(angle)) * (cross @ cross)


def transform(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    return matrix


def invert(matrix: np.ndarray) -> np.ndarray:
    """동차변환의 역. 일반 역행렬을 부르지 않는 것은 정확하고 빠르기 때문이다."""
    rotation = matrix[:3, :3]
    result = np.eye(4)
    result[:3, :3] = rotation.T
    result[:3, 3] = -rotation.T @ matrix[:3, 3]
    return result


@dataclass(frozen=True)
class Joint:
    name: str
    movable: bool
    origin: np.ndarray  # 부모 링크에서 이 관절까지의 고정 변환 (4×4)
    axis: np.ndarray    # 관절 좌표계에서의 회전축 (3,)


@dataclass(frozen=True)
class Chain:
    """`base_link`에서 `gripper_frame_link`까지의 관절 사슬."""

    joints: tuple[Joint, ...]

    @property
    def movable_names(self) -> tuple[str, ...]:
        return tuple(joint.name for joint in self.joints if joint.movable)

    @classmethod
    def from_urdf(
        cls, path: Path | str = URDF_PATH, root: str = ROOT_LINK, tip: str = TIP_LINK
    ) -> "Chain":
        try:
            tree = ET.parse(str(path))
        except (OSError, ET.ParseError) as exc:
            raise KinematicsError(f"Cannot read URDF: {path}") from exc

        # 자식 링크로 관절을 찾을 수 있어야 끝에서 뿌리로 거슬러 올라간다. URDF는 트리라
        # 자식 하나에 부모 관절이 정확히 하나다.
        by_child: dict[str, ET.Element] = {}
        for element in tree.getroot().findall("joint"):
            child = element.find("child")
            if child is not None and child.get("link"):
                by_child[str(child.get("link"))] = element

        joints: list[Joint] = []
        link = tip
        # 링크 수보다 많이 돌면 URDF가 트리가 아니라는 뜻이다. 무한 루프로 매달리는 대신
        # 그 사실을 말한다.
        for _ in range(len(by_child) + 1):
            if link == root:
                break
            element = by_child.get(link)
            if element is None:
                raise KinematicsError(f"URDF has no path from {tip} to {root} (stuck at {link})")
            joints.append(cls._joint(element))
            parent = element.find("parent")
            link = str(parent.get("link")) if parent is not None else root
        else:
            raise KinematicsError(f"URDF chain from {tip} to {root} does not terminate")
        if link != root:
            raise KinematicsError(f"URDF has no path from {tip} to {root}")
        return cls(tuple(reversed(joints)))

    @staticmethod
    def _joint(element: ET.Element) -> Joint:
        origin = element.find("origin")
        xyz = _triple(origin.get("xyz") if origin is not None else None, (0.0, 0.0, 0.0))
        rpy = _triple(origin.get("rpy") if origin is not None else None, (0.0, 0.0, 0.0))
        axis_element = element.find("axis")
        axis = _triple(axis_element.get("xyz") if axis_element is not None else None, (0.0, 0.0, 1.0))
        kind = element.get("type", "fixed")
        return Joint(
            name=str(element.get("name", "")),
            movable=kind in {"revolute", "continuous"},
            origin=transform(rotation_from_rpy(*rpy), np.array(xyz)),
            axis=np.array(axis),
        )

    def forward(self, angles: Mapping[str, float]) -> np.ndarray:
        """관절 각도[rad]에서 `base_link → gripper_frame_link` 변환 (4×4).

        빠진 관절은 0으로 본다. 모르는 이름은 **조용히 무시하지 않고** 거절한다 —
        오타 하나가 "그 관절은 0이었다"로 흡수되면 몇 mm씩 어긋난 답이 잔차만 조금
        키운 채 그럴듯하게 나온다.
        """
        known = set(self.movable_names)
        unknown = sorted(set(angles) - known)
        if unknown:
            raise KinematicsError(f"URDF has no such joints: {unknown}")
        pose = np.eye(4)
        for joint in self.joints:
            pose = pose @ joint.origin
            if joint.movable:
                pose = pose @ transform(
                    rotation_about(joint.axis, float(angles.get(joint.name, 0.0))), np.zeros(3)
                )
        return pose

    def tip_position(self, angles: Mapping[str, float]) -> np.ndarray:
        return self.forward(angles)[:3, 3]


def _triple(text: str | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if not text:
        return default
    parts = text.split()
    if len(parts) != 3:
        return default
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return default


def urdf_angles(specs, present: Mapping[str, float]) -> dict[str, float]:
    """서보가 내준 값(도·퍼센트)을 URDF 라디안으로.

    변환식을 여기에 다시 쓰지 않고 `vleader/spec.py`의 `JointSpec.to_radians()`를 부른다.
    같은 계산이 두 벌 있으면 한쪽만 고쳐지는 날이 오고, 그날 카메라는 팔이 실제로 있는
    곳과 다른 곳을 보고 있다고 믿는다.
    """
    result: dict[str, float] = {}
    for spec in specs:
        if spec.name not in present:
            continue
        result[spec.urdf_joint] = spec.to_radians(float(present[spec.name]))
    return result
