#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$USER_UNIT_DIR"
ln -sfn "$PROJECT_DIR/deploy/systemd/hubq.service" "$USER_UNIT_DIR/hubq.service"
systemctl --user daemon-reload
systemctl --user enable --now hubq.service
systemctl --user status hubq.service --no-pager
