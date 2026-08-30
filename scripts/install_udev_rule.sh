#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sudo install -o root -g root -m 0644 \
  "$PROJECT_DIR/deploy/udev/99-soarm101.rules" \
  /etc/udev/rules.d/99-soarm101.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
echo "Installed /etc/udev/rules.d/99-soarm101.rules"

