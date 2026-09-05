from __future__ import annotations

import hashlib
import os
import re
import json
import subprocess
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np
from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot.configs.dataset import DatasetRecordConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.robots.so_follower import SO101FollowerConfig
from lerobot.scripts import lerobot_record
from lerobot.scripts.lerobot_record import RecordConfig
from lerobot.teleoperators.so_leader import SO101LeaderConfig
from lerobot.utils.keyboard_input import apply_recording_control

from . import sensors
from .calibration import validate_calibration
from .config import Settings
from .follower_start import (
    align_follower_to_leader,
    install_safe_follower_start,
    install_safe_leader_start,
)
from .owner_lock import DeviceLockError, DeviceLockSet, inherited_locks_cover
from .record_manager import preview_path
from .vleader import teleoperator as teleoperator_module
from .vleader.teleoperator import SOArmVirtualLeaderConfig


RUNTIME_DIR = Path(__file__).parents[2] / "runtime/record"
CONTROL_PATH = RUNTIME_DIR / "control.json"
STATUS_PATH = RUNTIME_DIR / "status.json"
_STATUS_LOCK = threading.Lock()
_ORIGINAL_RECORD_LOOP = lerobot_record.record_loop
_ORIGINAL_SAVE_EPISODE = LeRobotDataset.save_episode
_ORIGINAL_COMBINE_FEATURE_DICTS = lerobot_record.combine_feature_dicts
_ORIGINAL_BUILD_DATASET_FRAME = lerobot_record.build_dataset_frame
DATA_ROOT = Path(__file__).parents[2] / "data"
NAME_PATTERN = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")

# A few seconds is long enough to smooth camera jitter, while still making a
# sustained slowdown visible to the operator before the episode is over.
LOOP_HZ_WINDOW_S = 3.0
LOOP_HZ_PUBLISH_S = 1.0

#: 스냅숏을 다시 쓰는 최대 빈도. 화면이 보여 주는 것은 사람이 보는 장면이지 데이터가
#: 아니므로 5Hz면 충분하고, 그 위로 올리면 30Hz 루프에서 치르는 값만 커진다.
PREVIEW_HZ = 5.0
#: JPEG 품질. 이 그림은 "카메라가 무엇을 보고 있나"를 답하는 용도다.
PREVIEW_QUALITY = 75

#: 이 실행이 데이터셋에 넣은 회차 수. `save_episode` wrapper가 갱신하고, 끝나는 자리에서
#: 상태에 함께 적는다 — 회차가 몇 개 남았는지는 수집이 끝난 뒤에도 화면이 물어보는 것이다.
_episodes_saved = 0

#: 0프레임이라 저장하지 않고 건너뛴 회차 수. 이것이 0이 아니면 무언가가 회를 시작하기도
#: 전에 끝냈다는 뜻이고, 그 사실은 데이터셋 어디에도 남지 않으므로 여기서 센다.
_empty_episodes_skipped = 0

#: 직전 저장에 걸린 초. 다음 저장이 시작될 때 `saving_seconds_estimate`로 내보낸다 —
#: 화면이 "약 N초"를 그릴 수 있는 유일한 근거는 방금 같은 일이 얼마나 걸렸는가다.
_saving_seconds: float | None = None

#: `record_loop`가 지금 돌고 있는가.
#:
#: 이 플래그가 없으면 회 사이(영상 굽기, 정리 구간 사이의 빈틈)에 도착한 키가
#: `events["exit_early"]`에 남아 있다가 **다음 회차의 첫 반복**에서 읽힌다. 실제로
#: `test3_20260905_1413`이 그렇게 죽었다 — 저장 11초 동안 누른 ⏎가 회 1을 0프레임으로
#: 끝냈고, 그 빈 회를 저장하다 `validate_episode_buffer`가 세션을 죽였다.
#:
#: `threading.Event`인 이유는 세우는 쪽이 수집 스레드이고 읽는 쪽이 리스너 스레드이기
#: 때문이다.
_loop_running = threading.Event()

#: 이 프로세스가 시작한 epoch. `observation.wall_time`의 기준이고, 그 기준이 무엇이었는지는
#: `soarm_provenance.json`의 `started_at`에 적힌다. 모듈을 읽는 자리에서 한 번만 잰다 —
#: 회차마다 다시 재면 회차 사이에서 시계가 뒤로 간다.
_started_at = time.time()

#: 시작 정렬을 할 것인가. 가상 리더로 찍을 때는 하지 않는다 —
#: `vleader.start_relay`가 이미 팔로워의 지금 자세에서 목표를 이어 준다.
_align_on_start = False
_aligned = False


def _idle_rate_fields() -> dict[str, object]:
    """루프가 아직 돌지 않는 구간에서 내보낼 속도 값.

    지난 회차의 loop_hz와 카메라 수치가 상태에 남아 있으면, 멈춘 수집이 계속 30Hz로
    돌고 있는 것처럼 보인다.
    """
    return {
        "loop_hz": 0.0,
        "camera_fresh_hz": {},
        "camera_stale_pct": {},
        "extras_read_ms": 0.0,
        "extras_read_failures": 0,
    }


def _write_status(**updates: object) -> None:
    # The GUI control listener and the loop-rate publisher are separate threads.
    # Serialize their read-modify-write cycles so neither can erase the other's
    # fields or replace the shared temporary file while it is being written.
    with _STATUS_LOCK:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        current: dict[str, object] = {}
        try:
            current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        current.update(updates, updated_at=time.time())
        temporary = STATUS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        os.replace(temporary, STATUS_PATH)


class _LoopRateMonitor:
    """Publish a rolling rate without doing file I/O in the control loop."""

    def __init__(self, reader: sensors.SensorReader | None = None) -> None:
        self.samples: deque[float] = deque()
        # 카메라별 (시각, 새 프레임인가) 표본. 루프 표본과 같은 창을 쓴다.
        self.camera_samples: dict[str, deque[tuple[float, bool]]] = {}
        # 서보 블록 읽기가 한 틱에서 얼마나 쓰는지. 이 값이 곧 새 열이 30Hz 예산에서
        # 가져간 몫이고, 사람이 실물로 확인할 때 물어보는 첫 번째 숫자다.
        self.reader = reader
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def tick(self) -> None:
        now = time.perf_counter()
        with self.lock:
            self.samples.append(now)
            cutoff = now - LOOP_HZ_WINDOW_S
            while self.samples and self.samples[0] < cutoff:
                self.samples.popleft()

    def observe_cameras(self, fresh: dict[str, bool]) -> None:
        if not fresh:
            return
        now = time.perf_counter()
        cutoff = now - LOOP_HZ_WINDOW_S
        with self.lock:
            for key, is_fresh in fresh.items():
                samples = self.camera_samples.setdefault(key, deque())
                samples.append((now, is_fresh))
                while samples and samples[0][0] < cutoff:
                    samples.popleft()

    def _rate_locked(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return (len(self.samples) - 1) / (self.samples[-1] - self.samples[0])

    def snapshot(self) -> tuple[float, dict[str, float], dict[str, float]]:
        """Loop rate plus, per camera, how much of that rate carried a new frame.

        All three come from one lock hold so they describe the same window. A
        fresh-frame rate published next to a loop rate it cannot divide into
        would read as a second measurement contradicting the first.
        """
        fresh_hz: dict[str, float] = {}
        stale_pct: dict[str, float] = {}
        with self.lock:
            loop_hz = self._rate_locked()
            for key, samples in self.camera_samples.items():
                if not samples:
                    fresh_hz[key] = 0.0
                    stale_pct[key] = 0.0
                    continue
                ratio = sum(1 for _, is_fresh in samples if is_fresh) / len(samples)
                fresh_hz[key] = loop_hz * ratio
                stale_pct[key] = 100.0 * (1.0 - ratio)
        return loop_hz, fresh_hz, stale_pct

    def _publish(self) -> None:
        loop_hz, fresh_hz, stale_pct = self.snapshot()
        extras: dict[str, object] = {}
        if self.reader is not None:
            extras = {
                "extras_read_ms": round(self.reader.read_ms(), 3),
                "extras_read_failures": self.reader.read_failures,
            }
        _write_status(
            loop_hz=float(loop_hz),
            camera_fresh_hz=fresh_hz,
            camera_stale_pct=stale_pct,
            **extras,
        )

    def _run(self) -> None:
        while not self.stopped.wait(LOOP_HZ_PUBLISH_S):
            self._publish()

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=LOOP_HZ_PUBLISH_S + 0.5)
        self._publish()


class _PreviewWriter:
    """수집 중인 카메라 그림을 파일 하나로 계속 갈아 끼운다.

    수집이 도는 동안 콘솔의 MJPEG 프리뷰는 꺼져 있다 — 장치를 쥔 것은 record 자식이고
    카메라 하나를 두 프로세스가 열 수는 없다. 그래서 찍는 쪽이 보고 있는 바로 그 프레임을
    내보낸다. 화면이 "지금 무엇을 찍고 있나"를 물을 곳이 여기뿐이다.

    **인코딩은 별 스레드에서 한다.** `cv2.imencode`는 640x480 한 장에 수 밀리초가 들고,
    30Hz 루프의 예산은 한 틱에 33ms다. 두 대를 루프 안에서 인코딩하면 그 자체가 데이터의
    시간축을 늘린다 — 그리고 그 늘어남은 `timestamp`가 합성값이라 데이터에 남지도 않는다.
    루프가 하는 일은 버퍼를 한 장 복사해 놓아 두는 것까지고, 그 복사는 100µs 아래다.

    복사하는 이유는 `read_latest()`가 돌려주는 버퍼를 카메라 스레드가 다시 채우기
    때문이다. 그대로 넘기면 인코더가 반쯤 덮어쓰인 그림을 읽는다.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._pending: dict[str, np.ndarray] = {}
        self._condition = threading.Condition()
        self._done = False
        self._last_offered: dict[str, float] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def offer(self, role: str, frame: np.ndarray) -> None:
        now = time.perf_counter()
        if now - self._last_offered.get(role, -1e9) < 1.0 / PREVIEW_HZ:
            return
        self._last_offered[role] = now
        copy = frame.copy()
        with self._condition:
            # 아직 인코딩하지 못한 그림이 있으면 그것을 버리고 새 것을 둔다. 프리뷰에서
            # 늦은 그림은 값이 없다.
            self._pending[role] = copy
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._done = True
            self._condition.notify()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        import cv2

        while True:
            with self._condition:
                while not self._pending and not self._done:
                    self._condition.wait()
                if not self._pending and self._done:
                    return
                role, frame = self._pending.popitem()
            try:
                ok, encoded = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_QUALITY]
                )
                if not ok:
                    continue
                target = preview_path(self.directory, role)
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(encoded.tobytes())
                # 반쯤 쓰인 파일을 읽는 요청이 없도록 자리를 통째로 바꾼다.
                os.replace(temporary, target)
            except Exception:  # noqa: BLE001 - 프리뷰가 수집을 멈추게 하지 않는다
                continue


class _RateTrackedRobot:
    """Count loop entries, and hang the servo's other readings off the same tick.

    LeRobot이 관측에서 데이터셋으로 옮기는 값은 `robot.observation_features`가 정하는데,
    거기에 무엇을 더하면 `hw_to_dataset_features`가 그것을 **`observation.state`로 묶는다**.
    그러면 상태 벡터가 여섯에서 마흔둘로 늘고 기존 데이터로 배운 정책과 사전학습 정규화
    통계가 전부 어긋난다. 그래서 features 쪽은 따로 손대고(`_combine_feature_dicts_with_sensors`),
    값은 여기서 관측 dict에 직접 얹는다 — `build_dataset_frame`이 `names`를 키로 값을
    모아 가므로, 열 이름만 맞으면 나머지는 LeRobot이 한다.
    """

    def __init__(
        self,
        robot: object,
        monitor: _LoopRateMonitor,
        preview: _PreviewWriter | None = None,
        reader: sensors.SensorReader | None = None,
    ) -> None:
        self._robot = robot
        self._monitor = monitor
        self._preview = preview
        self._reader = reader
        self._last_frames: dict[str, np.ndarray] = {}

    def __getattr__(self, name: str) -> object:
        return getattr(self._robot, name)

    def get_observation(self) -> object:
        self._monitor.tick()
        # `SOFollower.get_observation()`은 맨 처음에 `Present_Position`을 읽는다. 그래서
        # 여기가 이 프레임의 상태가 실제로 측정된 시각이다 — 카메라를 읽고 돌아온 뒤에
        # 재면 그사이의 수 밀리초가 위치의 시각인 것처럼 적힌다.
        position_read_at = time.time()
        observation = self._robot.get_observation()
        fresh = self._camera_freshness(observation)
        self._monitor.observe_cameras(fresh)
        self._offer_previews(observation)
        self._add_sensor_extras(observation, position_read_at, fresh)
        return observation

    def _add_sensor_extras(
        self, observation: object, position_read_at: float, fresh: dict[str, bool]
    ) -> None:
        """서보의 나머지 판독값을 관측 dict에 얹는다.

        카메라 그림을 다 읽은 **뒤에** 버스를 만지는 것이 맞다. `read_latest()`는 블로킹이
        아니지만 위치 읽기는 시리얼 왕복이고, 그 사이에 블록 읽기를 끼우면 위치와 그림의
        시차가 그만큼 벌어진다.
        """
        if self._reader is None or not isinstance(observation, dict):
            return
        observation.update(self._reader.observation_extras(position_read_at, fresh))

    def _offer_previews(self, observation: object) -> None:
        if self._preview is None or not isinstance(observation, dict):
            return
        for key, value in observation.items():
            if isinstance(value, np.ndarray) and value.ndim == 3:
                # 키의 마지막 마디가 역할이다. `scene`으로도 `observation.images.scene`
                # 으로도 올 수 있고, 화면이 부르는 이름은 어느 쪽에서든 뒤쪽이다.
                self._preview.offer(str(key).rsplit(".", 1)[-1], value)

    def _camera_freshness(self, observation: object) -> dict[str, bool]:
        """카메라마다, 이번 틱이 직전 틱과 다른 프레임을 받았는지.

        `SOFollower.get_observation()`은 카메라마다 `read_latest()`를 부른다. 이 호출은
        블로킹이 아니라서, 새 프레임이 아직 없으면 지난번에 돌려준 바로 그 버퍼를 다시
        돌려준다. 그래서 판정은 버퍼의 주소로 한다. 픽셀을 비교하면 안 된다 — 움직이지
        않는 장면은 서로 다른 두 번의 촬영인데도 같은 값이 나오고, 30Hz 루프에서 640x480
        두 장을 매 틱 비교하는 비용도 든다.

        영상에서 세는 것도 답이 되지 않는다. AV1 인코더는 움직임 없는 두 프레임을 하나로
        합치므로, 카메라가 같은 프레임을 두 번 준 것과 구별되지 않는다. 원천에서 세야 한다.
        """
        if not isinstance(observation, dict):
            return {}
        fresh: dict[str, bool] = {}
        for key, value in observation.items():
            if not isinstance(value, np.ndarray):
                continue
            previous = self._last_frames.get(key)
            fresh[key] = previous is None or previous.ctypes.data != value.ctypes.data
            # 주소만이 아니라 배열 자체를 붙잡아 둔다. 놓아 버리면 그 버퍼가 해제되고,
            # 다음 프레임이 같은 주소를 받아 새 프레임이 stale로 읽힐 수 있다.
            self._last_frames[key] = value
        return fresh


def _align_before_the_first_episode(robot: object, teleop: object) -> None:
    """첫 회차 앞에서 팔로워를 리더의 지금 자세까지 걸어간다. 한 번만 한다.

    LeRobot의 첫 틱은 리더 자세를 그대로 팔로워에 보낸다. 두 팔이 다른 자세로 서 있으면
    팔로워는 그 차이만큼 **한 번에** 뛴다 — `SOARM_MAX_RELATIVE_TARGET`이 걸릴 수 없는
    값이라 잘리지도 않는다.

    두 번째 회차부터는 필요 없다. 그때 팔로워는 이미 리더를 따라온 자리에 있다.
    """
    global _aligned

    if _aligned or not _align_on_start or teleop is None:
        return
    _aligned = True
    _write_status(
        phase="aligning",
        episode_started_at=None,
        reset_started_at=None,
        **_idle_rate_fields(),
    )
    align_follower_to_leader(
        robot,
        teleop,
        publish=lambda seconds_left: _write_status(aligning_seconds_left=seconds_left),
        log=print,
    )


def _record_loop_with_status(*args: object, **kwargs: object) -> object:
    """Wrap LeRobot's loop and expose episode boundaries plus its real rate.

    LeRobot currently calls this function entirely with keywords. Requiring the
    fields we consume and forwarding every argument unchanged (apart from the
    transparent robot proxy) makes an upstream signature change fail loudly.
    """
    robot = kwargs["robot"]
    dataset = kwargs.get("dataset")
    control_time_s = kwargs["control_time_s"]
    # 정리 구간(`dataset is None`)에서는 서보 블록을 읽지 않는다. 그 구간의 값은 어느
    # 프레임에도 들어가지 않으므로, 읽어 봐야 버스 시간만 쓴다.
    reader = (
        sensors.SensorReader(getattr(robot, "bus", None), _started_at)
        if dataset is not None
        else None
    )
    monitor = _LoopRateMonitor(reader)
    preview = _PreviewWriter(RUNTIME_DIR)

    if dataset is None:
        # 회 사이의 정리 구간이다. 화면이 남은 시간을 셀 수 있도록 시작 시각과 길이를
        # 함께 적는다 — 여기가 조용하면 사람은 수집이 멈춘 것인지 기다리는 것인지 모른다.
        _write_status(
            phase="resetting",
            episode_started_at=None,
            reset_started_at=time.time(),
            reset_seconds=int(control_time_s),
            **_idle_rate_fields(),
        )
    else:
        _align_before_the_first_episode(robot, kwargs.get("teleop"))
        _write_status(
            phase="recording",
            episode_started_at=time.time(),
            episode_seconds=int(control_time_s),
            episode_index=int(dataset.num_episodes),
            # 지난 정리 구간의 시작 시각이 남아 있으면 화면은 기록 중에도 정리 시계를
            # 센다. 구간이 바뀌는 자리에서 지운다.
            reset_started_at=None,
            **_idle_rate_fields(),
        )

    forwarded = dict(kwargs)
    forwarded["robot"] = _RateTrackedRobot(robot, monitor, preview, reader)
    monitor.start()
    # 걸려 있던 `exit_early`를 새 구간으로 들고 들어가지 않는다. LeRobot의 루프는 첫
    # 반복 맨 앞에서 이 값을 읽으므로, 여기 남은 True 하나가 회차를 0프레임으로 끝낸다.
    # 리스너가 이미 회 사이의 키를 버리지만, 이것이 마지막 방어다 — teleoperator처럼
    # 리스너를 거치지 않고 이 표를 만지는 길이 따로 있다.
    events = kwargs.get("events")
    if isinstance(events, dict):
        events["exit_early"] = False
    # 표를 비운 **뒤에** 문을 연다. 순서가 반대면 그사이에 적용된 키가 곧바로 지워진다.
    _loop_running.set()
    try:
        return _ORIGINAL_RECORD_LOOP(*args, **forwarded)
    finally:
        _loop_running.clear()
        monitor.stop()
        preview.stop()


def _combine_feature_dicts_with_sensors(*dicts: dict) -> dict:
    """LeRobot이 만든 features에 서보 판독값 열 아홉 개를 더한다.

    여기가 그 자리인 이유. `hw_to_dataset_features`는 `robot.observation_features`의 float
    항목을 **전부 `observation.state` 하나로 묶는다.** 그러니 관측 features에 더하면 상태
    벡터가 늘어나고, 그 순간 기존 데이터셋으로 배운 정책도 사전학습 정규화 통계도 모양이
    맞지 않게 된다. 묶기가 끝난 **뒤** 결과 dict에 별도 열로 더하면 그런 일이 없다 —
    정책은 자기가 모르는 열을 지나친다.

    `LeRobotDataset.create`가 이 dict를 그대로 `info.json`에 적고, 그 뒤로는 그 파일이
    "이 데이터셋에 무엇이 들어 있나"의 답이 된다.
    """
    features = _ORIGINAL_COMBINE_FEATURE_DICTS(*dicts)
    features.update(sensors.extra_features())
    return features


def _build_dataset_frame_with_sensors(
    ds_features: dict, values: dict, prefix: str
) -> dict:
    """`observation.camera_fresh`만 손으로 채우고 나머지는 LeRobot에게 맡긴다.

    다른 여덟 열은 `names`가 관측 dict의 키와 그대로 맞아떨어지므로 원본이 알아서 모은다.
    이 열의 이름은 `scene`·`wrist`인데, 관측 dict의 그 두 키에는 이미 카메라가 준 그림이
    들어 있다. 원본에게 맡기면 `np.array([그림, 그림], dtype=float32)`를 만들다 죽는다.
    그래서 이 열만 features에서 빼서 원본을 부르고, 값은 옆으로 실어 온 자리에서 꺼낸다.
    """
    handled = {
        key: ft
        for key, ft in ds_features.items()
        if key in sensors.SIDE_CHANNEL_COLUMNS and key.startswith(prefix)
    }
    if not handled:
        return _ORIGINAL_BUILD_DATASET_FRAME(ds_features, values, prefix)
    remaining = {key: ft for key, ft in ds_features.items() if key not in handled}
    frame = _ORIGINAL_BUILD_DATASET_FRAME(remaining, values, prefix)
    for key in handled:
        frame[key] = np.asarray(values[key], dtype=np.float32)
    return frame


def _episode_buffer_size(dataset: object, episode_data: object) -> int | None:
    """이번 저장이 담고 있는 프레임 수. 셀 수 없으면 `None`.

    LeRobot 0.6.1에서 버퍼를 쥔 것은 `LeRobotDataset`이 아니라 그 안의 `DatasetWriter`고,
    프레임 수는 `writer.episode_buffer["size"]`다 — `add_frame`이 한 프레임마다 하나씩
    올린다(streaming_encoding을 켜도 마찬가지다. 그림은 인코더로 흘러가지만 `size`는
    그대로 센다). `save_episode(episode_data=...)`로 버퍼를 직접 넘기는 길도 있어서
    그쪽을 먼저 본다.

    셀 수 없을 때 `None`을 주는 것이 중요하다. 모르는 것을 0으로 읽으면 멀쩡한 회차를
    버리게 된다 — 건너뛰기는 확실할 때만 한다.
    """
    buffer = episode_data
    if buffer is None:
        buffer = getattr(getattr(dataset, "writer", None), "episode_buffer", None)
    if not isinstance(buffer, dict) or "size" not in buffer:
        return None
    try:
        return int(buffer["size"])
    except (TypeError, ValueError):
        return None


def _save_episode_with_status(self, *args: object, **kwargs: object) -> object:
    """회차를 저장하는 동안 화면이 그렇다고 말할 수 있게 한다.

    정리 구간 15초가 끝난 뒤 인코딩에 8초쯤이 더 든다. 그동안 텔레옵 루프는 서 있고
    화면은 아무 말도 하지 않았다 — 사람은 수집이 죽은 줄 알고 팔을 흔들어 본다.

    빈 회차는 저장하지 않고 건너뛴다. LeRobot의 `validate_episode_buffer`는 0프레임을
    `ValueError`로 막고, 그 예외는 `record()` 밖까지 나가 세션을 통째로 끝낸다 — 지금까지
    찍은 회차는 살아남지만 남은 회차는 사라진다. 0프레임 회가 왜 생겼든 그것은 회 하나의
    문제지 세션의 문제가 아니다.

    속도 값은 건드리지 않는다. 여기 남아 있는 `loop_hz`는 방금 저장하는 그 회차가 실제로
    돈 속도이고, `record_manager`가 `soarm_quality.json`에 적는 것도 그 값이다.
    """
    global _saving_seconds

    episode_data = args[0] if args else kwargs.get("episode_data")
    if _episode_buffer_size(self, episode_data) == 0:
        return _skip_the_empty_episode(self)

    _write_status(
        phase="saving",
        episode_started_at=None,
        reset_started_at=None,
        saving_seconds_estimate=_saving_seconds,
    )
    started = time.perf_counter()
    try:
        return _ORIGINAL_SAVE_EPISODE(self, *args, **kwargs)
    finally:
        _saving_seconds = round(time.perf_counter() - started, 2)
        _note_episodes_saved(int(self.num_episodes))
        # `phase`는 여기서 되돌리지 않는다. 다음 `record_loop`가 자기 구간을 적는다.
        _write_status(episodes_saved=_episodes_saved)


def _skip_the_empty_episode(dataset: object) -> None:
    """0프레임 회차를 저장하지 않고 버퍼만 비운다.

    `episodes_saved`는 올리지 않는다 — 데이터셋에 들어간 것이 없기 때문이다. 대신
    `empty_episodes_skipped`를 올려서, 화면과 로그가 "회차 하나가 통째로 비었다"를
    말할 수 있게 한다. 조용히 넘어가면 사람은 회차 수가 왜 모자란지 알 수 없다.
    """
    global _empty_episodes_skipped

    _empty_episodes_skipped += 1
    print(
        "Empty episode buffer (0 frames): skipping save and clearing the buffer "
        f"(skipped so far: {_empty_episodes_skipped})"
    )
    try:
        dataset.clear_episode_buffer()
    finally:
        _write_status(
            empty_episodes_skipped=_empty_episodes_skipped,
            episodes_saved=_episodes_saved,
        )
    return None


def _note_episodes_saved(count: int) -> None:
    global _episodes_saved

    _episodes_saved = int(count)


class _GuiControlListener:
    def __init__(self, events: dict[str, bool]):
        self.events = events
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stopped.is_set():
            try:
                payload = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
                CONTROL_PATH.unlink(missing_ok=True)
                self._apply(str(payload.get("key", "")))
            except FileNotFoundError:
                pass
            except (json.JSONDecodeError, OSError):
                CONTROL_PATH.unlink(missing_ok=True)
            self.stopped.wait(0.05)

    def _apply(self, key: str) -> None:
        if key not in {"right", "left", "esc", "abort"}:
            return
        if not _loop_running.is_set():
            self._apply_between_loops(key)
            return
        if key == "abort":
            # 찍던 회를 **버리고** 끝낸다.
            #
            # `esc`(= stop_recording + exit_early)는 루프를 빠져나온 뒤
            # `save_episode()`를 그대로 돌아 찍다 만 회를 저장한다. 실제로
            # `soarm101_20260905_092024`의 2회차가 그렇게 남았다 — 82프레임,
            # 2.7초짜리 조각이 데이터셋 안에서 온전한 시연인 척한다.
            #
            # LeRobot 0.6.1의 `record()` 루프는 이 셋을 함께 세우면
            # `clear_episode_buffer()`를 지나 `while` 조건(`stop_recording`)에서
            # 빠져나온다 — 버리는 길과 끝내는 길이 그 한 바퀴 안에서 만난다.
            self.events["rerecord_episode"] = True
            self.events["stop_recording"] = True
            self.events["exit_early"] = True
        else:
            apply_recording_control(key, self.events)
        _write_status(last_control=key)

    def _apply_between_loops(self, key: str) -> None:
        """루프가 서 있는 동안 온 키. 회 사이에서는 조작할 것이 없다.

        영상 굽기와 정리 구간 사이의 몇 초는 화면에서 수집이 도는 것과 구별되지 않아서,
        사람은 그동안에도 ⏎를 누른다. 그 키를 `events`에 적으면 **다음 회차**가 그것을
        읽어 0프레임으로 끝난다 — 사람은 회 하나를 넘기려던 것이지 다음 회를 지우려던
        것이 아니다. 그래서 적용하지 않고, 버렸다는 사실만 상태에 남긴다.

        `esc`와 `abort`는 예외다. 저장 중에 "끝내기"를 눌렀으면 그것은 다음 회차에
        관한 뜻이 맞고, 그 뜻은 `stop_recording` 하나로 온전히 전해진다 — 저장이 끝난
        뒤 `record()`의 `while` 조건에서 조용히 나간다. `exit_early`는 세우지 않는다.
        그것까지 세우면 세션은 다음 회를 0프레임으로 한 번 더 열고 끝난다.
        """
        if key in {"esc", "abort"}:
            self.events["stop_recording"] = True
            _write_status(last_control=key)
            return
        print(f"Control '{key}' arrived between loops: ignored.")
        _write_status(
            last_control_ignored={
                "key": key,
                "reason": "no loop running",
                "at": time.time(),
            }
        )

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=0.5)


def _init_gui_listener():
    events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
    # 가상 리더 teleoperator가 이 표를 본다. 조작이 오래 끊기면 스스로 에피소드를
    # 끝내야 하는데, LeRobot은 teleoperator에게 이 표를 넘겨주지 않는다.
    teleoperator_module.RECORDING_EVENTS = events
    return _GuiControlListener(events), events


def default_dataset_name(task: str, now: datetime | None = None) -> str:
    """새 데이터셋 이름. **로컬 시각**으로 짓는다.

    UTC로 짓던 때는 저녁 6시 20분에 찍은 것이 `..._092024`로 적혔다. 사람이 데이터셋을
    고르는 유일한 단서가 이름인데, 그 이름이 아홉 시간 어긋난 시각을 말하고 있었다.

    과제를 ASCII로 적었으면 그것을 앞에 둔다 — `soarm101_...`이 열 줄이면 목록에서
    무엇이 무엇인지 알 수 없다. 한글로 적었으면 로마자로 옮기지 않고 시각만 쓴다.
    옮겨 적은 이름은 원래 과제로도 읽히지 않고 데이터셋 이름으로도 읽히지 않는다.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    slug = re.sub(r"[\s_]+", "_", task.strip().lower())
    if re.fullmatch(r"[a-z0-9_-]+", slug):
        slug = slug[:40].strip("-_")
        if slug and slug[0].isalnum():
            return f"{slug}_{stamp}"
    return "soarm101_" + (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def build_record_config(
    settings: Settings,
    task: str,
    dataset_name: str,
    teleop_source: str = "leader",
    resume: bool = False,
) -> RecordConfig:
    if not task.strip():
        raise ValueError("SOARM_TASK must describe one task")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", dataset_name):
        raise ValueError("SOARM_DATASET_NAME must contain only letters, numbers, '_' or '-'")

    root = Path(__file__).parents[2] / "data" / dataset_name
    cameras = {
        "scene": OpenCVCameraConfig(
            Path(settings.scene_camera), fps=30, width=640, height=480, fourcc="MJPG"
        ),
        "wrist": OpenCVCameraConfig(
            Path(settings.wrist_camera), fps=30, width=640, height=480, fourcc="MJPG"
        ),
    }
    robot = SO101FollowerConfig(
        port=settings.follower_port,
        id=settings.follower_id,
        cameras=cameras,
        max_relative_target=settings.effective_max_relative_target,
        # record loop의 정상 종료나 예외가 torque-off가 되면 팔이 떨어진다. 해제는 사람이
        # 팔을 받친 뒤 명시적으로만 한다.
        disable_torque_on_disconnect=False,
    )
    if teleop_source == "virtual":
        # 물리 리더 팔이 없다. 관절 목표는 콘솔이 검증해 들고 있는 것을 가져온다.
        teleop = SOArmVirtualLeaderConfig(id="soarm101_virtual_leader")
    else:
        teleop = SO101LeaderConfig(port=settings.leader_port, id=settings.leader_id)
    dataset = DatasetRecordConfig(
        repo_id=f"local/{dataset_name}",
        single_task=task.strip(),
        root=root,
        fps=30,
        episode_time_s=int(os.getenv("SOARM_EPISODE_SECONDS", "30")),
        reset_time_s=int(os.getenv("SOARM_RESET_SECONDS", "15")),
        num_episodes=int(os.getenv("SOARM_NUM_EPISODES", "10")),
        video=True,
        push_to_hub=False,
        tags=["so101", "teleoperation", "local"],
        no_stamp=True,
        # 회 사이의 대기를 없앤다. 이것을 끄면 LeRobot은 프레임마다 PNG를 쓰고
        # `save_episode()`에서 그것을 통째로 인코딩한다 — 30초짜리 회차 하나에 11초가
        # 걸렸고, 그 11초 동안 루프가 서 있어서 사람이 누른 키가 갈 곳을 잃었다.
        # 켜면 프레임이 들어오는 즉시 별 스레드가 굽고 `save_episode()`는 거의 바로
        # 끝난다.
        streaming_encoding=True,
        # 인코더 하나가 쓰는 스레드 수. 카메라가 둘이니 인코더도 둘이고, 이 기계는
        # 12스레드라 넷을 내주어도 30Hz 루프가 쓸 몫이 남는다. `loop_hz`가 떨어지면
        # 여기를 1로 낮추는 것이 첫 번째 손잡이다.
        encoder_threads=2,
    )
    return RecordConfig(
        robot=robot,
        teleop=teleop,
        dataset=dataset,
        display_data=False,
        play_sounds=False,
        # `record()`의 `if cfg.resume:` 갈래다. 새로 만드는 대신
        # `LeRobotDataset.resume(...)`으로 열고, 로봇·fps·feature가 맞는지
        # `sanity_check_dataset_robot_compatibility`가 본다.
        resume=resume,
    )


def _file_sha256(path: Path) -> str | None:
    """이 파일의 내용 해시. 없거나 읽지 못하면 `None`.

    calibration은 데이터를 읽는 방식 자체다 — 같은 서보 눈금이 어떤 각도로 번역되는지가
    거기 적혀 있다. 다시 잰 calibration으로 찍은 회차는 앞의 회차와 **다른 좌표계**에
    있고, 그 사실은 데이터셋 안에서 전혀 보이지 않는다. 해시를 남겨 두면 나중에 두 회차가
    같은 자로 잰 것인지 물어볼 수 있다.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _server_commit() -> str | None:
    """이 데이터를 찍은 코드의 커밋. 저장소가 아니거나 git이 없으면 `None`."""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parents[2]), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _provenance(settings: Settings, config: RecordConfig) -> dict[str, object]:
    """이 수집 회가 어떤 조건에서 찍혔는지. 자식이 아는 몫만.

    카메라 컨트롤과 시작 진단은 부모(`record_manager`)가 쥐고 있으므로 여기서 적지 않는다.
    부모가 회가 끝나는 자리에서 그 둘을 얹어 `data/<dataset>/soarm_provenance.json`에
    **덧붙인다** — 한 데이터셋에 여러 번 이어 찍으면 항목이 그만큼 늘어난다.
    """
    return {
        # `observation.wall_time`이 재는 0초. 이것이 없으면 그 열은 아무 시각도 가리키지 않는다.
        "started_at": _started_at,
        "server_commit": _server_commit(),
        "lerobot": version("lerobot"),
        "follower_calibration_sha256": _file_sha256(settings.follower_calibration),
        "leader_calibration_sha256": _file_sha256(settings.leader_calibration),
        "episode_seconds": int(config.dataset.episode_time_s),
        "reset_seconds": int(config.dataset.reset_time_s),
        "fps": int(config.dataset.fps),
        "extras_schema": sensors.EXTRAS_SCHEMA,
    }


def _existing_episode_count(dataset_name: str) -> int:
    """이어 찍기 전에 이미 들어 있는 회차 수.

    `save_episode` wrapper는 저장할 때만 돌므로, 이어 찍기가 한 회도 저장하지 못하고
    끝나면 `episodes_saved`가 0이 된다. 그 데이터셋에 여덟 회가 들어 있는데도 화면이
    0이라고 말하는 일은 없어야 한다.
    """
    try:
        info = json.loads(
            (DATA_ROOT / dataset_name / "meta/info.json").read_text(encoding="utf-8")
        )
        return int(info.get("total_episodes", 0))
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def main() -> None:
    global _align_on_start

    settings = Settings()
    if not settings.camera_roles_confirmed:
        raise SystemExit("Refusing record: SOARM_CAMERA_ROLES_CONFIRMED=1 is required")
    if not settings.motion_enabled:
        raise SystemExit("Refusing to record: SOARM_ENABLE_MOTION=1 is required")
    # 가상 리더로 찍을 때는 리더 팔의 calibration을 요구하지 않는다. 그 팔이 없는 것이
    # 이 경로의 존재 이유다. 팔로워 쪽은 어느 경로에서도 반드시 있어야 한다.
    teleop_source = os.getenv("SOARM_TELEOP_SOURCE", "leader")
    required = (
        [("follower", settings.follower_calibration)]
        if teleop_source == "virtual"
        else [("leader", settings.leader_calibration), ("follower", settings.follower_calibration)]
    )
    calibration_errors = [
        f"{role}: {error}" for role, path in required if (error := validate_calibration(path))
    ]
    if calibration_errors:
        raise SystemExit(f"Refusing record: invalid calibration: {calibration_errors}")
    missing = [path for _, path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Refusing to record: missing calibration files: {missing}")
    task = os.getenv("SOARM_TASK", "")
    resume = os.getenv("SOARM_RESUME", "0") == "1"
    dataset_name = os.getenv("SOARM_DATASET_NAME", "")
    if not dataset_name:
        dataset_name = default_dataset_name(task)
        if (DATA_ROOT / dataset_name).exists():
            # 같은 분 안에 같은 과제를 두 번 시작했다. 초까지 적은 이름으로 물러난다 —
            # 기존 폴더에 조용히 섞이는 것보다 이름이 덜 예쁜 편이 낫다.
            dataset_name = "soarm101_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    if resume:
        _note_episodes_saved(_existing_episode_count(dataset_name))
    try:
        devices = [settings.follower_port, settings.scene_camera, settings.wrist_camera]
        if teleop_source == "leader":
            devices.append(settings.leader_port)
        lock_context = (
            nullcontext()
            if inherited_locks_cover(devices)
            else DeviceLockSet.acquire(devices, f"record-{teleop_source}")
        )
    except DeviceLockError as exc:
        raise SystemExit(f"Refusing record: {exc}") from exc

    with lock_context:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        CONTROL_PATH.unlink(missing_ok=True)
        config = build_record_config(
            settings, task, dataset_name, teleop_source=teleop_source, resume=resume
        )
        _write_status(
            phase="starting",
            dataset_name=dataset_name,
            task=task,
            teleop=teleop_source,
            resumed=resume,
            episode_started_at=None,
            episode_seconds=int(config.dataset.episode_time_s),
            episode_index=0,
            reset_started_at=None,
            reset_seconds=int(config.dataset.reset_time_s),
            episodes_saved=_episodes_saved,
            empty_episodes_skipped=_empty_episodes_skipped,
            saving_seconds_estimate=None,
            last_control=None,
            last_control_ignored=None,
            started_at=_started_at,
            provenance=_provenance(settings, config),
            **_idle_rate_fields(),
        )
        # 물리 리더로 찍을 때만 정렬한다. 가상 리더는 `vleader.start_relay`가 팔로워의
        # 지금 자세에서 목표를 이어 주므로 첫 틱에 뛸 자리가 없다.
        _align_on_start = teleop_source == "leader"
        install_safe_follower_start()
        install_safe_leader_start()
        lerobot_record.init_keyboard_listener = _init_gui_listener
        lerobot_record.record_loop = _record_loop_with_status
        # 서보 판독값 열. features를 만드는 자리와 프레임을 채우는 자리를 함께 갈아 끼운다 —
        # 한쪽만 바꾸면 `validate_frame`이 "열은 있는데 값이 없다"로 회차를 통째로 막는다.
        lerobot_record.combine_feature_dicts = _combine_feature_dicts_with_sensors
        lerobot_record.build_dataset_frame = _build_dataset_frame_with_sensors
        LeRobotDataset.save_episode = _save_episode_with_status
        try:
            # 여기서 `phase="recording"`을 쓰지 않는다. 모터와 카메라를 여는 데 몇 초가
            # 걸리고, 그 몇 초 동안 화면이 "기록 중"이라고 말하면 사람은 이미 시연을
            # 시작한다. 구간은 `record()`가 부르는 첫 `record_loop`가 적는다.
            lerobot_record.record(config)
            _write_status(
                phase="complete", episode_started_at=None, episodes_saved=_episodes_saved
            )
        except BaseException:
            _write_status(
                phase="error", episode_started_at=None, episodes_saved=_episodes_saved
            )
            raise


if __name__ == "__main__":
    main()
