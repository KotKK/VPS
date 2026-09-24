#!/usr/bin/env bash
# Route Telegram Bot API traffic through the foreign AmneziaWG uplink.
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root." >&2; exit 64; }

UPLINK_INTERFACE=${AWG_UPLINK_INTERFACE:-awg-uplink}
[[ $UPLINK_INTERFACE =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "Invalid uplink interface." >&2
  exit 64
}
ip link show dev "$UPLINK_INTERFACE" >/dev/null

# Official Telegram IPv4 ranges: https://core.telegram.org/resources/cidr.txt
TELEGRAM_IPV4_SUBNETS=(
  91.108.56.0/22
  91.108.4.0/22
  91.108.8.0/22
  91.108.16.0/22
  91.108.12.0/22
  149.154.160.0/20
  91.105.192.0/23
  91.108.20.0/22
  185.76.151.0/24
)

for subnet in "${TELEGRAM_IPV4_SUBNETS[@]}"; do
  ip route replace "$subnet" dev "$UPLINK_INTERFACE"
done

