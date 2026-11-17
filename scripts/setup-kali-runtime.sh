#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this helper as your normal Kali user, not as root." >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-unit}"
if [[ "$MODE" != "unit" && "$MODE" != "override" ]]; then
  echo "Usage: $0 [override]" >&2
  exit 2
fi
PYTHON="$(command -v python3)" || {
  echo "python3 is required to configure Sage's runtime." >&2
  exit 1
}
VENV_DIR="$HOME/.local/share/sage/venv"

mkdir -p "$(dirname "$VENV_DIR")"
if ! "$PYTHON" -m venv --system-site-packages "$VENV_DIR"; then
  echo "Could not create Sage's virtual environment. Install python3-full, then retry." >&2
  exit 1
fi
if ! "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$ROOT_DIR/requirements-schemdraw.txt"; then
  echo "Could not install Schemdraw into Sage's virtual environment; the service was not updated." >&2
  exit 1
fi

SERVICE_DIR="$HOME/.config/systemd/user"
if [[ "$MODE" == "override" ]]; then
  SERVICE_DIR="$SERVICE_DIR/sage.service.d"
  SERVICE_FILE="$SERVICE_DIR/50-sage-python-env.conf"
else
  SERVICE_FILE="$SERVICE_DIR/sage.service"
fi
mkdir -p "$SERVICE_DIR"

"$PYTHON" - "$SERVICE_FILE" "$ROOT_DIR" "$VENV_DIR/bin/python" "$MODE" <<'PY'
from pathlib import Path
import sys


def systemd_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


service_file, root_dir, python, mode = sys.argv[1:]
if mode == "override":
    contents = "\n".join(["[Service]", "ExecStart=", f"ExecStart={systemd_quote(python)} -m sage", ""])
else:
    contents = "\n".join([
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
        ])
Path(service_file).write_text(contents, encoding="utf-8")
PY

echo "Sage runtime configured with $VENV_DIR/bin/python."
