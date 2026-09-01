"""가상 리더의 안전 사다리와 권한 상태기계.

팔을 움직이지 않고 확인할 수 있는 것을 전부 여기서 확인한다. 백엔드는 흉내(`simulated`)이고,
실물에서만 알 수 있는 것(접촉 문턱의 실제 값, 3D의 회전 방향)은 RUNBOOK의 현장 절차로
남겨 두었다.
"""

from __future__ import annotations

import json
import time

import pytest

from soarm_console.vleader.authority import AuthorityManager, LeaseConflict
from soarm_console.vleader.backend import SimulatedFollower
from soarm_console.vleader.owner import State, VirtualLeaderOwner
from soarm_console.vleader.safety import (
    CommandValidator,
    Reject,
    RejectError,
    Trip,
    TripDetector,
    VLeaderSettings,
)
from soarm_console.vleader.spec import JointSpec, SpecError, load_joint_specs


CALIBRATION = {
    "shoulder_pan": {"id": 1, "drive_mode": 0, "homing_offset": -101, "range_min": 758, "range_max": 3447},
    "shoulder_lift": {"id": 2, "drive_mode": 0, "homing_offset": -1980, "range_min": 1360, "range_max": 3746},
    "elbow_flex": {"id": 3, "drive_mode": 0, "homing_offset": -645, "range_min": 996, "range_max": 3200},
    "wrist_flex": {"id": 4, "drive_mode": 0, "homing_offset": 773, "range_min": 577, "range_max": 2913},
    "wrist_roll": {"id": 5, "drive_mode": 0, "homing_offset": -141, "range_min": 0, "range_max": 4095},
    "gripper": {"id": 6, "drive_mode": 0, "homing_offset": -472, "range_min": 1656, "range_max": 3100},
}


@pytest.fixture
def calibration_file(tmp_path):
    path = tmp_path / "soarm101_follower.json"
    path.write_text(json.dumps(CALIBRATION), encoding="utf-8")
    return path


@pytest.fixture
def specs(calibration_file):
    return load_joint_specs(calibration_file)


@pytest.fixture
def settings():
    return VLeaderSettings()


# --------------------------------------------------------------- 관절 계약


def test_absolute_limits_come_from_calibration_not_from_a_guess(specs):
    """추정값을 절대 한계로 쓰지 않는다(SAFETY.md 불변조건 4).

    LeRobot의 도 단위 정규화는 `(raw - mid) * 360 / 4095`이므로 범위는 0을 가운데 둔
    대칭 구간이다. 집게만 0~100 퍼센트인데, `SOFollower`가 집게에만
    `MotorNormMode.RANGE_0_100`을 하드코딩해 두었기 때문이다.
    """
    by_name = {spec.name: spec for spec in specs}
    span = lambda motor: (CALIBRATION[motor]["range_max"] - CALIBRATION[motor]["range_min"]) / 2 * 360 / 4095
    for motor in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"):
        assert by_name[motor].unit == "deg"
        assert by_name[motor].maximum == pytest.approx(span(motor))
        assert by_name[motor].minimum == pytest.approx(-span(motor))
    assert by_name["gripper"].unit == "percent"
    assert (by_name["gripper"].minimum, by_name["gripper"].maximum) == (0.0, 100.0)


def test_calibration_that_does_not_describe_this_arm_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"shoulder_pan": CALIBRATION["shoulder_pan"]}), encoding="utf-8")
    with pytest.raises(SpecError):
        load_joint_specs(path)


def test_viewer_conversion_is_defined_by_the_server(specs):
    """3D의 각도 변환식은 서버가 준다. 클라이언트마다 다시 쓰면 두 기기가 어긋난다."""
    by_name = {spec.name: spec for spec in specs}
    assert by_name["shoulder_pan"].to_radians(90) == pytest.approx(1.5707963, abs=1e-6)
    # 집게는 퍼센트다. 100%가 URDF의 열린 자세(1.74533 rad)에 대응한다.
    assert by_name["gripper"].to_radians(100) == pytest.approx(1.74533, abs=1e-5)


# --------------------------------------------------------------- 명령 검사


@pytest.mark.parametrize(
    "payload, code",
    [
        ("not a dict", Reject.INVALID_SHAPE),
        ({}, Reject.INVALID_SHAPE),
        ({"elbow_flex": "1"}, Reject.INVALID_SHAPE),
        ({"elbow_flex": True}, Reject.INVALID_SHAPE),
        ({"no_such_joint": 1.0}, Reject.INVALID_SHAPE),
        ({"elbow_flex": float("nan")}, Reject.NON_FINITE_VALUE),
        ({"elbow_flex": float("inf")}, Reject.NON_FINITE_VALUE),
        ({"elbow_flex": 400.0}, Reject.OUTSIDE_ABSOLUTE_LIMIT),
        ({"gripper": -1.0}, Reject.OUTSIDE_ABSOLUTE_LIMIT),
        ({"gripper": 101.0}, Reject.OUTSIDE_ABSOLUTE_LIMIT),
    ],
)
def test_the_validator_refuses_by_reason(specs, settings, payload, code):
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    with pytest.raises(RejectError) as error:
        validator.validate(payload, present=present, needs_sync=False)
    assert error.value.code == code


def test_the_first_command_after_a_lease_must_start_from_where_the_arm_is(specs, settings):
    """이것이 없으면 리스를 잡는 순간 팔이 가상 리더의 기본 자세로 튄다."""
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    with pytest.raises(RejectError) as error:
        validator.validate({"elbow_flex": 45.0}, present=present, needs_sync=True)
    assert error.value.code == Reject.POSE_NOT_SYNCED
    # 현재 자세 근처면 통과한다.
    assert validator.validate({"elbow_flex": 3.0}, present=present, needs_sync=True)


def test_step_limit_clamps_instead_of_refusing(specs, settings):
    """빠르게 끌면 목표는 늘 멀리 있다. 그때마다 거절하면 조작 자체가 되지 않는다."""
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    goal, limited = validator.clamp_step({"elbow_flex": 90.0}, present)
    assert goal["elbow_flex"] == pytest.approx(settings.step_deg)
    assert limited == ["elbow_flex"]


def test_sending_faster_does_not_move_the_arm_faster(specs, settings):
    """상한이 '명령당'이면 초당 300번 보내는 클라이언트가 초당 600도를 움직인다."""
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    tenth_of_a_tick = 0.1
    goal, _ = validator.clamp_step({"elbow_flex": 90.0}, present, scale=tenth_of_a_tick)
    assert goal["elbow_flex"] == pytest.approx(settings.step_deg * tenth_of_a_tick)


# --------------------------------------------------------------- 권한


def test_only_one_follower_motion_lease_at_a_time(settings):
    authority = AuthorityManager(settings)
    mac = authority.grant("맥북", "session-mac")
    with pytest.raises(LeaseConflict) as conflict:
        authority.grant("아이폰", "session-phone")
    assert conflict.value.holder == "맥북"
    # 반납하면 다음 사람이 받는다. 빼앗기는 없다.
    assert authority.release(mac.lease_id)
    assert authority.grant("아이폰", "session-phone").holder == "아이폰"


def test_a_lease_expires_without_a_heartbeat(settings):
    authority = AuthorityManager(settings)
    now = time.monotonic()
    lease = authority.grant("맥북", "s", now=now)
    ttl = settings.lease_ttl_ms / 1000.0
    assert authority.active(now + ttl - 0.1) is not None
    assert authority.active(now + ttl + 0.1) is None
    # 만료된 뒤에는 그 lease_id로 아무것도 못 한다.
    with pytest.raises(RejectError) as error:
        authority.authorise(lease.lease_id, 1, now=now + ttl + 0.2)
    assert error.value.code == Reject.NO_ACTIVE_LEASE


def test_a_heartbeat_extends_the_lease(settings):
    authority = AuthorityManager(settings)
    now = time.monotonic()
    lease = authority.grant("맥북", "s", now=now)
    ttl = settings.lease_ttl_ms / 1000.0
    authority.renew(lease.lease_id, now=now + ttl - 0.5)
    assert authority.active(now + ttl + 0.2) is not None


def test_commands_from_the_wrong_holder_and_replayed_sequences_are_refused(settings):
    authority = AuthorityManager(settings)
    lease = authority.grant("맥북", "s")
    authority.authorise(lease.lease_id, 5)
    with pytest.raises(RejectError) as duplicate:
        authority.authorise(lease.lease_id, 5)
    assert duplicate.value.code == Reject.DUPLICATE_SEQUENCE
    with pytest.raises(RejectError) as older:
        authority.authorise(lease.lease_id, 3)
    assert older.value.code == Reject.DUPLICATE_SEQUENCE
    with pytest.raises(RejectError) as wrong:
        authority.authorise("someone-elses-lease", 6)
    assert wrong.value.code == Reject.WRONG_AUTHORITY


# --------------------------------------------------------------- 관측이 거는 정지


def test_a_single_spike_does_not_stop_the_arm(specs, settings):
    """한 번 튄 값에 팔이 서면, 진짜로 막혔을 때 사람이 그 경고를 믿지 않게 된다."""
    detector = TripDetector(specs, settings)
    high = {"elbow_flex": settings.load_trip + 50}
    quiet = {name: 0.0 for name in high}
    now = time.monotonic()
    assert detector.inspect(now=now, present={}, goal={}, load=high, current={}, temperature={}) is None
    assert detector.inspect(now=now + 0.05, present={}, goal={}, load=quiet, current={}, temperature={}) is None
    # 다시 올라가도 창은 처음부터 다시 센다.
    assert detector.inspect(now=now + 0.1, present={}, goal={}, load=high, current={}, temperature={}) is None


def test_a_sustained_load_trips(specs, settings):
    detector = TripDetector(specs, settings)
    high = {"elbow_flex": settings.load_trip + 50}
    now = time.monotonic()
    detector.inspect(now=now, present={}, goal={}, load=high, current={}, temperature={})
    trip = detector.inspect(
        now=now + settings.load_trip_ms / 1000.0 + 0.01,
        present={}, goal={}, load=high, current={}, temperature={},
    )
    assert trip is not None and trip[0] == Trip.OVERLOAD and trip[1] == "elbow_flex"


def test_a_joint_that_is_asked_to_move_but_stands_still_trips(specs, settings):
    """계속 요청하는데 팔이 제자리다 — 무언가에 닿았다는 뜻이다."""
    detector = TripDetector(specs, settings)
    present = {"elbow_flex": 10.0}
    requested = {"elbow_flex": 10.0 + settings.following_error_deg + 1}
    standing = {"elbow_flex": 0.0}
    now = time.monotonic()
    detector.inspect(now=now, present=present, goal=present, load={}, current={},
                     temperature={}, requested=requested, moved=standing)
    trip = detector.inspect(
        now=now + settings.following_error_ms / 1000.0 + 0.01,
        present=present, goal=present, load={}, current={}, temperature={},
        requested=requested, moved=standing,
    )
    assert trip is not None and trip[0] == Trip.FOLLOWING_ERROR
    assert "닿았습니다" in trip[2]


def test_pushing_against_a_hard_stop_trips_even_with_nowhere_left_to_ask(specs, settings):
    """기계적 끝단에 닿으면 벌어질 자리가 없어 추종오차로는 걸리지 않는다.

    2026-09-01 실물: 집게를 0%로 계속 보내는데 팔은 1.6%에 서 있었다. 남은 벌어짐이
    추종오차 문턱(2%)보다 작아 20초 동안 부하 120으로 밀고 있는데도 사다리 어느 칸에도
    걸리지 않았다. 남은 보호는 온도뿐이었고 그것은 분 단위로 느리다.
    """
    detector = TripDetector(specs, settings)
    present = {"gripper": 1.6}
    goal = {"gripper": 1.6 - settings.stall_epsilon * 2}
    standing = {"gripper": 0.0}
    load = {"gripper": settings.stall_load + 40}
    now = time.monotonic()
    detector.inspect(now=now, present=present, goal=goal, load=load, current={},
                     temperature={}, requested=goal, moved=standing)
    trip = detector.inspect(
        now=now + settings.stall_load_ms / 1000.0 + 0.01,
        present=present, goal=goal, load=load, current={}, temperature={},
        requested=goal, moved=standing,
    )
    assert trip is not None and trip[0] == Trip.STALLED
    assert "막혀" in trip[2]


def test_holding_a_pose_is_not_a_stall(specs, settings):
    """가만히 자세를 버티는 것과 막힌 것을 가르는 것은 **목표가 앞서 있는가**이다.

    버티기만 할 때 목표는 실제와 같은 자리에 있다. 이것을 함께 보지 않으면 무거운
    자세를 버티는 관절이 조작하지도 않았는데 계속 걸린다.
    """
    detector = TripDetector(specs, settings)
    present = {"shoulder_lift": 40.0}
    standing = {"shoulder_lift": 0.0}
    load = {"shoulder_lift": settings.stall_load + 60}
    now = time.monotonic()
    for step in range(6):
        assert detector.inspect(
            now=now + step * 0.2, present=present, goal=present, load=load, current={},
            temperature={}, requested=present, moved=standing,
        ) is None


def test_catching_up_is_not_a_collision(specs, settings):
    """빠르게 끌면 요청은 늘 앞서간다. 그때마다 서면 조작 자체가 되지 않는다.

    구별하는 것은 **팔이 움직이고 있는가**이다. 따라오는 중이면 움직이고, 닿았으면 서 있다.
    """
    detector = TripDetector(specs, settings)
    present = {"elbow_flex": 10.0}
    requested = {"elbow_flex": 60.0}
    catching_up = {"elbow_flex": settings.stall_epsilon * 5}
    now = time.monotonic()
    for step in range(6):
        assert detector.inspect(
            now=now + step * 0.2, present=present, goal=present, load={}, current={},
            temperature={}, requested=requested, moved=catching_up,
        ) is None


def test_the_gripper_gets_its_own_following_error_in_percent(specs, settings):
    """집게만 단위가 퍼센트다. 도(degree)로 잰 문턱을 그대로 쓰면 뜻이 달라진다."""
    gripper = next(spec for spec in specs if spec.name == "gripper")
    elbow = next(spec for spec in specs if spec.name == "elbow_flex")
    assert settings.following_error(gripper) == settings.following_error_percent
    assert settings.following_error(elbow) == settings.following_error_deg


def test_temperature_stops_before_the_servo_cuts_its_own_torque(specs, settings):
    """STS3215는 70°C에서 스스로 토크를 끊는다 — 그러면 팔이 떨어진다."""
    detector = TripDetector(specs, settings)
    assert settings.temperature_trip_c < 70
    hot = {"wrist_flex": float(settings.temperature_trip_c)}
    now = time.monotonic()
    detector.inspect(now=now, present={}, goal={}, load={}, current={}, temperature=hot)
    trip = detector.inspect(
        now=now + settings.temperature_trip_ms / 1000.0 + 0.01,
        present={}, goal={}, load={}, current={}, temperature=hot,
    )
    assert trip is not None and trip[0] == Trip.OVER_TEMPERATURE


def test_one_corrupted_temperature_reading_does_not_stop_the_arm(specs, settings):
    """실물에서 45°C로 안정된 집게가 한 번 89°C로 읽혀 팔이 섰다.

    튀지 않는 것은 온도이지 판독값이 아니다. Feetech 버스는 여러 관절이 함께 움직일 때
    상태 패킷이 깨지고, 그때 값은 그럴듯한 숫자로 들어온다.
    """
    detector = TripDetector(specs, settings)
    now = time.monotonic()
    calm = {"gripper": 46.0}
    spike = {"gripper": 89.0}
    assert detector.inspect(now=now, present={}, goal={}, load={}, current={}, temperature=calm) is None
    assert detector.inspect(now=now + 0.1, present={}, goal={}, load={}, current={}, temperature=spike) is None
    # 다음 판독이 정상으로 돌아오면 아무 일도 없었던 것이 된다.
    assert detector.inspect(now=now + 0.2, present={}, goal={}, load={}, current={}, temperature=calm) is None
    assert detector.inspect(now=now + 1.0, present={}, goal={}, load={}, current={}, temperature=calm) is None
    # 경고도 한 번 튄 값으로는 뜨지 않는다.
    assert detector.warnings(spike) == []


# --------------------------------------------------------------- 제어 루프


@pytest.fixture
def owner(specs, settings, monkeypatch):
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")
    authority = AuthorityManager(settings)
    instance = VirtualLeaderOwner(
        specs=specs, settings=settings, port="/dev/null", robot_id="test", authority=authority
    )
    instance.start()
    yield instance
    instance.stop(force=True)


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def drive(owner, lease, joints, sequence):
    return owner.submit(
        payload=joints, lease_id=lease.lease_id, sequence=sequence, valid_for_ms=300, observation=None
    )


def test_the_loop_starts_observing_with_torque_off(owner):
    assert wait_for(lambda: owner.snapshot()["observation"] > 2)
    snapshot = owner.snapshot()
    assert snapshot["state"] == State.SAFE
    assert snapshot["torque_enabled"] is False


def test_a_goal_is_refused_until_torque_is_on(owner):
    lease = owner.authority.grant("테스트", "s")
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    with pytest.raises(RejectError) as error:
        drive(owner, lease, {"elbow_flex": 0.0}, 1)
    assert error.value.code == Reject.HARDWARE_NOT_READY


def test_silence_puts_the_arm_on_hold_rather_than_repeating_the_last_command(owner):
    """SAFETY.md 불변조건 6 — 유효기간 없는 마지막 명령을 무기한 반복하지 않는다."""
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    lease = owner.authority.grant("테스트", "s")
    drive(owner, lease, {"elbow_flex": 1.0}, 1)
    assert owner.snapshot()["state"] == State.ACTIVE
    assert wait_for(lambda: owner.snapshot()["state"] == State.HOLD, timeout=2.0)
    assert owner.snapshot()["fault"]["code"] == Trip.COMMAND_TIMEOUT


def test_hold_needs_a_person_before_it_accepts_motion_again(owner):
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    lease = owner.authority.grant("테스트", "s")
    drive(owner, lease, {"elbow_flex": 1.0}, 1)
    owner.hold()
    with pytest.raises(RejectError) as error:
        drive(owner, lease, {"elbow_flex": 1.2}, 2)
    assert error.value.code == Reject.NOT_ACCEPTING_MOTION
    owner.resume()
    # 다시 시작할 때는 현재 자세에서 출발해야 한다(불변조건 7).
    present = {joint["name"]: joint["present"] for joint in owner.snapshot()["joints"]}
    with pytest.raises(RejectError) as far:
        drive(owner, lease, {"elbow_flex": present["elbow_flex"] + 60}, 3)
    assert far.value.code == Reject.POSE_NOT_SYNCED
    assert drive(owner, lease, {"elbow_flex": present["elbow_flex"]}, 4)


def test_losing_the_lease_holds_the_arm_where_it_is(owner):
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    lease = owner.authority.grant("테스트", "s")
    drive(owner, lease, {"elbow_flex": 1.0}, 1)
    owner.authority.release(lease.lease_id)
    assert wait_for(lambda: owner.snapshot()["state"] == State.HOLD, timeout=2.0)
    assert owner.snapshot()["torque_enabled"] is True  # 떨어뜨리지 않는다


def test_the_loop_never_turns_torque_off_by_itself(owner):
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    owner.hold(Trip.HARDWARE_ERROR, None, "흉내 낸 고장")
    assert owner.snapshot()["torque_enabled"] is True
    # 내리는 것도 막는다. 토크가 걸린 채로 조용히 사라지면 팔을 붙잡을 것이 없어진다.
    with pytest.raises(Exception):
        owner.stop()


def test_backing_off_always_ends(specs, monkeypatch):
    """물러남에는 끝이 있어야 한다.

    물러남은 목표에 닿을 때까지 이어지는데, 닿지 못하는 자리가 있다. 걸린 방향의 반대편에도
    무언가가 있으면(책상 위에서 위로 밀다 걸리면 아래는 책상이다) 팔은 물러날 곳이 없다.
    2026-09-01 실물에서 `shoulder_lift`가 그 상태로 53초 넘게 부하 100으로 밀고 서 있는
    것을 봤다 — 물러나는 동안에는 관측 정지도 보지 않으므로 아무것도 그것을 끊지 못했다.

    여기서는 물러날 거리를 크게, 시간을 짧게 주어 같은 상황을 만든다. 시간이 다 되면
    지금 자리에 세워야 한다. 세우는 것은 언제나 할 수 있다.
    """
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")
    monkeypatch.setenv("SOARM_VL_SIM_OBSTACLE", "elbow_flex:5")
    monkeypatch.setenv("SOARM_VL_RETREAT_DEG", "60")
    monkeypatch.setenv("SOARM_VL_RETREAT_MS", "300")
    settings = VLeaderSettings()
    authority = AuthorityManager(settings)
    owner = VirtualLeaderOwner(
        specs=specs, settings=settings, port="/dev/null", robot_id="test", authority=authority
    )
    owner.start()
    try:
        wait_for(lambda: owner.snapshot()["observation"] > 2)
        owner.arm()
        lease = authority.grant("테스트", "s")
        sequence = 0
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and owner.state not in (State.HOLD, State.RETREATING):
            sequence += 1
            present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
            try:
                drive(owner, lease, {"elbow_flex": present["elbow_flex"] + 3.0}, sequence)
            except RejectError:
                break
            time.sleep(0.03)
        # 60°를 물러나려면 틱당 상한 때문에 1초가 넘게 걸린다. 300ms 뒤에는 그 자리에 선다.
        assert wait_for(lambda: owner.snapshot()["state"] == State.HOLD, timeout=3.0)
        assert "빠져나오지 못해" in owner.snapshot()["fault"]["message"]
    finally:
        owner.stop(force=True)


def test_a_blocked_joint_backs_off_and_then_holds(specs, settings, monkeypatch):
    """책상에 닿았을 때. 물러난 뒤 서고, 어느 관절이 왜 걸렸는지 남는다."""
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")
    monkeypatch.setenv("SOARM_VL_SIM_OBSTACLE", "elbow_flex:5")
    authority = AuthorityManager(settings)
    owner = VirtualLeaderOwner(
        specs=specs, settings=settings, port="/dev/null", robot_id="test", authority=authority
    )
    owner.start()
    try:
        wait_for(lambda: owner.snapshot()["observation"] > 2)
        owner.arm()
        lease = authority.grant("테스트", "s")
        sequence = 0
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if owner.state in (State.HOLD, State.RETREATING):
                break
            sequence += 1
            # 조작하는 사람이 하는 것과 같은 모양으로 민다. 현재 자세에서 조금씩 —
            # 리스를 잡은 직후 멀리 있는 목표는 자세 미동기로 거절되는 것이 맞다.
            present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
            try:
                drive(owner, lease, {"elbow_flex": present["elbow_flex"] + 3.0}, sequence)
            except RejectError:
                break
            time.sleep(0.03)
        assert wait_for(lambda: owner.snapshot()["state"] == State.HOLD, timeout=3.0)
        fault = owner.snapshot()["fault"]
        assert fault["joint"] == "elbow_flex"
        assert fault["code"] in (Trip.OVERLOAD, Trip.OVERCURRENT, Trip.FOLLOWING_ERROR, Trip.STALLED)
        present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
        # 걸린 자리보다 뒤로 물러나 있어야 한다.
        assert present["elbow_flex"] < 5.0
    finally:
        owner.stop(force=True)
