#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this installer as your normal Kali user, not as root." >&2
  exit 1
fi

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if ! bash "$ROOT_DIR/scripts/install-kali-dependencies.sh"; then
  echo "Could not install Sage's Kali dependencies." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || ! command -v loginctl >/dev/null 2>&1; then
  echo "A systemd-based Kali installation is required to install the auto-start service." >&2
  exit 1
fi

if ! bash "$ROOT_DIR/scripts/setup-kali-runtime.sh" unit; then
  echo "Could not configure Sage's Python environment and service." >&2
  exit 1
fi

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
