"""렌즈를 아는 일. 픽셀 ↔ 카메라 좌표계의 광선.

**왜 두 모델을 다 푸나.** 이 리그의 렌즈는 화각 100~120°로 추정되는 광각이고, 그 구간은
다항식 모델(`CALIB_RATIONAL_MODEL`, k1..k6)과 등거리 모델(`cv2.fisheye`, r = f·θ)의 경계다.
어느 쪽이 이 렌즈를 잘 근사하는지는 재 보기 전에는 알 수 없다. 그래서 고르지 않고 둘 다
풀어 **재투영 오차가 낮은 쪽을 채택하고 두 값을 모두 기록한다.** 추정으로 하나를 고르면
나중에 좌표가 틀렸을 때 그것이 원인인지 아닌지를 가릴 수 없다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .board import BoardSpec


#: 이보다 적으면 풀지 않는다. 미지수(초점 2, 주점 2, 왜곡 8)에 비해 장이 적으면 답이
#: 데이터에 맞춰 휘어 버린다 — 잔차는 작은데 화면 밖에서 엉뚱해진다.
MINIMUM_VIEWS = 12
#: 이보다 나쁘면 저장하지 않는다. 나쁜 캘리브레이션을 저장하면 그 뒤 모든 좌표가
#: 조용히 틀리고, 아무도 여기를 의심하지 않는다.
MAXIMUM_RMS_PX = 1.0
#: 한 장으로 인정할 최소 코너 수. 너무 적게 보이는 장은 자세가 불안정해 잔차만 키운다.
MINIMUM_CORNERS = 8


class IntrinsicsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Intrinsics:
    """카메라 한 대의 내부 파라미터."""

    model: str  # "rational" | "fisheye"
    matrix: np.ndarray
    distortion: np.ndarray
    width: int
    height: int
    rms_px: float
    views: int
    #: 채택되지 않은 쪽의 오차. 왜 이 모델을 골랐는지가 값과 함께 남는다.
    alternatives: dict[str, float] = field(default_factory=dict)

    @property
    def is_fisheye(self) -> bool:
        return self.model == "fisheye"

    def normalised(self, points_px: np.ndarray) -> np.ndarray:
        """픽셀을 **왜곡을 편** 정규화 좌표로. 이것이 곧 카메라 좌표계의 광선 방향이다.

        돌려주는 것은 `(x, y)`이고 광선은 `(x, y, 1)`을 정규화한 것이다. 왜곡을 여기서
        펴 두면 그 뒤의 기하는 이상적인 핀홀 하나로 끝난다.
        """
        points = np.asarray(points_px, dtype=np.float64).reshape(-1, 1, 2)
        if self.is_fisheye:
            undistorted = cv2.fisheye.undistortPoints(points, self.matrix, self.distortion)
        else:
            undistorted = cv2.undistortPoints(points, self.matrix, self.distortion)
        return undistorted.reshape(-1, 2)

    def rays(self, points_px: np.ndarray) -> np.ndarray:
        """카메라 좌표계의 **단위** 광선 방향 (N, 3)."""
        normalised = self.normalised(points_px)
        rays = np.hstack([normalised, np.ones((normalised.shape[0], 1))])
        return rays / np.linalg.norm(rays, axis=1, keepdims=True)

    def project(self, points_cam: np.ndarray) -> np.ndarray:
        """카메라 좌표계의 3D 점을 픽셀로. 왜곡을 **다시 입힌다.**"""
        points = np.asarray(points_cam, dtype=np.float64).reshape(-1, 1, 3)
        zero = np.zeros(3)
        if self.is_fisheye:
            projected, _ = cv2.fisheye.projectPoints(points, zero, zero, self.matrix, self.distortion)
        else:
            projected, _ = cv2.projectPoints(points, zero, zero, self.matrix, self.distortion)
        return projected.reshape(-1, 2)

    @property
    def horizontal_fov_deg(self) -> float:
        """실측된 화각. 문서에 적어 둔 `추정`을 여기서 실제 값으로 바꾼다."""
        return 2.0 * math.degrees(math.atan(self.width / (2.0 * float(self.matrix[0, 0]))))

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "K": self.matrix.tolist(),
            "dist": np.asarray(self.distortion).ravel().tolist(),
            "width": self.width,
            "height": self.height,
            "rms_px": round(float(self.rms_px), 4),
            "views": int(self.views),
            "fov_deg": round(self.horizontal_fov_deg, 1),
            "alternatives": {k: round(float(v), 4) for k, v in self.alternatives.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Intrinsics":
        distortion = np.array(payload["dist"], dtype=np.float64)
        model = str(payload.get("model", "rational"))
        # fisheye는 (4,1)을, 그 밖은 (1,N)을 기대한다. 모양이 틀리면 OpenCV가 죽는다.
        distortion = distortion.reshape(-1, 1) if model == "fisheye" else distortion.reshape(1, -1)
        return cls(
            model=model,
            matrix=np.array(payload["K"], dtype=np.float64),
            distortion=distortion,
            width=int(payload["width"]),
            height=int(payload["height"]),
            rms_px=float(payload.get("rms_px", 0.0)),
            views=int(payload.get("views", 0)),
            alternatives={str(k): float(v) for k, v in (payload.get("alternatives") or {}).items()},
        )


@dataclass
class BoardView:
    """한 장에서 읽은 ChArUco 코너들."""

    corners: np.ndarray  # (N, 1, 2)
    ids: np.ndarray      # (N, 1)

    @property
    def count(self) -> int:
        return int(self.corners.shape[0])

    def signature(self, width: int, height: int) -> np.ndarray:
        """이 장이 앞의 장들과 얼마나 다른지 재기 위한 네 숫자.

        보드를 같은 자리에서 여러 번 찍어 봐야 새 정보가 없다. 왜곡 계수는 **보드가
        화면의 다른 자리, 다른 크기, 다른 기울기로 있을 때** 결정되므로, 중심 · 크기 ·
        기울기가 충분히 다른 장만 담는다.
        """
        points = self.corners.reshape(-1, 2)
        centre = points.mean(axis=0) / np.array([width, height])
        extent = points.max(axis=0) - points.min(axis=0)
        scale = float(np.linalg.norm(extent)) / float(np.hypot(width, height))
        # 가장 멀리 떨어진 두 코너가 이루는 방향. 보드를 기울이면 이 값이 돈다.
        spread = points - points.mean(axis=0)
        _, _, basis = np.linalg.svd(spread, full_matrices=False)
        tilt = math.atan2(float(basis[0, 1]), float(basis[0, 0])) / math.pi
        return np.array([centre[0], centre[1], scale, tilt])

    def coverage(self, width: int, height: int) -> set[tuple[int, int]]:
        """이 장이 덮은 3×3 칸들."""
        points = self.corners.reshape(-1, 2)
        cells: set[tuple[int, int]] = set()
        for x, y in points:
            column = min(2, max(0, int(x / max(width, 1) * 3)))
            row = min(2, max(0, int(y / max(height, 1) * 3)))
            cells.add((row, column))
        return cells


def detect(image_bgr: np.ndarray, detector) -> BoardView | None:
    """한 프레임에서 ChArUco 코너를 읽는다. 못 읽으면 `None`."""
    grey = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    corners, ids, _, _ = detector.detectBoard(grey)
    if corners is None or ids is None or len(ids) < MINIMUM_CORNERS:
        return None
    return BoardView(
        corners=np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2),
        ids=np.asarray(ids, dtype=np.int32).reshape(-1, 1),
    )


def solve(views: list[BoardView], spec: BoardSpec, size: tuple[int, int]) -> Intrinsics:
    """모은 장으로 내부 파라미터를 푼다. 두 모델을 모두 풀어 좋은 쪽을 돌려준다."""
    if len(views) < MINIMUM_VIEWS:
        raise IntrinsicsError(f"장이 {len(views)}개뿐입니다. {MINIMUM_VIEWS}장은 모여야 풉니다.")
    board = spec.build()
    width, height = size

    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    for view in views:
        objects, images = board.matchImagePoints(view.corners, view.ids)
        if objects is None or images is None or len(objects) < MINIMUM_CORNERS:
            continue
        object_points.append(np.asarray(objects, dtype=np.float64).reshape(-1, 1, 3))
        image_points.append(np.asarray(images, dtype=np.float64).reshape(-1, 1, 2))
    if len(object_points) < MINIMUM_VIEWS:
        raise IntrinsicsError(f"쓸 수 있는 장이 {len(object_points)}개뿐입니다.")

    results: dict[str, tuple[float, np.ndarray, np.ndarray]] = {}

    try:
        rms, matrix, distortion, _, _ = cv2.calibrateCamera(
            object_points, image_points, (width, height), None, None,
            flags=cv2.CALIB_RATIONAL_MODEL,
        )
        results["rational"] = (float(rms), matrix, distortion)
    except cv2.error as exc:  # noqa: BLE001 - 한 모델이 실패해도 다른 모델은 볼 수 있다
        results["rational"] = (float("inf"), np.eye(3), np.zeros((1, 8)))
        del exc

    try:
        # fisheye는 장마다 같은 개수의 점을 요구하지 않지만, 모양에 예민하다.
        fisheye_objects = [points.reshape(1, -1, 3) for points in object_points]
        fisheye_images = [points.reshape(1, -1, 2) for points in image_points]
        matrix = np.eye(3)
        distortion = np.zeros((4, 1))
        rms, matrix, distortion, _, _ = cv2.fisheye.calibrate(
            fisheye_objects, fisheye_images, (width, height), matrix, distortion,
            flags=cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-6),
        )
        results["fisheye"] = (float(rms), matrix, distortion)
    except cv2.error:  # noqa: BLE001 - 평면 보드에서 어안 솔버는 자주 발산한다
        pass

    model = min(results, key=lambda key: results[key][0])
    rms, matrix, distortion = results[model]
    if not math.isfinite(rms) or rms > MAXIMUM_RMS_PX:
        raise IntrinsicsError(
            f"재투영 오차가 {rms:.2f}px입니다. {MAXIMUM_RMS_PX}px 아래로 내려가야 씁니다 — "
            "보드를 화면 구석까지, 여러 기울기로 다시 모으세요."
        )
    return Intrinsics(
        model=model,
        matrix=np.asarray(matrix, dtype=np.float64),
        distortion=np.asarray(distortion, dtype=np.float64),
        width=width,
        height=height,
        rms_px=rms,
        views=len(object_points),
        alternatives={key: value[0] for key, value in results.items() if key != model},
    )
