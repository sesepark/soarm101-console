"""URDF 정기구학과 자세 생성.

정기구학이 틀리면 외부 캘리브레이션이 통째로 틀린다 — 그런데 잔차는 작게 나온다.
팔이 "자기가 여기 있다"고 말한 값에 카메라를 맞추는 것이므로, 그 말이 일관되기만 하면
솔버는 만족한다. 그래서 여기서는 **일관성이 아니라 값 자체**를 본다.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from soarm_console.config import Settings
from soarm_console.perception import poses as poses_module
from soarm_console.perception.kinematics import (
    Chain,
    KinematicsError,
    invert,
    rotation_about,
    rotation_from_rpy,
    transform,
    urdf_angles,
)
from soarm_console.vleader.spec import load_joint_specs


def test_the_chain_is_the_five_arm_joints_in_order():
    chain = Chain.from_urdf()
    # `gripper`는 움직턱으로 갈라지는 가지라 손끝까지의 사슬에 없다. 있으면 집게를
    # 여닫을 때 그리퍼 프레임이 움직이는 것으로 계산된다.
    assert chain.movable_names == (
        "shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"
    )


def test_rpy_uses_the_urdf_order():
    # URDF의 rpy는 고정축 X→Y→Z이므로 행렬은 Rz·Ry·Rx다. 순서를 뒤집으면 이 URDF처럼
    # 관절 원점마다 ±90°가 들어 있는 팔에서 축이 통째로 바뀐다.
    roll, pitch, yaw = 0.3, -0.7, 1.1
    expected = (
        rotation_about(np.array([0.0, 0.0, 1.0]), yaw)
        @ rotation_about(np.array([0.0, 1.0, 0.0]), pitch)
        @ rotation_about(np.array([1.0, 0.0, 0.0]), roll)
    )
    assert np.allclose(rotation_from_rpy(roll, pitch, yaw), expected)


def test_the_home_pose_reaches_forward_and_pan_turns_it():
    chain = Chain.from_urdf()
    home = chain.tip_position({})
    # 관절이 모두 0이면 팔은 앞으로 곧게 뻗는다. 링크 길이의 합과 같은 자릿수여야 한다.
    assert home[1] == pytest.approx(0.0, abs=1e-3)
    assert 0.35 < home[0] < 0.45
    # 어깨 회전은 수평면 안에서만 돈다. 높이가 따라 움직이면 축을 잘못 읽은 것이다.
    #
    # 허용 오차가 나노미터가 아닌 이유. 이 URDF는 `rpy`에 π를 `3.14159`로 잘라 적어 두어서
    # 어깨 회전축이 수직에서 약 1e-6 rad 기울어 있다. 그래서 60° 돌리면 손끝이 0.5µm
    # 오르내린다. 실재하지만 5mm를 다루는 이 작업에서는 아무 뜻이 없는 양이다.
    turned = chain.tip_position({"shoulder_pan": 0.6})
    assert turned[2] == pytest.approx(home[2], abs=1e-5)
    # 회전의 중심은 베이스 원점이 아니라 **어깨 회전축**이다. URDF의 `shoulder_pan` 원점이
    # x=38.8mm에 있어서, 베이스 원점에서 잰 거리는 팬을 돌리면 6mm씩 달라진다. 처음에는
    # 원점 기준으로 재다가 이 시험이 실패해서 알았다 — 지어낸 값이 아니라 팔의 생김새다.
    axis_x = 0.0388353
    assert math.hypot(turned[0] - axis_x, turned[1]) == pytest.approx(
        math.hypot(home[0] - axis_x, home[1]), abs=1e-5
    )
    assert turned[1] < 0  # 양의 pan은 -Y 쪽으로 돈다


def test_unknown_joints_are_refused_rather_than_treated_as_zero():
    chain = Chain.from_urdf()
    # 오타 하나가 "그 관절은 0이었다"로 흡수되면, 몇 mm 어긋난 답이 잔차만 조금 키운 채
    # 그럴듯하게 나온다.
    with pytest.raises(KinematicsError):
        chain.forward({"shoulder_pann": 0.1})


def test_inverse_undoes_the_transform():
    pose = transform(rotation_from_rpy(0.2, -1.1, 2.0), np.array([0.1, -0.2, 0.3]))
    assert np.allclose(invert(pose) @ pose, np.eye(4), atol=1e-12)


def test_servo_units_become_urdf_radians_through_the_shared_spec():
    specs = load_joint_specs(Settings().follower_calibration)
    angles = urdf_angles(specs, {"shoulder_pan": 45.0, "gripper": 50.0})
    # 팔 관절은 도 → 라디안. 집게만 퍼센트라 다른 식을 탄다. 변환식을 perception 안에
    # 다시 쓰지 않고 `vleader/spec.py`를 부르는 것이 요점이다.
    assert angles["shoulder_pan"] == pytest.approx(math.radians(45.0))
    assert angles["gripper"] == pytest.approx(0.5 * 1.74533, rel=1e-6)


def test_generated_poses_stay_inside_the_limits_and_spread_their_rotations():
    settings = Settings()
    specs = load_joint_specs(settings.follower_calibration)
    chain = Chain.from_urdf()
    generated = poses_module.generate(specs, chain, 24, gripper=30.0)
    assert len(generated) == 24
    by_name = {spec.name: spec for spec in specs}
    for pose in generated:
        for name, value in pose.items():
            assert by_name[name].contains(value), f"{name}={value} is outside the calibration"
        # 집게는 시작할 때의 값 그대로여야 한다. 캘리브레이션 도중에 여닫으면 그리퍼에
        # 붙인 마커가 흔들릴 수 있고, 그러면 외부 파라미터가 통째로 틀린다.
        assert pose["gripper"] == 30.0
    # 회전이 넓게 퍼져야 손-눈 방정식이 축퇴하지 않는다. 이것이 자세를 20개 넘게 도는
    # 진짜 이유다.
    assert poses_module.rotation_spread(specs, chain, generated) > 90.0


def test_generated_poses_are_repeatable():
    settings = Settings()
    specs = load_joint_specs(settings.follower_calibration)
    chain = Chain.from_urdf()
    first = poses_module.generate(specs, chain, 12, gripper=0.0)
    second = poses_module.generate(specs, chain, 12, gripper=0.0)
    # 같은 씨앗이면 같은 자세다. 실패를 다시 재현할 수 있어야 하기 때문이다.
    assert first == second
