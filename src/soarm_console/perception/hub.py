"""프레임을 쥔 쪽이 넣고, 여기서 계산한다. 콘솔 프로세스가 하나 들고 있는 물건.

**왜 이 물건이 있나.** 장치 하나를 두 프로세스가 열 수 없다(`owner_lock.py`). 그러니
"카메라를 열고 계속 도는 perception 데몬"은 수집·정책과 공존할 수 없다. 대신 추정기를
장치를 모르는 순수 객체로 두고, 그때그때 프레임을 쥔 쪽이 `offer()`로 넣는다. 아무 모드도
안 돌 때는 `CameraWorker`의 캡처 루프가, 모드가 돌 때는 그 자식 프로세스가 넣는다.

`offer()`는 **절대 막지 않는다.** 30Hz 캡처 루프 안에서 불리므로, 여기서 몇십 ms를 쓰면
프리뷰의 프레임률이 통째로 떨어진다. 그래서 최신 프레임만 놓고 나가고, 계산은 일꾼
스레드가 자기 속도로 한다.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from . import intrinsics as intrinsics_module
from . import store
from .estimator import Estimate, ObjectEstimator
from .intrinsics import BoardView, Intrinsics, IntrinsicsError


#: 추정을 이보다 자주 돌리지 않는다. 정책 루프가 30Hz이지만 큐브는 사람이 옮기는 것이라
#: 10Hz면 화면이 즉각적으로 느껴지고, 남는 CPU는 캡처와 인코딩이 쓴다.
ESTIMATE_HZ = 10.0
#: 보드를 이보다 자주 찾지 않는다. 검출이 무거워 캡처와 다투면 프리뷰가 끊긴다.
DETECT_HZ = 5.0
#: 새 장으로 담을 최소 차이. `BoardView.signature`의 네 숫자 사이 거리다.
DIVERSITY = 0.08


class PerceptionHub:
    def __init__(self, rig_path: Path = store.RIG_PATH) -> None:
        self._rig_path = rig_path
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._frames: dict[str, tuple[np.ndarray, float]] = {}
        self._sizes: dict[str, tuple[int, int]] = {}
        self._estimate: Estimate | None = None
        self._stop = threading.Event()

        self._rig = store.load(rig_path)
        self._rig_mtime = self._mtime()
        self._estimator = ObjectEstimator(self._rig)

        # 내부 파라미터 수집
        self._collect_role: str | None = None
        self._collect_target = 30
        self._views: dict[str, list[BoardView]] = {}
        self._signatures: dict[str, list[np.ndarray]] = {}
        self._coverage: set[tuple[int, int]] = set()
        self._detected_now = False
        self._solving = False
        self._collect_error: str | None = None
        self._detector = None

        self._thread = threading.Thread(target=self._run, name="perception", daemon=True)
        self._thread.start()

    # MARK: 프레임 받기

    def offer(self, role: str, image_bgr: np.ndarray, at: float | None = None) -> None:
        """캡처 루프에서 부른다. 최신 한 장만 남기고 곧바로 돌아온다."""
        if image_bgr is None:
            return
        with self._condition:
            self._frames[role] = (image_bgr, time.time() if at is None else at)
            self._sizes[role] = (int(image_bgr.shape[1]), int(image_bgr.shape[0]))
            self._condition.notify()

    def forget(self, role: str) -> None:
        """그 카메라가 꺼졌다. 마지막 프레임을 붙들고 있으면 나이만 늘어난 옛 답이 남는다."""
        with self._condition:
            self._frames.pop(role, None)

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    # MARK: 읽기

    @property
    def rig(self) -> store.Rig:
        return self._rig

    def status(self) -> dict[str, object]:
        with self._lock:
            estimate = self._estimate
            rig = self._rig
        return {
            "rig": rig.status(),
            "object": (
                estimate.as_dict()
                if estimate is not None
                else Estimate.unknown("카메라 프리뷰를 켜면 좌표를 계산합니다")
            ),
        }

    def calibration_status(self) -> dict[str, object]:
        with self._lock:
            role = self._collect_role
            views = len(self._views.get(role or "", []))
            coverage = [[(row, column) in self._coverage for column in range(3)] for row in range(3)]
            return {
                "running": role is not None,
                "camera": role,
                "views": views,
                "target_views": self._collect_target,
                "coverage": [[int(cell) for cell in row] for row in coverage],
                "last_detected": self._detected_now,
                "solving": self._solving,
                "error": self._collect_error,
            }

    def reload(self) -> None:
        with self._lock:
            self._rig = store.load(self._rig_path)
            self._rig_mtime = self._mtime()
            self._estimator = ObjectEstimator(self._rig)

    # MARK: 내부 파라미터 수집

    def start_collect(self, role: str, target_views: int) -> None:
        if role not in store.ROLES:
            raise ValueError(f"모르는 카메라입니다: {role}")
        with self._lock:
            if self._solving:
                raise RuntimeError("아직 푸는 중입니다")
            self._collect_role = role
            self._collect_target = max(intrinsics_module.MINIMUM_VIEWS, int(target_views))
            self._views.setdefault(role, [])
            self._signatures.setdefault(role, [])
            self._coverage = set()
            for view in self._views[role]:
                width, height = self._sizes.get(role, (640, 480))
                self._coverage |= view.coverage(width, height)
            self._detected_now = False
            self._collect_error = None
            self._detector = self._rig.board.detector()

    def stop_collect(self) -> None:
        with self._lock:
            self._collect_role = None
            self._detector = None
            self._detected_now = False

    def discard(self, role: str) -> None:
        with self._lock:
            self._views.pop(role, None)
            self._signatures.pop(role, None)
            if self._collect_role == role:
                self._coverage = set()
            self._rig.intrinsics.pop(role, None)
            # 렌즈가 바뀌었으면 그 렌즈로 구한 자리도 뜻이 없다. 남겨 두면 내부만 새것이고
            # 외부는 옛것인 조합이 조용히 살아남는다.
            self._rig.extrinsics.pop(role, None)
            store.save(self._rig, self._rig_path)
            self._rig_mtime = self._mtime()
            self._estimator = ObjectEstimator(self._rig)

    def solve(self, role: str) -> None:
        """워커 스레드에서 푼다. FastAPI의 이벤트 루프를 몇 초씩 막지 않는다."""
        with self._lock:
            if self._solving:
                raise RuntimeError("이미 푸는 중입니다")
            views = list(self._views.get(role, []))
            size = self._sizes.get(role, (640, 480))
            self._solving = True
            self._collect_error = None
        threading.Thread(
            target=self._solve, args=(role, views, size), name="perception-solve", daemon=True
        ).start()

    def _solve(self, role: str, views: list[BoardView], size: tuple[int, int]) -> None:
        try:
            result = intrinsics_module.solve(views, self._rig.board, size)
        except (IntrinsicsError, Exception) as exc:  # noqa: BLE001 - 어떤 실패든 화면에 적는다
            with self._lock:
                self._solving = False
                self._collect_error = str(exc)
            return
        with self._lock:
            self._rig.intrinsics[role] = result
            # 렌즈가 새로 잡혔으면 그 전 렌즈로 구한 자리는 더 이상 맞지 않는다.
            self._rig.extrinsics.pop(role, None)
            store.save(self._rig, self._rig_path)
            self._rig_mtime = self._mtime()
            self._estimator = ObjectEstimator(self._rig)
            self._solving = False
            self._collect_role = None
            self._detector = None

    def views_of(self, role: str) -> list[BoardView]:
        with self._lock:
            return list(self._views.get(role, []))

    # MARK: 일꾼

    def _mtime(self) -> float:
        try:
            return self._rig_path.stat().st_mtime
        except OSError:
            return 0.0

    def _run(self) -> None:
        next_estimate = 0.0
        next_detect = 0.0
        while not self._stop.is_set():
            with self._condition:
                if not self._frames:
                    self._condition.wait(0.5)
                frames = {role: (image, at) for role, (image, at) in self._frames.items()}
                collect_role = self._collect_role
                detector = self._detector
            now = time.monotonic()

            # 캘리브레이션 자식이 rig.json을 다시 썼으면 그것을 읽는다. 파일이 하나라
            # 이 한 줄이 두 프로세스를 잇는 전부다.
            if self._mtime() != self._rig_mtime:
                self.reload()

            if collect_role and detector is not None and now >= next_detect:
                next_detect = now + 1.0 / DETECT_HZ
                self._collect(collect_role, detector, frames.get(collect_role))

            if now >= next_estimate:
                next_estimate = now + 1.0 / ESTIMATE_HZ
                self._estimate_once(frames)

            if not frames:
                continue
            time.sleep(0.01)

    def _estimate_once(self, frames: dict[str, tuple[np.ndarray, float]]) -> None:
        if not frames:
            return
        images = {role: image for role, (image, _) in frames.items()}
        newest = max(at for _, at in frames.values())
        with self._lock:
            estimator = self._estimator
        try:
            estimate = estimator.update(images, at=newest)
        except Exception:  # noqa: BLE001 - 추정이 콘솔을 세우지 않는다
            return
        with self._lock:
            self._estimate = estimate

    def _collect(self, role: str, detector, frame: tuple[np.ndarray, float] | None) -> None:
        if frame is None:
            return
        image, _ = frame
        try:
            view = intrinsics_module.detect(image, detector)
        except Exception:  # noqa: BLE001
            return
        width, height = int(image.shape[1]), int(image.shape[0])
        with self._lock:
            self._detected_now = view is not None
            if view is None or self._collect_role != role:
                return
            if len(self._views.setdefault(role, [])) >= self._collect_target:
                return
            signature = view.signature(width, height)
            # 같은 자리에서 여러 장 찍어 봐야 새 정보가 없다. 왜곡 계수는 보드가 화면의
            # 다른 자리·크기·기울기에 있을 때 결정된다.
            for existing in self._signatures.setdefault(role, []):
                if float(np.linalg.norm(signature - existing)) < DIVERSITY:
                    return
            self._views[role].append(view)
            self._signatures[role].append(signature)
            self._coverage |= view.coverage(width, height)
