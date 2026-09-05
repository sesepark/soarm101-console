from pathlib import Path

import pytest

from soarm_console.config import Settings, WIDEST_JOINT_SPAN
from soarm_console.calibration import validate_calibration
from soarm_console.diagnostics import EXPECTED_IDS, EXPECTED_MODEL
from soarm_console.teleop import TeleopManager
from soarm_console.recording import build_record_config
from soarm_console.record_manager import RecordManager
from soarm_console.vleader.spec import load_joint_specs


def test_motion_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SOARM_ENABLE_MOTION", raising=False)
    settings = Settings()
    assert settings.motion_enabled is False


def test_settings_read_environment_at_instantiation(monkeypatch):
    monkeypatch.setenv("SOARM_ENABLE_MOTION", "1")
    monkeypatch.setenv("SOARM_LEADER_ID", "dynamic_leader")
    settings = Settings()
    assert settings.motion_enabled is True
    assert settings.leader_id == "dynamic_leader"


def test_teleop_runs_our_own_module_not_the_lerobot_binary():
    """텔레옵 자식은 `scripts/teleoperate.sh`다.

    `lerobot-teleoperate` 바이너리로는 붙는 순간의 목표 동기화도 루프 앞의 자세 정렬도
    부탁할 수 없다. 그 둘이 없으면 시작할 때마다 팔이 뛴다.
    """
    command = TeleopManager(Settings()).command()
    assert len(command) == 1
    script = Path(command[0])
    assert script.name == "teleoperate.sh"
    assert script.exists()


def test_teleop_uses_stable_ports_and_safety_limit():
    """설정값은 이제 CLI 플래그가 아니라 `teleoperating`이 `Settings`에서 직접 읽는다."""
    from soarm_console import teleoperating

    settings = Settings(
        leader_port="/dev/serial/by-id/leader",
        follower_port="/dev/serial/by-id/follower",
    )
    robot = teleoperating.build_robot_config(settings)
    leader = teleoperating.build_teleop_config(settings)

    assert leader.port == "/dev/serial/by-id/leader"
    assert robot.port == "/dev/serial/by-id/follower"
    assert robot.max_relative_target == 2
    # 종료나 예외가 곧 torque-off가 되면 팔이 떨어진다.
    assert robot.disable_torque_on_disconnect is False
    # 수집과 같은 주기여야 "텔레옵으로 해 보고 그대로 찍는다"가 성립한다.
    assert teleoperating.FPS == 30


def test_teleop_preflight_looks_for_the_interpreter_it_actually_runs():
    problems = TeleopManager(Settings()).preflight()
    assert not any("lerobot-teleoperate" in problem for problem in problems)


def test_preflight_reports_closed_motion_gate():
    problems = TeleopManager(Settings(motion_enabled=False)).preflight()
    assert "SOARM_ENABLE_MOTION=1 is not set" in problems


def test_expected_so101_bus_identity():
    assert EXPECTED_IDS == {1, 2, 3, 4, 5, 6}
    assert EXPECTED_MODEL == 777


def test_record_config_is_local_and_uses_stable_camera_paths():
    settings = Settings(
        scene_camera="/dev/v4l/by-path/scene",
        wrist_camera="/dev/v4l/by-path/wrist",
    )
    config = build_record_config(settings, "Pick and place", "test_session")
    assert config.dataset.push_to_hub is False
    assert config.dataset.no_stamp is True
    assert config.dataset.fps == 30
    assert str(config.robot.cameras["scene"].index_or_path) == "/dev/v4l/by-path/scene"
    assert str(config.robot.cameras["wrist"].index_or_path) == "/dev/v4l/by-path/wrist"
    assert config.robot.cameras["scene"].fourcc == "MJPG"
    assert config.robot.disable_torque_on_disconnect is False


def test_record_manager_is_gated_before_calibration():
    problems = RecordManager(Settings(motion_enabled=False)).preflight()
    assert "SOARM_ENABLE_MOTION=1 is not set" in problems
    assert "SOARM_CAMERA_ROLES_CONFIRMED=1 is not set" in problems


def test_calibration_validator_rejects_missing_file(tmp_path: Path):
    assert validate_calibration(tmp_path / "missing.json").startswith("Missing calibration")


def test_widest_joint_span_still_covers_the_real_calibration():
    """`WIDEST_JOINT_SPAN`은 취향이 아니라 이 하드웨어를 설명하는 숫자다.

    상수가 실제 관절 폭보다 **작아지면** 안전 클램프가 조용히 꺼진다:
    `effective_max_relative_target`이 걸릴 수 있는 상한까지 `None`으로 바꿔 버리고,
    LeRobot은 목표를 자르지 않은 채 그대로 모터에 실어 버린다. 화면에도 로그에도
    아무 말이 남지 않으므로, 틀렸다는 것을 팔이 먼저 알게 된다.

    그래서 방향이 있는 시험이다. calibration을 다시 잡아 어느 관절이 이 상수보다
    넓어지면 여기서 먼저 깨져야 한다. 상수를 여기에 다시 적는 시험
    (`WIDEST_JOINT_SPAN == 360`)은 상수가 200이던 시절에도 통과했을 것이라
    이 버그를 잡지 못한다 — 대조할 상대는 상수가 아니라 calibration이다.

    실물 팔은 필요 없다. calibration 파일만 읽는다.
    """
    calibration = Settings().follower_calibration
    if not calibration.exists():
        # 실물과 무관한 CI에는 calibration이 없다. 없다고 빨간불을 켜면 이 시험은
        # 곧 무시당하고, 무시당하는 시험은 상수를 지키지 못한다.
        pytest.skip(f"No follower calibration on this machine: {calibration}")

    spans = {spec.name: spec.span for spec in load_joint_specs(calibration)}
    widest = max(spans.values())
    assert WIDEST_JOINT_SPAN >= widest, (
        f"관절 폭이 상수를 넘어섰다: {spans}. WIDEST_JOINT_SPAN을 {widest:g} 이상으로 올려야"
        " max_relative_target 상한이 다시 살아난다."
    )
