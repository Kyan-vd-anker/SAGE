#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this as the normal Sage user, not as root." >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required to restart Sage." >&2
  exit 1
fi

read -r -s -p "Paste the replacement Telegram bot token (input hidden): " token
echo
if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  unset token
  echo "That does not look like a Telegram bot token. Nothing was saved." >&2
  exit 1
fi

config_dir="$HOME/.config/sage"
dropin_dir="$HOME/.config/systemd/user/sage.service.d"
install -d -m 700 "$config_dir" "$dropin_dir"
umask 077
printf 'SAGE_TELEGRAM_BOT_TOKEN=%s\n' "$token" > "$config_dir/telegram.env"
unset token
cat > "$dropin_dir/telegram.conf" <<'UNIT'
[Service]
EnvironmentFile=%h/.config/sage/telegram.env
UNIT
chmod 600 "$config_dir/telegram.env" "$dropin_dir/telegram.conf"

systemctl --user daemon-reload
systemctl --user restart sage.service
echo "Telegram token saved privately and Sage restarted. The token was not printed or added to shell history."
