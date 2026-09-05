from __future__ import annotations

import hashlib
import json
import re
import shutil
from math import isfinite
from pathlib import Path
from typing import Any


# LeRobot 0.6 writes AV1. Apple Silicon has no AV1 decoder and AVFoundation will
# not decode it in software, so a Mac client shows a black frame for video it can
# otherwise download perfectly. Rather than change how datasets are recorded — the
# dataset should stay exactly what LeRobot wrote — the console re-encodes the one
# episode being watched into H.264 and caches it.
PLAYABLE_CODECS = {"h264", "hevc"}
CLIP_CACHE_LIMIT = 200
TRAJECTORY_FRAME_LIMIT = 20_000


# `recording.py`가 데이터셋 이름에 허용하는 것과 같은 문자만 받는다. 이름이 그대로 경로가
# 되므로, 여기서 막지 않으면 `..`이 섞인 이름 하나로 저장소 바깥을 읽을 수 있다.
NAME_PATTERN = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
# video key는 LeRobot이 정하는 feature 이름이다(`observation.images.scene`).
VIDEO_KEY_PATTERN = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}\Z")


class DatasetError(ValueError):
    pass


class TrajectoryTooLargeError(DatasetError):
    pass


def data_root() -> Path:
    return Path(__file__).parents[2] / "data"


def _dataset_dir(name: str) -> Path:
    if not NAME_PATTERN.match(name):
        raise DatasetError("Unknown dataset name")
    directory = data_root() / name
    # 이름 검사를 통과했더라도 실제 경로가 data/ 안에 있는지 한 번 더 본다. symlink로 바깥을
    # 가리키는 디렉터리가 있으면 이름만으로는 알 수 없다.
    resolved = directory.resolve()
    if not resolved.is_relative_to(data_root().resolve()):
        raise DatasetError("Unknown dataset name")
    if not (resolved / "meta/info.json").exists():
        raise FileNotFoundError(name)
    return resolved


def _read_info(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "meta/info.json").read_text(encoding="utf-8"))


def video_keys(info: dict[str, Any]) -> list[str]:
    features = info.get("features") or {}
    return sorted(key for key, value in features.items() if value.get("dtype") == "video")


def _directory_size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def _episode_rows(directory: Path) -> list[dict[str, Any]]:
    """`meta/episodes/**.parquet`를 읽어 에피소드별 한 줄씩.

    LeRobot v3는 에피소드 메타데이터를 parquet으로 쓴다. 읽는 쪽을 서버에 두는 이유가
    이것이다 — 형식을 아는 라이브러리가 여기에만 있다. 무거우므로 필요할 때만 import한다.
    """
    import pyarrow.parquet as pq

    files = sorted((directory / "meta/episodes").rglob("*.parquet"))
    rows: list[dict[str, Any]] = []
    for path in files:
        table = pq.read_table(path)
        # 통계 열은 크고 화면에서 쓰지 않는다. 읽지 않으면 큰 데이터셋에서 훨씬 가볍다.
        columns = [name for name in table.column_names if not name.startswith("stats")]
        rows.extend(table.select(columns).to_pylist())
    rows.sort(key=lambda row: row.get("episode_index", 0))
    return rows


def summarize(name: str) -> dict[str, Any]:
    directory = _dataset_dir(name)
    info = _read_info(directory)
    return {
        "name": name,
        "episodes": info.get("total_episodes", 0),
        "frames": info.get("total_frames", 0),
        "fps": info.get("fps", 0),
        "robot_type": info.get("robot_type"),
        "codebase_version": info.get("codebase_version"),
        "cameras": video_keys(info),
        "size_bytes": _directory_size(directory),
        "recorded_at": (directory / "meta/info.json").stat().st_mtime,
    }


def list_datasets() -> list[dict[str, Any]]:
    root = data_root()
    if not root.is_dir():
        return []
    names = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and NAME_PATTERN.match(entry.name) and (entry / "meta/info.json").exists()
    )
    return [summarize(name) for name in names]


def describe(name: str) -> dict[str, Any]:
    """목록 요약 + 에피소드별 재생 정보.

    v3에서는 여러 에피소드가 한 mp4 파일에 이어 붙는다. 그래서 에피소드마다 파일 하나가
    아니라 `(파일, 시작 초, 끝 초)`를 돌려준다. 재생하는 쪽은 그 구간만 보면 된다.
    """
    directory = _dataset_dir(name)
    info = _read_info(directory)
    keys = video_keys(info)
    episodes = []
    for row in _episode_rows(directory):
        index = int(row.get("episode_index", 0))
        tasks = row.get("tasks") or []
        videos = {}
        for key in keys:
            chunk = row.get(f"videos/{key}/chunk_index")
            file_index = row.get(f"videos/{key}/file_index")
            if chunk is None or file_index is None:
                continue
            videos[key] = {
                "chunk_index": int(chunk),
                "file_index": int(file_index),
                "from_seconds": float(row.get(f"videos/{key}/from_timestamp") or 0.0),
                "to_seconds": float(row.get(f"videos/{key}/to_timestamp") or 0.0),
                # 구간을 주소에 담는다. 클라이언트는 0초부터 그대로 재생하면 된다.
                "url": (
                    f"/api/datasets/{name}/video/{key}/{int(chunk)}/{int(file_index)}"
                    f"?from={float(row.get(f'videos/{key}/from_timestamp') or 0.0):.3f}"
                    f"&to={float(row.get(f'videos/{key}/to_timestamp') or 0.0):.3f}"
                ),
            }
        episodes.append({
            "index": index,
            "task": tasks[0] if tasks else "",
            "frames": int(row.get("length", 0)),
            "videos": videos,
        })
    summary = summarize(name)
    summary["episodes_detail"] = episodes
    return summary


def _find_episode(directory: Path, episode_index: int) -> dict[str, Any]:
    episode = next(
        (row for row in _episode_rows(directory) if int(row.get("episode_index", -1)) == episode_index),
        None,
    )
    if episode is None:
        raise FileNotFoundError(str(episode_index))
    return episode


def _episode_data_table(directory: Path, episode_index: int, episode: dict[str, Any], columns: list[str]):
    """이 에피소드의 행만, frame 순서대로.

    v3에서는 여러 에피소드가 한 parquet 파일에 이어 붙고 행 순서도 보장되지 않는다.
    그래서 `episode_index`로 거르고 `frame_index`로 다시 세운다.
    """
    chunk_index = episode.get("data/chunk_index")
    file_index = episode.get("data/file_index")
    if chunk_index is None or file_index is None:
        raise DatasetError("Episode does not identify its data parquet file")
    path = (
        directory
        / "data"
        / f"chunk-{int(chunk_index):03d}"
        / f"file-{int(file_index):03d}.parquet"
    )
    if not path.exists():
        raise FileNotFoundError(path.name)

    import pyarrow.parquet as pq

    return pq.read_table(
        path,
        columns=columns,
        filters=[("episode_index", "=", episode_index)],
    ).sort_by([("frame_index", "ascending")])


def _feature_joint_names(info: dict[str, Any], feature: str) -> list[str]:
    names = ((info.get("features") or {}).get(feature) or {}).get("names")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise DatasetError(f"Dataset does not describe {feature} joint names")
    return names


def trajectory(name: str, episode_index: int) -> dict[str, Any]:
    """Follower state and requested action for one episode, in frame order."""
    directory = _dataset_dir(name)
    info = _read_info(directory)
    episode = _find_episode(directory, episode_index)

    expected_frames = int(episode.get("length", 0))
    if expected_frames > TRAJECTORY_FRAME_LIMIT:
        raise TrajectoryTooLargeError(
            f"Episode has {expected_frames} frames; limit is {TRAJECTORY_FRAME_LIMIT}"
        )

    joints = _feature_joint_names(info, "observation.state")
    table = _episode_data_table(
        directory,
        episode_index,
        episode,
        ["episode_index", "frame_index", "observation.state", "action"],
    )
    frames = table.num_rows
    if frames > TRAJECTORY_FRAME_LIMIT:
        raise TrajectoryTooLargeError(
            f"Episode has {frames} frames; limit is {TRAJECTORY_FRAME_LIMIT}"
        )
    if frames != expected_frames:
        raise DatasetError(
            f"Episode metadata says {expected_frames} frames but parquet contains {frames}"
        )

    return {
        "fps": info.get("fps", 0),
        "frames": frames,
        # Do not sort this list: each column in both arrays corresponds to this
        # exact feature order from meta/info.json.
        "joints": joints,
        "state": table.column("observation.state").to_pylist(),
        "action": table.column("action").to_pylist(),
    }


def episode_actions(name: str, episode_index: int) -> dict[str, Any]:
    """재생이 팔에 흘려보낼 action만, frame 순서대로.

    `trajectory()`와 두 가지가 다르다. 관측(`observation.state`)을 읽지 않고, 프레임 수
    상한이 없다. 상한이 없는 이유는 이 값이 화면에 그려지는 것이 아니라 팔에 들어가는
    것이기 때문이다 — 에피소드가 길다는 이유로 앞부분만 재생하면, 팔은 사람이 본 것과
    다른 동작을 하다가 중간에 선다.
    """
    directory = _dataset_dir(name)
    info = _read_info(directory)
    episode = _find_episode(directory, episode_index)
    joints = _feature_joint_names(info, "action")
    table = _episode_data_table(
        directory, episode_index, episode, ["episode_index", "frame_index", "action"]
    )
    frames = table.num_rows
    expected_frames = int(episode.get("length", 0))
    if frames != expected_frames:
        raise DatasetError(
            f"Episode metadata says {expected_frames} frames but parquet contains {frames}"
        )
    if frames == 0:
        raise DatasetError("That episode has no frames to replay")
    action = table.column("action").to_pylist()
    if any(len(row) != len(joints) for row in action):
        # 이름과 값의 개수가 어긋나면 어느 값이 어느 관절인지 알 수 없다. 재생은 그 값을
        # 팔에 넣는 일이므로, 여기서 멈추는 것이 짝을 잘못 맞춰 보내는 것보다 낫다.
        raise DatasetError(
            f"Episode rows do not carry one value per action joint ({len(joints)} expected)"
        )
    # NaN/Inf는 여기서 걸러 낸다. `SAFETY.md`의 최소 불변조건이 그것을 실행하지 말라고
    # 적어 두었고, 정렬 검사는 첫 프레임만 보므로 뒤쪽에 숨은 값을 잡을 자리가 여기뿐이다.
    # `nan > 60`은 거짓이라, 거르지 않으면 60도 검사를 조용히 통과한다.
    if any(not isfinite(value) for row in action for value in row):
        raise DatasetError("Episode contains values that are not finite numbers")
    return {
        "fps": info.get("fps", 0),
        "frames": frames,
        # Do not sort this list: each column of `action` corresponds to this exact
        # feature order from meta/info.json.
        "joints": joints,
        "action": action,
    }


def _clip_cache() -> Path:
    directory = Path(__file__).parents[2] / "runtime/clips"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _prune_clip_cache() -> None:
    clips = sorted(_clip_cache().glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in clips[CLIP_CACHE_LIMIT:]:
        stale.unlink(missing_ok=True)


def source_codec(path: Path) -> str:
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return (stream.codec_context.name or "").lower()


def _transcode(source: Path, target: Path, from_seconds: float, to_seconds: float) -> None:
    import av

    temporary = target.with_suffix(".partial.mp4")
    with av.open(str(source)) as src:
        stream = src.streams.video[0]
        with av.open(str(temporary), "w", options={"movflags": "+faststart"}) as dst:
            out = dst.add_stream("libx264", rate=stream.average_rate)
            out.width = stream.codec_context.width
            out.height = stream.codec_context.height
            out.pix_fmt = "yuv420p"
            out.options = {"crf": "23", "preset": "veryfast"}
            # Carry the recording's own timing across. Handing frames over with
            # `pts = None` lets the encoder assign them, and every frame ended up
            # at the same instant: a two-second episode came out as a 0.1s file.
            out.codec_context.time_base = stream.time_base
            base_pts = None
            if from_seconds > 2:
                # Land on a keyframe a little before the episode and drop what
                # comes before it. Seeking exactly to the start would usually land
                # after it, which silently loses the opening of the episode.
                with suppress_errors():
                    src.seek(int((from_seconds - 2) * 1_000_000))
            written = 0
            for frame in src.decode(stream):
                # `frame.time` is the decoder's own seconds; computing it from pts
                # and the stream time base gets it wrong for some containers, which
                # is how a two-second episode came out as a single frame.
                moment = frame.time
                if moment is None:
                    continue
                if moment < from_seconds - 1e-6:
                    continue
                if to_seconds > from_seconds and moment >= to_seconds - 1e-6:
                    break
                if base_pts is None:
                    base_pts = frame.pts
                frame.pts = frame.pts - base_pts
                frame.time_base = stream.time_base
                written += 1
                for packet in out.encode(frame):
                    dst.mux(packet)
            if written == 0:
                raise DatasetError("That episode has no frames in the recording")
            for packet in out.encode():
                dst.mux(packet)
    temporary.replace(target)


class suppress_errors:
    """A seek that fails is not fatal: decoding from the start still works."""

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, Exception)


def playable_clip(
    name: str, video_key: str, chunk_index: int, file_index: int,
    from_seconds: float = 0.0, to_seconds: float = 0.0,
) -> Path:
    """A file the client can actually play, covering just this episode.

    Serves the recording untouched when it is already H.264 or HEVC and the whole
    file was asked for. Otherwise re-encodes the requested seconds once and keeps
    the result, so watching the same episode again costs nothing.
    """
    source = video_file(name, video_key, chunk_index, file_index)
    wants_whole_file = to_seconds <= from_seconds
    if wants_whole_file and source_codec(source) in PLAYABLE_CODECS:
        return source

    stamp = source.stat().st_mtime_ns
    token = f"{name}|{video_key}|{chunk_index}|{file_index}|{from_seconds:.3f}|{to_seconds:.3f}|{stamp}"
    target = _clip_cache() / f"{hashlib.sha256(token.encode()).hexdigest()[:20]}.mp4"
    if target.exists():
        # Touch so the cache keeps what is actually being watched.
        target.touch()
        return target
    _transcode(source, target, from_seconds, to_seconds)
    _prune_clip_cache()
    return target


def video_file(name: str, video_key: str, chunk_index: int, file_index: int) -> Path:
    directory = _dataset_dir(name)
    if not VIDEO_KEY_PATTERN.match(video_key):
        raise DatasetError("Unknown camera")
    if video_key not in video_keys(_read_info(directory)):
        raise DatasetError("Unknown camera")
    if not 0 <= chunk_index < 1000 or not 0 <= file_index < 1000:
        raise DatasetError("Unknown video file")
    path = directory / "videos" / video_key / f"chunk-{chunk_index:03d}" / f"file-{file_index:03d}.mp4"
    if not path.exists():
        raise FileNotFoundError(path.name)
    return path
