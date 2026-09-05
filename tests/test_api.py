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


def test_status_exposes_only_read_back_recording_camera_controls(monkeypatch):
    from soarm_console.app import recorder

    scene = {
        "values": {
            "power_line_frequency": 2,
            "exposure_dynamic_framerate": 0,
            "white_balance_automatic": 0,
            "white_balance_temperature": 4600,
            "auto_exposure": 3,
        },
        "failures": [],
    }
    wrist = {"values": {"power_line_frequency": 1}, "failures": ["Auto Exposure"]}
    monkeypatch.setattr(recorder, "_camera_controls", {"scene": scene, "wrist": wrist})

    cameras = status()["cameras"]

    assert cameras["scene"]["recording_controls"] == scene
    assert cameras["wrist"]["recording_controls"] == wrist


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


def test_camera_settings_accept_phone_saver_profile_and_report_it(monkeypatch):
    """폰의 절약 모드는 장치가 내는 320x240에서 서버가 2fps로 솎는다."""
    from fastapi.testclient import TestClient

    from soarm_console.app import app, cameras

    worker = cameras["scene"]
    # 하드웨어 없는 시험에서도 실물 카메라에서 확인한 두 모드를 같은 계약으로 둔다.
    monkeypatch.setattr(
        worker,
        "_modes",
        [
            {"width": 320, "height": 240, "fps": [30]},
            {"width": 640, "height": 480, "fps": [30]},
        ],
    )
    monkeypatch.setattr(worker, "_profile", worker.profile)

    client = TestClient(app)
    answer = client.post(
        "/api/cameras/scene/settings",
        json={"width": 320, "height": 240, "fps": 2},
    )

    assert answer.status_code == 200
    assert client.get("/api/status").json()["cameras"]["scene"]["requested"] == {
        "width": 320,
        "height": 240,
        "fps": 2,
    }


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


def test_the_document_stays_in_flow_so_ios_has_nothing_left_to_paint():
    """홈 화면 웹앱에서 아래쪽에 띠가 남지 않으려면 **문서를 흐름에서 들어내면 안 된다.**

    `body { position: fixed }`로 화면에 맞추려 했는데, 그러면 `body`가 통째로 흐름에서
    빠져 문서의 높이가 무너진다. 홈 화면 웹앱에서는 그 순간 iOS가 문서 아래에 남는 자리를
    `theme-color`로 칠하고, 그 자리는 문서 바깥이라 어떤 CSS로도 덮을 수 없다.

    `100dvh`도 같은 결과를 낸다. iOS가 말하는 `dvh`는 실제 보이는 높이보다 작을 수 있고,
    그 차이가 똑같이 문서 아래의 빈자리가 된다.

    쓰는 것은 `-webkit-fill-available`이다 — 그 기기가 실제로 내어 주는 높이. 굴러가지
    않게 하는 것은 `overflow: hidden`으로 충분하고, 문서를 옮길 이유는 없다.

    같은 사고를 다른 프로젝트에서 먼저 겪었고 기록이 남아 있다:
    `cookierunhub/docs/ios-webapp-modal-bottom-gap.md`.
    """
    import re

    static = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    css = (static / "viewer.css").read_text(encoding="utf-8")

    def rule(selector: str) -> str:
        found = re.search(r"(?m)^%s \{(.*?)^\}" % re.escape(selector), css, re.S)
        assert found, f"{selector} 규칙을 찾지 못했다"
        return found.group(1)

    body = rule("body")
    assert "position: fixed" not in body, (
        "body를 흐름에서 들어내면 홈 화면 웹앱 아래에 띠가 돌아온다"
    )
    assert "dvh" not in body, "dvh는 실제 보이는 높이와 어긋날 수 있다"
    assert "height: -webkit-fill-available;" in body
    assert "overflow: hidden;" in body

    html = rule("html")
    assert "height: -webkit-fill-available;" in html
    assert "overflow: hidden;" in html
    # 문서 바깥에 자리가 남더라도 색이 같으면 띠로 보이지 않는다. `body`도 같은 색이어야
    # 한다 — 실물에서 화면 아래 62pt를 칠한 것이 `body`의 배경이었다.
    #
    # 값을 여기에 적어 두지 않는다. 예전에는 `#080d16`을 그대로 박아 두었는데, 색을 한 번
    # 바꾸자 **여섯 군데**(html·body·머리글·아래 탭·메타·매니페스트)를 함께 고쳐야 했고
    # 시험은 "어디가 어긋났는지"가 아니라 "옛 값이 아니다"라고만 말했다. 이 시험이 지키려는
    # 것은 특정 색이 아니라 **여섯이 같다**는 것이므로, 기준을 `--navy` 하나로 두고 나머지를
    # 거기에 맞춘다.
    theme = re.search(r"--navy:\s*(#[0-9a-fA-F]{6});", css)
    assert theme, "--navy를 찾지 못했다"
    navy = theme.group(1)
    for selector, block in (("html", html), ("body", body)):
        assert "background: var(--navy);" in block or f"background: {navy};" in block, (
            f"{selector}의 배경이 머리글·아래 탭과 다르면 그 차이가 그대로 띠가 된다"
        )
    for selector in ("#top", "#tabs"):
        assert "background: var(--navy);" in rule(selector), (
            f"{selector}가 다른 색이면 문서 바깥의 띠가 그 연장으로 읽히지 않는다"
        )

    # **홈 화면 앱에서는 보이는 높이를 끝까지 쓴다.**
    #
    # 실물 아이폰(402×874)에서 웹 영역은 화면 위 0부터 그려지는데 `innerHeight`와
    # `-webkit-fill-available`은 812만 말했다. 874 − 812 = 62, 정확히 위쪽 안전 영역이다.
    # 배치가 812에서 끝나 812~874가 빈 채로 남았고 그것이 아래쪽 띠였다. 주소창이 없는
    # 홈 화면 앱에서는 `100vh`가 곧 보이는 높이라, `min-height`로 얹으면 채워진다.
    #
    # 브라우저에는 걸지 않는다 — 거기서는 `100vh`가 주소창이 숨었을 때의 높이라 아래 탭이
    # 화면 밖으로 밀린다. 정지 버튼이 화면 밖에 있는 것은 띠보다 나쁘다.
    assert "@media (display-mode: standalone)" in css
    standalone = re.search(
        r"@media \(display-mode: standalone\) \{(.*?)\}\s*\}", css, re.S
    )
    assert standalone and "min-height: 100vh" in standalone.group(1)

    page = (static / "index.html").read_text(encoding="utf-8")
    assert f'content="{navy}"' in page

    import json as _json

    manifest = _json.loads((static / "manifest.webmanifest").read_text(encoding="utf-8"))
    # **설치된 앱은 매니페스트의 색을 쓴다.** 메타만 고치면 브라우저에서만 맞는다.
    assert manifest["theme_color"] == navy
    assert manifest["background_color"] == navy


def test_the_phone_gets_one_way_to_drive_and_no_panel_under_it():
    """**폰에서는 끝점 하나뿐이고, 아래 조작판이 없다.**

    관절 모드는 관절 여섯 개를 하나씩 고르는 방식이라 슬라이더 여섯 줄이 따라온다. 실측
    852px 화면에서 그 여섯 줄이 267px을 가져갔고, 그만큼 위가 눌려 카메라가 92px짜리 띠로
    남았다 — 무엇이 찍히는지 알아볼 수 없는 높이다. 폰에서 실제로 쓰는 것은 끝점 조작이고,
    사람이 직접 정할 것은 앞뒤·손목 둘·집게 넷뿐이라 3D 위의 타일(`#hand`)로 옮겼다.

    이 시험이 지키는 것은 **판정이 한 군데**라는 것이다. 같은 미디어 질의를 CSS에도 적으면
    화면과 코드가 서로 다른 때에 폰이라고 믿게 되고, 그러면 조작판만 사라지고 방식은 관절로
    남는 상태가 생긴다 — 아무것도 만질 수 없는 화면이다.
    """
    import re

    static = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    css = (static / "viewer.css").read_text(encoding="utf-8")
    js = (static / "viewer.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    # 판정은 자바스크립트에만 있고, CSS는 그 결과(`body.phone`)만 본다.
    assert "const PHONE_QUERY =" in js
    assert "pointer: coarse" not in css, (
        "폰 판정을 CSS에도 적으면 화면과 코드가 서로 다른 때에 폰이라고 믿는다"
    )
    assert "document.body.classList.toggle('phone', isPhone());" in js

    # 무엇을 넣어도 폰에서는 끝점이다.
    setmode = re.search(r"function setMode\(next\) \{(.*?)\n\}", js, re.S)
    assert setmode and "isPhone() || next === 'endpoint'" in setmode.group(1), (
        "폰에서 관절 모드로 넘어가면 조작판 없는 화면에 슬라이더만 요구하게 된다"
    )

    # 조작판은 폰에서 격자 줄까지 비운다.
    assert "body.phone #panel { display: none !important; }" in css
    # 그 높이는 카메라로 간다.
    assert re.search(
        r"body\.phone\.tab-drive #stage,\s*\n?body\.phone\.tab-lease #stage \{[^}]*0\.75fr",
        css,
    ), "폰에서 카메라가 무대의 큰 쪽을 가져가야 한다"

    # 손 조작판은 3D 안에 있다. 밖에 두면 그 높이가 다시 무대에서 빠진다.
    view = re.search(r'<section id="view-pane">(.*?)</section>', html, re.S)
    assert view and 'id="hand"' in view.group(1)
    for name in ("depth", "wrist_flex", "wrist_roll", "gripper"):
        assert f"'{name}'" in js or f'"{name}"' in js

    # 폰 구획은 반응형 규칙들보다 뒤에 있어야 한다. 명시도가 같아서, 앞에 두면
    # `body.mode-endpoint.tab-drive #stage`가 이기고 카메라가 다시 얇아진다.
    assert css.index("body.phone #panel") > css.index("body.mode-endpoint.tab-drive #stage")
    # 맥 전용 구획은 그 아래에 남는다. 같은 이유다.
    assert css.index("body.host-native {") > css.index("body.phone #panel")


def test_adding_to_the_home_screen_works_from_whichever_page_is_open():
    """iOS는 **홈 화면에 추가하는 순간 열려 있던 페이지**의 메타를 저장한다.

    조작 화면에는 `viewport-fit=cover`와 `apple-mobile-web-app-capable`이 있었지만 콘솔
    페이지에는 없었다. 사람이 치는 주소는 서버 루트이므로 거기서 추가하는 것이 자연스러운데,
    그렇게 저장된 앱은 안전 영역(위의 상태 표시줄, 아래의 홈 인디케이터)을 **뷰포트 밖으로**
    두고 열린다. 그래서 아래 탭 밑에 쓸 수 없는 띠가 남았다 — 실물 아이폰에서 본 것이다.

    두 페이지가 같은 메타를 가져야 어느 쪽에서 추가하든 같은 앱이 된다.
    """
    import re

    static = Path(__file__).parents[1] / "src/soarm_console/static"
    console = (static / "index.html").read_text(encoding="utf-8")
    viewer = (static / "viewer/index.html").read_text(encoding="utf-8")
    css = (static / "viewer/viewer.css").read_text(encoding="utf-8")
    theme = re.search(r"--navy:\s*(#[0-9a-fA-F]{6});", css)
    assert theme, "--navy를 찾지 못했다"
    navy = theme.group(1)

    for page, label in ((console, "콘솔"), (viewer, "조작 화면")):
        assert "viewport-fit=cover" in page, f"{label}: 안전 영역까지 그려야 한다"
        assert 'name="apple-mobile-web-app-capable" content="yes"' in page, label
        assert 'rel="manifest"' in page, label
        assert 'rel="apple-touch-icon"' in page, label
        assert f'name="theme-color" content="{navy}"' in page, label

    # 문서 바깥으로 남는 띠는 머리글·아래 탭과 같은 색이어야 한다. 색까지 확인하는 것은
    # 위 `test_the_document_stays_in_flow_so_ios_has_nothing_left_to_paint`가 한다.


# MARK: 계약 — 앱이 무엇을 켤지 정하는 목록


def test_status_names_every_capability_the_app_looks_for():
    """맥 앱은 이 목록에 이름이 있을 때만 그 기능을 켠다. 이름은 글자까지 계약이다."""
    capabilities = status()["capabilities"]

    assert capabilities == [
        "abort",
        "resume",
        "preview",
        "quality",
        "delete",
        "train",
        "replay_preview",
        "soft_start",
        "sensor_extras",
    ]


def test_every_named_capability_has_something_behind_it():
    """목록에 이름만 있고 길이 없으면, 앱은 눌리지 않는 단추를 보여 준다."""
    from soarm_console.app import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    methods = {
        (route.path, method)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }

    assert ("/api/recording/preview/{role}.jpg", "GET") in methods
    assert ("/api/datasets/{name}", "DELETE") in methods
    assert ("/api/datasets/{name}/episodes/{episode_index}", "DELETE") in methods
    assert ("/api/replay/preview", "GET") in methods
    assert ("/api/spark/train", "POST") in methods
    assert ("/api/spark/runs/{run}/stop", "POST") in methods
    assert "/api/recording/control" in paths
    # `sensor_extras`가 가리키는 길은 새 끝점이 아니라 기존 응답에 실리는 열이다.
    # 궤적이 내주겠다고 적어 둔 열은 전부 수집이 실제로 쓰는 열이어야 한다 — 아니면 앱은
    # 영원히 오지 않는 값을 기다린다.
    from soarm_console import datasets, sensors

    assert set(datasets.TRAJECTORY_EXTRAS) <= set(sensors.extra_columns())
    # 그리고 요약이 내보내는 `extras` 목록은 수집이 넣은 열 그대로다.
    assert datasets.extra_columns({"features": sensors.extra_features()}) == list(
        sensors.extra_suffixes()
    )


def test_abort_is_a_control_the_recorder_accepts():
    from soarm_console.record_manager import CONTROLS

    assert "abort" in CONTROLS


def test_an_unknown_control_says_so_with_the_phrase_the_app_translates():
    from soarm_console.app import recorder
    from soarm_console.teleop import TeleopError

    with pytest.raises(TeleopError) as error:
        recorder.control("launch")
    assert str(error.value) == "Unknown recording control"


# MARK: 수집 중 스냅숏


def test_the_snapshot_is_only_served_while_a_recording_is_running(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from soarm_console.app import app, recorder

    monkeypatch.setattr(recorder, "runtime_dir", tmp_path)
    client = TestClient(app)

    assert client.get("/api/recording/preview/scene.jpg").status_code == 404

    monkeypatch.setattr(type(recorder), "running", property(lambda self: True))
    # 수집은 돌지만 아직 그림이 없다.
    assert client.get("/api/recording/preview/scene.jpg").status_code == 404
    assert client.get("/api/recording/preview/nose.jpg").status_code == 404


def test_a_stale_snapshot_is_not_served_at_all(tmp_path, monkeypatch):
    """멈춘 카메라의 마지막 장면을 계속 내주면, 화면은 아무 일도 없다는 듯 그것을 보여 준다."""
    import os
    import time

    from fastapi.testclient import TestClient

    from soarm_console.app import app, recorder

    monkeypatch.setattr(recorder, "runtime_dir", tmp_path)
    monkeypatch.setattr(type(recorder), "running", property(lambda self: True))
    snapshot = tmp_path / "preview-scene.jpg"
    snapshot.write_bytes(b"jpeg-bytes")
    client = TestClient(app)

    fresh = client.get("/api/recording/preview/scene.jpg")
    assert fresh.status_code == 200
    assert fresh.headers["content-type"] == "image/jpeg"
    # 자리를 지키고 내용만 바뀌는 파일이다. 캐시에 한 장이 남으면 화면은 그것을 계속 본다.
    assert fresh.headers["cache-control"] == "no-store"
    assert fresh.content == b"jpeg-bytes"

    old = time.time() - 10
    os.utime(snapshot, (old, old))
    assert client.get("/api/recording/preview/scene.jpg").status_code == 404


# MARK: 지우기


@pytest.fixture
def deletable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from soarm_console import datasets as datasets_module
    from soarm_console.app import app

    root = tmp_path / "data"
    (root / "soarm101_pick" / "meta").mkdir(parents=True)
    (root / "soarm101_pick" / "meta" / "info.json").write_text(
        json.dumps({"fps": 30, "total_episodes": 2, "features": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(datasets_module, "data_root", lambda: root)
    return TestClient(app), root


def test_deleting_a_dataset_moves_it_to_the_trash(deletable):
    client, root = deletable

    response = client.request("DELETE", "/api/datasets/soarm101_pick")

    assert response.status_code == 200
    assert not (root / "soarm101_pick").exists()
    trashed = list((root / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].name.startswith("soarm101_pick-")
    assert response.json()["trashed_to"] == str(trashed[0])


def test_deleting_a_dataset_that_is_not_there_is_not_found(deletable):
    client, _ = deletable
    response = client.request("DELETE", "/api/datasets/never_recorded")
    assert response.status_code == 404
    assert response.json()["detail"].startswith("No such dataset")


@pytest.mark.parametrize(
    "owner",
    [
        "soarm_console.record_manager.RecordManager",
        "soarm_console.replay_manager.ReplayManager",
    ],
)
def test_nothing_is_deleted_while_the_arm_is_using_the_data(deletable, monkeypatch, owner):
    """수집은 그 폴더에 쓰는 중이고, 재생은 그 파케이를 읽는 중이다."""
    client, root = deletable
    module_name, _, class_name = owner.rpartition(".")
    module = __import__(module_name, fromlist=[class_name])
    monkeypatch.setattr(getattr(module, class_name), "running", True)

    dataset = client.request("DELETE", "/api/datasets/soarm101_pick")
    episode = client.request("DELETE", "/api/datasets/soarm101_pick/episodes/0")

    assert dataset.status_code == 409
    assert episode.status_code == 409
    assert dataset.json()["detail"] == "Cannot delete while recording or replaying"
    assert (root / "soarm101_pick").exists()


# MARK: 품질 — 데이터셋이 스스로 말하지 못하는 것


def _finished_recording(tmp_path, monkeypatch, name: str, *, resumed: bool, warnings: int):
    from soarm_console.config import Settings
    from soarm_console.record_manager import RecordManager

    manager = RecordManager(Settings())
    monkeypatch.setattr(manager, "runtime_dir", tmp_path / "runtime")
    monkeypatch.setattr(manager, "log_path", tmp_path / "runtime" / "record.log")
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    manager.log_path.write_text("Record loop is running slower\n", encoding="utf-8")
    manager._slow_loop_warnings = warnings
    manager._resumed = resumed
    (manager.runtime_dir / "status.json").write_text(
        json.dumps({
            "dataset_name": name,
            "loop_hz": 28.6,
            "camera_stale_pct": {"scene": 0.0, "wrist": 9.0},
        }),
        encoding="utf-8",
    )
    return manager


def test_the_recording_leaves_its_own_measurements_beside_the_dataset(tmp_path, monkeypatch):
    """`timestamp`는 `frame_index / fps`로 합성된 값이라, 루프가 느렸어도 파케이는
    30Hz라고 적혀 있다. 그 사실을 아는 것은 찍는 동안 세어 둔 이 값뿐이다."""
    data = tmp_path / "data" / "soarm101_pick"
    data.mkdir(parents=True)
    manager = _finished_recording(tmp_path, monkeypatch, "soarm101_pick", resumed=False, warnings=2)

    manager._write_quality(data, {"loop_hz": 28.6, "camera_stale_pct": {"wrist": 9.0}})

    quality = json.loads((data / "soarm_quality.json").read_text(encoding="utf-8"))
    assert quality["loop_hz"] == 28.6
    assert quality["camera_stale_pct"] == {"wrist": 9.0}
    assert quality["slow_loop_warnings"] == 2
    assert quality["recorded_at"] > 0


def test_provenance_appends_one_entry_per_collection_session(tmp_path, monkeypatch):
    """이어 찍으면 항목이 늘어난다. 마지막 것만 남기면 앞 회차의 조건이 사라진다."""
    data = tmp_path / "data" / "soarm101_pick"
    data.mkdir(parents=True)
    manager = _finished_recording(tmp_path, monkeypatch, "soarm101_pick", resumed=False, warnings=0)
    manager._camera_controls = {"scene": {"values": {"auto_exposure": 3}, "failures": []}}
    manager._doctor = {"healthy": True}
    first = {"started_at": 1000.0, "server_commit": "abc123", "fps": 30, "extras_schema": 1}
    second = {"started_at": 2000.0, "server_commit": "def456", "fps": 30, "extras_schema": 1}

    manager._append_provenance(data, {"provenance": first})
    manager._append_provenance(data, {"provenance": second})

    history = json.loads((data / "soarm_provenance.json").read_text(encoding="utf-8"))
    assert [entry["started_at"] for entry in history] == [1000.0, 2000.0]
    assert [entry["server_commit"] for entry in history] == ["abc123", "def456"]
    # 자식이 모르는 두 가지는 부모가 얹는다.
    assert history[0]["camera_controls"]["scene"]["values"] == {"auto_exposure": 3}
    assert history[0]["doctor"] == {"healthy": True}


def test_a_run_that_wrote_no_provenance_leaves_the_history_alone(tmp_path, monkeypatch):
    """옛 자식이나 시작하다 죽은 자식이다. 빈 항목을 넣어 기록을 흐리지 않는다."""
    data = tmp_path / "data" / "soarm101_pick"
    data.mkdir(parents=True)
    manager = _finished_recording(tmp_path, monkeypatch, "soarm101_pick", resumed=False, warnings=0)

    manager._append_provenance(data, {"dataset_name": "soarm101_pick"})

    assert not (data / "soarm_provenance.json").exists()


def test_resuming_adds_this_runs_warnings_to_the_ones_already_counted(tmp_path, monkeypatch):
    """파일 하나가 데이터셋 전체를 말해야 한다. 마지막 실행만 남기면 앞 회차가 조용해진다."""
    data = tmp_path / "data" / "soarm101_pick"
    data.mkdir(parents=True)
    (data / "soarm_quality.json").write_text(
        json.dumps({"slow_loop_warnings": 5, "loop_hz": 22.0}), encoding="utf-8"
    )
    manager = _finished_recording(tmp_path, monkeypatch, "soarm101_pick", resumed=True, warnings=3)

    manager._write_quality(data, {"loop_hz": 29.9, "camera_stale_pct": {}})

    quality = json.loads((data / "soarm_quality.json").read_text(encoding="utf-8"))
    assert quality["slow_loop_warnings"] == 8
    # 속도는 이번 실행의 값으로 바뀐다 — 더할 수 있는 것이 아니다.
    assert quality["loop_hz"] == 29.9
