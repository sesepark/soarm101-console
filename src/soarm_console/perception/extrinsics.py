"""고정 카메라가 **팔의 좌표계 어디에 있는지**. 손-눈 캘리브레이션.

줄자로 재지 않는다. 팔에는 절대 오차 ε가 있는데(기어 백래시·캘리브레이션 오프셋·중력
처짐), 카메라를 팔의 정기구학을 통해 캘리브레이션하면 카메라 좌표계가 **같은 ε를 그대로
물려받아** 집는 순간에 상쇄된다. 자로 잰 참값을 쓰면 상쇄되지 않는다. 우리가 원하는 것은
참값이 아니라 "팔이 믿는 공간에서의 좌표"다.

미지수는 둘이고 자세마다 같은 식이 성립한다.

    T_marker→cam  =  T_base→cam · T_gripper→base(qᵢ) · T_marker→gripper

`cv2.calibrateRobotWorldHandEye`가 두 미지수의 초기해를 한 번에 주고, 그다음 **마커 네
코너의 재투영 오차**를 직접 줄이는 비선형 정제로 마무리한다. 초기해만 쓰지 않는 이유는
그것이 회전과 이동을 따로 푸는 근사여서, 우리가 실제로 신경 쓰는 양(픽셀에서 몇 개
어긋나는가)을 최소화하지 않기 때문이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .board import MarkerSpec
from .intrinsics import Intrinsics
from .kinematics import invert, transform


#: 이보다 적은 자세로는 풀지 않는다. 회전 미지수는 자세 쌍의 **회전축**에서 정보를 얻으므로
#: 자세가 적거나 한 축으로만 돌면 그 축 성분이 결정되지 않는다 — 그때 잔차는 작은데 답은
#: 틀린다. 가장 알아채기 어려운 실패라 개수로 한 번 막는다.
MINIMUM_POSES = 10
#: 재투영 잔차가 이보다 크면 저장하지 않는다.
MAXIMUM_RESIDUAL_PX = 3.0


class ExtrinsicsError(RuntimeError):
    pass


@dataclass
class PoseSample:
    """한 자세에서 모은 것. 팔이 아는 것과 카메라가 본 것."""

    #: 정기구학이 준 `base_link → gripper_frame_link` (4×4). 팔이 믿는 자기 자세다.
    gripper_in_base: np.ndarray
    #: 역할별 마커 네 코너의 픽셀 좌표 (4, 2). 못 본 카메라는 아예 넣지 않는다.
    corners: dict[str, np.ndarray]


@dataclass(frozen=True)
class Extrinsics:
    """카메라 한 대의 자리."""

    #: base 좌표를 카메라 좌표로 옮기는 변환 (4×4). 카메라의 자세는 이것의 역이다.
    base_to_camera: np.ndarray
    #: 그리퍼 좌표계에서 본 마커의 자세 (4×4). 재지 않고 함께 푼 값이다.
    marker_in_gripper: np.ndarray
    residual_px: float
    residual_mm: float
    poses: int

    @property
    def camera_in_base(self) -> np.ndarray:
        return invert(self.base_to_camera)

    @property
    def centre_in_base(self) -> np.ndarray:
        """카메라 렌즈 중심의 base 좌표. 모든 광선이 여기서 출발한다."""
        return self.camera_in_base[:3, 3]

    def as_dict(self) -> dict[str, object]:
        return {
            "T_base_cam": self.base_to_camera.tolist(),
            "T_gripper_marker": self.marker_in_gripper.tolist(),
            "residual_px": round(float(self.residual_px), 4),
            "residual_mm": round(float(self.residual_mm), 3),
            "poses": int(self.poses),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Extrinsics":
        return cls(
            base_to_camera=np.array(payload["T_base_cam"], dtype=np.float64),
            marker_in_gripper=np.array(payload["T_gripper_marker"], dtype=np.float64),
            residual_px=float(payload.get("residual_px", 0.0)),
            residual_mm=float(payload.get("residual_mm", 0.0)),
            poses=int(payload.get("poses", 0)),
        )


def _pose_from_vectors(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    rotation, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3, 1))
    return transform(rotation, np.asarray(tvec, dtype=np.float64).reshape(3))


def _vectors_from_pose(pose: np.ndarray) -> np.ndarray:
    rvec, _ = cv2.Rodrigues(pose[:3, :3])
    return np.concatenate([rvec.reshape(3), pose[:3, 3]])


def marker_pose_in_camera(
    intrinsics: Intrinsics, corners: np.ndarray, spec: MarkerSpec
) -> np.ndarray | None:
    """마커 네 코너에서 `T_marker→cam` (4×4).

    왜곡을 **먼저 펴고** 단위 행렬을 K로 써서 `solvePnP`를 부른다. 그래야 다항식 모델과
    어안 모델이 같은 코드로 지나간다 — `solvePnP`는 어안 왜곡 계수를 이해하지 못한다.
    """
    normalised = intrinsics.normalised(np.asarray(corners, dtype=np.float64))
    ok, rvec, tvec = cv2.solvePnP(
        spec.object_points(),
        normalised.reshape(-1, 1, 2),
        np.eye(3),
        np.zeros(5),
        flags=cv2.SOLVEPNP_IPPE_SQUARE,
    )
    if not ok:
        return None
    return _pose_from_vectors(rvec, tvec)


def _residuals(
    parameters: np.ndarray,
    gripper_poses: list[np.ndarray],
    measured: list[np.ndarray],
    object_points: np.ndarray,
    intrinsics: Intrinsics,
) -> np.ndarray:
    base_to_camera = _pose_from_vectors(parameters[0:3], parameters[3:6])
    marker_in_gripper = _pose_from_vectors(parameters[6:9], parameters[9:12])
    homogeneous = np.hstack([object_points, np.ones((object_points.shape[0], 1))])
    out: list[np.ndarray] = []
    for gripper_in_base, image_points in zip(gripper_poses, measured, strict=True):
        chain = base_to_camera @ gripper_in_base @ marker_in_gripper
        in_camera = (chain @ homogeneous.T).T[:, :3]
        # 카메라 뒤로 넘어간 점은 투영이 뒤집힌다. 큰 잔차로 밀어 두면 최적화가 그쪽으로
        # 가지 않는다.
        if np.any(in_camera[:, 2] <= 1e-4):
            out.append(np.full(object_points.shape[0] * 2, 1e3))
            continue
        projected = intrinsics.project(in_camera)
        out.append((projected - image_points).reshape(-1))
    return np.concatenate(out)


def _refine(
    initial: np.ndarray,
    gripper_poses: list[np.ndarray],
    measured: list[np.ndarray],
    object_points: np.ndarray,
    intrinsics: Intrinsics,
    iterations: int = 60,
) -> tuple[np.ndarray, float]:
    """재투영 오차를 직접 줄인다. 수치 야코비언 + Levenberg 감쇠.

    `scipy`를 들이지 않는다. 미지수가 12개뿐이라 야코비언 한 번이 12번의 전방 계산이고,
    그 정도는 numpy로 충분하다. 감쇠를 두는 이유는 초기해가 나쁠 때 Gauss-Newton이
    한 걸음에 발산하기 때문이다.
    """
    parameters = initial.copy()
    residual = _residuals(parameters, gripper_poses, measured, object_points, intrinsics)
    cost = float(residual @ residual)
    damping = 1e-3
    step = np.array([1e-5] * 3 + [1e-6] * 3 + [1e-5] * 3 + [1e-6] * 3)
    for _ in range(iterations):
        jacobian = np.empty((residual.size, parameters.size))
        for index in range(parameters.size):
            shifted = parameters.copy()
            shifted[index] += step[index]
            jacobian[:, index] = (
                _residuals(shifted, gripper_poses, measured, object_points, intrinsics) - residual
            ) / step[index]
        normal = jacobian.T @ jacobian
        gradient = jacobian.T @ residual
        improved = False
        for _ in range(8):
            try:
                delta = np.linalg.solve(normal + damping * np.diag(np.diag(normal) + 1e-12), -gradient)
            except np.linalg.LinAlgError:
                damping *= 10
                continue
            candidate = parameters + delta
            candidate_residual = _residuals(
                candidate, gripper_poses, measured, object_points, intrinsics
            )
            candidate_cost = float(candidate_residual @ candidate_residual)
            if candidate_cost < cost:
                parameters, residual, cost = candidate, candidate_residual, candidate_cost
                damping = max(damping / 10, 1e-9)
                improved = True
                break
            damping *= 10
        if not improved or cost < 1e-12:
            break
    rms = math.sqrt(cost / max(residual.size / 2, 1))
    return parameters, rms


def solve(
    samples: list[PoseSample], role: str, intrinsics: Intrinsics, spec: MarkerSpec
) -> Extrinsics:
    """한 카메라의 자리를 푼다."""
    usable = [sample for sample in samples if role in sample.corners]
    if len(usable) < MINIMUM_POSES:
        raise ExtrinsicsError(
            f"{role} 카메라가 마커를 본 자세가 {len(usable)}개뿐입니다. "
            f"{MINIMUM_POSES}개는 있어야 풉니다."
        )

    gripper_poses: list[np.ndarray] = []
    measured: list[np.ndarray] = []
    marker_poses: list[np.ndarray] = []
    for sample in usable:
        pose = marker_pose_in_camera(intrinsics, sample.corners[role], spec)
        if pose is None:
            continue
        gripper_poses.append(np.asarray(sample.gripper_in_base, dtype=np.float64))
        measured.append(np.asarray(sample.corners[role], dtype=np.float64).reshape(-1, 2))
        marker_poses.append(pose)
    if len(marker_poses) < MINIMUM_POSES:
        raise ExtrinsicsError(f"{role} 카메라의 마커 자세를 {len(marker_poses)}개밖에 풀지 못했습니다.")

    # OpenCV의 robot-world/hand-eye에 우리 배치를 끼워 넣는 방법.
    #
    #   OpenCV: T_world→cam · T_base→world = T_gripper→cam · T_base→gripper
    #   우리:   T_marker→cam · T_gripper→marker = T_base→cam · T_gripper→base
    #
    # 항끼리 맞추면 OpenCV의 `world`가 우리의 `marker`, OpenCV의 `base`가 우리의
    # `gripper`, OpenCV의 `gripper`가 우리의 `base`다. 즉 `R_base2gripper` 자리에
    # **정기구학이 준 값을 그대로**(뒤집지 않고) 넣고, `gripper2cam` 자리로 우리가 원하는
    # `T_base→cam`이 나온다. 뒤집어 넣으면 그럴듯한 잔차와 함께 틀린 답이 나오므로
    # 합성 데이터 시험(`tests/test_perception_extrinsics.py`)이 이 대응을 지킨다.
    base_to_world_r, base_to_world_t, gripper_to_cam_r, gripper_to_cam_t = (
        cv2.calibrateRobotWorldHandEye(
            [pose[:3, :3] for pose in marker_poses],
            [pose[:3, 3] for pose in marker_poses],
            [pose[:3, :3] for pose in gripper_poses],
            [pose[:3, 3] for pose in gripper_poses],
            method=cv2.CALIB_ROBOT_WORLD_HAND_EYE_SHAH,
        )
    )
    base_to_camera = transform(np.asarray(gripper_to_cam_r), np.asarray(gripper_to_cam_t).reshape(3))
    gripper_to_marker = transform(np.asarray(base_to_world_r), np.asarray(base_to_world_t).reshape(3))
    marker_in_gripper = invert(gripper_to_marker)

    initial = np.concatenate([_vectors_from_pose(base_to_camera), _vectors_from_pose(marker_in_gripper)])
    refined, residual_px = _refine(
        initial, gripper_poses, measured, spec.object_points(), intrinsics
    )
    base_to_camera = _pose_from_vectors(refined[0:3], refined[3:6])
    marker_in_gripper = _pose_from_vectors(refined[6:9], refined[9:12])

    if not math.isfinite(residual_px) or residual_px > MAXIMUM_RESIDUAL_PX:
        raise ExtrinsicsError(
            f"{role} 카메라의 재투영 잔차가 {residual_px:.2f}px입니다. "
            f"{MAXIMUM_RESIDUAL_PX}px 아래여야 씁니다 — 마커가 흔들렸거나 자세가 덜 다양합니다."
        )

    # 픽셀 잔차를 작업 거리에서의 mm로. 사람이 읽어야 하는 단위는 픽셀이 아니다.
    depths = []
    for gripper_in_base in gripper_poses:
        point = base_to_camera @ gripper_in_base @ marker_in_gripper @ np.array([0.0, 0.0, 0.0, 1.0])
        depths.append(float(point[2]))
    depth = float(np.median(depths)) if depths else 0.0
    focal = float(intrinsics.matrix[0, 0]) or 1.0
    residual_mm = residual_px * depth / focal * 1000.0

    return Extrinsics(
        base_to_camera=base_to_camera,
        marker_in_gripper=marker_in_gripper,
        residual_px=float(residual_px),
        residual_mm=float(residual_mm),
        poses=len(marker_poses),
    )
