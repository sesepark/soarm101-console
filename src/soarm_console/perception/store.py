"""리그 캘리브레이션을 디스크에 두는 자리. `runtime/perception/rig.json` 하나다.

정책 상태(`runtime/policy/status.json`)와 같은 자리 규칙을 쓴다. 파일 하나에 모으는 이유는
**부분만 최신인 상태를 만들지 않기 위해서**다 — 내부 파라미터와 외부 파라미터가 다른 파일에
있으면 렌즈를 다시 잡고 외부를 갱신하지 않은 조합이 조용히 살아남는다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .board import BoardSpec, MarkerSpec
from .extrinsics import Extrinsics
from .intrinsics import Intrinsics


RUNTIME_DIR = Path(__file__).resolve().parents[3] / "runtime/perception"
RIG_PATH = RUNTIME_DIR / "rig.json"
SCHEMA = 1

ROLES = ("scene", "wrist")


@dataclass
class ObjectSpec:
    """찾을 물체. 시뮬의 큐브와 **같은 치수**여야 한다."""

    #: 22 × 22 × 16 mm. `joint_pos_env_cfg.py`의 `OBJECT_SIZE`와 같은 값이다.
    size_m: tuple[float, float, float] = (0.022, 0.022, 0.016)
    #: 주황. 색상(Hue)은 OpenCV에서 0~179다.
    hsv_low: tuple[int, int, int] = (3, 120, 90)
    hsv_high: tuple[int, int, int] = (25, 255, 255)
    #: 역할별 작업 영역. 측면 카메라에는 팔 베이스의 주황 부품이 함께 잡히므로 필수다.
    roi: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "size_m": list(self.size_m),
            "hsv_low": list(self.hsv_low),
            "hsv_high": list(self.hsv_high),
            "roi": {role: list(box) for role, box in self.roi.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ObjectSpec":
        payload = payload or {}
        default = cls()
        roi = {}
        for role, box in (payload.get("roi") or {}).items():
            if isinstance(box, (list, tuple)) and len(box) == 4:
                roi[str(role)] = tuple(int(value) for value in box)
        return cls(
            size_m=tuple(float(v) for v in payload.get("size_m", default.size_m)),
            hsv_low=tuple(int(v) for v in payload.get("hsv_low", default.hsv_low)),
            hsv_high=tuple(int(v) for v in payload.get("hsv_high", default.hsv_high)),
            roi=roi,
        )


@dataclass
class Rig:
    """두 카메라의 캘리브레이션 전체."""

    board: BoardSpec = field(default_factory=BoardSpec)
    marker: MarkerSpec = field(default_factory=MarkerSpec)
    object: ObjectSpec = field(default_factory=ObjectSpec)
    intrinsics: dict[str, Intrinsics] = field(default_factory=dict)
    extrinsics: dict[str, Extrinsics] = field(default_factory=dict)
    #: 팔 베이스에서 책상 윗면까지의 높이 [m]. 평면 구속이 쓴다.
    #:
    #: `None`은 "아직 재지 않았다"이지 0이 아니다. 0으로 두면 평면 구속이 조용히 3~5mm
    #: 틀린 답을 계속 낸다. 외부 캘리브레이션이 끝나면 그때 실측해 채운다.
    table_z: float | None = None
    calibrated_at: float | None = None

    def is_ready(self, role: str) -> bool:
        return role in self.intrinsics and role in self.extrinsics

    @property
    def is_calibrated(self) -> bool:
        return all(self.is_ready(role) for role in ROLES)

    @property
    def problems(self) -> list[str]:
        """사람이 다음에 무엇을 해야 하는지. 비어 있으면 아무 말도 하지 않는다."""
        notes: list[str] = []
        for role in ROLES:
            if role not in self.intrinsics:
                notes.append(f"{role} 카메라의 렌즈(내부 파라미터)가 아직 없습니다.")
            elif role not in self.extrinsics:
                notes.append(f"{role} 카메라의 자리(외부 파라미터)가 아직 없습니다.")
        if self.is_calibrated and self.table_z is None:
            notes.append("책상 높이를 아직 재지 않았습니다. 한 대만 보일 때 좌표를 낼 수 없습니다.")
        if not self.object.roi:
            notes.append("작업 영역(ROI)이 비어 있어 팔의 주황 부품이 큐브로 잡힐 수 있습니다.")
        return notes

    def camera_summary(self, role: str) -> dict[str, object]:
        summary: dict[str, object] = {
            "intrinsics": role in self.intrinsics,
            "extrinsics": role in self.extrinsics,
        }
        if (intrinsics := self.intrinsics.get(role)) is not None:
            summary["model"] = intrinsics.model
            summary["rms_px"] = round(intrinsics.rms_px, 3)
            summary["views"] = intrinsics.views
            summary["fov_deg"] = round(intrinsics.horizontal_fov_deg, 1)
        if (extrinsics := self.extrinsics.get(role)) is not None:
            summary["residual_px"] = round(extrinsics.residual_px, 3)
            summary["residual_mm"] = round(extrinsics.residual_mm, 2)
            summary["poses"] = extrinsics.poses
        return summary

    def status(self) -> dict[str, object]:
        return {
            "calibrated": self.is_calibrated,
            "calibrated_at": self.calibrated_at,
            "table_z": self.table_z,
            "cameras": {role: self.camera_summary(role) for role in ROLES},
            "problems": self.problems,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "calibrated_at": self.calibrated_at,
            "table_z": self.table_z,
            "board": self.board.as_dict(),
            "marker": self.marker.as_dict(),
            "object": self.object.as_dict(),
            "cameras": {
                role: {
                    **(self.intrinsics[role].as_dict() if role in self.intrinsics else {}),
                    **(self.extrinsics[role].as_dict() if role in self.extrinsics else {}),
                }
                for role in ROLES
                if role in self.intrinsics or role in self.extrinsics
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Rig":
        rig = cls(
            board=BoardSpec.from_dict(payload.get("board")),
            marker=MarkerSpec.from_dict(payload.get("marker")),
            object=ObjectSpec.from_dict(payload.get("object")),
            table_z=payload.get("table_z"),
            calibrated_at=payload.get("calibrated_at"),
        )
        for role, camera in (payload.get("cameras") or {}).items():
            if not isinstance(camera, dict):
                continue
            if "K" in camera:
                rig.intrinsics[str(role)] = Intrinsics.from_dict(camera)
            if "T_base_cam" in camera:
                rig.extrinsics[str(role)] = Extrinsics.from_dict(camera)
        return rig


def load(path: Path = RIG_PATH) -> Rig:
    """없거나 깨졌으면 **빈 리그**를 돌려준다. 깨진 파일에 매달려 화면 전체를 세우지 않는다."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return Rig()
    if not isinstance(payload, dict):
        return Rig()
    try:
        return Rig.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return Rig()


def save(rig: Rig, path: Path = RIG_PATH) -> None:
    """반쯤 쓰인 파일을 남기지 않는다. 옆에 쓰고 자리를 통째로 바꾼다."""
    rig.calibrated_at = time.time()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(rig.as_dict(), ensure_ascii=False, indent=2, default=_encode), encoding="utf-8"
    )
    os.replace(temporary, target)


def _encode(value: object) -> object:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(f"JSON으로 옮길 수 없는 값입니다: {type(value)!r}")
