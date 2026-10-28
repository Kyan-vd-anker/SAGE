#!/bin/sh
# Wi-Fi safety net for the wall display. Run every minute by sage-wifi-watch.timer.
# If the router can't be reached 3 minutes in a row, restart Wi-Fi; if that does not help, reboot.
# State is kept in /run (RAM), so this also works with a read-only system.
STATE=/run/sage-wifi-fails
GW=$(ip route 2>/dev/null | awk '/^default/ {print $3; exit}')
if [ -n "$GW" ] && ping -c 1 -W 3 "$GW" >/dev/null 2>&1; then
  rm -f "$STATE"
  exit 0
fi
FAILS=$(cat "$STATE" 2>/dev/null || echo 0)
FAILS=$((FAILS + 1))
echo "$FAILS" > "$STATE"
logger -t sage-wifi-watch "router not reachable ($FAILS)"
if [ "$FAILS" -eq 3 ]; then
  logger -t sage-wifi-watch "restarting Wi-Fi"
  nmcli radio wifi off 2>/dev/null; sleep 3; nmcli radio wifi on 2>/dev/null
elif [ "$FAILS" -ge 6 ]; then
  logger -t sage-wifi-watch "still offline, rebooting"
  rm -f "$STATE"
  systemctl reboot
fi
