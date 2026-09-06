from __future__ import annotations

import dataclasses
import json
import os
import signal
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soarm_console import models, policying, spark
from soarm_console.config import Settings
from soarm_console.datasets import DatasetError
from soarm_console.policy_manager import PolicyManager
from soarm_console.replaying import REPLAY_ALIGNMENT


RUN = "soarm101_pick__smolvla__e315"
STEP = "020000"
HOME = {
    "shoulder_pan": 1.2,
    "shoulder_lift": -30.0,
    "elbow_flex": 40.0,
    "wrist_flex": 10.0,
    "wrist_roll": 0.0,
    "gripper": 8.0,
}
CALIBRATION = {
    "shoulder_pan": {
        "id": 1, "drive_mode": 0, "homing_offset": 0, "range_min": 758, "range_max": 3447
    },
    "shoulder_lift": {
        "id": 2, "drive_mode": 0, "homing_offset": 0, "range_min": 1360, "range_max": 3746
    },
    "elbow_flex": {
        "id": 3, "drive_mode": 0, "homing_offset": 0, "range_min": 996, "range_max": 3200
    },
    "wrist_flex": {
        "id": 4, "drive_mode": 0, "homing_offset": 0, "range_min": 577, "range_max": 2913
    },
    "wrist_roll": {
        "id": 5, "drive_mode": 0, "homing_offset": 0, "range_min": 0, "range_max": 4095
    },
    "gripper": {
        "id": 6, "drive_mode": 0, "homing_offset": 0, "range_min": 1656, "range_max": 3100
    },
}


@pytest.fixture
def model_root(tmp_path, monkeypatch):
    root = tmp_path / "models"
    monkeypatch.setattr(models, "models_root", lambda: root)
    return root


def _received_model(root: Path, *, policy: str = "smolvla", state_dim: int = 6) -> Path:
    pretrained = root / RUN / STEP / "pretrained_model"
    pretrained.mkdir(parents=True)
    config = {
        "type": policy,
        "input_features": {
            "observation.state": {"type": "STATE", "shape": [state_dim]},
            "observation.images.camera1": {"type": "VISUAL", "shape": [3, 256, 256]},
            "observation.images.camera2": {"type": "VISUAL", "shape": [3, 256, 256]},
            "observation.images.camera3": {"type": "VISUAL", "shape": [3, 256, 256]},
        },
        "output_features": {"action": {"type": "ACTION", "shape": [6]}},
        "device": "cpu",
        "chunk_size": 50,
        "n_action_steps": 50,
        "empty_cameras": 0,
        "max_state_dim": 32,
        "max_action_dim": 32,
        "resize_imgs_with_padding": [512, 512],
        "normalization_mapping": {"VISUAL": "IDENTITY", "STATE": "MEAN_STD", "ACTION": "MEAN_STD"},
        "vlm_model_name": "HuggingFaceTB/SmolVLM2-500M-Video-Instruct",
    }
    train = {"dataset": {"repo_id": "soarm101_pick"}, "steps": 20_000}
    (pretrained / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (pretrained / "train_config.json").write_text(json.dumps(train), encoding="utf-8")
    (pretrained / "model.safetensors").write_bytes(b"weights")
    return pretrained


def _settings(**overrides) -> Settings:
    values = {
        "spark_user": "operator",
        "spark_host": "spark-box",
        "spark_output_root": "outputs",
        "scene_camera": "/dev/scene",
        "wrist_camera": "/dev/wrist",
        "follower_port": "/dev/follower",
        "policy_max_relative_target": 3.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def policy_settings(tmp_path, monkeypatch):
    root = tmp_path / "calibration"
    path = root / "robots/so_follower/soarm101_follower.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(CALIBRATION), encoding="utf-8")
    monkeypatch.setenv("HF_LEROBOT_CALIBRATION", str(root))
    return _settings()


def test_manifest_is_derived_from_the_two_received_configs(model_root):
    pretrained = _received_model(model_root)

    manifest = models.build_manifest(_settings(), RUN, STEP, pulled_at=123.0)

    assert manifest == {
        "run": RUN,
        "step": STEP,
        "policy": "smolvla",
        "dataset": "soarm101_pick",
        "trained_steps": 20_000,
        "chunk_size": 50,
        "n_action_steps": 50,
        "image_features": [
            "observation.images.camera1",
            "observation.images.camera2",
            "observation.images.camera3",
        ],
        "state_dim": 6,
        "action_dim": 6,
        "pulled_at": 123.0,
        "source": f"operator@spark-box:outputs/{RUN}/checkpoints/{STEP}/pretrained_model",
        "bytes": sum(path.stat().st_size for path in pretrained.iterdir()),
    }
    assert json.loads((pretrained.parent / models.MANIFEST_NAME).read_text()) == manifest


def test_camera_map_fills_this_rigs_two_roles_in_model_order():
    assert models.camera_map(["camera.a", "camera.b", "camera.c"]) == {
        "camera.a": "scene",
        "camera.b": "wrist",
    }


def test_unrunnable_model_always_explains_why(model_root):
    _received_model(model_root, state_dim=7)
    models.build_manifest(_settings(), RUN, STEP)

    result = models.describe_model(RUN, STEP)

    assert result["runnable"] is False
    assert result["problems"]
    assert any("state dimension" in problem for problem in result["problems"])


def test_model_list_and_delete_report_the_local_copy(model_root):
    _received_model(model_root)
    models.build_manifest(_settings(), RUN, STEP, pulled_at=123.0)

    (listed,) = models.list_models()
    assert listed["run"] == RUN
    assert listed["runnable"] is True
    assert listed["camera_map"] == {
        "observation.images.camera1": "scene",
        "observation.images.camera2": "wrist",
    }

    deleted = models.delete_model(RUN, STEP)
    assert deleted["freed_bytes"] > 0
    assert models.list_models() == []


@pytest.mark.parametrize("run,step", [("../escape", STEP), (RUN, "../escape")])
def test_model_paths_reject_traversal(model_root, run, step):
    with pytest.raises(DatasetError):
        models.delete_model(run, step)


def test_model_paths_reject_symbolic_links(model_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    model_root.mkdir()
    os.symlink(outside, model_root / RUN)

    with pytest.raises(DatasetError, match="symbolic links"):
        models.delete_model(RUN, STEP)
    assert outside.exists()


def test_checkpoint_pull_targets_models_pretrained_model(model_root, monkeypatch):
    calls = []
    monkeypatch.setattr(spark, "_run", lambda args, timeout, stdin=None: calls.append(args) or "")

    result = spark.pull_checkpoint(_settings(), RUN, STEP)

    assert result["path"] == str(model_root / RUN / STEP / "pretrained_model")
    assert calls[0][-1] == f"{model_root}/{RUN}/{STEP}/pretrained_model/"


def test_checkpoint_pull_rejects_a_pretrained_model_symlink(model_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    step = model_root / RUN / STEP
    step.mkdir(parents=True)
    os.symlink(outside, step / "pretrained_model")

    with pytest.raises(DatasetError, match="symbolic links"):
        spark.pull_checkpoint(_settings(), RUN, STEP)

    assert list(outside.iterdir()) == []


def test_rollout_config_uses_rtc_duration_camera_rename_and_policy_limit(model_root):
    _received_model(model_root)
    models.build_manifest(_settings(), RUN, STEP)

    config = policying.build_rollout_config(_settings(), RUN, STEP, "Pick up block", 30, 137)

    assert config.strategy.type == "base"
    assert config.inference.type == "rtc"
    assert config.duration == 137
    assert config.return_to_initial_position is False
    assert config.robot.max_relative_target == 3.0
    assert config.rename_map == {
        "observation.images.scene": "observation.images.camera1",
        "observation.images.wrist": "observation.images.camera2",
    }


def test_policy_home_is_validated_in_follower_units_and_calibration(policy_settings):
    assert policying.validate_home(policy_settings, HOME) == HOME

    with pytest.raises(ValueError, match=r"unknown joints.*mystery"):
        policying.validate_home(policy_settings, {**HOME, "mystery": 1.0})
    with pytest.raises(ValueError, match=r"elbow_flex 400.*outside calibration range"):
        policying.validate_home(policy_settings, {**HOME, "elbow_flex": 400.0})


def test_policy_home_alignment_uses_replay_s_curve_and_exact_goal(policy_settings, monkeypatch):
    class Bus:
        readings = 0

        def sync_read(self, register, num_retry=0):
            assert register == "Present_Position"
            self.readings += 1
            return {name: 0.0 for name in HOME} if self.readings == 1 else HOME

    class Robot:
        bus = Bus()

        def disconnect(self):
            pass

    received = {}
    monkeypatch.setattr(policying, "_connect", lambda settings: Robot())

    def fake_align(robot, start, goal, *, should_stop, limits, progress):
        received.update(start=start, goal=goal, limits=limits)
        progress(0, 1, 2.0)
        return False

    monkeypatch.setattr(policying, "align", fake_align)
    monkeypatch.setattr(policying, "_write_status", lambda **values: None)

    assert policying.align_home(policy_settings, HOME, threading.Event()) is False
    assert received["goal"] == HOME
    assert received["limits"] is REPLAY_ALIGNMENT


def test_policy_without_home_skips_alignment_and_hardware(policy_settings, monkeypatch):
    monkeypatch.setattr(
        policying,
        "_connect",
        lambda settings: pytest.fail("home 없는 정책은 정렬용 팔 연결을 열면 안 된다"),
    )

    assert policying.align_home(policy_settings, None, threading.Event()) is False


def test_policy_home_alignment_retries_until_measured_arrival(policy_settings, monkeypatch):
    positions = [
        {name: 0.0 for name in HOME},
        {**HOME, "shoulder_lift": HOME["shoulder_lift"] + 5.0},
        HOME,
    ]

    class Bus:
        def sync_read(self, register, num_retry=0):
            return positions.pop(0)

    class Robot:
        bus = Bus()

        def disconnect(self):
            pass

    starts = []
    monkeypatch.setattr(policying, "_connect", lambda settings: Robot())
    monkeypatch.setattr(
        policying,
        "align",
        lambda robot, start, goal, **kwargs: starts.append(dict(start)) or False,
    )
    monkeypatch.setattr(policying, "_write_status", lambda **values: None)

    assert policying.align_home(policy_settings, HOME, threading.Event()) is False
    assert starts == [
        {name: 0.0 for name in HOME},
        {**HOME, "shoulder_lift": HOME["shoulder_lift"] + 5.0},
    ]


def test_policy_home_alignment_stop_holds_at_current_position(policy_settings, monkeypatch):
    disconnected = []
    stop_requested = threading.Event()
    stop_requested.set()

    class Bus:
        def sync_read(self, register, num_retry=0):
            return {name: 0.0 for name in HOME}

    class Robot:
        bus = Bus()

        def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(policying, "_connect", lambda settings: Robot())
    monkeypatch.setattr(policying, "align", lambda *args, **kwargs: True)

    assert policying.align_home(policy_settings, HOME, stop_requested, phase="returning") is True
    assert disconnected == [True]


def test_policy_home_alignment_reports_arrival_timeout(policy_settings, monkeypatch):
    class Bus:
        def sync_read(self, register, num_retry=0):
            return {name: 0.0 for name in HOME}

    class Robot:
        bus = Bus()

        def disconnect(self):
            pass

    times = iter([0.0, policying.ALIGNMENT_ARRIVAL_TIMEOUT_S + 1.0])
    monkeypatch.setattr(policying, "_connect", lambda settings: Robot())
    monkeypatch.setattr(policying.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        policying,
        "align",
        lambda *args, **kwargs: kwargs["should_stop"](),
    )
    monkeypatch.setattr(policying, "_write_status", lambda **values: None)

    with pytest.raises(policying.ReplayError, match="did not reach its target within 30s"):
        policying.align_home(policy_settings, HOME, threading.Event(), phase="returning")


@pytest.fixture
def client(model_root, monkeypatch):
    from soarm_console import app as app_module

    monkeypatch.setenv("SOARM_MOTION_TOKEN", "secret")
    monkeypatch.setattr(
        app_module, "settings", dataclasses.replace(app_module.settings, motion_enabled=False)
    )
    return TestClient(app_module.app)


def _policy_body(**overrides):
    body = {"run": RUN, "step": STEP, "task": "Pick up block", "fps": 30, "max_seconds": 120}
    body.update(overrides)
    return body


def test_policy_start_requires_the_motion_token_before_model_lookup(client):
    response = client.post("/api/policy/start", json=_policy_body())
    assert response.status_code == 401


def test_policy_start_rejects_unknown_home_joint_as_400(client):
    response = client.post(
        "/api/policy/start",
        json=_policy_body(home={**HOME, "mystery": 1.0}),
        headers={"X-SOARM-Motion-Token": "secret"},
    )

    assert response.status_code == 400
    assert "mystery" in response.json()["detail"]


def test_policy_start_rejects_home_outside_calibration_as_400(client, policy_settings):
    response = client.post(
        "/api/policy/start",
        json=_policy_body(home={**HOME, "elbow_flex": 400.0}),
        headers={"X-SOARM-Motion-Token": "secret"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "elbow_flex" in detail
    assert "400" in detail
    assert "outside calibration range" in detail


def test_policy_status_exposes_used_home_and_phase(tmp_path):
    manager = PolicyManager(_settings())
    manager.runtime_dir = tmp_path
    manager._home = dict(HOME)
    manager._phase = "aligning"

    result = manager.status()

    assert result["home"] == HOME
    assert result["phase"] == "aligning"

    manager._phase = "returning"
    manager._process = type("Running", (), {"poll": lambda self: None})()
    returning = manager.status()
    assert returning["phase"] == "returning"
    assert returning["running"] is True


@pytest.mark.parametrize("requested_home", [HOME, None])
def test_policy_main_returns_to_requested_or_captured_home(
    policy_settings, monkeypatch, requested_home
):
    captured = {**HOME, "shoulder_pan": 9.0}
    phases = []
    alignments = []

    monkeypatch.setattr(
        policying,
        "Settings",
        lambda: dataclasses.replace(
            policy_settings, motion_enabled=True, camera_roles_confirmed=True
        ),
    )
    monkeypatch.setattr(policying, "validate_calibration", lambda path: None)
    monkeypatch.setattr(policying, "inherited_locks_cover", lambda devices: True)
    monkeypatch.setattr(policying, "build_rollout_config", lambda *args: object())
    monkeypatch.setattr(
        policying,
        "present_position",
        lambda settings, acquire_owner_lock: captured,
    )
    monkeypatch.setattr(
        policying,
        "align_home",
        lambda settings, goal, event, *, phase: alignments.append((phase, goal)) or False,
    )
    monkeypatch.setattr(
        policying,
        "_write_status",
        lambda **values: phases.append(values["phase"]),
    )
    from lerobot.scripts import lerobot_rollout

    monkeypatch.setattr(lerobot_rollout, "rollout", lambda config: None)
    monkeypatch.setenv("SOARM_POLICY_RUN", RUN)
    monkeypatch.setenv("SOARM_POLICY_STEP", STEP)
    monkeypatch.setenv("SOARM_POLICY_TASK", "Pick up block")
    monkeypatch.setenv("SOARM_POLICY_HOME", json.dumps(requested_home) if requested_home else "")

    policying.main()

    assert list(dict.fromkeys(phases)) == (
        ["aligning", "running", "returning"]
        if requested_home
        else ["running", "returning"]
    )
    assert alignments[-1] == ("returning", requested_home or captured)


def test_policy_main_records_return_failure(policy_settings, monkeypatch):
    statuses = []

    monkeypatch.setattr(
        policying,
        "Settings",
        lambda: dataclasses.replace(
            policy_settings, motion_enabled=True, camera_roles_confirmed=True
        ),
    )
    monkeypatch.setattr(policying, "validate_calibration", lambda path: None)
    monkeypatch.setattr(policying, "inherited_locks_cover", lambda devices: True)
    monkeypatch.setattr(policying, "build_rollout_config", lambda *args: object())
    monkeypatch.setattr(policying, "_write_status", lambda **values: statuses.append(values))

    def alignment(settings, goal, event, *, phase):
        if phase == "returning":
            raise policying.ReplayError("arrival timed out")
        return False

    monkeypatch.setattr(policying, "align_home", alignment)
    from lerobot.scripts import lerobot_rollout

    monkeypatch.setattr(lerobot_rollout, "rollout", lambda config: None)
    monkeypatch.setenv("SOARM_POLICY_RUN", RUN)
    monkeypatch.setenv("SOARM_POLICY_STEP", STEP)
    monkeypatch.setenv("SOARM_POLICY_TASK", "Pick up block")
    monkeypatch.setenv("SOARM_POLICY_HOME", json.dumps(HOME))

    with pytest.raises(RuntimeError, match="return to initial position failed"):
        policying.main()

    assert statuses[-1]["phase"] == "returning"
    assert "return to initial position failed" in statuses[-1]["error"]


def test_sigterm_during_policy_return_stops_alignment(policy_settings, monkeypatch):
    return_was_stopped = []

    monkeypatch.setattr(
        policying,
        "Settings",
        lambda: dataclasses.replace(
            policy_settings, motion_enabled=True, camera_roles_confirmed=True
        ),
    )
    monkeypatch.setattr(policying, "validate_calibration", lambda path: None)
    monkeypatch.setattr(policying, "inherited_locks_cover", lambda devices: True)
    monkeypatch.setattr(policying, "build_rollout_config", lambda *args: object())
    monkeypatch.setattr(policying, "_write_status", lambda **values: None)

    def alignment(settings, goal, event, *, phase):
        if phase == "returning":
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            return_was_stopped.append(event.is_set())
            return True
        return False

    monkeypatch.setattr(policying, "align_home", alignment)
    from lerobot.scripts import lerobot_rollout

    monkeypatch.setattr(lerobot_rollout, "rollout", lambda config: None)
    monkeypatch.setenv("SOARM_POLICY_RUN", RUN)
    monkeypatch.setenv("SOARM_POLICY_STEP", STEP)
    monkeypatch.setenv("SOARM_POLICY_TASK", "Pick up block")
    monkeypatch.setenv("SOARM_POLICY_HOME", json.dumps(HOME))

    policying.main()

    assert return_was_stopped == [True]


def test_policy_start_refuses_a_missing_model(client):
    response = client.post(
        "/api/policy/start", json=_policy_body(), headers={"X-SOARM-Motion-Token": "secret"}
    )
    assert response.status_code == 404


def test_policy_start_refuses_the_motion_gate_without_starting(client, model_root, monkeypatch):
    from soarm_console import app as app_module

    _received_model(model_root)
    models.build_manifest(_settings(), RUN, STEP)
    started = []
    monkeypatch.setattr(app_module.policy_manager, "start", lambda *args: started.append(args))

    response = client.post(
        "/api/policy/start", json=_policy_body(), headers={"X-SOARM-Motion-Token": "secret"}
    )

    assert response.status_code == 400
    assert "SOARM_ENABLE_MOTION" in response.json()["detail"]
    assert started == []


def test_policy_start_returns_the_reasons_for_an_unrunnable_model(client, model_root):
    _received_model(model_root, state_dim=7)
    models.build_manifest(_settings(), RUN, STEP)

    response = client.post(
        "/api/policy/start", json=_policy_body(), headers={"X-SOARM-Motion-Token": "secret"}
    )

    assert response.status_code == 400
    assert "state dimension" in response.json()["detail"]


def test_policy_start_refuses_another_running_mode(client, model_root, monkeypatch):
    from soarm_console import app as app_module

    _received_model(model_root)
    models.build_manifest(_settings(), RUN, STEP)
    monkeypatch.setattr(app_module, "settings", dataclasses.replace(app_module.settings, motion_enabled=True))
    monkeypatch.setattr(app_module.teleop, "_process", type("Running", (), {"poll": lambda self: None})())

    response = client.post(
        "/api/policy/start", json=_policy_body(), headers={"X-SOARM-Motion-Token": "secret"}
    )
    assert response.status_code == 409


def test_teleoperation_refuses_while_policy_is_marked_running(monkeypatch):
    from soarm_console import app as app_module

    monkeypatch.setattr(
        app_module.policy_manager, "_process", type("Running", (), {"poll": lambda self: None})()
    )
    response = TestClient(app_module.app).post(
        "/api/teleoperation/start", json={"confirmation": "START SOARM101"}
    )
    assert response.status_code == 409


def test_model_rest_contract_lists_and_deletes(client, model_root):
    _received_model(model_root)
    models.build_manifest(_settings(), RUN, STEP)

    listed = client.get("/api/models")
    deleted = client.delete(f"/api/models/{RUN}/{STEP}")

    assert listed.status_code == 200
    assert listed.json()[0]["run"] == RUN
    assert deleted.status_code == 200
    assert deleted.json()["freed_bytes"] > 0


def test_model_pull_rest_contract_writes_manifest_and_returns_one_row(client, model_root, monkeypatch):
    from soarm_console import app as app_module

    monkeypatch.setattr(
        app_module,
        "spark_pull_checkpoint",
        lambda settings, run, step: _received_model(model_root),
    )

    response = client.post(f"/api/models/{RUN}/{STEP}")

    assert response.status_code == 200
    assert response.json()["camera_map"] == {
        "observation.images.camera1": "scene",
        "observation.images.camera2": "wrist",
    }
    assert response.json()["runnable"] is True
    assert (model_root / RUN / STEP / models.MANIFEST_NAME).is_file()


def test_policy_stop_uses_sigterm(monkeypatch):
    manager = PolicyManager(_settings())

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout):
            return 0

    manager._process = Process()
    signals = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    manager.stop()

    import signal

    assert signals == [(123, signal.SIGTERM)]


def test_mode_stop_stops_policy_before_every_other_process(monkeypatch):
    from soarm_console import app as app_module

    class Running:
        pid = 123

        def poll(self):
            return None

    order = []
    for name, manager in (
        ("policy", app_module.policy_manager),
        ("replay", app_module.replayer),
        ("record", app_module.recorder),
        ("teleop", app_module.teleop),
    ):
        monkeypatch.setattr(manager, "_process", Running())
        monkeypatch.setattr(manager, "stop", lambda name=name: order.append(name))

    response = TestClient(app_module.app).post("/api/mode/stop")

    assert response.status_code == 200
    assert order == ["policy", "replay", "record", "teleop"]


def test_removed_spark_run_routes_are_not_registered():
    from soarm_console.app import app

    methods = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    assert ("/api/spark/runs", "GET") not in methods
    assert ("/api/spark/runs/{run}/{step}", "POST") not in methods
    assert ("/api/spark/runs/{run}/stop", "POST") in methods
