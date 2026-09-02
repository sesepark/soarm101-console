import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from soarm_console.app import (
    MotionRequest,
    RecordRequest,
    camera_stream,
    mobile,
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


def test_the_phone_url_leads_to_the_one_screen_that_can_do_everything():
    """폰 화면이 둘이면 영상을 보다가 조작하려고 주소를 옮겨 가야 한다.

    예전에는 `/mobile`이 카메라만 보여 주는 별도 화면이었고 조작은 `/viewer/`에 있었다.
    두 화면은 생김새도 달라서 옮겨 갈 때마다 다시 배워야 했다. 지금은 조작 화면의
    `카메라` 칸이 같은 일을 하므로, 이 주소는 그리로 보내기만 한다 — 폰에 남아 있는
    옛 북마크와 홈 화면 아이콘이 빈 자리로 가지 않게 하는 길이다.
    """
    response = mobile()
    assert response.status_code == 307
    assert response.headers["location"] == "/viewer/?host=web"


def test_the_server_address_leads_to_the_screen_that_can_drive_the_arm():
    """서버 주소를 그냥 열었을 때 조작할 수 있는 화면에 닿아야 한다.

    `/`는 3열 데스크톱 콘솔이고 **3D 조작이 아예 없다.** 그런데 폰에서 서버 주소를 열면
    거기로 온다 — 사용자가 실제로 그 화면에 도착해서 "3D 조작이 구현이 안 되어 있는데?"
    라고 물었다. 조작 화면이 있다는 것을 그 화면이 말해 주지 않았으니 맞는 말이었다.

    좁은 화면은 조작 화면으로 보내고, 데스크톱에는 링크를 둔다. 사람이 친 주소를 말없이
    바꾸는 것은 데스크톱에서는 놀라운 일이라 그쪽은 링크로 남긴다.
    """
    static = Path(__file__).parents[1] / "src/soarm_console/static"
    console = (static / "index.html").read_text(encoding="utf-8")
    # 폰은 보내고, `?console=1`로 남을 수 있다.
    assert "location.replace('/viewer/?host=web')" in console
    assert "console" in console and "pointer: coarse" in console
    # 데스크톱에는 눈에 보이는 길.
    assert 'href="/viewer/?host=web"' in console
    # 그리고 돌아오는 길.
    viewer = (static / "viewer/index.html").read_text(encoding="utf-8")
    assert 'href="/?console=1"' in viewer


def test_the_phone_screen_fits_one_view_and_can_be_installed():
    """폰에서 조작할 때 필요한 것이 한 화면에 있어야 한다.

    스크롤로 내려가야 보이는 정지 버튼은 정지 버튼이 아니다. 그래서 화면 자체는 굴러가지
    않게 하고(`overflow: hidden`), 한 번에 다 보여 줄 수 없는 것은 탭으로 나눈다.
    """
    static = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "viewer.css").read_text(encoding="utf-8")
    javascript = (static / "viewer.js").read_text(encoding="utf-8")

    # 탭 넷과 조작 방식 둘.
    for tab in ("drive", "camera", "status", "lease"):
        assert f'data-tab="{tab}"' in html
    for feel in ("joint", "endpoint"):
        assert f'data-mode="{feel}"' in html

    # 굴러가지 않는다. 그리고 아래쪽 안전 영역(홈 인디케이터)을 피한다.
    assert "overflow: hidden" in css
    assert "safe-area-inset-bottom" in css
    assert "100dvh" in css
    # 손가락으로 눌리는 크기.
    assert "--touch: 44px" in css

    # 정지는 언제나 머리에 있다 — 탭을 바꿔도 사라지지 않는다.
    assert '<button id="hold"' in html
    assert "#top" in css

    # 홈 화면 앱.
    assert 'rel="manifest"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "apple-mobile-web-app-capable" in html
    manifest = json.loads((static / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/viewer/?host=web"
    assert any(icon["sizes"] == "180x180" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        assert (static / icon["src"].removeprefix("/viewer/")).exists()

    # 끝점 모드는 역기구학으로 세 관절을 함께 푼다. 손목과 집게는 사람이 정한다.
    assert "IK_JOINTS = ['shoulder_pan', 'shoulder_lift', 'elbow_flex']" in javascript
    assert "solveIK" in javascript
    # 그리고 그 결과도 결국 관절 절대 목표 하나로 나간다 — 서버가 보는 것은 그것뿐이다.
    assert "type: 'command'" in javascript or '"type": "command"' in javascript


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
