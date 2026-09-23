#!/usr/bin/env bash
set -euo pipefail
source /etc/awg-gateway/secrets.env
state=/var/lib/awg-gateway/uplink-health
now=$(date +%s)
last=$(awg show awg-uplink latest-handshakes | awk 'NR==1 {print $2}')
status=down
[[ ${last:-0} -gt $((now-180)) ]] && status=up
previous=$(cat "$state" 2>/dev/null || true)
printf '%s' "$status" >"$state"
if [[ "$status" != "$previous" && -n ${TELEGRAM_BOT_TOKEN:-} && -n ${TELEGRAM_CHAT_ID:-} ]]; then
  curl -fsS --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" -d chat_id="$TELEGRAM_CHAT_ID" --data-urlencode text="AmneziaWG зарубежный VPS: $status" >/dev/null
fi
