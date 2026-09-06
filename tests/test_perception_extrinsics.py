"""손-눈 캘리브레이션을 **정답을 아는 합성 데이터**로 검증한다.

이 시험이 있는 이유. `cv2.calibrateRobotWorldHandEye`는 카메라가 그리퍼에 붙고 표적이
세상에 고정된 배치를 위해 만들어졌는데, 우리 리그는 그 반대다(카메라 고정, 마커가 팔에).
끼워 넣는 대응을 잘못 잡으면 **그럴듯한 잔차와 함께 틀린 답**이 나온다 — 실물에서는
알아챌 방법이 사실상 없다. 그래서 정답을 아는 자리에서 먼저 못 박는다.
"""

from __future__ import annotations

import numpy as np
import pytest

from soarm_console.perception.board import MarkerSpec
from soarm_console.perception.extrinsics import PoseSample, solve
from soarm_console.perception.intrinsics import Intrinsics
from soarm_console.perception.kinematics import Chain, invert, rotation_from_rpy, transform


def _intrinsics() -> Intrinsics:
    matrix = np.array([[520.0, 0.0, 320.0], [0.0, 520.0, 240.0], [0.0, 0.0, 1.0]])
    return Intrinsics(
        model="rational",
        matrix=matrix,
        distortion=np.zeros((1, 8)),
        width=640,
        height=480,
        rms_px=0.2,
        views=30,
    )


def _look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    """`eye`에서 `target`을 보는 카메라의 자세 (base 기준 4×4).

    OpenCV의 카메라 좌표계는 +Z가 앞, +Y가 아래다. rpy를 손으로 지어내는 것보다 이렇게
    만드는 편이 실수가 없다 — 실물 리그도 "저기를 본다"로 설치하지 각도로 설치하지 않는다.
    """
    forward = target - eye
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)
    return transform(np.column_stack([right, down, forward]), eye)


def _truth() -> tuple[np.ndarray, np.ndarray]:
    """정답. 카메라는 작업 영역을 내려다보는 비스듬한 부감, 마커는 그리퍼에서 조금 옆으로."""
    camera_in_base = _look_at(np.array([0.25, 0.55, 0.60]), np.array([0.25, 0.0, 0.15]))
    marker_in_gripper = transform(
        rotation_from_rpy(0.05, -0.1, 0.3), np.array([0.012, -0.008, 0.030])
    )
    return invert(camera_in_base), marker_in_gripper


def _samples(rng: np.random.Generator, count: int, noise_px: float) -> list[PoseSample]:
    """팔을 여러 자세로 돌리며 마커 코너를 투영해 만든 관측."""
    chain = Chain.from_urdf()
    intrinsics = _intrinsics()
    base_to_camera, marker_in_gripper = _truth()
    spec = MarkerSpec()
    corners = np.hstack([spec.object_points(), np.ones((4, 1))])

    samples: list[PoseSample] = []
    for _ in range(count * 4):
        if len(samples) >= count:
            break
        # 회전축이 다양해야 회전 미지수가 결정된다. 한 축으로만 돌리면 축퇴한다.
        angles = {
            "shoulder_pan": float(rng.uniform(-0.9, 0.4)),
            "shoulder_lift": float(rng.uniform(-0.5, 0.9)),
            "elbow_flex": float(rng.uniform(-0.6, 0.9)),
            "wrist_flex": float(rng.uniform(-0.9, 1.2)),
            "wrist_roll": float(rng.uniform(-1.2, 1.2)),
        }
        gripper_in_base = chain.forward(angles)
        in_camera = (base_to_camera @ gripper_in_base @ marker_in_gripper @ corners.T).T[:, :3]
        if np.any(in_camera[:, 2] < 0.15):
            continue
        pixels = intrinsics.project(in_camera)
        if np.any(pixels < 0) or np.any(pixels[:, 0] > 640) or np.any(pixels[:, 1] > 480):
            continue
        if noise_px:
            pixels = pixels + rng.normal(0.0, noise_px, pixels.shape)
        samples.append(PoseSample(gripper_in_base=gripper_in_base, corners={"scene": pixels}))
    return samples


def test_recovers_the_camera_pose_exactly_without_noise():
    samples = _samples(np.random.default_rng(7), 24, noise_px=0.0)
    assert len(samples) >= 20, "합성 자세가 충분히 모이지 않았다"
    result = solve(samples, "scene", _intrinsics(), MarkerSpec())

    truth_base_to_camera, truth_marker = _truth()
    # 카메라 중심이 0.1mm 안으로 돌아와야 한다.
    error = np.linalg.norm(result.centre_in_base - invert(truth_base_to_camera)[:3, 3])
    assert error < 1e-4, f"카메라 중심이 {error * 1000:.3f}mm 어긋났다"
    # 마커 오프셋도 함께 풀린다 — 재지 않는다는 설계의 근거다.
    offset = np.linalg.norm(result.marker_in_gripper[:3, 3] - truth_marker[:3, 3])
    assert offset < 1e-4, f"마커 오프셋이 {offset * 1000:.3f}mm 어긋났다"
    assert result.residual_px < 0.01


def test_survives_realistic_pixel_noise():
    samples = _samples(np.random.default_rng(11), 24, noise_px=0.5)
    result = solve(samples, "scene", _intrinsics(), MarkerSpec())
    truth_base_to_camera, _ = _truth()
    error = np.linalg.norm(result.centre_in_base - invert(truth_base_to_camera)[:3, 3])
    # 코너 검출이 0.5px 흔들려도 카메라 자리는 5mm 안이어야 한다. 이보다 나쁘면
    # 실물에서 5mm 목표가 성립하지 않는다.
    assert error < 5e-3, f"잡음 0.5px에서 카메라 중심이 {error * 1000:.1f}mm 어긋났다"
    assert result.residual_px < 1.5


def test_refuses_when_there_are_too_few_poses():
    samples = _samples(np.random.default_rng(3), 6, noise_px=0.0)
    with pytest.raises(Exception) as excinfo:
        solve(samples, "scene", _intrinsics(), MarkerSpec())
    # 자세가 적으면 회전이 축퇴해 잔차만 작고 답은 틀린다. 개수로 먼저 막는다.
    assert "자세" in str(excinfo.value)
