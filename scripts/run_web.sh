#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
exec .venv/bin/uvicorn soarm_console.app:app \
  --app-dir src \
  --host "${SOARM_WEB_HOST:-127.0.0.1}" \
  --port "${SOARM_WEB_PORT:-8088}"

