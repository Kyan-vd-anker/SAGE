#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this helper as your normal Kali user; it uses sudo." >&2
  exit 1
fi
command -v sudo >/dev/null 2>&1 || {
  echo "sudo is required to install Sage's Kali dependencies." >&2
  exit 1
}
command -v apt-get >/dev/null 2>&1 || {
  echo "apt-get is required on Kali to install Sage's dependencies." >&2
  exit 1
}

sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pypdf \
  python3-matplotlib \
  python3-coolprop \
  git \
  curl \
  poppler-utils \
  xdotool \
  scrot \
  ffmpeg \
  espeak-ng
