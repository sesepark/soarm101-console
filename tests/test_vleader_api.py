"""가상 리더의 HTTP/WebSocket 계약.

여기서 확인하는 것은 *바깥에서 부를 때* 무엇이 막히는가다. 리스 경쟁, 토큰으로 갈린
관찰과 조작, 거절 사유가 코드로 돌아오는지, 그리고 조작 중 연결이 끊겼을 때.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from soarm_console.config import Settings
from soarm_console.vleader.api import VirtualLeader, build_router
from soarm_console.vleader.safety import Reject

from test_vleader import CALIBRATION


TOKEN = "test-motion-token"


@pytest.fixture
def console(tmp_path, monkeypatch):
    calibration = tmp_path / "calibration/robots/so_follower"
    calibration.mkdir(parents=True)
    (calibration / "soarm101_follower.json").write_text(json.dumps(CALIBRATION), encoding="utf-8")
    follower_port = tmp_path / "follower-port"
    follower_port.write_text("", encoding="utf-8")

    monkeypatch.setenv("HF_LEROBOT_CALIBRATION", str(tmp_path / "calibration"))
    monkeypatch.setenv("SOARM_FOLLOWER_PORT", str(follower_port))
    monkeypatch.setenv("SOARM_ENABLE_MOTION", "1")
    monkeypatch.setenv("SOARM_MOTION_TOKEN", TOKEN)
    monkeypatch.setenv("SOARM_VL_BACKEND", "simulated")

    # 읽기 전용 진단은 실제 버스를 연다. 하드웨어 없는 시험에서는 통과한 것으로 둔다 —
    # 이 시험이 확인하려는 것은 진단 자체가 아니라 그 뒤의 권한과 검증이다.
    import soarm_console.vleader.api as api

    class _Report:
        healthy = True
        safe_for_motion_start = True
        error = None

    monkeypatch.setattr(api, "inspect_arm", lambda *_: _Report())

    vleader = VirtualLeader(Settings())
    app = FastAPI()
    app.include_router(build_router(vleader))
    with TestClient(app) as client:
        yield client, vleader
    vleader.stop(force=True)


def motion(client, method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-SOARM-Motion-Token"] = TOKEN
    return client.request(method, path, headers=headers, **kwargs)


def start_and_arm(client):
    assert motion(client, "POST", "/api/vleader/start").status_code == 200
    # 첫 관절값을 읽을 때까지 기다린다. 자세를 모르는 채로 토크를 걸지 않는다.
    for _ in range(100):
        if client.get("/api/vleader").json()["observation"] > 2:
            break
        time.sleep(0.02)
    return motion(
        client, "POST", "/api/vleader/arm", json={"confirmation": "MOVE SOARM101"}
    )


def test_observation_needs_nothing_and_motion_needs_a_token(console):
    client, _ = console
    assert client.get("/api/vleader").status_code == 200
    # 토큰 없이 조작 권한을 달라고 하면 401.
    assert client.post("/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "누구"}).status_code == 401
    assert client.post(
        "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "누구"},
        headers={"X-SOARM-Motion-Token": "wrong"},
    ).status_code == 401


def test_the_arm_confirmation_is_not_pre_filled_and_must_match(console):
    client, _ = console
    motion(client, "POST", "/api/vleader/start")
    wrong = motion(client, "POST", "/api/vleader/arm", json={"confirmation": "move soarm101"})
    assert wrong.status_code == 400
    # 화면이 대신 채워 넣을 수 있는 값을 서버가 내려주지 않는지 본다. 길이만 알려 준다.
    payload = client.get("/api/vleader").json()
    assert "MOVE SOARM101" not in json.dumps(payload)
    assert payload["arm_confirmation_length"] == len("MOVE SOARM101")


def test_taking_authority_needs_the_phrase_even_when_torque_is_already_on(console):
    """토크를 거는 자리에만 게이트를 두면, 이미 켜져 있을 때 그 자리를 지나치게 된다.

    실제로 그렇게 만들어 두었다가 시험에서 잡혔다. 먼저 켜 둔 사람이 있는 팔에 아무나
    문구 없이 붙을 수 있었다. 팔이 움직일 수 있게 되는 순간은 리스를 받는 순간이다.
    """
    client, _ = console
    assert start_and_arm(client).status_code == 200
    assert client.get("/api/vleader").json()["torque_enabled"] is True
    wrong = motion(
        client, "POST", "/api/vleader/lease",
        json={"confirmation": "move soarm101", "holder": "맥북"},
    )
    assert wrong.status_code == 400
    assert client.get("/api/vleader").json()["lease"] is None
    right = motion(
        client, "POST", "/api/vleader/lease",
        json={"confirmation": "MOVE SOARM101", "holder": "맥북"},
    )
    assert right.status_code == 200


def test_a_lease_is_refused_while_torque_is_off(console):
    client, _ = console
    motion(client, "POST", "/api/vleader/start")
    response = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"})
    assert response.status_code == 409
    assert "torque" in response.json()["detail"].lower()


def test_two_devices_cannot_hold_the_lease_at_once(console):
    client, _ = console
    assert start_and_arm(client).status_code == 200
    mac = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"})
    assert mac.status_code == 200
    phone = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "아이폰"})
    assert phone.status_code == 409
    assert "맥북" in phone.json()["detail"]
    # 반납하면 폰이 받는다. 빼앗기는 없다.
    lease_id = mac.json()["lease_id"]
    assert motion(client, "DELETE", f"/api/vleader/lease/{lease_id}").json()["released"] is True
    assert motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "아이폰"}).status_code == 200


def test_anyone_can_stop_the_arm_without_a_token_or_a_lease(console):
    """폰이 맥을 멈출 수 있어야 한다. 멈추는 것은 권한을 빼앗는 것이 아니다."""
    client, _ = console
    start_and_arm(client)
    motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"})
    response = client.post("/api/vleader/hold")  # 토큰 없음
    assert response.status_code == 200
    assert response.json()["state"] == "HOLD"
    # 멈춘 뒤에도 리스는 맥북이 쥐고 있고, 토크는 걸려 있다(팔을 떨어뜨리지 않는다).
    assert response.json()["torque_enabled"] is True
    assert response.json()["lease"]["holder"] == "맥북"


def test_the_socket_refuses_a_command_without_a_lease_and_names_the_reason(console):
    client, _ = console
    start_and_arm(client)
    with client.websocket_connect("/api/vleader/stream") as socket:
        assert socket.receive_json()["type"] == "hello"
        socket.send_json({"type": "command", "sequence": 1, "joints": {"elbow_flex": 0.0}})
        message = next_of_type(socket, "reject")
        assert message["code"] == Reject.NO_ACTIVE_LEASE


def test_the_socket_drives_the_arm_and_reports_what_it_clamped(console):
    client, vleader = console
    start_and_arm(client)
    lease = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"}).json()
    with client.websocket_connect("/api/vleader/stream") as socket:
        hello = socket.receive_json()
        present = {joint["name"]: joint["present"] for joint in hello["joints"]}
        # 첫 명령은 현재 자세에서 시작한다.
        socket.send_json(
            {
                "type": "command",
                "lease_id": lease["lease_id"],
                "sequence": 1,
                "valid_for_ms": 300,
                "joints": present,
            }
        )
        assert next_of_type(socket, "ack")["sequence"] == 1
        # 그다음 멀리 있는 목표를 보내면 자르고, 무엇을 잘랐는지 말해 준다.
        time.sleep(0.05)
        socket.send_json(
            {
                "type": "command",
                "lease_id": lease["lease_id"],
                "sequence": 2,
                "valid_for_ms": 300,
                "joints": {**present, "elbow_flex": present["elbow_flex"] + 60},
            }
        )
        ack = next_of_type(socket, "ack")
        assert ack["rate_limited"] == ["elbow_flex"]
        assert abs(ack["goal"]["elbow_flex"] - present["elbow_flex"]) <= 2.001


def test_a_command_out_of_the_absolute_limit_is_refused_by_code(console):
    client, _ = console
    start_and_arm(client)
    lease = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"}).json()
    with client.websocket_connect("/api/vleader/stream") as socket:
        socket.receive_json()
        socket.send_json(
            {
                "type": "command",
                "lease_id": lease["lease_id"],
                "sequence": 1,
                "joints": {"elbow_flex": 400.0},
            }
        )
        assert next_of_type(socket, "reject")["code"] == Reject.OUTSIDE_ABSOLUTE_LIMIT


def test_dropping_the_connection_leaves_the_arm_holding_not_repeating(console):
    client, _ = console
    start_and_arm(client)
    lease = motion(client, "POST", "/api/vleader/lease", json={"confirmation": "MOVE SOARM101", "holder": "맥북"}).json()
    with client.websocket_connect("/api/vleader/stream") as socket:
        hello = socket.receive_json()
        present = {joint["name"]: joint["present"] for joint in hello["joints"]}
        socket.send_json(
            {
                "type": "command",
                "lease_id": lease["lease_id"],
                "sequence": 1,
                "valid_for_ms": 300,
                "joints": present,
            }
        )
        next_of_type(socket, "ack")
    # 연결이 사라졌다. 잠깐 뒤 워치독이 자세 유지로 떨어뜨린다.
    deadline = time.time() + 3
    state = None
    while time.time() < deadline:
        state = client.get("/api/vleader").json()
        if state["state"] == "HOLD":
            break
        time.sleep(0.05)
    assert state["state"] == "HOLD"
    assert state["fault"]["code"] == "COMMAND_TIMEOUT"
    # 리스는 아직 살아 있다. 연결이 돌아오면 다시 이어서 할 수 있다 — 다만 자세를
    # 다시 맞춰야 한다.
    assert state["lease"] is not None
    assert state["lease"]["needs_sync"] is True


def test_releasing_torque_needs_its_own_confirmation(console):
    client, _ = console
    start_and_arm(client)
    wrong = motion(
        client, "POST", "/api/vleader/torque/release", json={"confirmation": "MOVE SOARM101"}
    )
    assert wrong.status_code == 400
    assert client.get("/api/vleader").json()["torque_enabled"] is True
    right = motion(
        client,
        "POST",
        "/api/vleader/torque/release",
        json={"confirmation": "RELEASE TORQUE SOARM101"},
    )
    assert right.status_code == 200
    assert right.json()["torque_enabled"] is False


def test_the_goal_relay_says_when_it_is_stale(console):
    """수집 중 `lerobot-record` 안의 teleoperator가 이 답을 보고 팔을 세운다."""
    client, _ = console
    start_and_arm(client)
    payload = client.get("/api/vleader/goal").json()
    assert set(payload["joints"]) == set(CALIBRATION)
    assert payload["stale"] is True  # 아무도 조작하고 있지 않다


def next_of_type(socket, kind, limit=200):
    for _ in range(limit):
        message = socket.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"{kind} 메시지가 오지 않았습니다")


# --------------------------------------------------------------- 수집으로 넘기기


def test_the_relay_starts_from_where_the_arm_was_and_refuses_when_it_does_not_know(console):
    """수집으로 넘어갈 때 팔이 튀지 않으려면 출발점을 알아야 한다.

    `lerobot-record`가 팔로워 serial의 소유자가 되므로 콘솔은 장치를 놓는다. 놓기 직전의
    자세를 들고 있지 않으면 중계할 첫 목표가 어디인지 아무도 모른다.
    """
    from soarm_console.vleader.backend import HardwareError

    client, vleader = console
    # 한 번도 읽은 적이 없으면 거절한다.
    with pytest.raises(HardwareError):
        vleader.start_relay()

    start_and_arm(client)
    for _ in range(100):
        if client.get("/api/vleader").json()["observation"] > 2:
            break
        time.sleep(0.02)
    before = {j["name"]: j["present"] for j in client.get("/api/vleader").json()["joints"]}

    snapshot = vleader.start_relay()
    assert snapshot["relay"] is True
    assert snapshot["running"] is True
    # 중계는 놓기 직전의 자세에서 출발한다.
    after = {joint["name"]: joint["goal"] for joint in snapshot["joints"]}
    assert after == pytest.approx(before, abs=0.001)

    # 중계 중에는 토크가 우리 것이 아니다.
    with pytest.raises(HardwareError):
        vleader.owner.arm()

    goal = client.get("/api/vleader/goal").json()
    assert set(goal["joints"]) == set(CALIBRATION)
    assert goal["state"] in {"READY", "HOLD"}


def test_the_lerobot_teleoperator_reads_the_relay_and_refuses_when_there_is_none():
    """`lerobot-record`가 쓰는 teleoperator. 콘솔이 중계하고 있지 않으면 붙지 않는다.

    LeRobot의 `make_teleoperator_from_config`가 이 클래스를 찾아내는지까지 함께 본다 —
    third-party teleoperator는 이름 규칙으로 해결되므로, 클래스나 모듈 이름을 바꾸면
    조용히 못 찾게 된다.
    """
    import http.server
    import threading

    from lerobot.teleoperators.utils import make_teleoperator_from_config

    from soarm_console.vleader.teleoperator import SOArmVirtualLeaderConfig

    payload = {"joints": {name: 1.5 for name in CALIBRATION}, "stale": False, "state": "ACTIVE"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server의 이름 규칙
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = SOArmVirtualLeaderConfig(
            id="test", host="127.0.0.1", port=server.server_address[1]
        )
        teleoperator = make_teleoperator_from_config(config)
        # 이름 규칙으로 실제 클래스가 잡혔는가.
        assert type(teleoperator).__name__ == "SOArmVirtualLeader"
        teleoperator.connect()
        assert teleoperator.is_connected
        action = teleoperator.get_action()
        assert set(action) == {f"{name}.pos" for name in CALIBRATION}
        assert all(value == pytest.approx(1.5) for value in action.values())
        # 목표가 낡았다고 알려 오면 마지막 값을 그대로 유지한다 — 절대 위치를 다시 쓰는
        # 것은 "거기 서 있으라"는 뜻이고, 움직이라는 명령을 반복하는 것과 다르다.
        payload["stale"] = True
        payload["joints"] = {name: 40.0 for name in CALIBRATION}
        held = teleoperator.get_action()
        assert all(value == pytest.approx(1.5) for value in held.values())
        teleoperator.disconnect()
        assert not teleoperator.is_connected
    finally:
        server.shutdown()


def test_handover_clears_a_released_hold_but_not_a_real_one(console):
    """앞 사람이 반납해서 선 것과, 누가 정지를 눌러 선 것은 다르다.

    앞의 것은 정상적인 교대라 새 사람이 이어받으면 풀린다. 뒤의 것은 이유를 읽고 확인해야
    풀린다 — 권한을 새로 받는 것으로 조용히 지워지면 아무도 그 이유를 보지 않는다.
    """
    client, _ = console
    start_and_arm(client)
    lease = motion(
        client, "POST", "/api/vleader/lease",
        json={"confirmation": "MOVE SOARM101", "holder": "맥북"},
    ).json()
    motion(client, "DELETE", f"/api/vleader/lease/{lease['lease_id']}")
    assert client.get("/api/vleader").json()["fault"]["code"] == "LEASE_RELEASED"
    # 이어받으면 풀린다.
    motion(
        client, "POST", "/api/vleader/lease",
        json={"confirmation": "MOVE SOARM101", "holder": "아이폰"},
    )
    assert client.get("/api/vleader").json()["state"] != "HOLD"

    # 누가 정지를 누른 것은 그렇지 않다.
    client.post("/api/vleader/hold")
    held = client.get("/api/vleader").json()
    assert held["state"] == "HOLD" and held["fault"]["code"] == "OPERATOR_HOLD"
    motion(client, "DELETE", f"/api/vleader/lease/{held['lease']['lease_id']}")
    motion(
        client, "POST", "/api/vleader/lease",
        json={"confirmation": "MOVE SOARM101", "holder": "맥북"},
    )
    after = client.get("/api/vleader").json()
    assert after["state"] == "HOLD"
    assert after["fault"]["code"] == "OPERATOR_HOLD"
