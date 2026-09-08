from __future__ import annotations

import ast
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hubq import claims
from hubq.app import app
from soarm_console import hubq_client


FOLLOWER = "/dev/ttyACM1"
REAL_CLIENT_CLAIM = hubq_client.claim


def _held(owner: str, device: str = FOLLOWER) -> list[dict[str, object]]:
    return [
        {
            "device": device,
            "locked": True,
            "owner": owner,
            "pid": 42,
            "acquired_at": 123.0,
        }
    ]


@pytest.mark.parametrize(
    ("requester", "holder", "message"),
    [
        ("physical-leader-teleop", "record-leader", "Stop recording before teleoperation"),
        (
            "physical-leader-teleop",
            "virtual-leader",
            "Stop the virtual leader before physical-leader teleoperation: the follower has one owner",
        ),
        ("record-leader", "physical-leader-teleop", "Stop teleoperation before recording"),
        (
            "record-leader",
            "calibration",
            "Stop the camera calibration before recording: the follower and cameras have one owner",
        ),
        ("replay", "replay", "Stop the replay that is already running"),
        ("replay", "policy", "Stop the policy before replaying: the follower has one owner"),
        ("policy", "record-virtual", "Stop the running mode before starting a policy"),
        ("rig-calibration", "policy", "Stop the running mode before starting calibration"),
        (
            "virtual-leader",
            "policy",
            "Stop the policy before starting the virtual leader: the follower has one owner",
        ),
        ("hardware-doctor", "replay", "Cannot inspect serial buses during an active mode"),
        ("torque-release", "replay", "Stop the running mode before releasing torque"),
        (
            "replay-preflight",
            "record-leader",
            "Stop the running mode before reading the follower: it has one owner",
        ),
        ("intrinsics", "policy", "Stop the running mode before collecting board views"),
        ("dataset-delete", "policy", "Cannot delete while recording or replaying"),
    ],
)
def test_claim_preserves_console_conflict_wording(monkeypatch, requester, holder, message):
    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held(holder))

    decision = claims.decide_claim(requester, [FOLLOWER])

    assert decision.allowed is False
    assert decision.detail == message


def test_claim_ignores_unrequested_devices(monkeypatch):
    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held("policy", "/dev/video0"))

    assert claims.decide_claim("replay", [FOLLOWER]).allowed is True


def test_virtual_recording_may_take_over_from_the_virtual_leader(monkeypatch):
    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held("virtual-leader"))

    assert claims.decide_claim("record-virtual", [FOLLOWER]).allowed is True
    assert claims.decide_claim("record-leader", [FOLLOWER]).allowed is False


def test_camera_workers_are_relinquishable_for_jobs_that_stop_them(monkeypatch):
    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held("camera-preview"))

    assert claims.decide_claim("policy", [FOLLOWER]).allowed is True


def test_claim_endpoint_returns_a_structured_409(monkeypatch):
    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held("virtual-leader"))

    response = TestClient(app).post(
        "/claim", json={"kind": "physical-leader-teleop", "devices": [FOLLOWER]}
    )

    assert response.status_code == 409
    assert response.json()["allowed"] is False
    assert response.json()["detail"].startswith("Stop the virtual leader")
    assert response.json()["conflicts"][0]["device"] == FOLLOWER


def test_console_propagates_hubq_conflicts_without_rewording(monkeypatch):
    from soarm_console import app as console

    detail = "Stop the virtual leader before physical-leader teleoperation: the follower has one owner"

    def refuse(_kind, _devices):
        raise hubq_client.HubQConflict(detail)

    monkeypatch.setattr(hubq_client, "claim", refuse)

    with pytest.raises(HTTPException) as error:
        console._claim_hardware("physical-leader-teleop", [FOLLOWER])

    assert error.value.status_code == 409
    assert error.value.detail == detail


def test_console_fails_closed_if_hubq_is_unavailable(monkeypatch):
    from soarm_console import app as console

    def unavailable(_kind, _devices):
        raise hubq_client.HubQError("HUBq is unavailable")

    monkeypatch.setattr(hubq_client, "claim", unavailable)

    with pytest.raises(HTTPException) as error:
        console._claim_hardware("replay", [FOLLOWER])

    assert error.value.status_code == 503


def test_client_reads_the_detail_from_a_hubq_409(monkeypatch):
    detail = "Stop recording before teleoperation"
    error = HTTPError(
        "http://127.0.0.1:8094/claim",
        409,
        "Conflict",
        {},
        BytesIO(json.dumps({"detail": detail}).encode("utf-8")),
    )
    monkeypatch.setattr(hubq_client, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(hubq_client.HubQConflict, match=detail):
        REAL_CLIENT_CLAIM("physical-leader-teleop", [FOLLOWER])


def test_hubq_imports_only_the_shared_console_lock_module() -> None:
    root = Path(__file__).parents[1] / "src/hubq"
    imports = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))

    assert "from soarm_console.owner_lock import" in imports
    assert "from soarm_console." not in imports.replace(
        "from soarm_console.owner_lock import", ""
    )


def test_each_console_exclusion_route_has_one_claim_and_no_running_matrix() -> None:
    source = (Path(__file__).parents[1] / "src/soarm_console/app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    guarded = {
        "camera_stream",
        "configure_camera",
        "doctor",
        "release_torque",
        "start_teleoperation",
        "start_recording",
        "delete_dataset",
        "delete_dataset_episode",
        "replay_preview",
        "start_replay",
        "start_policy",
        "start_intrinsics",
        "start_extrinsics",
    }

    for name in guarded:
        node = functions[name]
        claims_in_route = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_claim_hardware"
        ]
        manual_running_checks = [
            attribute
            for attribute in ast.walk(node)
            if isinstance(attribute, ast.Attribute) and attribute.attr == "running"
        ]
        assert len(claims_in_route) == 1, name
        assert manual_running_checks == [], name


def test_camera_configuration_refusal_still_matches_the_console_profile(monkeypatch) -> None:
    from soarm_console.cameras import RECORDING_PROFILE

    monkeypatch.setattr(claims, "read_lock_ledger", lambda: _held("record-leader"))
    decision = claims.decide_claim("camera-config", [FOLLOWER])

    assert decision.detail == (
        "Recording fixes every camera at "
        f"{RECORDING_PROFILE.width}x{RECORDING_PROFILE.height}@{RECORDING_PROFILE.fps}"
    )
