from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

import cv2

from .owner_lock import DeviceLockError, DeviceLockSet
from .v4l2_modes import discrete_modes


@dataclass(frozen=True)
class CameraProfile:
    """What the console asked the camera for."""

    width: int = 640
    height: int = 480
    fps: int = 30

    def as_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height, "fps": self.fps}


#: 데이터 수집이 언제나 쓰는 값. 고를 수 없게 두는 것이 요점이다 — VLA 학습용 데이터셋은
#: 에피소드마다 카메라 설정이 달라지면 못 쓰게 되고, LeRobot 레코딩 파이프라인도 이 값으로
#: 고정되어 있다. 프리뷰에서 무엇을 골라 두었든 수집은 이 프로필로 돌아온다.
RECORDING_PROFILE = CameraProfile(width=640, height=480, fps=30)


class CameraWorker:
    """One capture thread per camera so every camera has exactly one owner."""

    def __init__(self, path: str, width: int = 640, height: int = 480, fps: int = 30):
        self.path = path
        self._condition = threading.Condition()
        self._profile = CameraProfile(width=width, height=height, fps=fps)
        self._actual: CameraProfile | None = None
        # 실제로 내보낸 프레임의 시각. 요청한 프레임과 나가는 프레임은 자주 다르다.
        self._delivered: deque[float] = deque(maxlen=60)
        self._modes: list[dict[str, object]] | None = None
        self._frame: bytes | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        # 프로필이 바뀌면 캡처 루프만 다시 연다. 스트림을 보고 있던 쪽은 끊기지 않는다.
        self._reconfigure = threading.Event()
        self._thread: threading.Thread | None = None
        self._clients = 0

    # 기존 코드가 읽던 이름들은 그대로 둔다.
    @property
    def width(self) -> int:
        return self._profile.width

    @property
    def height(self) -> int:
        return self._profile.height

    @property
    def fps(self) -> int:
        return self._profile.fps

    @property
    def profile(self) -> CameraProfile:
        with self._condition:
            return self._profile

    @property
    def actual(self) -> CameraProfile | None:
        """실제로 나가고 있는 것. 요청과 다를 수 있고, 다르면 그대로 내보인다.

        크기는 드라이버가 열어 준 값이고, 프레임은 최근 몇 초 동안 이 워커가 내보낸 장수를
        센 값이다. 장치가 30fps를 낸다고 해서 30장이 나가는 것은 아니다 — 1280×720쯤
        되면 다시 인코딩하는 비용에 걸려 그보다 적게 나간다. 고른 값 대신 나가는 값을
        보여 주어야 화면이 실제와 같은 말을 한다.
        """
        with self._condition:
            if self._actual is None:
                return None
            return CameraProfile(
                width=self._actual.width,
                height=self._actual.height,
                fps=self._delivered_fps(),
            )

    def _delivered_fps(self) -> int:
        """호출자가 `_condition`을 쥐고 있어야 한다."""
        if len(self._delivered) < 2:
            return 0
        span = self._delivered[-1] - self._delivered[0]
        if span <= 0:
            return 0
        # 마지막 프레임이 오래됐으면 지금 나가는 것이 없다는 뜻이다.
        if time.monotonic() - self._delivered[-1] > 2:
            return 0
        return round((len(self._delivered) - 1) / span)

    @property
    def error(self) -> str | None:
        with self._condition:
            return self._error

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def clients(self) -> int:
        with self._condition:
            return self._clients

    def modes(self) -> list[dict[str, object]]:
        """이 장치가 실제로 낼 수 있는 MJPG 모드. 한 번 읽고 들고 있는다."""
        if self._modes is None:
            found = discrete_modes(self.path)
            # 장치를 못 읽었으면 비워 두지 않는다. 지금 쓰고 있는 모드는 확실히 가능하다.
            self._modes = found or [{"width": self.width, "height": self.height, "fps": [self.fps]}]
        return self._modes

    def supports(self, profile: CameraProfile) -> bool:
        for mode in self.modes():
            if mode["width"] == profile.width and mode["height"] == profile.height:
                rates = mode["fps"] or [profile.fps]
                # 요청한 프레임이 장치 최대보다 낮으면 이쪽에서 솎아 내면 되므로 허용한다.
                return profile.fps <= max(rates)
        return False

    def configure(self, profile: CameraProfile) -> None:
        """다음 프레임부터 이 설정으로. 보고 있는 사람이 있으면 캡처만 다시 연다."""
        with self._condition:
            if profile == self._profile:
                return
            self._profile = profile
            self._actual = None
            self._delivered.clear()
            if self._thread is not None and self._thread.is_alive():
                self._reconfigure.set()
            self._condition.notify_all()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            # 수집으로 owner를 넘길 때는 VideoCapture.close와 lock 반납까지 끝나야 한다.
            # client 수만 0이 된 순간 돌아가면 record 시작과 capture thread 종료가 경합한다.
            self._condition.wait_for(
                lambda: self._clients == 0 and self._thread is None, timeout=3.0
            )

    def acquire(self) -> None:
        with self._condition:
            self._clients += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._reconfigure.clear()
                self._thread = threading.Thread(target=self._capture, daemon=True)
                self._thread.start()

    def release(self) -> None:
        with self._condition:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0:
                self._stop.set()
                self._condition.notify_all()

    def frames(self) -> Iterator[bytes]:
        self.acquire()
        last_frame: bytes | None = None
        try:
            while not self._stop.is_set():
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._frame is not None and self._frame is not last_frame
                        or self._error is not None
                        or self._stop.is_set(),
                        timeout=2,
                    )
                    if self._error and self._frame is None:
                        raise RuntimeError(self._error)
                    frame = self._frame
                if frame is not None and frame is not last_frame:
                    last_frame = frame
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        finally:
            self.release()

    def _capture(self) -> None:
        owner_locks: DeviceLockSet | None = None
        try:
            try:
                owner_locks = DeviceLockSet.acquire([self.path], "camera-preview")
            except DeviceLockError as exc:
                with self._condition:
                    self._error = str(exc)
                    self._condition.notify_all()
                return
            # 프로필이 바뀌면 안쪽 루프만 빠져나와 장치를 다시 연다. 바깥에서 보면 스트림은
            # 이어지고 프레임 크기만 달라진다.
            while not self._stop.is_set():
                self._reconfigure.clear()
                if not self._capture_once(self.profile):
                    return
        finally:
            with self._condition:
                self._thread = None
                self._frame = None
                self._actual = None
                self._delivered.clear()
                self._condition.notify_all()
            if owner_locks is not None:
                owner_locks.release()

    def _capture_once(self, profile: CameraProfile) -> bool:
        """한 프로필로 여는 캡처 한 판. 다시 열어야 하면 True, 끝났으면 False."""
        capture = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        try:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, profile.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, profile.height)
            capture.set(cv2.CAP_PROP_FPS, profile.fps)
            # 드라이버 큐를 한 칸으로 줄이면 처리 중 다음 버퍼를 채우지 못해 매번 한 프레임을 잃는다.
            if not capture.isOpened():
                with self._condition:
                    self._error = f"Cannot open camera: {self.path}"
                    self._condition.notify_all()
                return False

            with self._condition:
                self._error = None
                self._delivered.clear()
                self._actual = CameraProfile(
                    width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    fps=int(round(capture.get(cv2.CAP_PROP_FPS))) or profile.fps,
                )
                self._condition.notify_all()

            # 장치는 30fps만 내주므로, 그보다 낮은 값은 여기서 솎아 낸다. 읽기는 계속 해야
            # 드라이버 버퍼가 밀리지 않으므로, 버리는 것은 인코딩과 전달뿐이다.
            interval = 1 / profile.fps if profile.fps > 0 else 0
            last_sent = 0.0
            consecutive_failures = 0
            while not self._stop.is_set() and not self._reconfigure.is_set():
                ok, image = capture.read()
                if not ok:
                    consecutive_failures += 1
                    if consecutive_failures >= 10:
                        with self._condition:
                            self._error = f"Camera stopped returning frames: {self.path}"
                            self._condition.notify_all()
                        return False
                    time.sleep(0.03)
                    continue
                consecutive_failures = 0
                now = time.monotonic()
                if now - last_sent < interval:
                    continue
                last_sent = now
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok:
                    with self._condition:
                        self._frame = encoded.tobytes()
                        self._delivered.append(now)
                        self._condition.notify_all()
            return self._reconfigure.is_set() and not self._stop.is_set()
        finally:
            capture.release()
