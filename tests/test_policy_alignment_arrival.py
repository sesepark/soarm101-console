from __future__ import annotations

import json
import logging

import pytest

from soarm_console import policying


HOME = {
    "shoulder_pan": 1.2,
    "shoulder_lift": -53.63,
    "elbow_flex": 49.14,
    "wrist_flex": 78.15,
    "wrist_roll": 0.0,
    "gripper": 8.0,
}


def pose_with_error(joint: str, error: float) -> dict[str, float]:
    return {**HOME, joint: HOME[joint] + error}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeStopEvent:
    def __init__(self, clock: FakeClock, *, stop_on_wait: bool = False) -> None:
        self.clock = clock
        self.stop_on_wait = stop_on_wait
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        self.clock.now += timeout
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped


class FakeBus:
    def __init__(self, readings: list[dict[str, float]]) -> None:
        self.readings = readings
        self.index = 0

    def sync_read(self, register: str, num_retry: int = 0) -> dict[str, float]:
        assert register == "Present_Position"
        reading = self.readings[min(self.index, len(self.readings) - 1)]
        self.index += 1
        return dict(reading)


class FakeRobot:
    def __init__(self, readings: list[dict[str, float]]) -> None:
        self.bus = FakeBus(readings)
        self.actions: list[dict[str, float]] = []
        self.disconnected = False

    def send_action(self, action: dict[str, float]) -> None:
        self.actions.append(action)

    def disconnect(self) -> None:
        self.disconnected = True


def run_alignment(monkeypatch, readings, *, stop=None):
    clock = FakeClock()
    stop = stop or FakeStopEvent(clock)
    robot = FakeRobot(readings)
    align_starts = []
    statuses = []
    monkeypatch.setattr(policying, "_connect", lambda settings: robot)
    monkeypatch.setattr(policying.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        policying,
        "align",
        lambda robot, start, goal, **kwargs: align_starts.append(dict(start)) or False,
    )
    monkeypatch.setattr(policying, "_write_status", lambda **values: statuses.append(values))
    result = policying.align_home(object(), HOME, stop)
    return result, robot, align_starts, statuses


def test_settle_poll_detects_arrival_without_another_s_curve(monkeypatch):
    result, robot, starts, statuses = run_alignment(
        monkeypatch,
        [pose_with_error("elbow_flex", 30.0), pose_with_error("elbow_flex", 5.0), HOME],
    )

    assert result is False
    assert len(starts) == 1
    assert robot.actions == [{f"{name}.pos": value for name, value in HOME.items()}]
    assert statuses[-1]["alignment_residual"] == {}


def test_settled_one_point_five_degree_error_is_arrival(monkeypatch):
    result, robot, starts, statuses = run_alignment(
        monkeypatch,
        [pose_with_error("elbow_flex", 20.0), pose_with_error("elbow_flex", 1.5)],
    )

    assert result is False
    assert len(starts) == 1
    assert robot.actions == [{f"{name}.pos": value for name, value in HOME.items()}]
    assert statuses[-1]["alignment_residual"] == {}


def test_settled_five_degree_error_continues_with_residual(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=policying.__name__):
        result, _robot, starts, statuses = run_alignment(
            monkeypatch,
            [pose_with_error("elbow_flex", 20.0), pose_with_error("elbow_flex", 5.0)],
        )

    assert result is False
    assert len(starts) == 1
    assert statuses[-1]["alignment_residual"] == {"elbow_flex": 5.0}
    assert "continuing with settled alignment residual: elbow_flex 5.0" in caplog.text


def test_settled_forty_degree_error_times_out(monkeypatch):
    with pytest.raises(policying.ReplayError, match=r"remaining: elbow_flex 40.0"):
        run_alignment(
            monkeypatch,
            [pose_with_error("elbow_flex", 40.0)],
        )


def test_stop_during_settle_wait_returns_immediately(monkeypatch):
    clock = FakeClock()
    stop = FakeStopEvent(clock, stop_on_wait=True)

    result, robot, starts, _statuses = run_alignment(
        monkeypatch,
        [pose_with_error("elbow_flex", 20.0), pose_with_error("elbow_flex", 5.0)],
        stop=stop,
    )

    assert result is True
    assert len(starts) == 1
    assert clock.now == policying.ALIGNMENT_SETTLE_POLL_S
    assert robot.disconnected is True


def test_alignment_residual_survives_the_running_status_update(monkeypatch, tmp_path):
    status_path = tmp_path / "policy" / "status.json"
    monkeypatch.setattr(policying, "RUNTIME_DIR", status_path.parent)
    monkeypatch.setattr(policying, "STATUS_PATH", status_path)

    policying._write_status(phase="aligning", alignment_residual={"elbow_flex": 5.0})
    policying._write_status(phase="running")

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["phase"] == "running"
    assert status["alignment_residual"] == {"elbow_flex": 5.0}
