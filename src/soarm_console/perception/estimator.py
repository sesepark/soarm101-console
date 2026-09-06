"""프레임 두 장에서 큐브의 base 좌표 세 숫자를 만든다.

**장치를 모른다.** 카메라를 열지도 닫지도 않고, 누가 프레임을 주든 같은 답을 낸다. 이것이
이 파일의 설계 전부다 — 콘솔의 장치 소유권 규칙(`owner_lock.py`) 아래에서는 카메라를 여는
상주 프로세스가 수집·정책과 공존할 수 없으므로, 추정기는 **그때그때 프레임을 쥔 쪽**이
부르는 순수 객체여야 한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import cube
from .store import ROLES, Rig


#: 이보다 오래된 값은 내주지 않는다. 프리뷰(`app.py`의 `PREVIEW_MAX_AGE_S`)와 같은 규칙이다.
#: 멈춘 카메라의 마지막 좌표를 계속 내주면 화면은 아무 일도 없다는 듯 그것을 보여 준다.
MAX_AGE_S = 3.0
#: 두 광선이 이보다 나쁜 조건수로 만나면 삼각측량을 믿지 않는다.
MAXIMUM_CONDITION = 1e4


@dataclass
class Estimate:
    """한 순간의 답. **모르면 `position`이 `None`이고 `reason`이 채워진다.**"""

    at: float
    position: np.ndarray | None = None
    source: str | None = None  # "triangulated" | "plane"
    spread_mm: float | None = None
    seen: dict[str, bool] = field(default_factory=dict)
    pixels: dict[str, tuple[float, float]] = field(default_factory=dict)
    reason: str | None = None

    @staticmethod
    def unknown(reason: str) -> dict[str, object]:
        """아직 한 번도 계산한 적이 없는 상태.

        나이가 없는 것과 오래된 것은 다른 일이다. 한 번도 프레임이 온 적이 없는데
        "영상이 3초 넘게 오지 않았습니다"라고 적으면, 사람은 카메라가 고장 났다고 읽고
        프리뷰를 켜면 된다는 것을 모른다.
        """
        return {
            "at": None,
            "age_s": None,
            "frame": "base_link",
            "position": None,
            "source": None,
            "disagreement_mm": None,
            "seen": {role: False for role in ROLES},
            "pixels": {},
            "reason": reason,
        }

    def as_dict(self, now: float | None = None) -> dict[str, object]:
        now = time.time() if now is None else now
        age = max(0.0, now - self.at)
        stale = age > MAX_AGE_S
        return {
            "at": self.at,
            "age_s": round(age, 3),
            "frame": "base_link",
            "position": None if (stale or self.position is None) else [
                round(float(value), 5) for value in self.position
            ],
            "source": None if stale else self.source,
            "disagreement_mm": None if self.spread_mm is None else round(self.spread_mm, 2),
            "seen": {role: bool(self.seen.get(role, False)) for role in ROLES},
            "pixels": {
                role: [round(float(x), 1), round(float(y), 1)]
                for role, (x, y) in self.pixels.items()
            },
            "reason": "영상이 3초 넘게 오지 않았습니다" if stale else self.reason,
        }


class ObjectEstimator:
    """리그 하나에 매인 추정기. 프레임을 주면 답을 돌려준다."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig

    def update(self, frames: dict[str, np.ndarray], at: float | None = None) -> Estimate:
        """`frames`는 역할 이름 → **BGR** ndarray. 없는 역할은 빼고 준다.

        색 순서를 못 박는 이유. `cameras.py`의 `VideoCapture`는 BGR을 주고 LeRobot 관측은
        RGB를 준다. 둘을 섞으면 주황이 파랑이 되어 아무것도 검출되지 않는데, 그때 화면에는
        "큐브를 찾지 못했습니다"만 뜨고 색이 뒤집혔다는 말은 어디에도 없다.
        """
        at = time.time() if at is None else at
        rig = self.rig
        estimate = Estimate(at=at)

        detections: dict[str, cube.Detection] = {}
        for role, image in frames.items():
            if role not in ROLES or image is None:
                continue
            found = cube.detect(
                image, rig.object.hsv_low, rig.object.hsv_high, rig.object.roi.get(role)
            )
            estimate.seen[role] = found is not None
            if found is not None:
                detections[role] = found
                estimate.pixels[role] = (float(found.centroid[0]), float(found.centroid[1]))

        ready = [role for role in detections if rig.is_ready(role)]
        if not ready:
            if not detections:
                estimate.reason = "두 카메라 모두 큐브를 찾지 못했습니다"
            else:
                found = ", ".join(sorted(detections))
                estimate.reason = f"{found} 카메라가 큐브를 보고 있지만 캘리브레이션이 아직 없습니다"
            return estimate

        rays = {
            role: cube.ray_from_pixel(
                rig.intrinsics[role], rig.extrinsics[role].base_to_camera, detections[role].centroid
            )
            for role in ready
        }
        views = [
            (rig.intrinsics[role], rig.extrinsics[role].base_to_camera, detections[role].centroid)
            for role in ready
        ]

        if len(rays) >= 2:
            point, spread, condition = cube.triangulate(list(rays.values()))
            estimate.spread_mm = spread
            if spread <= cube.MAXIMUM_SPREAD_MM and condition <= MAXIMUM_CONDITION:
                estimate.position = cube.refine_centre(point, views, rig.object.size_m)
                estimate.source = "triangulated"
                return estimate
            if condition > MAXIMUM_CONDITION:
                estimate.reason = "두 카메라가 거의 같은 방향에서 보고 있어 깊이를 정할 수 없습니다"
            else:
                estimate.reason = f"두 카메라가 {spread:.0f}mm 어긋납니다"
            # 어긋났다고 곧바로 포기하지 않는다. 큐브가 책상 위에 있다면 부감 한 대로도
            # 답이 나오고, 그 답이 어긋남의 원인(한쪽의 오검출)을 피해 간다.

        if rig.table_z is None:
            if estimate.reason is None:
                estimate.reason = "한 대만 보이는데 책상 높이를 아직 재지 않았습니다"
            return estimate

        plane_z = rig.table_z + rig.object.size_m[2] / 2.0
        # 평면과 크게 만나는 카메라부터 쓴다. 스치듯 보는 카메라는 같은 픽셀 오차가 수 cm다.
        best: tuple[float, str, np.ndarray] | None = None
        for role in ready:
            point, angle = cube.intersect_plane(rays[role], plane_z)
            if point is None or angle < cube.MINIMUM_PLANE_ANGLE_DEG:
                continue
            if best is None or angle > best[0]:
                best = (angle, role, point)
        if best is None:
            if estimate.reason is None:
                estimate.reason = "광선이 책상면을 너무 얕게 스쳐 자리를 정할 수 없습니다"
            return estimate

        _, role, point = best
        view = [(rig.intrinsics[role], rig.extrinsics[role].base_to_camera, detections[role].centroid)]
        estimate.position = cube.refine_centre(point, view, rig.object.size_m, fixed_z=plane_z)
        estimate.source = "plane"
        estimate.reason = None
        return estimate
