"""큐브를 찾고, 픽셀에서 3D로 되돌린다.

세 가지 기하가 여기 모여 있다. 광선 만들기, 두 광선 합치기(삼각측량), 광선 하나를 책상
평면과 만나게 하기(평면 구속). 그리고 마지막에 한 가지 계통 오차를 없앤다 — 색 덩어리의
무게중심은 큐브 중심의 투영이 **아니다.**
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .intrinsics import Intrinsics


#: 이보다 작은 덩어리는 큐브로 보지 않는다. 640×480에서 22mm 큐브는 약 300픽셀이었으므로
#: (2026-09-06 실측) 60은 그 5분의 1이다. 반사광이나 잡티를 큐브로 오인하지 않을 만큼
#: 크고, 큐브가 멀거나 절반쯤 가려도 살아남을 만큼 작다.
MINIMUM_AREA_PX = 60
#: 두 광선이 이보다 더 어긋나면 값을 내지 않는다. 그리퍼 여유(한쪽 약 10mm)의 절반이다.
MAXIMUM_SPREAD_MM = 8.0
#: 광선이 책상 평면과 이보다 얕게 만나면 평면 구속을 쓰지 않는다. 스치는 각도에서는
#: 픽셀 하나가 수 cm가 되기 때문이다.
MINIMUM_PLANE_ANGLE_DEG = 15.0


@dataclass(frozen=True)
class Detection:
    """한 프레임에서 찾은 색 덩어리 하나."""

    centroid: np.ndarray  # (2,) 픽셀
    area: float
    bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class Ray:
    """base 좌표계의 광선 하나. 픽셀 하나가 말해 줄 수 있는 전부다."""

    origin: np.ndarray
    direction: np.ndarray  # 단위 벡터


def detect(
    image_bgr: np.ndarray,
    hsv_low: tuple[int, int, int],
    hsv_high: tuple[int, int, int],
    roi: tuple[int, int, int, int] | None = None,
    minimum_area: int = MINIMUM_AREA_PX,
) -> Detection | None:
    """색으로 큐브를 찾는다. 못 찾으면 `None`.

    **ROI가 왜 필수인가.** 측면 카메라에는 팔 베이스의 주황색 부품이 함께 잡힌다
    (2026-09-06 실측: 큐브 149px 옆에 81px·49px 두 덩이). 가장 큰 덩어리를 고르는 것만으로는
    큐브가 반쯤 가렸을 때 팔의 부품을 큐브라고 말하게 된다.

    HSV로 자르는 이유는 색상(Hue)이 밝기와 분리되어 있기 때문이다. RGB로 문턱을 잡으면
    같은 주황이 그늘에 들어가는 순간 세 채널이 함께 내려가 문턱 밖으로 나간다.
    """
    offset = np.zeros(2)
    view = image_bgr
    if roi is not None:
        x, y, width, height = roi
        x = max(0, int(x)); y = max(0, int(y))
        view = image_bgr[y : y + int(height), x : x + int(width)]
        offset = np.array([x, y], dtype=np.float64)
    if view.size == 0:
        return None

    hsv = cv2.cvtColor(view, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(hsv_low, np.uint8), np.array(hsv_high, np.uint8))
    # 한 픽셀짜리 잡티를 지우고 큐브 표면의 반사로 뚫린 구멍을 메운다. 이 두 줄이 없으면
    # 무게중심이 반사광 위치에 따라 프레임마다 흔들린다.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    best = 0
    best_area = 0
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area > best_area:
            best, best_area = index, area
    if best == 0 or best_area < minimum_area:
        return None
    x, y, width, height = (int(stats[best, i]) for i in range(4))
    return Detection(
        centroid=np.asarray(centroids[best], dtype=np.float64) + offset,
        area=float(best_area),
        bounds=(x + int(offset[0]), y + int(offset[1]), width, height),
    )


def ray_from_pixel(
    intrinsics: Intrinsics, base_to_camera: np.ndarray, pixel: np.ndarray
) -> Ray:
    """픽셀 하나를 base 좌표계의 광선으로."""
    direction_camera = intrinsics.rays(np.asarray(pixel, dtype=np.float64).reshape(1, 2))[0]
    camera_to_base = np.linalg.inv(base_to_camera)
    origin = camera_to_base[:3, 3]
    direction = camera_to_base[:3, :3] @ direction_camera
    return Ray(origin=origin, direction=direction / np.linalg.norm(direction))


def triangulate(rays: list[Ray]) -> tuple[np.ndarray, float, float]:
    """여러 광선에 가장 가까운 점. 점, 광선까지의 평균 거리[mm], 조건수.

    두 광선은 잡음 때문에 실제로 만나지 않으므로 교점 대신 거리 제곱합을 최소로 하는 점을
    찾는다. 광선 k가 `oₖ`에서 단위 방향 `dₖ`로 뻗을 때 점 p까지의 수직 거리 제곱은
    `‖(I − dₖdₖᵀ)(p − oₖ)‖²`이고(`I − ddᵀ`가 d에 수직인 성분만 남긴다), 합을 p로 미분해
    0으로 두면 3×3 선형계 하나가 된다.

    조건수를 함께 돌려주는 이유가 있다. 두 광선이 거의 평행하면 이 행렬이 특이에 가까워져
    답이 광선 방향으로 길게 늘어난다 — **잔차는 작은데 위치는 못 믿는 상태**다. 잔차만
    보면 이것을 놓친다.
    """
    if len(rays) < 2:
        raise ValueError("삼각측량에는 광선이 둘 이상 있어야 합니다")
    normal = np.zeros((3, 3))
    right = np.zeros(3)
    for ray in rays:
        projector = np.eye(3) - np.outer(ray.direction, ray.direction)
        normal += projector
        right += projector @ ray.origin
    condition = float(np.linalg.cond(normal))
    point = np.linalg.solve(normal, right)
    distances = [
        float(np.linalg.norm(np.cross(point - ray.origin, ray.direction))) for ray in rays
    ]
    return point, float(np.mean(distances)) * 1000.0, condition


def intersect_plane(ray: Ray, z: float) -> tuple[np.ndarray | None, float]:
    """광선과 수평면 `z`의 교점, 그리고 만나는 각도[도].

    각도를 함께 돌려주는 이유는 그것이 신뢰도이기 때문이다. 광선이 평면을 스치듯 지나면
    (측면 카메라가 그렇다) 픽셀 하나의 오차가 시선 방향으로 수 cm가 된다.
    """
    direction_z = float(ray.direction[2])
    angle = math.degrees(math.asin(min(1.0, abs(direction_z))))
    if abs(direction_z) < 1e-9:
        return None, angle
    distance = (z - float(ray.origin[2])) / direction_z
    if distance <= 0:
        return None, angle
    return ray.origin + distance * ray.direction, angle


def _hull_centroid(points: np.ndarray) -> np.ndarray:
    """다각형의 **면적** 무게중심. 꼭짓점 평균이 아니다.

    덩어리의 무게중심은 채워진 면적의 중심이므로, 꼭짓점 평균을 쓰면 꼭짓점이 몰린 쪽으로
    끌려간다.
    """
    hull = cv2.convexHull(points.astype(np.float32)).reshape(-1, 2).astype(np.float64)
    if hull.shape[0] < 3:
        return points.mean(axis=0)
    shifted = np.roll(hull, -1, axis=0)
    cross = hull[:, 0] * shifted[:, 1] - shifted[:, 0] * hull[:, 1]
    area = float(cross.sum()) / 2.0
    if abs(area) < 1e-12:
        return hull.mean(axis=0)
    centroid = ((hull + shifted) * cross[:, None]).sum(axis=0) / (6.0 * area)
    return centroid


def predicted_centroid(
    intrinsics: Intrinsics,
    base_to_camera: np.ndarray,
    centre: np.ndarray,
    size: tuple[float, float, float],
) -> np.ndarray | None:
    """중심이 `centre`인 큐브가 이 카메라에 만들 **덩어리의 무게중심**.

    큐브는 base 축에 나란하다고 본다. 22×22×16mm에서 yaw가 실루엣에 주는 영향은 작고,
    yaw는 애초에 정책의 관측에 없다(턱 개구 43mm > 대각선 31mm).
    """
    half = np.array(size, dtype=np.float64) / 2.0
    signs = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)], float)
    corners = np.asarray(centre, dtype=np.float64) + signs * half
    homogeneous = np.hstack([corners, np.ones((8, 1))])
    in_camera = (base_to_camera @ homogeneous.T).T[:, :3]
    if np.any(in_camera[:, 2] <= 1e-4):
        return None
    return _hull_centroid(intrinsics.project(in_camera))


def refine_centre(
    centre: np.ndarray,
    views: list[tuple[Intrinsics, np.ndarray, np.ndarray]],
    size: tuple[float, float, float],
    fixed_z: float | None = None,
    iterations: int = 4,
) -> np.ndarray:
    """가시면 중심 편향을 없앤다.

    색 덩어리의 무게중심은 큐브 중심의 투영이 아니라 **보이는 면들**의 중심이다. 정면에서
    보면 앞면 하나뿐이라 중심과 거의 같지만, 비스듬히 보면 윗면과 옆면이 함께 보여 무게
    중심이 카메라 쪽으로 밀린다. 22mm 큐브에서 수 mm이고 **방향이 일정한 계통 오차**라
    프레임을 아무리 평균해도 사라지지 않는다.

    그래서 앞으로 푼다. 후보 중심에서 "그 자리였다면 보였을 무게중심"을 계산해 측정값과의
    차이를 줄인다. `views`는 (내부 파라미터, base→cam, 측정된 무게중심).

    `fixed_z`가 있으면 높이를 고정하고 x·y만 움직인다 — 큐브가 책상 위에 있을 때는 높이가
    미지수가 아니라 아는 값이고, 미지수를 하나 줄이면 답이 훨씬 덜 흔들린다.
    """
    if not views:
        return centre
    current = np.asarray(centre, dtype=np.float64).copy()
    if fixed_z is not None:
        current[2] = fixed_z
    free = [0, 1] if fixed_z is not None else [0, 1, 2]
    step = 1e-4

    def residual(point: np.ndarray) -> np.ndarray | None:
        parts: list[np.ndarray] = []
        for intrinsics, base_to_camera, measured in views:
            predicted = predicted_centroid(intrinsics, base_to_camera, point, size)
            if predicted is None:
                return None
            parts.append(predicted - measured)
        return np.concatenate(parts)

    for _ in range(iterations):
        base = residual(current)
        if base is None:
            return current
        jacobian = np.zeros((base.size, len(free)))
        for column, axis in enumerate(free):
            shifted = current.copy()
            shifted[axis] += step
            moved = residual(shifted)
            if moved is None:
                return current
            jacobian[:, column] = (moved - base) / step
        try:
            delta, *_ = np.linalg.lstsq(jacobian, -base, rcond=None)
        except np.linalg.LinAlgError:
            return current
        candidate = current.copy()
        for column, axis in enumerate(free):
            candidate[axis] += float(delta[column])
        # 한 걸음이 큐브 크기를 넘으면 발산한 것이다. 그럴듯한 헛수를 두지 않는다.
        if np.linalg.norm(candidate - current) > 0.05:
            return current
        current = candidate
        if np.linalg.norm(delta) < 1e-6:
            break
    return current
