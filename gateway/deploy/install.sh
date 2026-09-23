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

install -d -m 0700 /etc/awg-gateway /var/lib/awg-gateway
if [[ "$TELEGRAM_BOT_TOKEN" != "skip" && "$TELEGRAM_CHAT_ID" != "skip" ]]; then
  printf 'TELEGRAM_BOT_TOKEN=%q\nTELEGRAM_CHAT_ID=%q\n' "$TELEGRAM_BOT_TOKEN" "$TELEGRAM_CHAT_ID" >/etc/awg-gateway/secrets.env
else
  : >/etc/awg-gateway/secrets.env
fi
chmod 0600 /etc/awg-gateway/secrets.env

command -v awg >/dev/null
command -v awg-quick >/dev/null
modinfo amneziawg >/dev/null

echo "Secrets saved. Install the application bundle and gateway-web.service before starting the panel."
