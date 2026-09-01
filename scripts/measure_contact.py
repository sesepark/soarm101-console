#!/usr/bin/env python3
"""접촉 문턱을 정하기 위해 관절별 부하·전류·온도를 재는 도구.

지금 `config/soarm.env`에 들어 있는 `SOARM_VL_LOAD_TRIP`과 `SOARM_VL_CURRENT_TRIP`은
데이터시트와 LeRobot이 쓰는 값에서 **유추한** 것이지 이 팔에서 잰 값이 아니다. 너무 높으면
늦게 걸리고, 너무 낮으면 정상 동작 중에 자꾸 선다. 그래서 실제 숫자가 필요하다.

세 가지를 잰다.

- `quiescent` — 토크를 끄고 팔을 가만히 둔 채. 바닥값이다. **사람이 없어도 된다.**
- `handled`   — 토크를 끈 채 사람이 팔을 손으로 움직이는 동안. 관절이 도는 데 드는 힘.
- `holding`   — 토크가 걸린 채 자세를 유지하는 동안. 중력을 버티는 부하.
- `contact`   — 토크가 걸린 채 사람이 팔을 무언가에 대고 미는 동안. **문턱은 이 값과
                `holding` 사이에 있어야 한다.**

`holding`과 `contact`는 토크를 요구하므로 **사람이 현장에 있을 때만** 쓴다. 이 스크립트는
스스로 토크를 걸지 않는다 — 이미 걸려 있는 상태에서 읽기만 한다.

    # 사람이 없어도 되는 것
    .venv/bin/python scripts/measure_contact.py quiescent --seconds 20

    # 사람이 팔을 잡고
    .venv/bin/python scripts/measure_contact.py handled --seconds 30

    # 콘솔에서 토크를 건 뒤(가상 리더 → 조작 권한 받기), 사람이 지켜보며
    .venv/bin/python scripts/measure_contact.py holding --seconds 30
    .venv/bin/python scripts/measure_contact.py contact --seconds 30

가상 리더가 팔로워 serial을 쥐고 있으면 이 스크립트는 열지 못한다. 소유자는 한 번에
하나다(ADR 0001). `holding`/`contact`를 재려면 콘솔이 아니라 이 스크립트가 유일한 소유자여야
하므로, 토크를 건 채 콘솔의 가상 리더를 내리는 길이 필요하다 —
`POST /api/vleader/stop?force=true`가 그것이고, 토크는 그대로 남는다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from soarm_console.config import Settings  # noqa: E402
from soarm_console.diagnostics import MOTORS  # noqa: E402
from soarm_console.owner_lock import DeviceLockError, DeviceLockSet  # noqa: E402


PHASES = ("quiescent", "handled", "holding", "contact")
#: STS3215의 `Present_Current` 눈금. 데이터시트 값이고, 이것 역시 실측이 아니다.
MILLIAMPS_PER_COUNT = 6.5


def summarise(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }


def summarise_temperature(samples: list[float]) -> dict[str, float]:
    summary = summarise(samples)
    if samples:
        summary.update(
            {
                "start": round(samples[0], 1),
                "end": round(samples[-1], 1),
                "rise": round(samples[-1] - samples[0], 1),
            }
        )
    return summary


def measure(phase: str, seconds: float, hz: float) -> dict[str, object]:
    from lerobot.motors.feetech import FeetechMotorsBus

    settings = Settings()
    bus = FeetechMotorsBus(port=settings.follower_port, motors=MOTORS)
    readings: dict[str, dict[str, list[float]]] = {
        name: {"load": [], "current": [], "temperature": []} for name in MOTORS
    }
    torque_seen: set[int] = set()
    owner_locks = DeviceLockSet.acquire([settings.follower_port], f"contact-measure-{phase}")
    try:
        bus.connect(handshake=False)
        bus.set_baudrate(1_000_000)
        deadline = time.monotonic() + seconds
        period = 1.0 / hz
        while time.monotonic() < deadline:
            started = time.perf_counter()
            load = bus.sync_read("Present_Load", normalize=False, num_retry=2)
            current = bus.sync_read("Present_Current", normalize=False, num_retry=2)
            temperature = bus.sync_read("Present_Temperature", normalize=False, num_retry=2)
            torque = bus.read("Torque_Enable", "shoulder_lift", normalize=False, num_retry=2)
            torque_seen.add(int(torque))
            for name in MOTORS:
                readings[name]["load"].append(abs(float(load[name])))
                readings[name]["current"].append(abs(float(current[name])))
                readings[name]["temperature"].append(float(temperature[name]))
            time.sleep(max(0.0, period - (time.perf_counter() - started)))
    finally:
        # 토크는 건드리지 않는다. 이 스크립트가 팔을 떨어뜨리는 일은 없어야 한다.
        if bus.is_connected:
            bus.disconnect(disable_torque=False)
        owner_locks.release()

    return {
        "phase": phase,
        "measured_at": datetime.now(UTC).isoformat(),
        "seconds": seconds,
        "hz": hz,
        "torque_enabled_during": sorted(torque_seen),
        "milliamps_per_count": MILLIAMPS_PER_COUNT,
        "joints": {
            name: {
                kind: summarise_temperature(values) if kind == "temperature" else summarise(values)
                for kind, values in kinds.items()
            }
            for name, kinds in readings.items()
        },
    }


def report(result: dict[str, object]) -> str:
    lines = [
        f"# {result['phase']} · {result['seconds']}초 · {result['hz']}Hz · {result['measured_at']}",
        f"# 측정 중 토크: {result['torque_enabled_during']} (0=꺼짐, 1=걸림)",
        "",
        f"{'관절':<16}{'부하 min':>9}{'p50':>7}{'p95':>7}{'max':>7}"
        f"{'전류 min':>9}{'p50':>7}{'p95':>7}{'max':>7}{'(mA max)':>10}"
        f"{'온도 시작':>10}{'끝':>6}{'상승':>7}{'max':>6}",
    ]
    for name, kinds in result["joints"].items():
        load, current, temperature = kinds["load"], kinds["current"], kinds["temperature"]
        lines.append(
            f"{name:<16}{load['min']:>9.0f}{load['p50']:>7.0f}"
            f"{load['p95']:>7.0f}{load['max']:>7.0f}"
            f"{current['min']:>9.0f}{current['p50']:>7.0f}"
            f"{current['p95']:>7.0f}{current['max']:>7.0f}"
            f"{current['max'] * MILLIAMPS_PER_COUNT:>10.0f}"
            f"{temperature['start']:>10.0f}{temperature['end']:>6.0f}"
            f"{temperature['rise']:>+7.0f}{temperature['max']:>6.0f}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--json", type=Path, help="요약 결과를 이 JSON 파일에 남긴다")
    arguments = parser.parse_args()

    if arguments.phase in {"holding", "contact"}:
        print(
            "이 단계는 토크가 걸린 상태를 잽니다. 팔이 움직일 수 있으므로 현장에 사람이\n"
            "있어야 하고, 전원 플러그에 손이 닿는 자리에서만 하세요. 이 스크립트는 스스로\n"
            "토크를 걸지 않습니다 — 이미 걸려 있는 상태를 읽기만 합니다.\n",
            file=sys.stderr,
        )

    try:
        result = measure(arguments.phase, arguments.seconds, arguments.hz)
    except DeviceLockError as exc:
        raise SystemExit(f"측정 거부: {exc}") from exc
    print(report(result))
    if arguments.json:
        arguments.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\n요약 결과: {arguments.json}", file=sys.stderr)

    if arguments.phase in {"holding", "contact"} and result["torque_enabled_during"] != [1]:
        print(
            "\n주의: 측정하는 동안 토크가 걸려 있지 않았습니다. 이 숫자는 이 단계의 값이"
            " 아닙니다.",
            file=sys.stderr,
        )
    if arguments.phase in {"quiescent", "handled"} and 1 in result["torque_enabled_during"]:
        print(
            "\n주의: 토크가 꺼져 있어야 하는 단계에서 켜진 표본이 있습니다. 이 숫자는 이"
            " 단계의 값이 아닙니다.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
