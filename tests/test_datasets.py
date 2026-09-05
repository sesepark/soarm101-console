from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from soarm_console import datasets


def _make_dataset(root: Path, name: str, keys=("observation.images.scene",)) -> Path:
    directory = root / name
    (directory / "meta").mkdir(parents=True)
    features = {key: {"dtype": "video", "shape": [240, 320, 3]} for key in keys}
    joints = [
        "wrist_roll.pos",
        "shoulder_pan.pos",
        "elbow_flex.pos",
        "shoulder_lift.pos",
        "gripper.pos",
        "wrist_flex.pos",
    ]
    features["observation.state"] = {
        "dtype": "float32",
        "shape": [6],
        "names": joints,
    }
    features["action"] = {"dtype": "float32", "shape": [6], "names": joints}
    (directory / "meta/info.json").write_text(
        json.dumps({
            "codebase_version": "v3.0",
            "fps": 10,
            "total_episodes": 2,
            "total_frames": 40,
            "robot_type": "so101_follower",
            "features": features,
        }),
        encoding="utf-8",
    )
    for key in keys:
        video = directory / "videos" / key / "chunk-000"
        video.mkdir(parents=True)
        (video / "file-000.mp4").write_bytes(b"not really an mp4")
    return directory


def _write_episode(directory: Path, episode_index: int, state: list[list[float]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    action = [[value + 0.5 for value in frame] for frame in state]
    meta = directory / "meta/episodes/chunk-000/file-000.parquet"
    meta.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "episode_index": [episode_index],
            "length": [len(state)],
            "data/chunk_index": [0],
            "data/file_index": [0],
        }),
        meta,
    )
    data = directory / "data/chunk-000/file-000.parquet"
    data.parent.mkdir(parents=True, exist_ok=True)
    # Store in reverse order so the endpoint has to honor frame_index.
    pq.write_table(
        pa.table({
            "episode_index": [episode_index] * len(state),
            "frame_index": list(reversed(range(len(state)))),
            "observation.state": list(reversed(state)),
            "action": list(reversed(action)),
        }),
        data,
    )


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(datasets, "data_root", lambda: root)
    return root


def test_lists_only_real_datasets(data_root):
    _make_dataset(data_root, "soarm101_pick")
    (data_root / "README.md").write_text("not a dataset", encoding="utf-8")
    (data_root / "half_written").mkdir()

    listed = datasets.list_datasets()

    assert [entry["name"] for entry in listed] == ["soarm101_pick"]
    assert listed[0]["episodes"] == 2
    assert listed[0]["cameras"] == ["observation.images.scene"]
    assert listed[0]["size_bytes"] > 0


def test_missing_data_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "data_root", lambda: tmp_path / "absent")
    assert datasets.list_datasets() == []


@pytest.mark.parametrize("name", ["../config", "a/b", "", ".", "-leading", "x" * 200])
def test_refuses_names_that_could_leave_the_data_directory(data_root, name):
    with pytest.raises((datasets.DatasetError, FileNotFoundError)):
        datasets.summarize(name)


def test_refuses_a_dataset_symlinked_out_of_the_data_directory(data_root, tmp_path):
    outside = tmp_path / "outside"
    _make_dataset(tmp_path, "outside")
    (data_root / "escape").symlink_to(outside)
    with pytest.raises(datasets.DatasetError):
        datasets.summarize("escape")


def test_video_file_resolves_and_checks_the_camera(data_root):
    _make_dataset(data_root, "soarm101_pick")

    path = datasets.video_file("soarm101_pick", "observation.images.scene", 0, 0)
    assert path.name == "file-000.mp4"

    with pytest.raises(datasets.DatasetError):
        datasets.video_file("soarm101_pick", "observation.images.nope", 0, 0)
    with pytest.raises(datasets.DatasetError):
        datasets.video_file("soarm101_pick", "../../etc/passwd", 0, 0)
    with pytest.raises(FileNotFoundError):
        datasets.video_file("soarm101_pick", "observation.images.scene", 0, 7)


def test_serves_a_playable_recording_untouched(data_root, monkeypatch):
    _make_dataset(data_root, "soarm101_pick")
    monkeypatch.setattr(datasets, "source_codec", lambda path: "h264")

    served = datasets.playable_clip("soarm101_pick", "observation.images.scene", 0, 0)

    # Already playable and the whole file was asked for: hand back the recording
    # itself rather than making a copy of it.
    assert served.name == "file-000.mp4"
    assert "runtime/clips" not in str(served)


def test_reencodes_only_when_the_client_could_not_play_it(data_root, monkeypatch):
    _make_dataset(data_root, "soarm101_pick")
    monkeypatch.setattr(datasets, "source_codec", lambda path: "av1")
    calls = []

    def fake_transcode(source, target, start, end):
        calls.append((start, end))
        target.write_bytes(b"h264")

    monkeypatch.setattr(datasets, "_transcode", fake_transcode)

    first = datasets.playable_clip("soarm101_pick", "observation.images.scene", 0, 0, 2.0, 4.0)
    second = datasets.playable_clip("soarm101_pick", "observation.images.scene", 0, 0, 2.0, 4.0)

    assert first == second
    # The same episode is only ever encoded once.
    assert calls == [(2.0, 4.0)]
    # A different episode of the same file is a different clip.
    other = datasets.playable_clip("soarm101_pick", "observation.images.scene", 0, 0, 4.0, 6.0)
    assert other != first
    assert len(calls) == 2


def test_trajectory_endpoint_returns_frame_order_and_metadata_joint_order(data_root):
    from fastapi.testclient import TestClient

    from soarm_console.app import app

    directory = _make_dataset(data_root, "soarm101_pick")
    state = [
        [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        [20.0, 21.0, 22.0, 23.0, 24.0, 25.0],
    ]
    _write_episode(directory, 1, state)

    response = TestClient(app).get("/api/datasets/soarm101_pick/episodes/1/trajectory")

    assert response.status_code == 200
    payload = response.json()
    assert payload["fps"] == 10
    assert payload["frames"] == 3
    assert payload["joints"] == [
        "wrist_roll.pos",
        "shoulder_pan.pos",
        "elbow_flex.pos",
        "shoulder_lift.pos",
        "gripper.pos",
        "wrist_flex.pos",
    ]
    assert payload["state"] == state
    assert payload["action"] == [[value + 0.5 for value in frame] for frame in state]


def test_trajectory_endpoint_reports_missing_dataset_and_episode(data_root):
    from fastapi.testclient import TestClient

    from soarm_console.app import app

    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[0.0] * 6])
    client = TestClient(app)

    assert client.get("/api/datasets/never_recorded/episodes/0/trajectory").status_code == 404
    assert client.get("/api/datasets/soarm101_pick/episodes/7/trajectory").status_code == 404


def test_trajectory_endpoint_rejects_an_episode_over_the_frame_limit(data_root, monkeypatch):
    from fastapi.testclient import TestClient

    from soarm_console.app import app

    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[0.0] * 6, [1.0] * 6])
    monkeypatch.setattr(datasets, "TRAJECTORY_FRAME_LIMIT", 1)

    response = TestClient(app).get("/api/datasets/soarm101_pick/episodes/0/trajectory")

    assert response.status_code == 413


def test_episode_urls_carry_their_own_range(data_root, monkeypatch):
    _make_dataset(data_root, "soarm101_pick")
    row = {
        "episode_index": 0, "tasks": ["집기"], "length": 20,
        "videos/observation.images.scene/chunk_index": 0,
        "videos/observation.images.scene/file_index": 0,
        "videos/observation.images.scene/from_timestamp": 2.0,
        "videos/observation.images.scene/to_timestamp": 4.0,
    }
    import soarm_console.datasets as module
    monkeypatch.setattr(module, "_episode_rows", lambda directory: [row])

    detail = datasets.describe("soarm101_pick")

    url = detail["episodes_detail"][0]["videos"]["observation.images.scene"]["url"]
    assert url.endswith("?from=2.000&to=4.000")


def test_unknown_dataset_reports_not_found(data_root):
    with pytest.raises(FileNotFoundError):
        datasets.describe("never_recorded")


# MARK: 목록에 실리는 것들


def _write_tasks(directory: Path, tasks: list[str]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table({"task_index": list(range(len(tasks))), "task": tasks}),
        directory / "meta/tasks.parquet",
    )


def test_the_listing_carries_the_task_and_the_quality_the_console_measured(data_root):
    """앱이 데이터셋마다 상세를 한 번씩 더 묻지 않게 한다.

    품질은 데이터셋 자신이 말하지 못하는 것이다 — `timestamp`가 `frame_index / fps`로
    합성된 값이라, 루프가 느렸어도 파케이는 30Hz라고 적혀 있다.
    """
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_tasks(directory, ["Pick and place"])
    (directory / "soarm_quality.json").write_text(
        json.dumps({
            "loop_hz": 29.4,
            "camera_stale_pct": {"scene": 0.0, "wrist": 12.5},
            "slow_loop_warnings": 3,
            "recorded_at": 1.0,
        }),
        encoding="utf-8",
    )

    (listed,) = datasets.list_datasets()

    assert listed["tasks"] == ["Pick and place"]
    assert listed["quality"]["loop_hz"] == 29.4
    assert listed["quality"]["slow_loop_warnings"] == 3
    assert listed["quality"]["camera_stale_pct"]["wrist"] == 12.5


def test_a_dataset_without_a_quality_file_says_so_rather_than_guessing(data_root):
    _make_dataset(data_root, "soarm101_pick")
    (listed,) = datasets.list_datasets()
    assert listed["quality"] is None
    assert listed["tasks"] == []


def test_tasks_are_deduplicated_and_sorted(data_root):
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_tasks(directory, ["Wipe", "Pick", "Wipe"])
    assert datasets.dataset_tasks("soarm101_pick") == ["Pick", "Wipe"]


# MARK: 지우기


def test_deleting_a_dataset_moves_it_out_of_the_listing_but_not_off_the_disk(data_root):
    """몇 시간짜리 시연을 담은 폴더를 웹 요청 하나가 영구히 없애도 되는 이유가 없다."""
    _make_dataset(data_root, "soarm101_pick")

    moved = datasets.move_to_trash("soarm101_pick")

    assert datasets.list_datasets() == []
    assert moved.is_dir()
    assert moved.parent == data_root / ".trash"
    assert (moved / "meta/info.json").exists()
    # `.trash`는 점으로 시작하므로 목록의 이름 규칙에 걸리지 않는다.
    assert not datasets.NAME_PATTERN.match(".trash")


def test_two_deletions_of_the_same_name_do_not_collide(data_root, monkeypatch):
    _make_dataset(data_root, "soarm101_pick")
    first = datasets.move_to_trash("soarm101_pick")
    _make_dataset(data_root, "soarm101_pick")
    monkeypatch.setattr(datasets, "_trash_target", lambda name: datasets.trash_root() / "later")
    second = datasets.move_to_trash("soarm101_pick")
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_deleting_a_dataset_that_is_not_there_is_not_found(data_root):
    with pytest.raises(FileNotFoundError):
        datasets.move_to_trash("never_recorded")
    with pytest.raises(datasets.DatasetError):
        datasets.move_to_trash("../config")


def test_deleting_an_episode_that_is_not_in_the_dataset_is_not_found(data_root):
    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[0.0] * 6, [1.0] * 6])

    with pytest.raises(FileNotFoundError):
        datasets.delete_episode("soarm101_pick", 7)


def test_a_failed_episode_edit_puts_the_dataset_back_where_it_was(data_root, monkeypatch):
    """제자리 편집은 원본을 `<name>_old`로 먼저 옮긴다. 편집이 실패해도 데이터셋이
    사라진 채로 남으면 안 된다."""
    import subprocess

    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[0.0] * 6])

    def fake_run(args, **kwargs):
        # LeRobot이 원본을 옮겨 둔 뒤 실패하는 자리를 흉내낸다.
        shutil.move(str(directory), str(directory.with_name("soarm101_pick_old")))
        return subprocess.CompletedProcess(args, 1, "", "RuntimeError: something went wrong")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(datasets.DatasetError):
        datasets.delete_episode("soarm101_pick", 0)

    assert (data_root / "soarm101_pick" / "meta/info.json").exists()
    assert not (data_root / "soarm101_pick_old").exists()


def test_the_backup_the_editor_leaves_behind_does_not_become_a_second_dataset(
    data_root, monkeypatch
):
    """`<name>_old`는 `NAME_PATTERN`에 맞는 이름이라, 그냥 두면 목록에 데이터셋이 하나 더
    생긴다. 실제로 `lerobot_edit_dataset.get_output_path`가 그 자리에 만든다."""
    import subprocess

    directory = _make_dataset(data_root, "soarm101_pick")
    _write_episode(directory, 0, [[0.0] * 6])

    def fake_run(args, **kwargs):
        shutil.copytree(directory, directory.with_name("soarm101_pick_old"))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    datasets.delete_episode("soarm101_pick", 0)

    assert [entry["name"] for entry in datasets.list_datasets()] == ["soarm101_pick"]
    assert not (data_root / "soarm101_pick_old").exists()
    assert any(path.name.startswith("soarm101_pick_old-") for path in datasets.trash_root().iterdir())
