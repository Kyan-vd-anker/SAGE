#!/usr/bin/env bash
# Installs the Sage wall display on Raspberry Pi OS Lite (32-bit is fine for a Pi Zero W).
# Run it as your normal user (not root) from this folder:  bash install.sh
#   bash install.sh --readonly   also makes the system read-only (no SD card corruption when power is cut)
set -euo pipefail
cd "$(dirname "$0")"
READONLY=0
[ "${1:-}" = "--readonly" ] && READONLY=1

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not root (it uses sudo where needed)." >&2
  exit 1
fi

echo "==> Installing packages (pygame, fonts)"
sudo apt-get update
sudo apt-get install -y python3-pygame fonts-dejavu-core
# Roboto gives the display its look; the app falls back to DejaVu if it is missing.
sudo apt-get install -y fonts-roboto || echo "    (fonts-roboto not available, using DejaVu)"

echo "==> Giving $USER access to the display and touch input"
sudo usermod -aG video,render,input "$USER"

echo "==> Installing the program"
sudo install -d /opt/sage-wall
sudo install -m 755 sage_wall.py /opt/sage-wall/sage_wall.py
if [ ! -f /etc/sage-wall.conf ]; then
  sudo install -m 644 sage-wall.conf.example /etc/sage-wall.conf
  echo "    Created /etc/sage-wall.conf  -> edit SAGE_URL before starting"
fi

echo "==> Installing the service"
sed "s/__USER__/$USER/" sage-wall.service | sudo tee /etc/systemd/system/sage-wall.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable sage-wall.service

# Wi-Fi power saving makes the first packet after a pause slow; turn it off if NetworkManager is used.
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null
fi

# Wi-Fi safety net: restarts Wi-Fi (and finally reboots) if the router stays unreachable.
sudo install -m 755 wifi-watch.sh /opt/sage-wall/wifi-watch.sh
sudo tee /etc/systemd/system/sage-wifi-watch.service >/dev/null <<'UNIT'
[Unit]
Description=Sage wall Wi-Fi watchdog
[Service]
Type=oneshot
ExecStart=/opt/sage-wall/wifi-watch.sh
UNIT
sudo tee /etc/systemd/system/sage-wifi-watch.timer >/dev/null <<'UNIT'
[Unit]
Description=Check Wi-Fi every minute
[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
UNIT
# Hardware watchdog: reboot automatically if the Pi ever hangs.
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=20\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable sage-wifi-watch.timer

echo
echo "Done. Next steps:"
echo "  1. sudo nano /etc/sage-wall.conf      (set SAGE_URL to your PC, e.g. http://192.168.1.10:8001)"
echo "  2. sudo reboot                         (needed once so the new group access applies)"
echo "To test without rebooting:  SDL_VIDEODRIVER=kmsdrm python3 /opt/sage-wall/sage_wall.py"
if [ "$READONLY" -eq 1 ]; then
  echo
  echo "==> Making the system read-only (overlay file system). Set /etc/sage-wall.conf FIRST, it can't be edited afterwards."
  sudo raspi-config nonint enable_overlayfs
  sudo raspi-config nonint enable_bootro
  echo "    Read-only mode starts after the next reboot. To change settings later: sudo raspi-config nonint disable_overlayfs; sudo raspi-config nonint disable_bootro; reboot, edit, then enable again."
fi
