#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$USER_UNIT_DIR"
ln -sfn "$PROJECT_DIR/deploy/systemd/soarm-console.service" "$USER_UNIT_DIR/soarm-console.service"
systemctl --user daemon-reload
systemctl --user enable soarm-console.service
systemctl --user restart soarm-console.service
systemctl --user status soarm-console.service --no-pager
