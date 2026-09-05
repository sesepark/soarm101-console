#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# `lerobot-teleoperate` 바이너리를 부르지 않는다. 붙는 순간의 목표 동기화와 루프 앞의
# 자세 정렬은 CLI 플래그로 부탁할 수 있는 일이 아니다 — `soarm_console.teleoperating`이
# LeRobot의 루프를 그대로 쓰되 그 앞뒤에 그 둘을 놓는다. `record.sh`와 같은 모양이다.
PYTHONPATH=src exec .venv/bin/python -m soarm_console.teleoperating
