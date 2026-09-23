#!/usr/bin/env bash
# Configure forwarding on a foreign exit after its awg-exit interface exists.
set -euo pipefail

[[ $(id -u) -eq 0 ]] || exit 64

EXIT_INTERFACE=${AWG_EXIT_INTERFACE:-awg-exit}
CLIENT_SUBNET=${AWG_CLIENT_SUBNET:-10.20.0.0/24}
WAN_INTERFACE=${AWG_WAN_INTERFACE:-$(ip -4 route show default | awk 'NR==1 {print $5}')}

[[ $EXIT_INTERFACE =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Invalid exit interface" >&2; exit 64; }
[[ $WAN_INTERFACE =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Unable to determine WAN interface" >&2; exit 64; }
[[ $CLIENT_SUBNET =~ ^[0-9.]+/[0-9]+$ ]] || { echo "Invalid client subnet" >&2; exit 64; }

install -d -m 0700 /etc/amnezia
printf 'net.ipv4.ip_forward = 1\n' >/etc/sysctl.d/90-awg-exit.conf
sysctl --system >/dev/null
ip route replace "$CLIENT_SUBNET" dev "$EXIT_INTERFACE"

cat >/etc/amnezia/awg-exit-nat.nft <<EOF
table ip awg_exit {
  chain forward {
    type filter hook forward priority filter; policy drop;
    iifname "$EXIT_INTERFACE" oifname "$WAN_INTERFACE" ip saddr $CLIENT_SUBNET accept
    iifname "$WAN_INTERFACE" oifname "$EXIT_INTERFACE" ct state established,related accept
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "$WAN_INTERFACE" ip saddr $CLIENT_SUBNET masquerade
  }
}
EOF

cat >/etc/systemd/system/awg-exit-routing.service <<EOF
[Unit]
Description=Route AmneziaWG clients from foreign exit to the Internet
Requires=awg-exit.service
After=awg-exit.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table ip awg_exit
ExecStart=/usr/sbin/ip route replace $CLIENT_SUBNET dev $EXIT_INTERFACE
ExecStart=/usr/sbin/nft -f /etc/amnezia/awg-exit-nat.nft
ExecStop=-/usr/sbin/nft delete table ip awg_exit
ExecStop=-/usr/sbin/ip route del $CLIENT_SUBNET dev $EXIT_INTERFACE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now awg-exit-routing.service
