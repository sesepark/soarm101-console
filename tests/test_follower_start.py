"""붙는 순간과 첫 틱 — 팔이 튀는 두 자리. 팔 없이 도는 시험만 여기에 둔다.

실물 확인은 사람이 옆에 있을 때만 하고, 그 절차는 `SAFETY.md`에 적혀 있다.
"""

from __future__ import annotations

import pytest

from soarm_console.follower_start import (
    TELEOP_ALIGNMENT,
    align_follower_to_leader,
    follower_pose,
    leader_pose,
    sync_goal_to_present,
)
from soarm_console.replaying import ALIGN_HZ, SCURVE_PEAK_FACTOR, alignment_frames


MOTORS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]


def _pose(*values: float) -> dict[str, float]:
    return dict(zip(MOTORS, values, strict=True))


class _FakeBus:
    """`sync_read`/`sync_write`만 있는 버스. 서보가 즉시 따라간다고 본다."""

    def __init__(self, position: dict[str, float], raw: dict[str, int] | None = None):
        self.position = dict(position)
        self.raw = dict(raw or {name: 2048 for name in position})
        self.writes: list[tuple[str, dict[str, float], bool]] = []

    def sync_read(self, register, normalize=True, num_retry=0):
        assert register == "Present_Position"
        return dict(self.raw) if not normalize else dict(self.position)

    def sync_write(self, register, values, normalize=True, num_retry=0):
        self.writes.append((register, dict(values), normalize))


class _FakeRobot:
    def __init__(self, position: dict[str, float]):
        self.bus = _FakeBus(position)
        self.sent: list[dict[str, float]] = []

    def send_action(self, action: dict[str, float]) -> dict[str, float]:
        pose = {key.removesuffix(".pos"): float(value) for key, value in action.items()}
        self.bus.position.update(pose)
        self.sent.append(pose)
        return action


class _FakeLeader:
    def __init__(self, position: dict[str, float]):
        self.position = dict(position)

    def get_action(self) -> dict[str, float]:
        return {f"{name}.pos": value for name, value in self.position.items()}


# MARK: 붙는 순간


def test_goal_is_moved_to_where_the_arm_stands_before_torque_can_be_enabled():
    """STS3215는 토크가 걸리는 순간 남아 있던 옛 목표까지 **최고 속도로** 간다.

    현재 자세를 목표로 삼는 것은 그 레지스터에 값 128을 쓸 때뿐이므로, 우리가 먼저
    지금 자리를 써 넣어야 한다.
    """
    bus = _FakeBus(_pose(0, 0, 0, 0, 0, 0), raw={name: 1000 + index for index, name in enumerate(MOTORS)})

    sync_goal_to_present(bus)

    assert len(bus.writes) == 1
    register, values, normalize = bus.writes[0]
    assert register == "Goal_Position"
    # 읽은 것을 그대로 쓴다. 정규화를 거치지 않으므로 calibration이 아직 모터에 적히지
    # 않은 순간에도 두 값의 뜻이 흔들리지 않는다.
    assert normalize is False
    assert values == bus.raw


# MARK: 첫 틱


def test_start_alignment_never_commands_more_than_forty_degrees_a_second():
    """시작 정렬의 첨두는 40°/s(집게 50%/s)다. 재생(20°/s)보다 빠른 값이고, 사람이
    리더를 쥐고 보고 있는 동안 일어나는 걸음이라 그렇게 정했다."""
    start = _pose(0, 0, 0, 0, 0, 0)
    # `max_seconds`가 시간을 자르기 전까지 — 그 안에서 상한은 실제 상한이다.
    goal = _pose(150, -100, 60, -30, 120, 40)
    assert max(abs(value) for value in goal.values()) <= TELEOP_ALIGNMENT.smooth_distance

    previous = start
    for frame in alignment_frames(start, goal, ALIGN_HZ, TELEOP_ALIGNMENT):
        for name, value in frame.items():
            limit = TELEOP_ALIGNMENT.speed_limit_of(name)
            assert abs(value - previous[name]) * ALIGN_HZ <= limit + 1e-6, name
        previous = frame


def test_start_alignment_is_faster_than_replay_but_still_walks():
    """같은 거리를 재생보다 빨리 간다 — 그리고 여전히 한 번에 뛰지는 않는다."""
    from soarm_console.replaying import REPLAY_ALIGNMENT, alignment_seconds

    start, goal = _pose(0, 0, 0, 0, 0, 0), _pose(80, 0, 0, 0, 0, 0)
    assert alignment_seconds(start, goal, TELEOP_ALIGNMENT) == pytest.approx(3.0)
    assert alignment_seconds(start, goal, REPLAY_ALIGNMENT) == pytest.approx(6.0)


def test_alignment_walks_the_follower_all_the_way_to_the_leaders_pose(monkeypatch):
    monkeypatch.setattr("lerobot.utils.robot_utils.precise_sleep", lambda seconds: None)
    robot = _FakeRobot(_pose(0, 0, 0, 0, 0, 0))
    leader = _FakeLeader(_pose(30, -20, 10, -5, 15, 40))
    left: list[float] = []

    align_follower_to_leader(robot, leader, publish=left.append)

    # 첫 명령은 지금 자리에서 아주 조금만 떨어져 있다. 정렬은 출발부터 기어간다.
    assert max(abs(robot.sent[0][name]) for name in MOTORS) < 0.5
    # 마지막은 정확히 리더의 자세다. 여기서 어긋나면 루프의 첫 틱이 그 차이만큼 뛴다.
    assert robot.sent[-1] == pytest.approx(leader.position)
    # 남은 시간은 줄어들다 0으로 끝난다.
    assert left[0] > 0.0
    assert left[-1] == 0.0


def test_alignment_reads_the_two_arms_in_the_units_the_dataset_uses():
    """팔로워는 정규화된 `Present_Position`, 리더는 `get_action()`의 `<joint>.pos`."""
    robot = _FakeRobot(_pose(1, 2, 3, 4, 5, 6))
    leader = _FakeLeader(_pose(7, 8, 9, 10, 11, 12))

    assert follower_pose(robot) == _pose(1, 2, 3, 4, 5, 6)
    assert leader_pose(leader) == _pose(7, 8, 9, 10, 11, 12)


def test_alignment_that_is_interrupted_stops_where_it_stands(monkeypatch):
    """SIGINT는 잡지 않는다. 부르는 쪽이 토크를 놓지 않으므로 팔은 그 자리에 선다."""
    monkeypatch.setattr("lerobot.utils.robot_utils.precise_sleep", lambda seconds: None)
    robot = _FakeRobot(_pose(0, 0, 0, 0, 0, 0))
    leader = _FakeLeader(_pose(100, 0, 0, 0, 0, 0))
    original = robot.send_action

    def send(action):
        if len(robot.sent) >= 3:
            raise KeyboardInterrupt
        return original(action)

    robot.send_action = send
    with pytest.raises(KeyboardInterrupt):
        align_follower_to_leader(robot, leader)

    assert len(robot.sent) == 3
    # 목표 근처에도 가지 않은 채 멈췄다 — 마지막으로 명령한 그 자리다.
    assert robot.sent[-1]["shoulder_pan"] < 1.0


def test_the_scurve_peak_factor_is_why_the_limit_means_the_peak():
    """s-curve의 최고 속도는 평균의 1.5배다. 상한은 평균이 아니라 첨두에 걸려야 한다."""
    assert SCURVE_PEAK_FACTOR == 1.5
    assert TELEOP_ALIGNMENT.smooth_distance == pytest.approx(
        TELEOP_ALIGNMENT.max_seconds * TELEOP_ALIGNMENT.degrees_per_second / SCURVE_PEAK_FACTOR
    )
