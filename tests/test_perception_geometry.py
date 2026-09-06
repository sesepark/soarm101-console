"""광선·삼각측량·평면 구속·가시면 편향을 정답을 아는 자리에서 검증한다."""

from __future__ import annotations

import numpy as np
import pytest

from soarm_console.perception import cube
from soarm_console.perception.intrinsics import Intrinsics
from soarm_console.perception.kinematics import invert, transform


def _camera(eye, target, focal=520.0):
    forward = np.asarray(target, float) - np.asarray(eye, float)
    forward = forward / np.linalg.norm(forward)
    right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
    right = right / np.linalg.norm(right)
    down = np.cross(forward, right)
    camera_in_base = transform(np.column_stack([right, down, forward]), np.asarray(eye, float))
    intrinsics = Intrinsics(
        model="rational",
        matrix=np.array([[focal, 0.0, 320.0], [0.0, focal, 240.0], [0.0, 0.0, 1.0]]),
        distortion=np.zeros((1, 8)),
        width=640, height=480, rms_px=0.2, views=30,
    )
    return intrinsics, invert(camera_in_base)


#: 실물 리그를 흉내 낸 두 대. 하나는 비스듬한 부감, 하나는 책상 높이의 측면.
TOP = _camera([0.25, 0.45, 0.60], [0.22, 0.0, 0.05])
SIDE = _camera([0.20, -0.62, 0.10], [0.22, 0.0, 0.05])
SIZE = (0.022, 0.022, 0.016)


def _project_centre(camera, point):
    intrinsics, base_to_camera = camera
    homogeneous = np.append(np.asarray(point, float), 1.0)
    return intrinsics.project((base_to_camera @ homogeneous)[:3].reshape(1, 3))[0]


def test_a_pixel_is_a_ray_that_comes_back_to_its_point():
    truth = np.array([0.108, -0.164, 0.005])
    for camera in (TOP, SIDE):
        intrinsics, base_to_camera = camera
        ray = cube.ray_from_pixel(intrinsics, base_to_camera, _project_centre(camera, truth))
        # 점은 광선 위에 있어야 한다. 광선까지의 수직 거리로 잰다.
        distance = np.linalg.norm(np.cross(truth - ray.origin, ray.direction))
        assert distance < 1e-9


def test_two_rays_recover_the_point_and_report_their_disagreement():
    truth = np.array([0.108, -0.164, 0.040])
    rays = [
        cube.ray_from_pixel(*camera, _project_centre(camera, truth))
        for camera in (TOP, SIDE)
    ]
    point, spread, condition = cube.triangulate(rays)
    assert np.linalg.norm(point - truth) < 1e-9
    assert spread < 1e-6
    # 두 대가 크게 다른 방향에서 보므로 조건수가 나쁘지 않아야 한다.
    assert condition < 1e3


def test_disagreement_grows_when_one_camera_is_wrong():
    truth = np.array([0.108, -0.164, 0.040])
    good = cube.ray_from_pixel(*TOP, _project_centre(TOP, truth))
    # 측면 카메라가 5픽셀 빗나가면 그 어긋남이 mm로 드러나야 한다. 이 숫자가 화면의
    # `두 대가 N mm 어긋남`이고, 값을 낼지 말지의 근거다.
    wrong_pixel = _project_centre(SIDE, truth) + np.array([5.0, 0.0])
    bad = cube.ray_from_pixel(*SIDE, wrong_pixel)
    _, spread, _ = cube.triangulate([good, bad])
    assert spread > 1.0


def test_two_cameras_from_the_same_side_are_refused_by_the_condition_number():
    # 조건수가 잔차와 다른 것을 잡는다는 것이 요점이다. 거의 같은 방향에서 보면 답이
    # 광선 방향으로 길게 늘어나는데, 잔차는 작다.
    near = _camera([0.25, 0.45, 0.60], [0.22, 0.0, 0.05])
    also_near = _camera([0.252, 0.451, 0.601], [0.22, 0.0, 0.05])
    truth = np.array([0.108, -0.164, 0.040])
    rays = [
        cube.ray_from_pixel(*camera, _project_centre(camera, truth))
        for camera in (near, also_near)
    ]
    _, spread, condition = cube.triangulate(rays)
    assert spread < 1e-6
    assert condition > 1e4


def test_the_plane_constraint_needs_a_camera_that_is_not_grazing():
    truth = np.array([0.108, -0.164, 0.005])
    top_ray = cube.ray_from_pixel(*TOP, _project_centre(TOP, truth))
    point, angle = cube.intersect_plane(top_ray, 0.005)
    assert angle > cube.MINIMUM_PLANE_ANGLE_DEG
    assert np.linalg.norm(point - truth) < 1e-9

    side_ray = cube.ray_from_pixel(*SIDE, _project_centre(SIDE, truth))
    _, side_angle = cube.intersect_plane(side_ray, 0.005)
    # 책상 높이의 측면 카메라는 평면을 스치듯 본다. 그래서 평면 구속의 주인이 아니다.
    assert side_angle < cube.MINIMUM_PLANE_ANGLE_DEG


def test_visible_face_bias_is_real_and_the_refinement_removes_it():
    truth = np.array([0.108, -0.164, 0.005])
    views = []
    for camera in (TOP, SIDE):
        intrinsics, base_to_camera = camera
        measured = cube.predicted_centroid(intrinsics, base_to_camera, truth, SIZE)
        views.append((intrinsics, base_to_camera, measured))

    # 보정하지 않으면 — 즉 덩어리 중심을 큐브 중심의 투영으로 착각하면 — 치우친다.
    #
    # **크기를 실측해 두는 것이 이 시험의 요점이다.** 처음에는 이 편향을 "수 mm"로 적어
    # 두었는데 실제로 재 보니 이 배치에서 0.1~0.4mm였다. 큐브가 22mm로 작고 카메라가
    # 0.4~0.6m 떨어져 있어 원근이 실루엣에 주는 영향이 그만큼 작다. 즉 이 보정은
    # **오차 예산 5mm의 10% 이하를 다루는 마무리**이지 승부처가 아니다. 그래도 남겨 두는
    # 이유는 방향이 일정한 계통 오차라 프레임을 평균해도 사라지지 않고, 없애는 비용이
    # 8점 투영 몇 번뿐이기 때문이다.
    naive_rays = [
        cube.ray_from_pixel(intrinsics, base_to_camera, measured)
        for intrinsics, base_to_camera, measured in views
    ]
    naive, _, _ = cube.triangulate(naive_rays)
    bias_mm = float(np.linalg.norm(naive - truth)) * 1000
    assert 0.05 < bias_mm < 1.0, f"편향이 {bias_mm:.2f}mm다 — 실측해 둔 범위를 벗어났다"

    refined = cube.refine_centre(naive, views, SIZE)
    error_mm = float(np.linalg.norm(refined - truth)) * 1000
    assert error_mm < 0.05, f"보정 뒤에도 {error_mm:.3f}mm 남았다"
    assert error_mm < bias_mm / 5


def test_the_refinement_keeps_the_height_when_the_cube_is_on_the_table():
    truth = np.array([0.108, -0.164, 0.005])
    intrinsics, base_to_camera = TOP
    measured = cube.predicted_centroid(intrinsics, base_to_camera, truth, SIZE)
    ray = cube.ray_from_pixel(intrinsics, base_to_camera, measured)
    start, _ = cube.intersect_plane(ray, 0.005)
    refined = cube.refine_centre(
        start, [(intrinsics, base_to_camera, measured)], SIZE, fixed_z=0.005
    )
    # 높이는 아는 값이므로 미지수가 아니다. 그것을 풀게 두면 한 대에서 답이 흔들린다.
    assert refined[2] == pytest.approx(0.005, abs=1e-12)
    assert float(np.linalg.norm(refined[:2] - truth[:2])) * 1000 < 0.5
