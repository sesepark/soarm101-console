#!/usr/bin/env python3
"""한 시행의 동작 기록(`runtime/policy/trace.jsonl`)을 숫자로 바꾼다.

끊김은 눈으로만 판정하던 것이었다. 이 도구가 재는 것은 세 가지다.

1. **제어 간격** — 루프가 실제로 33.3ms마다 도는가. 로그의 `Avg FPS`는 관측 전송률이라
   제어율이 아니고(2026-09-09), 여기 시각은 밀리초 아래까지 있어 흔들림까지 보인다.
2. **추종 오차** `|명령 - 실제|` — 팔이 계획에 붙어 있는가. 학습 데이터의 같은 값은
   중앙값 4.8°다. 그보다 크게 벌어져 있으면 팔은 계획이 아니라 다른 것을 따르고 있다.
3. **저크** — 눈에 "툭툭"으로 보이는 것의 정체. 명령의 저크와 팔의 저크를 나눠서 잰다.
   명령이 매끄러운데 팔만 거칠면 배관·리미터·서보 쪽이고, 명령부터 거칠면 정책이다.

쓰는 법: `python3 scripts/trace_smoothness.py [기록파일] [--from 초] [--to 초]`
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

DEFAULT = Path(__file__).parents[1] / "runtime/policy/trace.jsonl"
#: 학습 데이터(soarm101_cube134_dnv_strat 134회)의 |action - observation.state|.
#: 사람이 몰 때 팔이 명령에 얼마나 붙어 있었는지의 기준선이다.
DEMO_TRACKING_MEDIAN_DEG = 4.8


def _load(path: Path, lo: float, hi: float):
    lines = path.read_text().splitlines()
    if not lines:
        raise SystemExit(f"비어 있는 기록입니다: {path}")
    joints = json.loads(lines[0])["joints"]
    rows = []
    for line in lines[1:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # 시행이 중간에 끊기면 마지막 줄이 잘려 있을 수 있다
        if lo <= r["t"] <= hi:
            rows.append(r)
    return joints, rows


def _derivative(values: list[float], dt: list[float]) -> list[float]:
    return [(b - a) / h if h > 0 else 0.0 for a, b, h in zip(values, values[1:], dt)]


def _rms(values: list[float]) -> float:
    return (sum(v * v for v in values) / len(values)) ** 0.5 if values else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default=str(DEFAULT))
    ap.add_argument("--from", dest="lo", type=float, default=0.0)
    ap.add_argument("--to", dest="hi", type=float, default=float("inf"))
    args = ap.parse_args()

    joints, rows = _load(Path(args.path), args.lo, args.hi)
    if len(rows) < 8:
        raise SystemExit(f"표본이 {len(rows)}개뿐이라 잴 수 없습니다")

    times = [r["t"] for r in rows]
    gaps = [b - a for a, b in zip(times, times[1:])]
    span = times[-1] - times[0]

    print(f"기록 {Path(args.path).name} · {len(rows)}틱 · {span:.1f}초")
    print()
    print("=== 1. 제어 간격 (목표 33.3ms) ===")
    ms = sorted(g * 1000 for g in gaps)
    print(f"  중앙값 {statistics.median(ms):.1f}ms · 90% {ms[int(.9*len(ms))-1]:.1f}ms · 최대 {max(ms):.1f}ms")
    over = sum(1 for g in ms if g > 50)
    print(f"  50ms 넘긴 틱 {over}개 ({100*over/len(ms):.1f}%) · 실효 제어율 {len(gaps)/span:.1f} Hz")

    print()
    print(f"=== 2. 추종 오차 |명령 - 실제| (시연 중앙값 {DEMO_TRACKING_MEDIAN_DEG}°) ===")
    print(f"  {'관절':<16}{'중앙값':>9}{'90%':>9}{'최대':>9}")
    worst = 0.0
    for i, name in enumerate(joints):
        err = sorted(abs(r["g"][i] - r["p"][i]) for r in rows)
        worst = max(worst, statistics.median(err))
        print(f"  {name:<16}{statistics.median(err):>9.1f}{err[int(.9*len(err))-1]:>9.1f}{max(err):>9.1f}")
    verdict = "시연과 같은 수준" if worst <= 2 * DEMO_TRACKING_MEDIAN_DEG else "시연보다 크게 벌어져 있음"
    print(f"  → 가장 나쁜 관절의 중앙값 {worst:.1f}° — {verdict}")

    print()
    print("=== 3. 저크 (°/s³, RMS) — 눈에 보이는 '툭툭'의 정체 ===")
    print(f"  {'관절':<16}{'명령':>12}{'팔':>12}{'팔/명령':>10}")
    ratios = []
    for i, name in enumerate(joints):
        out = {}
        for key in ("g", "p"):
            series = [r[key][i] for r in rows]
            v = _derivative(series, gaps)
            a = _derivative(v, gaps[1:])
            out[key] = _rms(_derivative(a, gaps[2:]))
        ratio = out["p"] / out["g"] if out["g"] > 0 else float("inf")
        ratios.append(ratio)
        print(f"  {name:<16}{out['g']:>12.0f}{out['p']:>12.0f}{ratio:>10.1f}x")
    median_ratio = statistics.median(ratios)
    print()
    if median_ratio > 3:
        print(f"  → 팔이 명령보다 {median_ratio:.1f}배 거칠다. 원인은 정책이 아니라 그 아래에 있다")
        print("     (리미터·서보·제어 간격 흔들림을 먼저 본다)")
    else:
        print(f"  → 팔이 명령을 그대로 따르고 있다 (배율 {median_ratio:.1f}x).")
        print("     그래도 끊겨 보이면 거친 쪽은 정책의 궤적이다 — 위 '명령' 열을 체크포인트끼리 비교한다")


if __name__ == "__main__":
    main()
