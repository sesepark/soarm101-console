from pathlib import Path

from soarm_console.config import Settings
from soarm_console.calibration import validate_calibration
from soarm_console.diagnostics import EXPECTED_IDS, EXPECTED_MODEL
from soarm_console.teleop import TeleopManager
from soarm_console.recording import build_record_config
from soarm_console.record_manager import RecordManager


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


def test_command_uses_stable_ports_and_safety_limit(tmp_path: Path):
    settings = Settings(
        leader_port="/dev/serial/by-id/leader",
        follower_port="/dev/serial/by-id/follower",
    )
    command = TeleopManager(settings).command()
    assert "--teleop.port=/dev/serial/by-id/leader" in command
    assert "--robot.port=/dev/serial/by-id/follower" in command
    assert "--robot.max_relative_target=2" in command
    assert "--fps=30" in command


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


def test_record_manager_is_gated_before_calibration():
    problems = RecordManager(Settings(motion_enabled=False)).preflight()
    assert "SOARM_ENABLE_MOTION=1 is not set" in problems
    assert "SOARM_CAMERA_ROLES_CONFIRMED=1 is not set" in problems


def test_calibration_validator_rejects_missing_file(tmp_path: Path):
    assert validate_calibration(tmp_path / "missing.json").startswith("Missing calibration")
