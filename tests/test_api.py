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
    from fastapi.testclient import TestClient

    from soarm_console.app import app

    client = TestClient(app, follow_redirects=False)
    phone = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1"}
    pad = {"user-agent": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1"}
    mac = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Version/18.0 Safari/605.1.15"}

    # **판정은 서버가 한다.** 화면 안의 자바스크립트로도 같은 일을 하지만, 옛 사본이
    # 캐시에 남아 있으면 그쪽은 실행되지 않는다. 실제로 고친 뒤에도 폰에서 조작 화면이
    # 뜨지 않는 일이 있었고, 그때 서버는 새 파일을 내주고 있었다.
    for agent in (phone, pad):
        answer = client.get("/", headers=agent)
        assert answer.status_code == 307, answer.status_code
        assert answer.headers["location"] == "/viewer/?host=web"
    # 돌아올 길은 남는다.
    assert client.get("/?console=1", headers=phone).status_code == 200
    # 데스크톱의 주소는 말없이 바뀌지 않는다.
    assert client.get("/", headers=mac).status_code == 200

    # 화면을 이루는 것은 캐시에서 살아 돌아오지 않는다. 옛 화면이 새 서버의 거절 코드를
    # 모르면 팔이 왜 안 움직이는지 아무 말도 하지 못한다.
    for path in ("/", "/viewer/", "/static/viewer/viewer.js", "/static/viewer/viewer.css"):
        answer = client.get(path, headers=mac)
        assert answer.status_code == 200, path
        assert "no-store" in answer.headers.get("cache-control", ""), path

    static = Path(__file__).parents[1] / "src/soarm_console/static"
    console = (static / "index.html").read_text(encoding="utf-8")
    # 자바스크립트 쪽 그물도 그대로 둔다. 서버가 못 알아본 기기를 여기서 잡는다.
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


def test_no_style_class_can_be_worn_by_something_it_was_not_meant_for():
    """상태 이름과 단추 모양이 같은 클래스를 쓰면, 상태가 단추가 된다.

    실물 아이폰에서 팔이 자세 유지에 들어가는 순간 상태 글줄이 **빨간 알약 덩어리**로
    보였다. 정지 단추의 스타일이 `.stop`이라는 맨 클래스로 적혀 있었고, 상태 글줄도
    자세 유지일 때 `class="stop"`이 됐기 때문이다. 자바스크립트는 맞게 동작하고 있었고
    이름 하나가 겹쳤을 뿐이라, 화면을 실제로 보기 전에는 드러나지 않았다.

    두 가지를 잠근다. 단추 스타일은 요소와 함께 적고(`button.stop`), 상태 이름에는
    접두사를 붙인다(`state-stop`). 둘 중 하나만 있어도 이 사고는 나지 않지만, 둘 다
    있으면 다음 사람이 어느 쪽 규칙을 몰라도 안전하다.
    """
    import re

    static = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    css = (static / "viewer.css").read_text(encoding="utf-8")
    javascript = (static / "viewer.js").read_text(encoding="utf-8")

    # 단추 모양은 요소와 함께 적혀 있어야 한다.
    assert "button.stop {" in css
    assert not re.search(r"(?m)^\.stop\s*[,{]", css), "맨 `.stop`은 다른 것이 뒤집어쓸 수 있다"

    # 상태 이름에는 접두사가 붙는다.
    assert "line.className = kind ? `state-${kind}` : ''" in javascript
    for kind in ("live", "warn", "stop"):
        assert f".lines strong.state-{kind}" in css

    # 자바스크립트가 붙이는 이름 가운데 CSS의 맨 클래스와 겹치는 것이 없어야 한다.
    applied = set(re.findall(r"classList\.(?:add|toggle|remove)\('([a-z-]+)'", javascript))
    bare = set(re.findall(r"(?m)^\.([a-z-]+)\s*[,{]", css))
    assert not (applied & bare), f"겹치는 이름: {sorted(applied & bare)}"


def test_the_phone_screen_is_pinned_to_the_viewport():
    """홈 화면 앱에서 아래 탭 밑에 빈 띠가 남지 않아야 한다.

    `height: 100dvh`만 두었더니 실물 아이폰에서 탭 줄 아래로 빈 공간이 남았다. iOS가
    말하는 `dvh`와 실제로 보이는 높이가 늘 같지는 않다. `position: fixed`에 `inset: 0`
    이면 그 값이 무엇이든 몸통이 정확히 화면을 채운다.

    화면 맨 아래(홈 인디케이터 자리)는 웹 내용이 아니라 `theme-color`로 칠해지므로,
    그 색이 아래 탭과 같아야 이어져 보인다.
    """
    static = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    css = (static / "viewer.css").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "position: fixed;\n  inset: 0;" in css
    assert 'content="#080d16"' in html, "theme-color가 아래 탭 색과 같아야 한다"
    assert "#tabs {" in css and "background: #080d16" in css


def test_adding_to_the_home_screen_works_from_whichever_page_is_open():
    """iOS는 **홈 화면에 추가하는 순간 열려 있던 페이지**의 메타를 저장한다.

    조작 화면에는 `viewport-fit=cover`와 `apple-mobile-web-app-capable`이 있었지만 콘솔
    페이지에는 없었다. 사람이 치는 주소는 서버 루트이므로 거기서 추가하는 것이 자연스러운데,
    그렇게 저장된 앱은 안전 영역(위의 상태 표시줄, 아래의 홈 인디케이터)을 **뷰포트 밖으로**
    두고 열린다. 그래서 아래 탭 밑에 쓸 수 없는 띠가 남았다 — 실물 아이폰에서 본 것이다.

    두 페이지가 같은 메타를 가져야 어느 쪽에서 추가하든 같은 앱이 된다.
    """
    static = Path(__file__).parents[1] / "src/soarm_console/static"
    console = (static / "index.html").read_text(encoding="utf-8")
    viewer = (static / "viewer/index.html").read_text(encoding="utf-8")

    for page, label in ((console, "콘솔"), (viewer, "조작 화면")):
        assert "viewport-fit=cover" in page, f"{label}: 안전 영역까지 그려야 한다"
        assert 'name="apple-mobile-web-app-capable" content="yes"' in page, label
        assert 'rel="manifest"' in page, label
        assert 'rel="apple-touch-icon"' in page, label
        assert 'name="theme-color" content="#080d16"' in page, label

    # 뷰포트 밖으로 남는 띠는 머리글·아래 탭과 같은 색이어야 한다. 그러지 않으면 그 띠가
    # "빈 자리"로 보인다.
    css = (static / "viewer/viewer.css").read_text(encoding="utf-8")
    assert "html { background: #080d16; }" in css
