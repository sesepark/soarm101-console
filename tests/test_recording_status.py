from __future__ import annotations

import json
import time
from types import SimpleNamespace

from soarm_console import recording


class _Robot:
    name = "so_follower"

    def get_observation(self) -> dict[str, float]:
        return {"shoulder_pan.pos": 0.0}


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
