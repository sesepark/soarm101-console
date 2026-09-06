"""외부 파라미터 모드의 자식 프로세스. **팔이 혼자 움직인다.**

`policying.py`와 같은 골격이다 — owner lock을 쥐고, SIGTERM에 스스로 서고,
`runtime/perception/status.json`에 진행을 적고, 끝나면 시작 자세로 돌아간다. 팔을 옮기는
것은 새로 만들지 않고 재생·정책 복귀가 쓰는 `replaying.align()`과 `REPLAY_ALIGNMENT`를
그대로 쓴다: 사람 손이 리더에 닿아 있지 않은 자동 동작이라 느린 쪽이 맞다.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from ..calibration import validate_calibration
from ..config import Settings
from ..owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover
from ..replaying import REPLAY_ALIGNMENT, _connect, align, present_position
from ..v4l2_controls import apply_perception_controls
from ..vleader.spec import load_joint_specs
from . import extrinsics as extrinsics_module
from . import poses as poses_module
from . import store
from .kinematics import Chain, urdf_angles


RUNTIME_DIR = store.RUNTIME_DIR
STATUS_PATH = RUNTIME_DIR / "status.json"
PREVIEW_DIR = RUNTIME_DIR / "preview"

#: 자세에 도착한 뒤 이만큼 기다렸다가 찍는다. 서보가 멈춰도 링크는 잠깐 더 흔들리고,
#: 그 흔들림은 마커 코너를 픽셀 단위로 밀어 잔차에 그대로 들어간다.
SETTLE_S = 0.6
#: 버퍼에 남은 옛 프레임을 흘려보내는 장수. `VideoCapture`는 드라이버 큐를 들고 있어서,
#: 한 장만 읽으면 팔이 아직 움직이던 시절의 그림을 받는다.
FLUSH_FRAMES = 5


def preview_path(role: str) -> Path:
    return PREVIEW_DIR / f"{role}.jpg"


def _write_status(**updates: object) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **updates}
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


class _Camera:
    """캘리브레이션이 직접 여는 카메라 하나. 이 프로세스가 owner다."""

    def __init__(self, role: str, path: str) -> None:
        self.role = role
        self.path = path
        apply_perception_controls(path)
        self.capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
        self.capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.capture.set(cv2.CAP_PROP_FPS, 30)

    @property
    def opened(self) -> bool:
        return bool(self.capture.isOpened())

    def fresh(self) -> np.ndarray | None:
        """지금 장면. 버퍼에 남은 옛 프레임을 먼저 흘려보낸다."""
        image = None
        for _ in range(FLUSH_FRAMES):
            ok, frame = self.capture.read()
            if ok:
                image = frame
        return image

    def write_preview(self, image: np.ndarray) -> None:
        """카메라를 쥔 쪽이 그림을 내려놓는다. 수집 중 프리뷰와 같은 패턴이다."""
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return
        target = preview_path(self.role)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded.tobytes())
        os.replace(temporary, target)

    def release(self) -> None:
        self.capture.release()


def find_marker(image_bgr: np.ndarray, detector, wanted: tuple[int, ...]) -> np.ndarray | None:
    """마커 네 코너 (4, 2). 못 찾으면 `None`.

    id를 확인하는 이유. 방 안에 다른 ArUco가 있을 수 있고(보드에도 마커가 잔뜩 박혀 있다),
    엉뚱한 마커를 그리퍼의 것으로 착각하면 카메라 자리가 통째로 틀린다.
    """
    grey = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = detector.detectMarkers(grey)
    if ids is None or corners is None:
        return None
    found = {int(value): index for index, value in enumerate(ids.ravel())}
    for marker_id in wanted:
        if marker_id in found:
            return np.asarray(corners[found[marker_id]], dtype=np.float64).reshape(4, 2)
    return None


def main() -> None:
    settings = Settings()
    try:
        count = int(os.getenv("SOARM_CALIB_POSES", "24"))
    except ValueError as exc:
        raise SystemExit(f"Refusing calibration: invalid pose count: {exc}") from exc
    if not 10 <= count <= 40:
        raise SystemExit("Refusing calibration: pose count must be between 10 and 40")
    if not settings.motion_enabled:
        raise SystemExit("Refusing calibration: SOARM_ENABLE_MOTION=1 is required")
    if error := validate_calibration(settings.follower_calibration):
        raise SystemExit(f"Refusing calibration: invalid follower calibration: {error}")

    rig = store.load()
    missing = [role for role in store.ROLES if role not in rig.intrinsics]
    if missing:
        raise SystemExit(
            f"Refusing calibration: intrinsics are missing for {', '.join(missing)}"
        )

    devices = [settings.follower_port, settings.scene_camera, settings.wrist_camera]
    try:
        lock_context = (
            DeviceLockSet.acquire(devices, "calibration")
            if not inherited_locks_cover(devices)
            else None
        )
    except DeviceLockError as exc:
        raise SystemExit(f"Refusing calibration: {exc}") from exc

    from contextlib import nullcontext

    stop_requested = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_requested.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop_requested.set())

    with lock_context or nullcontext():
        _run(settings, rig, count, stop_requested)


def _run(settings: Settings, rig: store.Rig, count: int, stop_requested: threading.Event) -> None:
    phase = "moving"
    _write_status(phase=phase, pose=0, total_poses=count, detected={}, error=None)
    specs = load_joint_specs(settings.follower_calibration)
    chain = Chain.from_urdf()
    detector = rig.marker.detector()

    cameras: dict[str, _Camera] = {}
    robot = None
    start_pose: dict[str, float] | None = None
    try:
        start_pose = present_position(settings, acquire_owner_lock=False)
        targets = poses_module.generate(
            specs, chain, count, gripper=float(start_pose.get("gripper", 0.0))
        )
        if len(targets) < extrinsics_module.MINIMUM_POSES:
            raise RuntimeError(
                f"쓸 수 있는 자세를 {len(targets)}개밖에 만들지 못했습니다. "
                "관절 범위나 작업 영역 설정을 보세요."
            )

        for role, path in (("scene", settings.scene_camera), ("wrist", settings.wrist_camera)):
            camera = _Camera(role, path)
            if not camera.opened:
                raise RuntimeError(f"{role} 카메라를 열지 못했습니다: {path}")
            cameras[role] = camera

        robot = _connect(settings)
        current = {
            name: float(value)
            for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
        }
        samples: list[extrinsics_module.PoseSample] = []
        detected = {role: 0 for role in cameras}

        for index, target in enumerate(targets, start=1):
            if stop_requested.is_set():
                break
            _write_status(
                phase="moving", pose=index, total_poses=len(targets),
                detected=dict(detected), error=None,
            )
            stopped = align(
                robot, current, target,
                should_stop=stop_requested.is_set, limits=REPLAY_ALIGNMENT,
            )
            current = dict(target)
            if stopped or stop_requested.is_set():
                break

            _write_status(
                phase="settling", pose=index, total_poses=len(targets),
                detected=dict(detected), error=None,
            )
            time.sleep(SETTLE_S)

            _write_status(
                phase="capturing", pose=index, total_poses=len(targets),
                detected=dict(detected), error=None,
            )
            # 관절값은 **그림을 찍기 직전에** 읽는다. 명령한 값이 아니라 실제 값이어야
            # 중력 처짐까지 카메라 좌표계에 흡수된다 — 그것이 이 방식의 요점이다.
            measured = {
                name: float(value)
                for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
            }
            gripper_in_base = chain.forward(urdf_angles(specs, measured))
            corners: dict[str, np.ndarray] = {}
            for role, camera in cameras.items():
                image = camera.fresh()
                if image is None:
                    continue
                camera.write_preview(image)
                found = find_marker(image, detector, rig.marker.ids)
                if found is not None:
                    corners[role] = found
                    detected[role] += 1
            if corners:
                samples.append(
                    extrinsics_module.PoseSample(gripper_in_base=gripper_in_base, corners=corners)
                )

        _write_status(
            phase="solving", pose=len(samples), total_poses=len(targets),
            detected=dict(detected), error=None,
        )
        problems: list[str] = []
        for role in store.ROLES:
            if role not in rig.intrinsics:
                continue
            try:
                rig.extrinsics[role] = extrinsics_module.solve(
                    samples, role, rig.intrinsics[role], rig.marker
                )
            except extrinsics_module.ExtrinsicsError as exc:
                # 한 대가 실패해도 다른 대는 살린다. 부감 한 대만 있어도 평면 구속으로
                # 좌표가 나오므로, 둘 다 실패한 것과는 다른 상태다.
                problems.append(str(exc))
        if rig.extrinsics:
            store.save(rig)
        _write_status(
            phase="returning", pose=len(samples), total_poses=len(targets),
            detected=dict(detected), error="; ".join(problems) or None,
        )
    except BaseException as exc:  # noqa: BLE001 - 어떤 실패든 화면에 적고 팔은 돌려놓는다
        _write_status(phase="error", error=str(exc))
        raise
    finally:
        for camera in cameras.values():
            camera.release()
        if robot is not None and start_pose is not None:
            # 돌아가는 길은 중지 신호로 끊지 않는다. 팔을 아무 데나 두고 끝내지 않는다.
            try:
                current = {
                    name: float(value)
                    for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
                }
                align(robot, current, start_pose, should_stop=lambda: False, limits=REPLAY_ALIGNMENT)
            except BaseException:  # noqa: BLE001
                pass
            try:
                robot.disconnect()
            except BaseException:  # noqa: BLE001
                pass
        payload = {}
        try:
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if payload.get("phase") != "error":
            _write_status(
                phase="complete",
                pose=payload.get("pose", 0),
                total_poses=payload.get("total_poses", 0),
                detected=payload.get("detected", {}),
                error=payload.get("error"),
            )


if __name__ == "__main__":
    main()
