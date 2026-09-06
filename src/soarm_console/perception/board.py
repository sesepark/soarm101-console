"""인쇄물의 치수. 맥 앱의 `scripts/make-calibration-targets.py`와 **같은 값**이어야 한다.

두 곳이 다른 숫자를 쓰면 아무 오류 없이 조용히 틀린다 — 검출은 되고, 풀리기도 하고,
좌표만 배율만큼 어긋난다. 그래서 값을 여기 한 곳에 모으고 `rig.json`에 그대로 적어
둔다. 인쇄한 뒤 캘리퍼로 잰 실제 값이 있으면 그것이 우선이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2


@dataclass(frozen=True)
class BoardSpec:
    """내부 파라미터를 구하는 ChArUco 보드."""

    dictionary: str = "DICT_5X5_100"
    squares_x: int = 7
    squares_y: int = 5
    square_m: float = 0.0250
    marker_m: float = 0.01875

    def build(self):
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))
        return cv2.aruco.CharucoBoard(
            (self.squares_x, self.squares_y), self.square_m, self.marker_m, dictionary
        )

    def detector(self):
        return cv2.aruco.CharucoDetector(self.build())

    def as_dict(self) -> dict[str, object]:
        return {
            "dictionary": self.dictionary,
            "squares_x": self.squares_x,
            "squares_y": self.squares_y,
            "square_m": self.square_m,
            "marker_m": self.marker_m,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> "BoardSpec":
        payload = payload or {}
        default = cls()
        return cls(
            dictionary=str(payload.get("dictionary", default.dictionary)),
            squares_x=int(payload.get("squares_x", default.squares_x)),
            squares_y=int(payload.get("squares_y", default.squares_y)),
            square_m=float(payload.get("square_m", default.square_m)),
            marker_m=float(payload.get("marker_m", default.marker_m)),
        )


@dataclass(frozen=True)
class MarkerSpec:
    """그리퍼에 붙이는 ArUco. 이 마커가 팔의 자세와 카메라를 잇는 유일한 다리다."""

    dictionary: str = "DICT_4X4_50"
    ids: tuple[int, ...] = (0, 1)
    length_m: float = 0.040

    def build(self):
        return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))

    def detector(self):
        return cv2.aruco.ArucoDetector(self.build(), cv2.aruco.DetectorParameters())

    def object_points(self):
        """마커 자신의 좌표계에서 네 코너. `solvePnP`가 먹는 순서(좌상→우상→우하→좌하)다."""
        import numpy as np

        half = self.length_m / 2.0
        return np.array(
            [[-half, half, 0.0], [half, half, 0.0], [half, -half, 0.0], [-half, -half, 0.0]],
            dtype=np.float64,
        )

    def as_dict(self) -> dict[str, object]:
        return {"dictionary": self.dictionary, "ids": list(self.ids), "length_m": self.length_m}

    @classmethod
    def from_dict(cls, payload: dict | None) -> "MarkerSpec":
        payload = payload or {}
        default = cls()
        ids = payload.get("ids", default.ids)
        if isinstance(ids, int):
            ids = (ids,)
        return cls(
            dictionary=str(payload.get("dictionary", default.dictionary)),
            ids=tuple(int(value) for value in ids),
            length_m=float(payload.get("length_m", default.length_m)),
        )
