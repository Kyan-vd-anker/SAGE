#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this installer as your normal Kali user, not as root." >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "python3 is required. Install it with: sudo apt install python3" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || ! command -v loginctl >/dev/null 2>&1; then
  echo "A systemd-based Kali installation is required to install the auto-start service." >&2
  exit 1
fi

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/sage.service"
mkdir -p "$SERVICE_DIR"

python3 - "$SERVICE_FILE" "$ROOT_DIR" "$PYTHON" <<'PY'
from pathlib import Path
import sys


def systemd_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


service_file, root_dir, python = sys.argv[1:]
Path(service_file).write_text(
    "\n".join(
        [
            "[Unit]",
            "Description=Sage personal assistant",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={systemd_quote(root_dir)}",
            f"ExecStart={systemd_quote(python)} -m sage",
            "Environment=SAGE_HOST=127.0.0.1",
            "Environment=SAGE_PORT=8000",
            "Environment=PYTHONUNBUFFERED=1",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    ),
    encoding="utf-8",
)
PY

if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || true)" != "yes" ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to enable boot-time startup for your user." >&2
    exit 1
  fi
  sudo loginctl enable-linger "$USER"
fi

systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now sage.service

echo
echo "Sage is installed and will start automatically at boot."
echo "Open http://127.0.0.1:8000"
echo "Service status: systemctl --user status sage"
echo "Live logs:      journalctl --user -u sage -f"
echo "Restart:        systemctl --user restart sage"
echo "Stop:           systemctl --user stop sage"
if ! command -v ollama >/dev/null 2>&1; then
  echo
  echo "Ollama is not installed or not on PATH. Install/start Ollama and pull Sage's models;"
  echo "see the Kali Linux setup section in README.md."
fi
