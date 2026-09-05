#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 카메라는 열지 않는다. 재생은 팔을 움직이는 일이고 영상은 이미 데이터셋 안에 있다.
# 그래서 `record.sh`와 달리 카메라 점유 검사가 없다.
PYTHONPATH=src exec .venv/bin/python -m soarm_console.replaying
