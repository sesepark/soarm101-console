from pathlib import Path

import pytest
from fastapi import HTTPException

from soarm_console.app import (
    MotionRequest,
    RecordRequest,
    camera_stream,
    start_recording,
    start_teleoperation,
    status,
)


def test_status_is_observation_only_and_exposes_all_subsystems():
    payload = status()
    assert payload["motion_enabled"] is False
    assert set(payload["devices"]) == {"leader", "follower", "scene_camera", "wrist_camera"}
    assert "teleoperation" in payload
    assert "recording" in payload
    assert "doctor" in payload


def test_motion_endpoints_require_exact_confirmation_before_hardware_access():
    with pytest.raises(HTTPException) as teleop_error:
        start_teleoperation(MotionRequest(confirmation="wrong"))
    with pytest.raises(HTTPException) as record_error:
        start_recording(
            RecordRequest(
                confirmation="wrong",
                task="Pick and place",
                episodes=2,
                episode_seconds=10,
            )
        )
    assert teleop_error.value.status_code == 400
    assert record_error.value.status_code == 400


def test_unknown_camera_is_rejected():
    with pytest.raises(HTTPException) as error:
        camera_stream("unknown")
    assert error.value.status_code == 404


def test_ui_is_fixed_console_with_capsule_views_and_reference_design_tokens():
    static = Path(__file__).parents[1] / "src/soarm_console/static"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    for marker in ("환경 진단", "Teleoperation", "Dataset recording", "성공 저장"):
        assert marker in html
    for view in ('data-view="observe"', 'data-view="teleop"', 'data-view="dataset"'):
        assert view in html
    assert "--blue:#2855f3" in css
    assert "html,body{width:100%;height:100%;overflow:hidden}" in css
    assert "border-radius:999px" in css
    assert "function switchView" in js
    assert 'id="device-status"' not in html
    assert "grid-template-rows:auto minmax(0,1fr)" in css
    assert "log.scrollTop = log.scrollHeight" in js
