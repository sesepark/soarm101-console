from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import cv2


class CameraWorker:
    """One capture thread per camera so every camera has exactly one owner."""

    def __init__(self, path: str, width: int = 640, height: int = 480, fps: int = 30):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self._condition = threading.Condition()
        self._frame: bytes | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._clients = 0

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

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._clients == 0, timeout=1.0)

    def acquire(self) -> None:
        with self._condition:
            self._clients += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
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
        capture = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        try:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            capture.set(cv2.CAP_PROP_FPS, self.fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not capture.isOpened():
                with self._condition:
                    self._error = f"Cannot open camera: {self.path}"
                    self._condition.notify_all()
                return

            with self._condition:
                self._error = None
            consecutive_failures = 0
            while not self._stop.is_set():
                ok, image = capture.read()
                if not ok:
                    consecutive_failures += 1
                    if consecutive_failures >= 10:
                        with self._condition:
                            self._error = f"Camera stopped returning frames: {self.path}"
                            self._condition.notify_all()
                        return
                    time.sleep(0.03)
                    continue
                consecutive_failures = 0
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok:
                    with self._condition:
                        self._frame = encoded.tobytes()
                        self._condition.notify_all()
        finally:
            capture.release()
            with self._condition:
                self._thread = None
                self._frame = None
                self._condition.notify_all()
