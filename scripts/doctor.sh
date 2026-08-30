#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHONPATH=src exec .venv/bin/python - <<'PY'
import json
from soarm_console.config import Settings
from soarm_console.diagnostics import run_hardware_doctor

print(json.dumps(run_hardware_doctor(Settings()), indent=2))
PY
