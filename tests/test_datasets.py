from __future__ import annotations

import json
from pathlib import Path

import pytest

from soarm_console import datasets


def _make_dataset(root: Path, name: str, keys=("observation.images.scene",)) -> Path:
    directory = root / name
    (directory / "meta").mkdir(parents=True)
    features = {key: {"dtype": "video", "shape": [240, 320, 3]} for key in keys}
    features["observation.state"] = {"dtype": "float32", "shape": [6]}
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


def test_episode_urls_carry_their_own_range(data_root):
    _make_dataset(data_root, "soarm101_pick")
    row = {
        "episode_index": 0, "tasks": ["집기"], "length": 20,
        "videos/observation.images.scene/chunk_index": 0,
        "videos/observation.images.scene/file_index": 0,
        "videos/observation.images.scene/from_timestamp": 2.0,
        "videos/observation.images.scene/to_timestamp": 4.0,
    }
    import soarm_console.datasets as module
    module._episode_rows = lambda directory: [row]

    detail = datasets.describe("soarm101_pick")

    url = detail["episodes_detail"][0]["videos"]["observation.images.scene"]["url"]
    assert url.endswith("?from=2.000&to=4.000")


def test_unknown_dataset_reports_not_found(data_root):
    with pytest.raises(FileNotFoundError):
        datasets.describe("never_recorded")
