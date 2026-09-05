"""재생 경로. 팔 없이 도는 시험만 여기에 둔다.

실물 확인은 사람이 옆에 있을 때만 하고, 그 절차는 `SAFETY.md`에 적혀 있다.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soarm_console import datasets, replaying
from soarm_console.follower_start import TELEOP_ALIGNMENT
from soarm_console.replaying import (
    ALIGN_DEGREES_PER_SECOND,
    ALIGN_HZ,
    ALIGN_MAX_SECONDS,
    ALIGN_MIN_SECONDS,
    ALIGN_PERCENT_PER_SECOND,
    ALIGN_REFUSE_DISTANCE,
    REPLAY_ALIGNMENT,
    SCURVE_PEAK_FACTOR,
    alignment_frames,
    alignment_refusal,
    alignment_seconds,
    smoothstep,
)


JOINTS = [
    "shoulder_pan.pos",
    "shoulder_lift.pos",
    "elbow_flex.pos",
    "wrist_flex.pos",
    "wrist_roll.pos",
    "gripper.pos",
]
MOTORS = [name.removesuffix(".pos") for name in JOINTS]


def _pose(*values: float) -> dict[str, float]:
    return dict(zip(MOTORS, values, strict=True))


# MARK: 보간 — 순수 함수


def test_smoothstep_starts_and_ends_at_rest():
    assert smoothstep(0.0) == 0.0
    assert smoothstep(1.0) == 1.0
    assert smoothstep(0.5) == pytest.approx(0.5)
    # 양 끝의 속도가 0이라는 것이 s-curve를 고른 이유다. 선형이면 출발과 정지가 계단으로
    # 꺾이고, 그 두 지점이 팔이 가장 크게 흔들리는 자리다.
    step = 1e-4
    assert smoothstep(step) / step < 0.01
    assert (1.0 - smoothstep(1.0 - step)) / step < 0.01


def test_alignment_time_is_clamped_at_both_ends():
    assert alignment_seconds(_pose(0, 0, 0, 0, 0, 0), _pose(0, 0, 0, 0, 0, 0)) == ALIGN_MIN_SECONDS
    assert alignment_seconds(_pose(0, 0, 0, 0, 0, 0), _pose(1, 0, 0, 0, 0, 0)) == ALIGN_MIN_SECONDS
    far = _pose(1000, 0, 0, 0, 0, 0)
    assert alignment_seconds(_pose(0, 0, 0, 0, 0, 0), far) == ALIGN_MAX_SECONDS


def test_the_speed_limit_holds_up_to_the_distance_the_time_clamp_can_carry():
    """첨두 20°/s가 실제 상한인 구간이 어디까지인지를 여기에 적어 둔다.

    이동 시간이 `ALIGN_MAX_SECONDS`에서 잘리면 그 위로는 같은 시간에 더 먼 거리를 가야
    하므로 속도가 다시 올라간다. 한때는 거절 거리(60도)가 그 지점보다 아래에 있어서
    "초당 20도"가 어느 자세에서든 참이었다. 거절 거리를 관절 폭(360도)까지 올린 지금은
    그렇지 않고, 대신 사람이 `/api/replay/preview`에서 거리와 시간을 먼저 읽는다.

    그러니 여기서 지키는 것은 두 가지다 — 상한이 뜻을 갖는 구간의 끝이 어디인지, 그리고
    그 바깥에서도 속도가 팔이 감당할 수 없는 값으로 튀지는 않는지.
    """
    assert REPLAY_ALIGNMENT.smooth_distance == pytest.approx(200.0)
    # 가장 먼 자세(관절 하나가 폭 전체만큼 떨어진 경우)에서도 첨두는 이 값이다.
    worst = ALIGN_REFUSE_DISTANCE * SCURVE_PEAK_FACTOR / ALIGN_MAX_SECONDS
    assert worst == pytest.approx(36.0)
    # 그리고 그 값은 시작 정렬(첨두 40°/s)이 이미 내는 속도보다 느리다. 팔이 한 번도 내
    # 본 적 없는 속도를 재생이 처음으로 내는 일은 없어야 한다.
    assert worst <= TELEOP_ALIGNMENT.degrees_per_second
    assert ALIGN_REFUSE_DISTANCE * SCURVE_PEAK_FACTOR / ALIGN_MAX_SECONDS <= ALIGN_PERCENT_PER_SECOND * 2


@pytest.mark.parametrize(
    "goal",
    [
        _pose(60, -60, 60, -60, 60, 60),
        _pose(59.9, 0, 0, 0, 0, 0),
        _pose(0, 0, 0, 0, 0, 60),
        _pose(1, -2, 3, -4, 5, 6),
        _pose(0.05, 0, 0, 0, 0, 0),
    ],
)
def test_alignment_never_commands_more_than_the_speed_limit(goal):
    """60도 검사를 통과한 자세라면 보간이 초당 20도(집게 25%)를 넘지 않는다."""
    start = _pose(0, 0, 0, 0, 0, 0)
    assert alignment_refusal(start, goal) is None

    frames = alignment_frames(start, goal)
    previous = start
    for frame in frames:
        for name, value in frame.items():
            limit = ALIGN_PERCENT_PER_SECOND if name == "gripper" else ALIGN_DEGREES_PER_SECOND
            per_second = abs(value - previous[name]) * ALIGN_HZ
            assert per_second <= limit + 1e-6, (name, per_second, limit)
        previous = frame


def test_alignment_lands_exactly_on_the_episodes_first_frame():
    start = _pose(0, 0, 0, 0, 0, 0)
    goal = _pose(10, -20, 30, -5, 15, 40)
    frames = alignment_frames(start, goal)
    assert frames[-1] == pytest.approx(goal)
    # 첫 명령은 지금 자리에서 아주 조금만 떨어져 있다. 정렬은 출발부터 기어간다.
    assert max(abs(frames[0][name] - start[name]) for name in goal) < 0.05


def test_a_pose_the_arm_can_actually_reach_is_never_refused_for_distance():
    """95도 떨어진 팔은 이제 시작한다. 한때 이것이 거절이었다.

    통상 흐름에서는 걸리지 않는 값이었지만, 걸리면 빠져나올 길이 없었다 — 토크가 걸린
    팔은 손으로 옮길 수 없어 텔레옵을 한 번 띄워야 했다. 지금은 걸어서 데려간다.
    """
    assert alignment_refusal(_pose(0, 0, 0, 0, 0, 0), _pose(5, -95, 0, 0, 0, 0)) is None


def test_alignment_is_refused_and_says_which_joint_is_far():
    """거리로 거절하는 일은 이제 관절 폭을 넘는 값에서만 일어난다."""
    start = _pose(0, 0, 0, 0, 0, 0)
    goal = _pose(5, -400, 0, 0, 0, 0)
    refusal = alignment_refusal(start, goal)
    assert refusal is not None
    assert "shoulder_lift" in refusal
    assert "400.0°" in refusal
    # 통과한 관절은 문구에 끼지 않는다 — 사람이 어디를 보아야 하는지가 흐려진다.
    assert "shoulder_pan" not in refusal


def test_alignment_is_refused_when_the_episode_has_other_joints():
    refusal = alignment_refusal(_pose(0, 0, 0, 0, 0, 0), {"elbow_flex": 0.0})
    assert refusal is not None
    assert "do not match" in refusal


# MARK: 데이터셋


def _make_dataset(root: Path, name: str) -> Path:
    directory = root / name
    (directory / "meta").mkdir(parents=True)
    feature = {"dtype": "float32", "shape": [6], "names": JOINTS}
    (directory / "meta/info.json").write_text(
        json.dumps({
            "codebase_version": "v3.0",
            "fps": 30,
            "total_episodes": 1,
            "total_frames": 3,
            "robot_type": "so101_follower",
            "features": {"observation.state": feature, "action": feature},
        }),
        encoding="utf-8",
    )
    return directory


def _write_episode(directory: Path, episode_index: int, action: list[list[float]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    meta = directory / "meta/episodes/chunk-000/file-000.parquet"
    meta.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "episode_index": [episode_index],
            "length": [len(action)],
            "data/chunk_index": [0],
            "data/file_index": [0],
        }),
        meta,
    )
    data = directory / "data/chunk-000/file-000.parquet"
    data.parent.mkdir(parents=True, exist_ok=True)
    # 거꾸로 적어 둔다. 재생이 frame_index를 실제로 보는지 확인하려는 것이다 — 순서가
    # 뒤집힌 채 팔에 들어가면 동작이 거꾸로 재생된다.
    pq.write_table(
        pa.table({
            "episode_index": [episode_index] * len(action),
            "frame_index": list(reversed(range(len(action)))),
            "observation.state": list(reversed(action)),
            "action": list(reversed(action)),
        }),
        data,
    )


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(datasets, "data_root", lambda: root)
    return root


@pytest.fixture
def episode(data_root):
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(
        directory,
        0,
        [
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            [2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
        ],
    )
    return directory


def test_episode_actions_are_read_in_frame_order_without_a_frame_limit(episode, monkeypatch):
    monkeypatch.setattr(datasets, "TRAJECTORY_FRAME_LIMIT", 1)
    payload = datasets.episode_actions("soarm101_pick", 0)
    assert payload["fps"] == 30
    assert payload["frames"] == 3
    assert payload["joints"] == JOINTS
    assert payload["action"][0] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert payload["action"][-1] == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]


def test_episode_first_pose_is_keyed_by_motor_name(episode):
    assert replaying.episode_first_pose("soarm101_pick", 0) == _pose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_a_missing_episode_is_not_found(episode):
    with pytest.raises(FileNotFoundError):
        datasets.episode_actions("soarm101_pick", 7)


# MARK: 끝점


@pytest.fixture
def client(monkeypatch, tmp_path):
    from soarm_console import app as app_module

    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, motion_enabled=True)
    )
    monkeypatch.setattr(app_module.replayer, "runtime_dir", tmp_path / "replay")
    return TestClient(app_module.app)


@pytest.fixture
def started(monkeypatch):
    """`replayer.start`를 붙잡아 둔다. 시험에서 실제 프로세스를 띄우지 않는다."""
    from soarm_console import app as app_module

    calls: list[tuple[str, int, float]] = []
    monkeypatch.setattr(
        app_module.replayer,
        "start",
        lambda dataset, episode, speed: calls.append((dataset, episode, speed)),
    )
    return calls


@pytest.fixture
def near(monkeypatch):
    """팔이 에피소드의 첫 자세 바로 옆에 있다고 둔다."""
    from soarm_console import app as app_module

    monkeypatch.setattr(
        app_module, "present_position", lambda settings: _pose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    )


def _body(**overrides) -> dict[str, object]:
    body = {"confirmation": "REPLAY SOARM101", "dataset": "soarm101_pick", "episode": 0}
    body.update(overrides)
    return body


def test_replay_requires_the_exact_confirmation_phrase(client, episode, near, started):
    response = client.post("/api/replay/start", json=_body(confirmation="replay soarm101"))
    assert response.status_code == 400
    assert started == []


def test_replay_refuses_while_the_motion_gate_is_shut(monkeypatch, episode, near, started, tmp_path):
    from soarm_console import app as app_module

    monkeypatch.setattr(app_module.replayer, "runtime_dir", tmp_path / "replay")
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, motion_enabled=False)
    )
    response = TestClient(app_module.app).post("/api/replay/start", json=_body())
    assert response.status_code == 400
    assert "SOARM_ENABLE_MOTION=1" in response.json()["detail"]
    assert started == []


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ("soarm_console.record_manager.RecordManager", "Stop recording"),
        ("soarm_console.teleop.TeleopManager", "Stop teleoperation"),
        ("soarm_console.vleader.api.VirtualLeader", "Stop the virtual leader"),
        ("soarm_console.replay_manager.ReplayManager", "Stop the replay that is already running"),
    ],
)
def test_replay_refuses_while_another_mode_owns_the_arm(
    client, episode, near, started, monkeypatch, owner, expected
):
    module_name, _, class_name = owner.rpartition(".")
    module = __import__(module_name, fromlist=[class_name])
    monkeypatch.setattr(getattr(module, class_name), "running", True)

    response = client.post("/api/replay/start", json=_body())

    assert response.status_code == 409
    # 거절 문구는 무엇을 먼저 멈춰야 하는지 적는다. 화면이 그것을 옮겨 어느 단추인지
    # 알려 줄 수 있어야 한다.
    assert expected in response.json()["detail"]
    assert started == []


def test_replay_refuses_a_dataset_or_episode_that_is_not_there(client, episode, near, started):
    missing_dataset = client.post("/api/replay/start", json=_body(dataset="never_recorded"))
    missing_episode = client.post("/api/replay/start", json=_body(episode=7))
    assert missing_dataset.status_code == 400
    assert missing_episode.status_code == 400
    assert started == []


def test_replay_takes_only_the_three_speeds(client, episode, near, started):
    assert client.post("/api/replay/start", json=_body(speed=2.0)).status_code == 400
    assert client.post("/api/replay/start", json=_body(speed=0.1)).status_code == 400
    assert started == []


def test_replay_starts_from_eighty_degrees_away(
    client, episode, started, monkeypatch
):
    """한때 이 자세는 400이었다. 지금은 걸어서 데려간다."""
    from soarm_console import app as app_module

    # 팔꿈치가 에피소드의 첫 자세에서 80도 떨어져 있다.
    monkeypatch.setattr(
        app_module, "present_position", lambda settings: _pose(1.0, 2.0, 83.0, 4.0, 5.0, 6.0)
    )

    assert client.post("/api/replay/start", json=_body()).status_code == 200
    assert started == [("soarm101_pick", 0, 0.5)]


    assert client.post("/api/replay/start", json=_body()).status_code == 200
def test_replay_starts_at_half_speed_by_default(client, episode, near, started):
    response = client.post("/api/replay/start", json=_body())
    assert response.status_code == 200
    assert started == [("soarm101_pick", 0, 0.5)]
    assert response.json()["running"] is False  # start가 붙잡혀 있으므로 프로세스는 없다


def test_replay_carries_the_chosen_speed_through(client, episode, near, started):
    assert client.post("/api/replay/start", json=_body(speed=0.25)).status_code == 200
    assert client.post("/api/replay/start", json=_body(speed=1.0)).status_code == 200
    assert started == [("soarm101_pick", 0, 0.25), ("soarm101_pick", 0, 1.0)]


def test_replay_reports_an_arm_it_cannot_read_as_a_conflict(client, episode, started, monkeypatch):
    from soarm_console import app as app_module

    def refuse(settings):
        raise replaying.ReplayError("Another process owns the follower arm")

    monkeypatch.setattr(app_module, "present_position", refuse)
    response = client.post("/api/replay/start", json=_body())
    assert response.status_code == 409
    assert started == []


def test_stopping_writes_the_control_file_the_loop_watches(client, tmp_path):
    from soarm_console import app as app_module

    response = client.post("/api/replay/stop")

    assert response.status_code == 200
    control = app_module.replayer.runtime_dir / "control.json"
    assert json.loads(control.read_text(encoding="utf-8")) == {"key": "stop"}


def test_status_carries_the_replay_subsystem(client):
    payload = client.get("/api/status").json()
    assert payload["replay"]["running"] is False
    assert payload["replay"]["speeds"] == [0.25, 0.5, 1.0]
    assert payload["replay"]["default_speed"] == 0.5
    assert "replay_preflight" in payload


# MARK: 루프 — 팔 대신 흉내 낸 로봇으로


class _FakeBus:
    def __init__(self, position: dict[str, float]):
        self.position = dict(position)

    def sync_read(self, name: str, num_retry: int = 0) -> dict[str, float]:
        assert name == "Present_Position"
        return dict(self.position)


class _FakeRobot:
    """`send_action`을 받아 적기만 한다. 서보가 즉시 따라간다고 본다."""

    def __init__(self, position: dict[str, float]):
        self.bus = _FakeBus(position)
        self.sent: list[dict[str, float]] = []
        self.disconnected = False
        self.on_send = None

    def get_observation(self) -> dict[str, float]:
        return {f"{name}.pos": value for name, value in self.bus.position.items()}

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        pose = {key.removesuffix(".pos"): float(value) for key, value in action.items()}
        self.bus.position.update(pose)
        self.sent.append(pose)
        if self.on_send is not None:
            self.on_send(len(self.sent))
        return action

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def loop_runtime(tmp_path, monkeypatch):
    """상태와 제어 파일을 시험 폴더로 옮기고 정렬을 짧게 줄인다."""
    runtime = tmp_path / "replay"
    runtime.mkdir()
    monkeypatch.setattr(replaying, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(replaying, "CONTROL_PATH", runtime / "control.json")
    monkeypatch.setattr(replaying, "STATUS_PATH", runtime / "status.json")
    monkeypatch.setattr(replaying, "ALIGN_HZ", 200.0)
    monkeypatch.setattr(replaying, "ALIGN_MIN_SECONDS", 0.05)
    return runtime


def _status(runtime: Path) -> dict:
    return json.loads((runtime / "status.json").read_text(encoding="utf-8"))


def test_the_loop_aligns_first_and_then_walks_the_episode(loop_runtime, episode, monkeypatch):
    from soarm_console.config import Settings

    robot = _FakeRobot(_pose(0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(replaying, "_connect", lambda settings: robot)

    replaying.run(Settings(), "soarm101_pick", 0, 1.0)

    runtime = _status(loop_runtime)
    assert runtime["phase"] == "complete"
    assert runtime["frame"] == 3
    assert runtime["total_frames"] == 3
    assert runtime["speed"] == 1.0
    # 정렬이 먼저 있었고, 그 마지막 자세가 에피소드의 첫 action이다.
    assert len(robot.sent) > 3
    assert robot.sent[-4] == pytest.approx(_pose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
    # 그다음이 에피소드다 — 순서 그대로.
    assert robot.sent[-3:] == [
        pytest.approx(_pose(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)),
        pytest.approx(_pose(1.5, 2.5, 3.5, 4.5, 5.5, 6.5)),
        pytest.approx(_pose(2.0, 3.0, 4.0, 5.0, 6.0, 7.0)),
    ]
    assert robot.disconnected is True


def test_the_loop_refuses_an_arm_it_cannot_measure_before_it_commands_anything(
    loop_runtime, episode, monkeypatch
):
    """콘솔이 이미 한 검사를 자식도 한 번 더 한다. 여기가 팔이 움직이기 직전의 마지막 자리다.

    거리로 거절하는 일은 이제 거의 없지만, **잴 수 없는 값**은 여전히 거절이다. 팔이
    어디 있는지 모르는 채로 보간을 시작하면 첫 프레임이 어디로 갈지 아무도 모른다.
    """
    from soarm_console.config import Settings

    robot = _FakeRobot(_pose(0.0, 0.0, float("nan"), 0.0, 0.0, 0.0))
    monkeypatch.setattr(replaying, "_connect", lambda settings: robot)

    with pytest.raises(replaying.ReplayError):
        replaying.run(Settings(), "soarm101_pick", 0, 1.0)

    assert robot.sent == []
    assert _status(loop_runtime)["phase"] == "error"
    assert robot.disconnected is True


def test_a_stop_leaves_the_loop_where_it_stands(loop_runtime, episode, monkeypatch):
    from soarm_console.config import Settings

    long_episode = [[float(index)] * 6 for index in range(300)]
    monkeypatch.setattr(
        replaying,
        "episode_actions",
        lambda name, index: {"fps": 30, "frames": len(long_episode), "joints": JOINTS,
                             "action": long_episode},
    )
    robot = _FakeRobot(_pose(0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

    def stop_after_a_few(count: int) -> None:
        # 정렬이 끝나고 재생이 몇 장 지난 뒤에 사람이 중지를 누른다.
        if count == 30:
            (loop_runtime / "control.json").write_text(json.dumps({"key": "stop"}), encoding="utf-8")

    robot.on_send = stop_after_a_few
    monkeypatch.setattr(replaying, "_connect", lambda settings: robot)

    replaying.run(Settings(), "soarm101_pick", 0, 1.0)

    runtime = _status(loop_runtime)
    assert runtime["phase"] == "stopped"
    assert runtime["frame"] < 300
    # 멈춘 뒤에는 아무 자세도 명령하지 않는다. 마지막으로 명령한 곳에 그대로 선다.
    assert len(robot.sent) < 300
    # 힘을 놓는 것은 다른 일이다 — 끊었지만 토크는 설정이 지킨다.
    assert robot.disconnected is True


def test_the_stop_listener_reads_the_control_file(loop_runtime):
    listener = replaying._StopListener()
    try:
        assert listener.stopped is False
        (loop_runtime / "control.json").write_text(json.dumps({"key": "stop"}), encoding="utf-8")
        for _ in range(100):
            if listener.stopped:
                break
            time.sleep(0.02)
        assert listener.stopped is True
        # 읽은 명령은 지운다. 다음 회차가 지난 회차의 중지로 시작하지 않게.
        assert not (loop_runtime / "control.json").exists()
    finally:
        listener.stop()


def test_the_speed_multiplier_only_changes_the_waiting(loop_runtime, episode, monkeypatch):
    """배율은 `precise_sleep`의 간격에만 곱한다. action 값 자체는 건드리지 않는다."""
    from soarm_console.config import Settings

    slept: list[float] = []
    monkeypatch.setattr(replaying, "_connect", lambda settings: _FakeRobot(_pose(1, 2, 3, 4, 5, 6)))
    import lerobot.utils.robot_utils as robot_utils

    monkeypatch.setattr(robot_utils, "precise_sleep", lambda seconds, **kwargs: slept.append(seconds))

    replaying.run(Settings(), "soarm101_pick", 0, 0.25)
    quarter = max(slept[-3:])
    slept.clear()
    replaying.run(Settings(), "soarm101_pick", 0, 1.0)
    full = max(slept[-3:])

    assert quarter == pytest.approx(full * 4, rel=0.05)


def test_a_row_that_does_not_match_the_joint_names_is_refused(data_root):
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[1.0, 2.0, 3.0]])
    with pytest.raises(datasets.DatasetError):
        datasets.episode_actions("soarm101_pick", 0)


def test_present_position_needs_the_follower_calibration(tmp_path):
    from soarm_console.config import Settings

    settings = Settings()
    assert not (tmp_path / "absent.json").exists()
    with pytest.raises(replaying.ReplayError):
        replaying.present_position(
            dataclasses.replace(settings, follower_id="never_calibrated"),
        )


def test_a_value_that_is_not_a_number_never_reaches_the_arm(data_root):
    """`nan > 60`은 거짓이다 — 거르지 않으면 60도 검사를 그냥 통과한다."""
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[1.0, 2.0, float("nan"), 4.0, 5.0, 6.0]])
    with pytest.raises(datasets.DatasetError):
        datasets.episode_actions("soarm101_pick", 0)

    # 검사는 거절 **문구**를 돌려준다. 화면이 그대로 옮겨 적을 수 있어야 하기 때문이다.
    refusal = replaying.alignment_refusal(
        _pose(0, 0, 0, 0, 0, 0), _pose(0, 0, float("inf"), 0, 0, 0)
    )
    assert refusal is not None
    assert "elbow_flex" in refusal
