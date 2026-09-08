#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
exec .venv/bin/uvicorn hubq.app:app \
  --app-dir src \
  --host 127.0.0.1 \
  --port "${HUBQ_PORT:-8094}"
