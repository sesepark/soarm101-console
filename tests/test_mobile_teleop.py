from pathlib import Path

import json

import pytest

from soarm_console.mobile_watchdog import lease_alive


def test_mobile_physical_teleop_uses_existing_owner_and_stop_contracts():
    viewer = Path(__file__).parents[1] / "src/soarm_console/static/viewer"
    html = (viewer / "index.html").read_text()
    js = (viewer / "viewer.js").read_text()
    css = (viewer / "viewer.css").read_text()
    assert 'data-tab="teleop"' in html
    assert 'id="physical-start"' in html
    assert 'id="physical-stop"' in html
    assert 'id="physical-phrase"' not in html
    assert "post('/api/teleoperation/mobile/start', {" in js
    assert "post('/api/teleoperation/stop')" in js
    assert "post('/api/mode/stop')" in js
    assert "confirmation: 'START SOARM101', session: startingSession" in js
    assert "!el('physical-confirm').checked" in js
    assert "post('/api/teleoperation/mobile/heartbeat'" in js
    assert "if (document.hidden) stopPhoneSession(true)" in js
    assert "el('physical-confirm').checked = false" in js
    assert "state.teleop_preflight.length === 0" in js
    assert "Boolean(virtual?.lease)" in js
    assert "repeat(5, 1fr)" in css
    assert "body.host-native #teleop-pane { display: none !important; }" in css
    assert '<details class="physical-details"><summary>실행 로그</summary>' in html
    assert 'id="physical-diagnostics"' in html
    assert 'id="physical-token-link"' in html
    assert 'body.tab-teleop .camera-chips { display: none !important; }' in css
    assert 'grid-template-columns: minmax(0, 1fr) 88px' in css
    assert "el('physical-token-link').hidden = Boolean(el('token').value.trim())" in js


@pytest.mark.parametrize("record,expected", [
    ({"state": "running", "lease_expires_at": 11}, True),
    ({"state": "starting", "lease_expires_at": 11}, True),
    ({"state": "running", "lease_expires_at": 10}, False),
    ({"state": "running", "lease_expires_at": 9}, False),
    ({"state": "exited", "lease_expires_at": 11}, False),
    ({"state": "running", "lease_expires_at": float("nan")}, False),
    ({"state": "running", "lease_expires_at": 11, "expiry_stop_requested_at": 9}, False),
    ({}, False),
])
def test_worker_lease_guard(record, expected, tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(record))
    assert lease_alive(path, 10) is expected


def test_worker_guard_fails_closed_on_missing_or_corrupt_record(tmp_path):
    path = tmp_path / "job.json"
    assert not lease_alive(path, 10)
    path.write_text("broken")
    assert not lease_alive(path, 10)


def test_worker_refuses_expired_session_before_connecting(tmp_path, monkeypatch):
    from soarm_console.mobile_watchdog import mobile_watchdog

    path = tmp_path / "job.json"
    path.write_text(json.dumps({"state": "running", "lease_expires_at": 0}))
    monkeypatch.setenv("SOARM_MOBILE_JOB_RECORD", str(path))
    with pytest.raises(SystemExit, match="lease has ended"):
        with mobile_watchdog():
            pytest.fail("Must not enter hardware connection code")


def test_mobile_start_reuses_motion_auth_and_common_preflight(monkeypatch):
    import importlib
    from fastapi.testclient import TestClient

    module = importlib.import_module("soarm_console.app")
    calls = []
    monkeypatch.setattr(module, "_authorise_motion", lambda token: calls.append(token))
    monkeypatch.setattr(module, "_start_teleoperation",
                        lambda body, session: calls.append((body.confirmation, session)) or {"running": True})
    client = TestClient(module.app)
    result = client.post("/api/teleoperation/mobile/start",
                         headers={"X-SOARM-Motion-Token": "test-token"},
                         json={"confirmation": "START SOARM101", "session": "a" * 32})
    assert result.status_code == 200
    assert calls == ["test-token", ("START SOARM101", "a" * 32)]


def test_late_phone_stop_cannot_stop_a_new_phone_or_desktop_job(monkeypatch):
    from soarm_console.config import Settings
    from soarm_console.teleop import TeleopError, TeleopManager
    from soarm_console import hubq_client

    class Process:
        metadata = {"mobile_session": "b" * 32}

        def poll(self):
            return None

    manager = TeleopManager(Settings())
    manager._process = Process()
    stops = []
    monkeypatch.setattr(hubq_client, "stop_job", lambda *args: stops.append(args))
    with pytest.raises(TeleopError, match="does not match"):
        manager.stop(mobile_session="a" * 32)
    manager._process.metadata = {}
    with pytest.raises(TeleopError, match="does not match"):
        manager.stop(mobile_session="a" * 32)
    assert stops == []
