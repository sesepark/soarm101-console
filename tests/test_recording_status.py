from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from soarm_console import recording, sensors


class _Robot:
    name = "so_follower"

    def get_observation(self) -> dict[str, float]:
        return {"shoulder_pan.pos": 0.0}


class _CameraRobot:
    """A follower whose cameras hand back a buffer every `repeat_every` ticks.

    LeRobot's `read_latest()` is non-blocking: with no new frame it returns the
    very same array object again. Handing back the same object is what a stalled
    camera looks like from inside the record loop, so that is what this fakes.
    """

    name = "so_follower"

    def __init__(self, repeat_every: int | None = None) -> None:
        self.repeat_every = repeat_every
        self.ticks = 0
        self.last_observation: dict[str, object] | None = None
        self._held: dict[str, np.ndarray] = {}

    def _frame(self, key: str) -> np.ndarray:
        held = self._held.get(key)
        if held is not None and self.repeat_every and self.ticks % self.repeat_every == 0:
            return held
        # Fresh capture: a new buffer, but identical pixels. A still scene must
        # not be mistaken for a camera that stopped delivering.
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        self._held[key] = frame
        return frame

    def get_observation(self) -> dict[str, object]:
        self.ticks += 1
        observation: dict[str, object] = {"shoulder_pan.pos": 0.0}
        for key in ("scene", "wrist"):
            observation[key] = self._frame(key)
        self.last_observation = observation
        return observation


def test_record_loop_status_tracks_episode_rate_and_reset(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", status_path)
    forwarded = []

    def fake_record_loop(*args, **kwargs):
        forwarded.append((args, kwargs["upstream_argument"]))
        for _ in range(8):
            kwargs["robot"].get_observation()
            time.sleep(1 / 30)

    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", fake_record_loop)
    dataset = SimpleNamespace(num_episodes=2)
    started_before = time.time()

    recording._record_loop_with_status(
        robot=_Robot(),
        dataset=dataset,
        control_time_s=17,
        upstream_argument="kept",
    )

    runtime = json.loads(status_path.read_text(encoding="utf-8"))
    assert runtime["phase"] == "recording"
    assert runtime["episode_started_at"] >= started_before
    assert runtime["episode_seconds"] == 17
    assert runtime["episode_index"] == 2
    assert 25.0 <= runtime["loop_hz"] <= 35.0
    assert forwarded == [((), "kept")]

    recording._record_loop_with_status(
        robot=_Robot(),
        control_time_s=5,
        upstream_argument="reset-kept",
    )

    resetting = json.loads(status_path.read_text(encoding="utf-8"))
    assert resetting["phase"] == "resetting"
    assert resetting["episode_started_at"] is None
    # Reset duration must not replace the configured episode duration.
    assert resetting["episode_seconds"] == 17
    assert resetting["episode_index"] == 2
    assert 25.0 <= resetting["loop_hz"] <= 35.0
    assert forwarded[-1] == ((), "reset-kept")


def _run_loop(monkeypatch, tmp_path, robot, ticks: int):
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")

    def fake_record_loop(*args, **kwargs):
        for _ in range(ticks):
            kwargs["robot"].get_observation()
            time.sleep(1 / 200)

    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", fake_record_loop)
    recording._record_loop_with_status(
        robot=robot,
        dataset=SimpleNamespace(num_episodes=0),
        control_time_s=10,
    )
    return json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))


def test_repeated_camera_buffer_counts_as_stale(tmp_path, monkeypatch):
    # Every other tick hands back the array the previous tick already saw.
    runtime = _run_loop(monkeypatch, tmp_path, _CameraRobot(repeat_every=2), ticks=20)

    assert sorted(runtime["camera_stale_pct"]) == ["scene", "wrist"]
    for role in ("scene", "wrist"):
        assert runtime["camera_stale_pct"][role] == pytest.approx(50.0)
        # Half the loop's ticks carried a new frame, so half its rate did too.
        assert runtime["camera_fresh_hz"][role] == pytest.approx(runtime["loop_hz"] / 2.0)


def test_new_buffer_every_tick_is_never_stale(tmp_path, monkeypatch):
    # Pixels never change, only the buffer does — the still-scene case that
    # counting from the encoded video cannot tell apart from a stalled camera.
    runtime = _run_loop(monkeypatch, tmp_path, _CameraRobot(), ticks=20)

    for role in ("scene", "wrist"):
        assert runtime["camera_stale_pct"][role] == 0.0
        assert runtime["camera_fresh_hz"][role] == runtime["loop_hz"]


def test_observation_without_cameras_publishes_no_camera_rates(tmp_path, monkeypatch):
    runtime = _run_loop(monkeypatch, tmp_path, _Robot(), ticks=5)

    assert runtime["camera_fresh_hz"] == {}
    assert runtime["camera_stale_pct"] == {}
    assert runtime["loop_hz"] > 0.0


def test_camera_frames_reach_the_loop_unchanged(tmp_path, monkeypatch):
    """The proxy consumes observations; it must not substitute or copy them."""
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    robot = _CameraRobot()
    seen = []

    def fake_record_loop(*args, **kwargs):
        for _ in range(3):
            seen.append(kwargs["robot"].get_observation())

    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", fake_record_loop)
    recording._record_loop_with_status(robot=robot, control_time_s=4)

    assert len(seen) == 3
    # The loop must receive the follower's own dict, with the follower's own
    # frame buffers still in it -- not a copy the proxy made while counting.
    assert seen[-1] is robot.last_observation
    assert seen[-1]["scene"] is robot._held["scene"]
    assert seen[-1]["wrist"] is robot._held["wrist"]


# MARK: 구간 — 회 사이 정리와 저장


def test_reset_phase_carries_its_own_clock(tmp_path, monkeypatch):
    """정리 구간 15초 동안 화면이 남은 시간을 셀 수 있어야 한다."""
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", lambda *a, **k: None)
    started_before = time.time()

    recording._record_loop_with_status(robot=_Robot(), control_time_s=15)
    resetting = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))

    assert resetting["phase"] == "resetting"
    assert resetting["reset_started_at"] >= started_before
    assert resetting["reset_seconds"] == 15

    # 다음 기록 구간은 그 시계를 지운다. 남아 있으면 화면이 기록 중에도 정리 시간을 센다.
    recording._record_loop_with_status(
        robot=_Robot(), dataset=SimpleNamespace(num_episodes=1), control_time_s=30
    )
    recording_status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert recording_status["phase"] == "recording"
    assert recording_status["reset_started_at"] is None
    # 정리 구간의 길이는 그대로 남는다 — 다음 정리도 같은 길이다.
    assert recording_status["reset_seconds"] == 15


def test_saving_an_episode_says_so_and_counts_what_landed(tmp_path, monkeypatch):
    """정리 15초 뒤 인코딩 ~8초. 그동안 루프는 서 있고 화면은 아무 말도 못 했다."""
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    seen: list[str] = []

    def fake_save(self):
        seen.append(json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))["phase"])
        self.num_episodes += 1

    monkeypatch.setattr(recording, "_ORIGINAL_SAVE_EPISODE", fake_save)
    dataset = SimpleNamespace(num_episodes=3)

    recording._save_episode_with_status(dataset)

    # 저장하는 **동안** 그렇게 말한다. 끝나고 말하면 조용한 8초는 그대로다.
    assert seen == ["saving"]
    runtime = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert runtime["episodes_saved"] == 4
    assert runtime["episode_started_at"] is None


def test_a_failed_save_still_reports_what_is_actually_in_the_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")

    def explode(self):
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(recording, "_ORIGINAL_SAVE_EPISODE", explode)
    with pytest.raises(RuntimeError):
        recording._save_episode_with_status(SimpleNamespace(num_episodes=2))

    runtime = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert runtime["episodes_saved"] == 2


# MARK: 조작 — 버리고 끝내기


def _control(monkeypatch, tmp_path, key: str) -> dict[str, bool]:
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(recording, "CONTROL_PATH", tmp_path / "control.json")
    events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
    listener = recording._GuiControlListener(events)
    try:
        (tmp_path / "control.json").write_text(json.dumps({"key": key}), encoding="utf-8")
        deadline = time.time() + 2.0
        while time.time() < deadline and not any(events.values()):
            time.sleep(0.01)
    finally:
        listener.stop()
    return events


def test_abort_throws_the_episode_away_and_ends_the_run(tmp_path, monkeypatch):
    """LeRobot 0.6.1의 `record()`는 이 셋을 함께 세우면 버리는 길과 끝내는 길을 한
    바퀴 안에서 만난다 — `clear_episode_buffer()`를 지나 `while` 조건에서 나간다."""
    events = _control(monkeypatch, tmp_path, "abort")

    assert events["rerecord_episode"] is True
    assert events["stop_recording"] is True
    assert events["exit_early"] is True
    runtime = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert runtime["last_control"] == "abort"


def test_esc_still_keeps_the_episode_it_was_recording(tmp_path, monkeypatch):
    """`esc`는 루프를 나온 뒤 `save_episode()`가 그대로 돌아 찍다 만 회를 저장한다.

    그것이 `abort`가 따로 있는 이유다 — 두 조작의 차이는 이 한 줄에 있다.
    """
    events = _control(monkeypatch, tmp_path, "esc")

    assert events["stop_recording"] is True
    assert events["exit_early"] is True
    assert events["rerecord_episode"] is False


def test_an_unknown_control_touches_nothing(tmp_path, monkeypatch):
    events = _control(monkeypatch, tmp_path, "launch")
    assert not any(events.values())


# MARK: 시작 정렬


class _Leader:
    def __init__(self, pose: dict[str, float]):
        self.pose = pose

    def get_action(self) -> dict[str, float]:
        return {f"{name}.pos": value for name, value in self.pose.items()}


class _AligningRobot(_Robot):
    def __init__(self, pose: dict[str, float]):
        self.bus = SimpleNamespace(sync_read=lambda register, num_retry=0: dict(pose))
        self.sent: list[dict[str, float]] = []

    def send_action(self, action):
        self.sent.append({k.removesuffix(".pos"): v for k, v in action.items()})
        return action


@pytest.fixture
def unaligned(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", lambda *a, **k: None)
    monkeypatch.setattr(recording, "_aligned", False)
    monkeypatch.setattr(recording, "_align_on_start", True)
    monkeypatch.setattr("lerobot.utils.robot_utils.precise_sleep", lambda seconds: None)


def test_the_first_episode_walks_the_follower_to_the_leader_first(unaligned, tmp_path):
    """LeRobot의 첫 틱은 리더 자세를 그대로 보낸다. 두 팔이 다르면 팔로워가 그 차이만큼
    한 번에 뛴다 — `SOARM_MAX_RELATIVE_TARGET`이 걸릴 수 없는 값이라 잘리지도 않는다."""
    robot = _AligningRobot({"shoulder_pan": 0.0, "gripper": 0.0})
    leader = _Leader({"shoulder_pan": 30.0, "gripper": 20.0})

    recording._record_loop_with_status(
        robot=robot, teleop=leader, dataset=SimpleNamespace(num_episodes=0), control_time_s=30
    )

    assert robot.sent, "정렬이 아무것도 명령하지 않았다"
    assert robot.sent[-1] == pytest.approx({"shoulder_pan": 30.0, "gripper": 20.0})
    # 두 번째 회차에는 하지 않는다 — 그때 팔로워는 이미 리더를 따라온 자리에 있다.
    before = len(robot.sent)
    recording._record_loop_with_status(
        robot=robot, teleop=leader, dataset=SimpleNamespace(num_episodes=1), control_time_s=30
    )
    assert len(robot.sent) == before


def test_the_virtual_leader_path_does_not_align(monkeypatch, tmp_path, unaligned):
    """`vleader.start_relay`가 팔로워의 지금 자세에서 목표를 이어 주므로 뛸 자리가 없다."""
    monkeypatch.setattr(recording, "_align_on_start", False)
    robot = _AligningRobot({"shoulder_pan": 0.0})

    recording._record_loop_with_status(
        robot=robot,
        teleop=_Leader({"shoulder_pan": 90.0}),
        dataset=SimpleNamespace(num_episodes=0),
        control_time_s=30,
    )

    assert robot.sent == []


# MARK: 데이터셋 이름


def test_a_new_dataset_is_named_in_local_time_and_says_what_it_holds():
    """UTC로 짓던 때는 저녁 6시 20분에 찍은 것이 `..._092024`로 적혔다."""
    from datetime import datetime

    evening = datetime(2026, 9, 5, 18, 20, 24)
    assert (
        recording.default_dataset_name("Pick and place", evening) == "pick_and_place_20260905_1820"
    )


@pytest.mark.parametrize(
    "task",
    ["Pick and place", "test2", "빨간 블록 집기", "x" * 200, "  ", "-lead", "a/b", "drop; rm -rf"],
)
def test_every_generated_dataset_name_is_one_the_console_will_accept(task):
    """이름이 그대로 경로가 된다. 여기서 만드는 이름은 `NAME_PATTERN` 안이어야 한다."""
    name = recording.default_dataset_name(task)
    assert recording.NAME_PATTERN.match(name), name

    from soarm_console.datasets import NAME_PATTERN

    assert NAME_PATTERN.match(name), name


# MARK: 수집 중 스냅숏


class _PreviewRobot:
    """카메라 키가 `observation.images.<role>`로 오는 팔로워."""

    name = "so_follower"

    def __init__(self, keys=("scene", "observation.images.wrist")):
        self.keys = keys

    def get_observation(self) -> dict[str, object]:
        observation: dict[str, object] = {"shoulder_pan.pos": 0.0}
        for key in self.keys:
            observation[key] = np.full((8, 8, 3), 7, dtype=np.uint8)
        return observation


def test_a_slow_encoder_does_not_slow_the_thirty_hertz_loop(tmp_path, monkeypatch):
    """인코딩은 별 스레드에서 한다.

    `cv2.imencode`는 640x480 한 장에 수 밀리초가 들고 한 틱의 예산은 33ms다. 루프
    안에서 두 대를 인코딩하면 그 자체가 데이터의 시간축을 늘리는데, `timestamp`가
    `frame_index / fps`로 합성된 값이라 그 늘어남은 데이터에 남지도 않는다.
    """
    import cv2

    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")

    def slow_encode(extension, frame, params=None):
        time.sleep(0.05)  # 한 틱 예산의 1.5배. 루프 안에 있었다면 반드시 표가 난다.
        return True, np.frombuffer(b"jpeg-bytes", dtype=np.uint8)

    monkeypatch.setattr(cv2, "imencode", slow_encode)

    def fake_record_loop(*args, **kwargs):
        for _ in range(45):
            kwargs["robot"].get_observation()
            time.sleep(1 / 30)

    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", fake_record_loop)
    recording._record_loop_with_status(
        robot=_PreviewRobot(), dataset=SimpleNamespace(num_episodes=0), control_time_s=10
    )

    runtime = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert 25.0 <= runtime["loop_hz"] <= 35.0
    # 그리고 그림은 실제로 나왔다. 역할은 키의 마지막 마디다.
    assert (tmp_path / "preview-scene.jpg").read_bytes() == b"jpeg-bytes"
    assert (tmp_path / "preview-wrist.jpg").read_bytes() == b"jpeg-bytes"


def test_the_snapshot_is_written_at_most_five_times_a_second(tmp_path, monkeypatch):
    """30Hz 루프의 프레임을 전부 인코딩할 이유가 없다. 사람이 보는 그림이지 데이터가 아니다."""
    writer = recording._PreviewWriter(tmp_path)
    try:
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        for _ in range(30):
            writer.offer("scene", frame)
    finally:
        writer.stop()

    # 30번을 내밀었지만 5Hz 문턱을 지난 것은 처음 하나뿐이다.
    assert list(writer._last_offered) == ["scene"]
    assert len(writer._pending) == 0


def test_a_preview_failure_never_stops_the_recording(tmp_path, monkeypatch):
    import cv2

    monkeypatch.setattr(
        cv2, "imencode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no encoder"))
    )
    writer = recording._PreviewWriter(tmp_path)
    writer.offer("scene", np.zeros((4, 4, 3), dtype=np.uint8))
    time.sleep(0.2)
    writer.stop()

    assert not (tmp_path / "preview-scene.jpg").exists()


# MARK: 서보 판독값이 관측에 얹히는 자리


class _BusRobot(_CameraRobot):
    """버스를 가진 팔로워. 블록 읽기가 몇 번 일어나는지 세기 위한 것이다."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.last_read: tuple[int, int, tuple[int, ...]] | None = None
        robot = self

        class _Bus:
            motors = {
                name: SimpleNamespace(id=index + 1)
                for index, name in enumerate(sensors.MOTORS)
            }
            # 어느 레지스터를 물어보든 7을 돌려준다. 여기서 보려는 것은 값이 아니라
            # "틱당 몇 번 읽었나"와 "값이 관측에 실렸나"이다. 바이트를 쪼개고 부호를 푸는
            # 쪽은 `test_sensors.py`가 진짜 버스로 본다.
            sync_reader = SimpleNamespace(getData=lambda *args: 7)

            def _sync_read(self, addr, length, ids, **kwargs):
                robot.reads += 1
                robot.last_read = (addr, length, tuple(ids))
                return {id_: 0 for id_ in ids}, 0

            def _is_comm_success(self, comm):
                return comm == 0

            def _decode_sign(self, data_name, ids_values):
                return ids_values

        self.bus = _Bus()


def test_an_episode_reads_the_servo_block_once_per_tick_and_lands_it_on_the_observation(
    tmp_path, monkeypatch
):
    robot = _BusRobot()
    runtime = _run_loop(monkeypatch, tmp_path, robot, ticks=6)

    assert robot.reads == 6
    assert robot.last_read == (sensors.BLOCK_ADDRESS, sensors.BLOCK_LENGTH, (1, 2, 3, 4, 5, 6))
    # 관측 dict에 값이 실렸다. `build_dataset_frame`이 이 키들을 보고 열을 채운다.
    observation = robot.last_observation
    assert observation["gripper.load"] == 7.0
    assert observation["shoulder_pan.temp"] == 7.0
    assert observation["since_start"] > 0.0
    assert observation[sensors.CAMERA_FRESH_KEY] == [1.0, 1.0]
    # 그리고 카메라 그림은 그대로 남아 있다.
    assert observation["scene"] is robot._held["scene"]
    # 상태는 이 읽기가 30Hz 예산에서 얼마를 가져갔는지 말한다.
    assert runtime["extras_read_ms"] >= 0.0
    assert runtime["extras_read_failures"] == 0


def test_the_reset_segment_does_not_touch_the_bus(tmp_path, monkeypatch):
    """정리 구간의 값은 어느 프레임에도 들어가지 않는다. 읽으면 버스 시간만 쓴다."""
    monkeypatch.setattr(recording, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(recording, "STATUS_PATH", tmp_path / "status.json")
    robot = _BusRobot()

    def fake_record_loop(*args, **kwargs):
        for _ in range(4):
            kwargs["robot"].get_observation()

    monkeypatch.setattr(recording, "_ORIGINAL_RECORD_LOOP", fake_record_loop)
    recording._record_loop_with_status(robot=robot, dataset=None, control_time_s=5)

    assert robot.reads == 0
    assert "gripper.load" not in robot.last_observation


# MARK: 회 단위 provenance


def test_provenance_says_what_this_run_was_recorded_with(tmp_path, monkeypatch):
    from soarm_console.config import Settings

    settings = Settings(
        scene_camera="/dev/v4l/by-path/scene", wrist_camera="/dev/v4l/by-path/wrist"
    )
    config = recording.build_record_config(settings, "Pick and place", "soarm101_pick")

    provenance = recording._provenance(settings, config)

    assert provenance["started_at"] == recording._started_at
    assert provenance["fps"] == 30
    assert provenance["episode_seconds"] == config.dataset.episode_time_s
    assert provenance["reset_seconds"] == config.dataset.reset_time_s
    assert provenance["extras_schema"] == sensors.EXTRAS_SCHEMA
    assert provenance["lerobot"].startswith("0.6")
    # calibration 해시는 파일이 있을 때만 값이 있다. 없다는 것도 기록이다.
    assert set(provenance) == {
        "started_at",
        "server_commit",
        "lerobot",
        "follower_calibration_sha256",
        "leader_calibration_sha256",
        "episode_seconds",
        "reset_seconds",
        "fps",
        "extras_schema",
    }


def test_calibration_hashes_identify_the_ruler_the_data_was_measured_with(tmp_path):
    calibration = tmp_path / "follower.json"
    calibration.write_text('{"shoulder_pan": {}}', encoding="utf-8")

    import hashlib

    assert recording._file_sha256(calibration) == hashlib.sha256(
        calibration.read_bytes()
    ).hexdigest()
    assert recording._file_sha256(tmp_path / "absent.json") is None
