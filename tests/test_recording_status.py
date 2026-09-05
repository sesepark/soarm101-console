from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from soarm_console import recording


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
