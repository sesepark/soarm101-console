from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soarm_console import models, policying, spark
from soarm_console.config import Settings
from soarm_console.datasets import DatasetError
from soarm_console.policy_manager import PolicyManager


RUN = "soarm101_pick__smolvla__e315"
STEP = "020000"


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
    assert config.return_to_initial_position is True
    assert config.robot.max_relative_target == 3.0
    assert config.rename_map == {
        "observation.images.scene": "observation.images.camera1",
        "observation.images.wrist": "observation.images.camera2",
    }


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
