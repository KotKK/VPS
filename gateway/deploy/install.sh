#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 64
fi

printf "Telegram bot token (or 'skip'): " >&2
read -r -s TELEGRAM_BOT_TOKEN
printf '\n'
read -r -p "Telegram chat ID (or 'skip'): " TELEGRAM_CHAT_ID

install -d -m 0700 /etc/awg-gateway
install -d -m 0700 /var/lib/awg-gateway /run/awg-gateway
touch /var/lib/awg-gateway/known_hosts
chmod 0600 /var/lib/awg-gateway/known_hosts
if [[ "$TELEGRAM_BOT_TOKEN" != "skip" && "$TELEGRAM_CHAT_ID" != "skip" ]]; then
  printf 'TELEGRAM_BOT_TOKEN=%q\nTELEGRAM_CHAT_ID=%q\n' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" >/etc/awg-gateway/secrets.env
else
  : >/etc/awg-gateway/secrets.env
fi
chmod 0600 /etc/awg-gateway/secrets.env

command -v awg >/dev/null
command -v awg-quick >/dev/null
modinfo amneziawg >/dev/null

install -d -m 0755 /usr/local/libexec
install -m 0700 gateway/deploy/remote-exit.sh /usr/local/libexec/awg-gateway-remote-exit
install -m 0644 gateway/deploy/systemd/gateway-web.service /etc/systemd/system/gateway-web.service
install -m 0644 gateway/deploy/systemd/gateway-health.service /etc/systemd/system/gateway-health.service
install -m 0644 gateway/deploy/systemd/gateway-health.timer /etc/systemd/system/gateway-health.timer
systemctl daemon-reload
systemctl enable --now gateway-health.timer

echo "Secrets and VPS provisioner installed. Enable gateway-web.service after installing the Python environment."
