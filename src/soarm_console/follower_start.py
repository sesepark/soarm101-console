"""팔로워를 켜는 자리에서 팔이 튀지 않게 하는 것들.

세 경로가 팔로워에 붙는다 — 재생(`replaying`), 수집(`recording`), 물리 리더 텔레옵
(`teleoperating`). 세 경로 모두 붙는 순간과 루프의 첫 틱에서 팔이 뛸 수 있고, 이유는
서로 다르다. 이 모듈은 그 둘을 각각 한 번씩만 적어 둔 자리다.

**붙는 순간**: STS3215는 `Torque_Enable=1`을 쓰는 순간 서보에 남아 있는 옛
`Goal_Position`을 향해 최고 속도로 간다. 현재 자세를 목표로 삼는 것은 그 레지스터에 값
128을 쓸 때뿐이고, LeRobot은 그렇게 쓰지 않는다. 그래서 토크가 켜지기 전에 목표를 지금
자리로 옮겨 두어야 한다(`sync_goal_to_present`).

**첫 틱**: LeRobot의 텔레옵 루프는 첫 틱에 리더의 자세를 그대로 팔로워에 보낸다. 두 팔이
다른 자세로 서 있으면 팔로워는 그 차이만큼 한 번에 뛴다 —
`SOARM_MAX_RELATIVE_TARGET=1000`이라 잘리지도 않는다. 그래서 루프에 들어가기 전에
팔로워를 리더의 **지금** 자세까지 s-curve로 걸어간다(`align_follower_to_leader`).
"""

from __future__ import annotations

import time
from typing import Callable

from .replaying import ALIGN_HZ, AlignmentLimits, alignment_frames, alignment_seconds


#: 시작 정렬이 지키는 값. 재생(`replaying.REPLAY_ALIGNMENT`)보다 빠르다.
#:
#: 재생은 사람이 손을 대지 않는 자동 동작이라 느린 편이 낫지만, 이 정렬은 사람이 리더를
#: 쥐고 팔로워를 보고 있는 동안 일어난다. 그리고 이 시간은 매 세션 앞에 붙는 대기 시간이다
#: — 20°/s로 걸으면 두 팔이 반대편에 있을 때 시작이 십몇 초씩 밀린다.
TELEOP_ALIGNMENT = AlignmentLimits(
    degrees_per_second=40.0,
    percent_per_second=50.0,
    min_seconds=1.0,
    max_seconds=6.0,
)


def sync_goal_to_present(bus) -> None:
    """서보가 겨냥하는 곳을 지금 서 있는 자리로 옮긴다. **토크를 켜기 전에** 부른다.

    `normalize=False`로 읽고 그대로 쓴다. 두 값이 같은 단위여야 한다는 것 말고는 이
    함수가 단위에 대해 아는 것이 없어야 하고, raw로 오가면 calibration이 아직 모터에
    적히지 않은 순간에도 뜻이 흔들리지 않는다.

    토크가 꺼진 상태에서도 `Goal_Position`은 쓸 수 있다. 써 두면 토크가 걸리는 순간
    서보가 겨냥하는 곳이 지금 있는 자리가 되어, 팔은 제자리에서 뻣뻣해진다.
    """
    present = bus.sync_read("Present_Position", normalize=False, num_retry=2)
    bus.sync_write("Goal_Position", present, normalize=False, num_retry=2)


def install_safe_follower_start() -> None:
    """LeRobot의 `SOFollower`를 tty 없는 자식 프로세스에서 쓸 수 있게 고친다.

    두 가지를 클래스 속성 교체로 바꾼다. 자식 프로세스가 `lerobot-record`의 `record()`
    안쪽에서 로봇을 만들기 때문에 생성 지점에 손을 댈 수 없다 — `record_loop`를 이미
    같은 방식으로 감싸고 있다.

    1. `configure()` 앞에서 `sync_goal_to_present`. `configure()` 안의
       `torque_disabled()`가 토크를 잠깐 껐다가 **다시 켜므로**, 동기화가 설 자리는 그
       앞이다.
    2. `calibrate()`를 `input()` 없이. 모터에 적힌 calibration이 파일과 다르면
       LeRobot은 사람에게 물어보는데, record/teleop 자식에는 tty가 없어 그 자리가 곧
       `EOFError`다. 사람이 ENTER를 눌렀을 때 일어나는 일 — 파일의 값을 모터에 쓰는 것 —
       을 그대로 한다.
    """
    from lerobot.robots.so_follower import SOFollower

    if getattr(SOFollower, "_soarm_safe_start", False):
        return

    original_configure = SOFollower.configure

    def configure(self) -> None:
        sync_goal_to_present(self.bus)
        original_configure(self)

    def calibrate(self) -> None:
        if not self.calibration:
            raise RuntimeError(
                "Follower calibration file is missing; run scripts/calibrate_follower.sh"
            )
        self.bus.write_calibration(self.calibration)

    SOFollower.configure = configure
    SOFollower.calibrate = calibrate
    SOFollower._soarm_safe_start = True


def install_safe_leader_start() -> None:
    """리더에도 같은 이유로 `input()` 없는 `calibrate()`를 놓는다.

    리더의 `configure()`는 토크를 **끄기만** 하므로 목표 동기화는 필요 없다. 물어보는
    자리만 없애면 된다.
    """
    from lerobot.teleoperators.so_leader import SOLeader

    if getattr(SOLeader, "_soarm_safe_start", False):
        return

    def calibrate(self) -> None:
        if not self.calibration:
            raise RuntimeError("Leader calibration file is missing; run scripts/calibrate_leader.sh")
        self.bus.write_calibration(self.calibration)

    SOLeader.calibrate = calibrate
    SOLeader._soarm_safe_start = True


def leader_pose(teleop) -> dict[str, float]:
    """리더가 **지금** 잡고 있는 자세. 모터 이름으로(`.pos`를 뗀다)."""
    return {
        str(name).removesuffix(".pos"): float(value)
        for name, value in teleop.get_action().items()
        if str(name).endswith(".pos")
    }


def follower_pose(robot) -> dict[str, float]:
    """팔로워가 지금 서 있는 자세. 정규화된 값 — 데이터셋과 같은 단위다."""
    return {
        str(name): float(value)
        for name, value in robot.bus.sync_read("Present_Position", num_retry=2).items()
    }


def align_follower_to_leader(
    robot,
    teleop,
    *,
    hz: float = ALIGN_HZ,
    publish: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """팔로워를 리더의 지금 자세까지 걸어서 데려간다. 루프에 들어가기 **전에** 부른다.

    양 끝이 이미 관절 각도이므로 풀 것이 없다 — 두 자세 사이를 s-curve로 보간해 한 틱에
    하나씩 명령한다. 재생의 정렬과 같은 함수를 쓰고 상한만 다르다(`TELEOP_ALIGNMENT`).

    SIGINT가 오면 여기서 `KeyboardInterrupt`가 올라간다. 잡지 않는다 — 부르는 쪽은 팔을
    놓지 않는 `disable_torque_on_disconnect=False`로 붙어 있으므로, 팔은 마지막으로
    명령받은 자리에 선 채 힘을 유지한다. 멈추는 것과 힘을 놓는 것은 다른 일이다.
    """
    from lerobot.utils.robot_utils import precise_sleep

    start = follower_pose(robot)
    goal = leader_pose(teleop)
    seconds = alignment_seconds(start, goal, TELEOP_ALIGNMENT)
    frames = alignment_frames(start, goal, hz, TELEOP_ALIGNMENT)
    if log is not None:
        far = max(
            ((name, abs(goal[name] - start[name])) for name in goal),
            key=lambda item: item[1],
            default=("", 0.0),
        )
        log(
            f"Walking the follower to the leader's pose over {seconds:.1f}s "
            f"(furthest joint {far[0]} {far[1]:.1f})"
        )

    period = 1.0 / hz
    total = len(frames)
    if publish is not None:
        publish(round(total * period, 2))
    published = 0.0
    for index, frame in enumerate(frames, start=1):
        tick = time.perf_counter()
        robot.send_action({f"{name}.pos": value for name, value in frame.items()})
        now = time.perf_counter()
        if publish is not None and now - published >= 0.1:
            published = now
            publish(round((total - index) * period, 2))
        precise_sleep(max(period - (time.perf_counter() - tick), 0.0))
    if publish is not None:
        publish(0.0)
