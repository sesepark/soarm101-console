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


def test_the_lead_limit_clamps_instead_of_refusing(specs, settings):
    """빠르게 끌면 목표는 늘 멀리 있다. 그때마다 거절하면 조작 자체가 되지 않는다."""
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    goal, limited = validator.clamp_lead({"elbow_flex": 90.0}, present)
    assert goal["elbow_flex"] == pytest.approx(settings.lead_deg)
    assert limited == ["elbow_flex"]


def test_the_lead_limit_does_not_depend_on_how_often_commands_arrive(specs, settings):
    """상한이 시간에 비례하던 시절에는, 느린 연결에서 팔이 느려졌다.

    속도를 서보가 지키게 되면서 그 보정이 필요 없어졌다. 10Hz로 보내든 30Hz로 보내든
    한 번에 앞세울 수 있는 거리는 같고, 실제 속도는 `Goal_Velocity`가 정한다.
    """
    validator = CommandValidator(specs, settings)
    present = {spec.name: 0.0 for spec in specs}
    first, _ = validator.clamp_lead({"elbow_flex": 90.0}, present)
    second, _ = validator.clamp_lead({"elbow_flex": 90.0}, present)
    assert first == second


def test_the_speed_limit_is_expressed_in_servo_ticks_per_second(specs, settings):
    """`Goal_Velocity` 한 칸은 초당 위치 눈금 하나다 (2026-09-02 집게로 실측).

    200 → 21°/s, 500 → 47°/s, 1000 → 93°/s. 즉 눈금 하나가 360/4096도이므로
    N칸 ≈ N × 0.0879 °/s이고, 뒤집으면 °/s ÷ 0.0879 = 칸이다.
    """
    from dataclasses import replace

    arm = next(spec for spec in specs if spec.name == "elbow_flex")
    gripper = next(spec for spec in specs if spec.name == "gripper")
    tuned = replace(settings, max_deg_per_s=90.0, max_percent_per_s=100.0)
    assert tuned.ticks_per_second(arm) == pytest.approx(90.0 / (360.0 / 4096.0), rel=0.01)
    # 집게는 퍼센트다. 0~100%가 calibration 범위(1656~3100)에 펴지므로 100%/s는
    # 1444눈금/s(약 127°/s)이고, 같은 숫자라도 팔 관절과 다른 속도를 뜻한다.
    assert tuned.ticks_per_second(gripper) == 1444
    # 서보가 낼 수 없는 값을 써 넣지 않는다. 0은 "제한 없음"이라 최소 1로 올린다.
    assert replace(settings, max_deg_per_s=0.0).ticks_per_second(arm) == 1
    assert replace(settings, max_deg_per_s=9999.0).ticks_per_second(arm) == 4000


def test_a_profile_moves_speed_and_force_together(specs):
    """세 값을 따로 고르면 어긋난다 — 빠르면서 예민하면 정상 조작 중에 자꾸 선다."""
    from dataclasses import replace

    from soarm_console.vleader.safety import PROFILES, profile_of

    base = VLeaderSettings()
    for name, values in PROFILES.items():
        tuned = replace(base, **values)
        assert profile_of(tuned) == name
    gentle = replace(base, **PROFILES["gentle"])
    quick = replace(base, **PROFILES["quick"])
    assert gentle.max_deg_per_s < quick.max_deg_per_s
    assert gentle.lead_deg < quick.lead_deg
    # 예민함도 함께 움직인다. 빠른 쪽이 더 오래 참는다.
    assert gentle.following_error_ms < quick.following_error_ms


def test_a_hand_written_env_value_wins_over_the_profile(monkeypatch):
    """프로필은 고르는 것이고 env는 재 본 뒤 못 박는 것이다."""
    from soarm_console.vleader.safety import PROFILES, load_settings

    monkeypatch.setenv("SOARM_VL_PROFILE", "quick")
    monkeypatch.delenv("SOARM_VL_LEAD_DEG", raising=False)
    assert load_settings().lead_deg == PROFILES["quick"]["lead_deg"]
    monkeypatch.setenv("SOARM_VL_LEAD_DEG", "7.5")
    assert load_settings().lead_deg == pytest.approx(7.5)
    # 프로필이 정하는 나머지 값은 그대로 온다.
    assert load_settings().max_deg_per_s == PROFILES["quick"]["max_deg_per_s"]


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
    assert wait_for(lambda: owner.snapshot()["state"] == State.HOLD, timeout=4.0)
    assert owner.snapshot()["fault"]["code"] == Trip.COMMAND_TIMEOUT


def test_a_short_gap_stops_the_arm_without_asking_the_person_anything(owner):
    """서는 것과 사람에게 확인을 요구하는 것은 다른 일이다.

    무선 구간이 한 번 흔들려 300ms가 비면 팔은 곧바로 서야 한다. 그러나 그때마다
    `확인하고 계속`을 눌러야 하면 폰으로는 조작이 되지 않는다 — 사용자가 "왜 안 되지"
    하던 자리 가운데 하나가 이것이었다. 짧은 침묵에는 목표만 지금 자리에 붙이고,
    명령이 다시 오면 그대로 이어 간다.
    """
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    lease = owner.authority.grant("테스트", "s")
    present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
    drive(owner, lease, {"elbow_flex": present["elbow_flex"] + 4.0}, 1)
    # 워치독의 첫 단계가 지나갈 만큼만 기다린다. HOLD 문턱보다는 한참 짧다.
    time.sleep((owner.settings.command_timeout_ms + 150) / 1000.0)
    snapshot = owner.snapshot()
    assert snapshot["state"] == State.ACTIVE, "짧은 침묵으로 HOLD에 떨어지지 않는다"
    assert snapshot["command_stalled"] is True, "선 것은 화면이 알 수 있어야 한다"
    # 목표가 지금 자리에 붙어 있으므로 팔은 더 가지 않는다.
    stood = {j["name"]: j["goal"] for j in snapshot["joints"]}
    assert abs(stood["elbow_flex"] - snapshot["joints"][2]["present"]) < 0.6
    # 그리고 확인 없이 그대로 이어진다.
    present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
    assert drive(owner, lease, {"elbow_flex": present["elbow_flex"] + 4.0}, 2)
    assert owner.snapshot()["command_stalled"] is False


def test_the_goal_stays_the_absolute_pose_the_operator_asked_for(owner):
    """가상 리더가 말하는 것은 절대 자세다. 서버가 그것을 증분으로 바꾸지 않는다.

    예전에는 여기서 목표를 `present + step`으로 잘라 두었다. 그 순간 절대 목표가
    사라지고, 다음 명령이 오지 않으면 팔은 목표에 닿지 못한 채 선다. 한 프레임이 밀려도
    팔이 스스로 수렴한다는 것이 이 구조의 요점이므로, 목표는 사람이 말한 자리 그대로
    남아 있어야 한다.
    """
    wait_for(lambda: owner.snapshot()["observation"] > 2)
    owner.arm()
    lease = owner.authority.grant("테스트", "s")
    present = {j["name"]: j["present"] for j in owner.snapshot()["joints"]}
    far = present["elbow_flex"] + owner.settings.sync_tolerance_deg * 0.5
    answer = drive(owner, lease, {"elbow_flex": far}, 1)
    assert answer["goal"]["elbow_flex"] == pytest.approx(far, abs=0.01)
    # 명령을 한 번만 보내도 팔은 목표까지 간다 — 다음 명령을 기다리지 않는다.
    assert wait_for(
        lambda: abs(
            next(j for j in owner.snapshot()["joints"] if j["name"] == "elbow_flex")["present"] - far
        )
        < 0.6,
        timeout=2.0,
    )


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


# --------------------------------------------------------------- 루프는 죽지 않는다


def test_a_corrupted_packet_while_arming_does_not_kill_the_control_loop(specs, monkeypatch):
    """예외 하나가 팔을 통째로 잠그던 일.

    2026-09-02, 토크를 거는 중에 Feetech 상태 패킷 하나가 깨져 LeRobot이
    `ConnectionError`를 올렸다. 그 종류는 제어 루프의 `except HardwareError`에 걸리지
    않아 스레드를 통째로 죽였다. 죽은 루프는 serial과 owner lock을 쥔 채 남았고, 화면은
    `Virtual leader is not running`만 말했다. 다시 시작하려 해도 자기 자신이 쥔 lock에
    막혔다 — 서비스를 재시작하는 것 말고는 빠져나올 길이 없었다.

    이제 루프는 그 예외를 잡아 `FAULT`로 적고 **계속 돈다.** 팔을 다시 잡을 수 있다.
    """
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")
    settings = VLeaderSettings()
    authority = AuthorityManager(settings)
    owner = VirtualLeaderOwner(
        specs=specs, settings=settings, port="/dev/null", robot_id="test", authority=authority
    )
    owner.start()
    try:
        assert wait_for(lambda: owner.snapshot()["observation"] > 2)
        backend = owner._backend
        calls = {"n": 0}
        real = backend.set_torque

        def flaky(enabled):
            calls["n"] += 1
            if calls["n"] == 1:
                # LeRobot이 올리는 것과 같은 종류. `HardwareError`가 아니다.
                raise ConnectionError("Failed to write 'Lock' on id_=1 ... There is no status packet!")
            return real(enabled)

        backend.set_torque = flaky
        with pytest.raises(Exception):
            owner.arm()
        # 루프는 살아 있다. 이것이 이 시험의 전부다.
        assert owner.running, "예외 하나로 제어 루프가 죽으면 안 된다"
        assert owner.snapshot()["state"] == State.FAULT
        # 이유는 `fault`에 남는다. `error`는 다음 성공한 읽기가 지우는 자리라 —
        # "지금 버스가 이상하다"는 뜻이지 "무엇 때문에 섰다"는 뜻이 아니다.
        assert "status packet" in owner.snapshot()["fault"]["message"]
        # 그리고 사람이 다시 잡을 수 있다.
        owner.resume()
        owner.arm()
        assert owner.snapshot()["torque_enabled"] is True
    finally:
        owner.stop(force=True)


def test_the_loop_lets_go_of_the_device_if_it_ever_does_die(specs, monkeypatch):
    """그래도 루프가 끝나면, 장치를 쥔 채 사라지지 않는다.

    쥔 채 사라지면 다음 시작이 **자기 자신이 쥔 lock**에 막힌다. 실제로 그 상태를
    만들었고, 거절 문구에 적힌 pid가 콘솔 자신이었다.
    """
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")
    settings = VLeaderSettings()
    authority = AuthorityManager(settings)
    owner = VirtualLeaderOwner(
        specs=specs, settings=settings, port="/dev/null", robot_id="test", authority=authority
    )
    owner.start()
    assert wait_for(lambda: owner.snapshot()["observation"] > 2)
    # 루프 안에서 아무도 잡지 않는 예외를 낸다.
    def explode(*args, **kwargs):
        raise BaseException("루프를 뚫고 나가는 것")

    owner.validator.clamp_lead = explode
    owner._backend.read = explode
    assert wait_for(lambda: not owner.running, timeout=3.0)
    # 장치를 놓았는가. 놓았으면 참조가 비어 있다.
    assert owner._backend is None
    assert owner._owner_locks is None
