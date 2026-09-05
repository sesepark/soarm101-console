"""기존 데이터셋에 회차를 이어 붙이는 길. 팔 없이 도는 시험만 여기에 둔다.

실물 이어 찍기 한 회는 사람이 옆에 있을 때 한다.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from soarm_console import datasets
from soarm_console.config import Settings
from soarm_console.recording import build_record_config


TASK = "Pick and place"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(datasets, "data_root", lambda: root)
    return root


def _record_one_episode(root, name: str, task: str = TASK):
    """LeRobot이 실제로 쓰는 길로 데이터셋 하나를 만든다.

    파케이를 손으로 짜 넣지 않는 이유가 있다. 이어 찍기가 통과해야 하는 것은 우리가
    적은 모양이 아니라 `LeRobotDataset.resume`과
    `sanity_check_dataset_robot_compatibility`가 기대하는 모양이다. 손으로 지은
    데이터셋은 그 둘을 지나는지 말해 주지 못한다.

    카메라는 넣지 않는다. 영상 인코딩은 이 기계에 `libavdevice`가 없어 돌지 않고
    (`RUNBOOK`), 이어 찍기가 보는 것 — 로봇 종류, fps, feature, 과제 — 은 카메라 없이도
    그대로 시험된다.
    """
    from lerobot.common.control_utils import sanity_check_dataset_robot_compatibility
    from lerobot.datasets import (
        LeRobotDataset,
        aggregate_pipeline_dataset_features,
        create_initial_features,
    )
    from lerobot.processor import make_default_processors
    from lerobot.robots.so_follower import SO101FollowerConfig, SOFollower
    from lerobot.utils.feature_utils import combine_feature_dicts

    robot = SOFollower(
        SO101FollowerConfig(port="/dev/null", id="soarm101_follower", cameras={})
    )
    teleop_pipeline, _, observation_pipeline = make_default_processors()
    features = combine_feature_dicts(
        aggregate_pipeline_dataset_features(
            pipeline=teleop_pipeline,
            initial_features=create_initial_features(action=robot.action_features),
            use_videos=True,
        ),
        aggregate_pipeline_dataset_features(
            pipeline=observation_pipeline,
            initial_features=create_initial_features(observation=robot.observation_features),
            use_videos=True,
        ),
    )
    dataset = LeRobotDataset.create(
        f"local/{name}", 30, root=root / name, robot_type=robot.name, features=features,
        use_videos=True,
    )
    frame = {key: np.zeros(value["shape"], dtype=np.float32) for key, value in features.items()}
    frame["task"] = task
    for _ in range(3):
        dataset.add_frame(dict(frame))
    dataset.save_episode()
    dataset.finalize()
    return robot, features, sanity_check_dataset_robot_compatibility


def test_a_recorded_dataset_can_actually_be_reopened_for_more_episodes(data_root):
    """`record()`의 `if cfg.resume:` 갈래가 지나는 길을 그대로 걸어 본다."""
    from lerobot.datasets import LeRobotDataset

    robot, features, sanity_check = _record_one_episode(data_root, "soarm101_pick")

    resumed = LeRobotDataset.resume("local/soarm101_pick", root=data_root / "soarm101_pick")
    # 이 검사가 로봇 종류·fps·feature를 견준다. 여기서 걸리면 이어 찍기는 시작조차 못 한다.
    sanity_check(resumed, robot, 30, features)

    assert resumed.num_episodes == 1
    # 그리고 이어 찍기 판정이 읽는 과제 목록이 실제로 그 값이다.
    assert datasets.dataset_tasks("soarm101_pick") == [TASK]


def test_the_record_config_asks_lerobot_to_resume_rather_than_create():
    settings = Settings(scene_camera="/dev/v4l/by-path/scene", wrist_camera="/dev/v4l/by-path/wrist")
    assert build_record_config(settings, TASK, "soarm101_pick").resume is False
    assert build_record_config(settings, TASK, "soarm101_pick", resume=True).resume is True


# MARK: 콘솔이 거절하는 것들


@pytest.fixture
def client(data_root, monkeypatch):
    from soarm_console import app as app_module

    started: list[tuple] = []
    monkeypatch.setattr(
        app_module.recorder,
        "start",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )
    monkeypatch.setattr(app_module.teleop, "preflight", lambda: [])
    monkeypatch.setattr(app_module, "run_hardware_doctor", lambda settings: {"healthy": True})
    for worker in app_module.cameras.values():
        monkeypatch.setattr(worker, "stop", lambda: None)
    return TestClient(app_module.app), started


def _body(**overrides) -> dict[str, object]:
    body = {
        "confirmation": "RECORD SOARM101",
        "task": TASK,
        "episodes": 2,
        "episode_seconds": 10,
        "dataset": "soarm101_pick",
        "resume": True,
    }
    body.update(overrides)
    return body


def test_resuming_a_dataset_that_is_not_there_is_not_found(client):
    session, started = client
    response = session.post("/api/recording/start", json=_body(dataset="never_recorded"))
    assert response.status_code == 404
    assert response.json()["detail"].startswith("No such dataset")
    assert started == []


def test_resuming_without_naming_a_dataset_is_not_found(client):
    session, started = client
    assert session.post("/api/recording/start", json=_body(dataset=None)).status_code == 404
    # 이름이 그대로 경로가 되므로, 경로를 벗어나려는 이름도 여기서 멈춘다.
    assert session.post("/api/recording/start", json=_body(dataset="../config")).status_code == 404
    assert started == []


def test_resuming_with_a_different_task_is_refused_and_says_both(client, data_root):
    """데이터셋 하나는 학습 한 번의 단위다.

    LeRobot 자체는 과제가 다른 회차도 같은 폴더에 넣어 주지만, 그렇게 섞인 데이터는
    파케이를 열기 전에는 섞였다는 사실조차 보이지 않는다.
    """
    session, started = client
    _record_one_episode(data_root, "soarm101_pick", task=TASK)

    response = session.post("/api/recording/start", json=_body(task="Wipe the table"))

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("Dataset task does not match")
    assert TASK in detail
    assert "Wipe the table" in detail
    assert started == []


def test_resuming_the_same_task_starts_and_tells_the_child_to_append(client, data_root):
    session, started = client
    _record_one_episode(data_root, "soarm101_pick", task=TASK)

    assert session.post("/api/recording/start", json=_body()).status_code == 200

    (_, kwargs), = started
    assert kwargs["dataset"] == "soarm101_pick"
    assert kwargs["resume"] is True


def test_a_fresh_recording_does_not_need_a_dataset_name(client):
    session, started = client
    assert session.post(
        "/api/recording/start", json=_body(dataset=None, resume=False)
    ).status_code == 200
    (_, kwargs), = started
    assert kwargs["resume"] is False
