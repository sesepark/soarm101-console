#!/usr/bin/env python3
"""녹화한 데이터셋이 학습에 쓸 수 있는 모양인지 본다.

`lerobot-record`가 만든 것을 그대로 믿지 않는 이유는, 형식이 맞아도 내용이 틀릴 수 있기
때문이다. 타임스탬프가 뛰거나, 어떤 에피소드만 카메라가 빠졌거나, task 문자열이 비어 있는
것은 파일 구조로는 드러나지 않고 학습을 돌린 뒤에야 알게 된다.

    .venv/bin/python scripts/validate_dataset.py <이름>
    .venv/bin/python scripts/validate_dataset.py --all

나가는 코드는 문제가 하나라도 있으면 1이다.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

# SO-ARM101은 6관절이다. 다른 수가 나오면 calibration이나 로봇 설정이 어긋난 것이다.
EXPECTED_DOF = 6
# LeRobot v3는 에피소드 메타를 parquet에 쓴다. GR00T를 NVIDIA 저장소로 돌릴 때만 v2가 필요하다.
EXPECTED_CODEBASE = "v3.0"
# 프레임 간격이 이 비율 이상 흔들리면 액션 청크 정책의 시간 일관성이 깨진다.
TIMESTAMP_TOLERANCE = 0.25


class Problem(Exception):
    pass


def data_root() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _episode_rows(directory: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    rows: list[dict[str, Any]] = []
    for path in sorted((directory / "meta/episodes").rglob("*.parquet")):
        table = pq.read_table(path)
        keep = [name for name in table.column_names if not name.startswith("stats")]
        rows.extend(table.select(keep).to_pylist())
    rows.sort(key=lambda row: row.get("episode_index", 0))
    return rows


def _frame_table(directory: Path):
    import pyarrow.parquet as pq

    files = sorted((directory / "data").rglob("*.parquet"))
    if not files:
        raise Problem("data/ 아래에 parquet이 없다")
    return pq.read_table(files[0]) if len(files) == 1 else pq.concat_tables(
        [pq.read_table(f) for f in files]
    )


def _probe_video(path: Path) -> dict[str, Any]:
    """PyAV로 읽는다.

    `ffprobe` 바이너리를 부르지 않는 이유는 이 서버에 FFmpeg가 설치되어 있지 않기
    때문이다. 콘솔이 재생용 클립을 만들 때도 같은 이유로 PyAV를 쓴다 — 자체 FFmpeg를
    안고 있어 시스템에 무엇이 깔렸는지와 무관하다.
    """
    import av

    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            rate = stream.average_rate or stream.base_rate
            return {
                "codec": stream.codec_context.name,
                "width": stream.codec_context.width,
                "height": stream.codec_context.height,
                "fps": float(rate) if rate else 0.0,
                "frames": stream.frames or 0,
                "seconds": float(stream.duration * stream.time_base) if stream.duration else 0.0,
            }
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 이름과 함께 보고한다
        return {"error": f"{type(error).__name__}: {error}"}


def check(name: str) -> tuple[list[str], list[str]]:
    """(문제, 참고) 두 목록."""
    bad: list[str] = []
    note: list[str] = []
    directory = data_root() / name
    if not (directory / "meta/info.json").exists():
        return [f"{name}: meta/info.json이 없다 — 데이터셋이 아니거나 전송이 덜 끝났다"], []

    info = _load(directory / "meta/info.json")

    # --- 1. 형식 버전 ---
    version = info.get("codebase_version")
    if version != EXPECTED_CODEBASE:
        note.append(f"codebase_version={version} (기대 {EXPECTED_CODEBASE})")

    fps = info.get("fps") or 0
    if not fps:
        bad.append("info.json에 fps가 없다")

    # --- 2. feature 구성 ---
    features = info.get("features") or {}
    videos = sorted(k for k, v in features.items() if v.get("dtype") == "video")
    if not videos:
        bad.append("영상 feature가 하나도 없다 — VLA는 이미지 없이 학습할 수 없다")
    for key in ("observation.state", "action"):
        if key not in features:
            bad.append(f"필수 feature `{key}`가 없다")
        else:
            shape = features[key].get("shape") or []
            if shape and shape[0] != EXPECTED_DOF:
                bad.append(f"{key}의 차원이 {shape[0]} — SO-ARM101은 {EXPECTED_DOF}이어야 한다")
    note.append(f"카메라 {len(videos)}대: {', '.join(v.split('.')[-1] for v in videos)}")

    # --- 3. 에피소드 메타 ---
    try:
        episodes = _episode_rows(directory)
    except Exception as error:
        return bad + [f"에피소드 메타를 읽지 못했다: {error}"], note
    if not episodes:
        bad.append("에피소드가 하나도 없다")
        return bad, note
    if len(episodes) != info.get("total_episodes"):
        bad.append(f"info.json은 에피소드 {info.get('total_episodes')}개라 하는데 실제 {len(episodes)}개")

    # --- 4. 언어 지시 ---
    # VLA에서 가장 되돌릴 수 없는 항목이다. 비어 있으면 나중에 채울 방법이 없다.
    tasks = []
    for row in episodes:
        value = row.get("tasks") or row.get("task")
        if isinstance(value, list):
            tasks.extend(str(v) for v in value)
        elif value:
            tasks.append(str(value))
    empty = len(episodes) - len([t for t in tasks if t.strip()])
    if empty > 0:
        bad.append(f"language instruction이 빈 에피소드 {empty}개 — VLA 학습에 쓸 수 없다")
    unique = sorted(set(t.strip() for t in tasks if t.strip()))
    note.append(f"task 문자열 {len(unique)}종: " + "; ".join(unique[:3]) + ("…" if len(unique) > 3 else ""))

    # --- 5. 프레임 테이블: 타임스탬프와 인덱스 ---
    try:
        table = _frame_table(directory)
    except Exception as error:
        return bad + [f"프레임 parquet을 읽지 못했다: {error}"], note

    columns = set(table.column_names)
    for required in ("timestamp", "episode_index", "index", "observation.state", "action"):
        if required not in columns:
            bad.append(f"프레임 parquet에 `{required}` 열이 없다")

    if info.get("total_frames") and table.num_rows != info["total_frames"]:
        bad.append(f"info.json은 {info['total_frames']} 프레임이라 하는데 parquet은 {table.num_rows}행")

    if "timestamp" in columns and "episode_index" in columns:
        stamps = table.column("timestamp").to_pylist()
        eps = table.column("episode_index").to_pylist()
        expected_dt = 1.0 / fps if fps else 0
        backwards = 0
        jumps: list[tuple[int, float]] = []
        per_episode: dict[int, list[float]] = {}
        for stamp, episode in zip(stamps, eps):
            per_episode.setdefault(int(episode), []).append(float(stamp))
        for episode, series in sorted(per_episode.items()):
            for i in range(1, len(series)):
                dt = series[i] - series[i - 1]
                if dt <= 0:
                    backwards += 1
                elif expected_dt and abs(dt - expected_dt) > expected_dt * TIMESTAMP_TOLERANCE:
                    jumps.append((episode, dt))
        if backwards:
            bad.append(f"타임스탬프가 뒤로 가거나 멈춘 지점 {backwards}곳")
        if jumps:
            worst = max(jumps, key=lambda item: item[1])
            share = len(jumps) / max(table.num_rows, 1)
            line = (f"프레임 간격이 {1/fps*1000:.1f}ms에서 {TIMESTAMP_TOLERANCE:.0%} 넘게 벗어난 지점 "
                    f"{len(jumps)}곳 ({share:.1%}), 최대 {worst[1]*1000:.0f}ms (에피소드 {worst[0]})")
            (bad if share > 0.02 else note).append(line)
        # 에피소드마다 실제 길이
        lengths = [len(v) for v in per_episode.values()]
        note.append(f"에피소드 길이 {min(lengths)}~{max(lengths)} 프레임 "
                    f"({min(lengths)/fps:.1f}~{max(lengths)/fps:.1f}초)" if fps else "")

    # --- 6. 영상 ---
    for key in videos:
        files = sorted((directory / "videos").rglob("*.mp4"))
        files = [f for f in files if key.split(".")[-1] in str(f)]
        if not files:
            bad.append(f"{key}: mp4 파일이 없다")
            continue
        probe = _probe_video(files[0])
        if "error" in probe:
            bad.append(f"{key}: 영상을 읽지 못했다 ({probe['error']})")
            continue
        note.append(f"{key.split('.')[-1]}: {probe['codec']} {probe['width']}x{probe['height']} "
                    f"@{probe['fps']:.1f}fps, {len(files)}파일")
        if fps and probe["fps"] and abs(probe["fps"] - fps) > 1:
            bad.append(f"{key}: 영상 fps {probe['fps']:.1f}가 데이터셋 fps {fps}와 다르다")

    return bad, note


def main() -> int:
    parser = argparse.ArgumentParser(description="녹화한 LeRobot 데이터셋을 검사한다")
    parser.add_argument("name", nargs="?", help="데이터셋 이름")
    parser.add_argument("--all", action="store_true", help="data/ 아래 전부")
    args = parser.parse_args()

    if args.all:
        names = sorted(p.name for p in data_root().iterdir()
                       if p.is_dir() and (p / "meta/info.json").exists())
    elif args.name:
        names = [args.name]
    else:
        parser.error("이름을 주거나 --all을 쓰세요")

    if not names:
        print("검사할 데이터셋이 없습니다.")
        return 0

    failed = False
    for name in names:
        print(f"\n=== {name} ===")
        try:
            bad, note = check(name)
        except Exception as error:  # noqa: BLE001 - 어떤 실패든 이름과 함께 보여야 한다
            print(f"  검사 자체가 실패했다: {type(error).__name__}: {error}")
            failed = True
            continue
        for line in note:
            if line:
                print(f"  · {line}")
        if bad:
            failed = True
            for line in bad:
                print(f"  ✗ {line}")
        else:
            print("  ✓ 학습에 쓸 수 있는 모양이다")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
