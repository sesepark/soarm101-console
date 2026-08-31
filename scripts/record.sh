#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

scene_camera="${SOARM_SCENE_CAMERA:-/dev/v4l/by-path/pci-0000:00:14.0-usb-0:11.4:1.0-video-index0}"
wrist_camera="${SOARM_WRIST_CAMERA:-/dev/v4l/by-path/pci-0000:00:14.0-usb-0:5:1.0-video-index0}"

for camera in "$scene_camera" "$wrist_camera"; do
  if fuser "$camera" >/dev/null 2>&1; then
    echo "Refusing to record: camera is already in use: $camera" >&2
    exit 1
  fi
done

PYTHONPATH=src exec .venv/bin/python -m soarm_console.recording
