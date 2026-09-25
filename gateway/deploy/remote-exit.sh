#!/usr/bin/env bash
set -euo pipefail

MODE=${1:-}

KMOD_REPOSITORY=https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git
KMOD_TAG=v1.0.20260725
KMOD_COMMIT=ae0924ca700520ca34c5bdbcfd05b2f683ea9353
TOOLS_REPOSITORY=https://github.com/amnezia-vpn/amneziawg-tools.git
TOOLS_TAG=v1.0.20260618-2
TOOLS_COMMIT=61e741780e8465a67a7d7fb6cffe14a8a15d624a
SOURCE_STAMP=/var/lib/awg-gateway/amneziawg-source-version

require_supported_debian() {
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ ${ID:-} == debian && ${VERSION_ID:-} =~ ^(12|13)$ ]] || {
    echo "Debian 12 or Debian 13 is required" >&2
    exit 65
  }
}

install_debian_13_packages() {
  curl -fsSL 'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x57290828' \
    | gpg --dearmor --yes -o /usr/share/keyrings/amnezia-ppa.gpg
  cat >/etc/apt/sources.list.d/amneziawg.sources <<'EOF'
Types: deb
URIs: https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu
Suites: noble
Components: main
Signed-By: /usr/share/keyrings/amnezia-ppa.gpg
EOF
  apt-get update
  apt-get install -y amneziawg-dkms amneziawg-tools
}

clone_verified_tag() {
  local repository=${1:?repository is required}
  local tag=${2:?tag is required}
  local expected_commit=${3:?expected commit is required}
  local destination=${4:?destination is required}
  git clone --quiet --depth 1 --branch "$tag" "$repository" "$destination"
  [[ $(git -C "$destination" rev-parse HEAD) == "$expected_commit" ]] || {
    echo "Pinned AmneziaWG source verification failed" >&2
    exit 67
  }
}

install_debian_12_packages() {
  local expected_stamp="$KMOD_COMMIT $TOOLS_COMMIT"
  if [[ -f $SOURCE_STAMP ]] \
    && [[ $(<"$SOURCE_STAMP") == "$expected_stamp" ]] \
    && command -v awg >/dev/null \
    && command -v awg-quick >/dev/null \
    && modinfo amneziawg >/dev/null 2>&1; then
    return
  fi

  apt-get install -y build-essential dkms git
  local build_root module_source tools_source
  build_root=$(mktemp -d)
  module_source=$build_root/amneziawg-kmod
  tools_source=$build_root/amneziawg-tools
  trap "rm -rf -- '$build_root'" EXIT

  clone_verified_tag "$KMOD_REPOSITORY" "$KMOD_TAG" "$KMOD_COMMIT" "$module_source"
  clone_verified_tag "$TOOLS_REPOSITORY" "$TOOLS_TAG" "$TOOLS_COMMIT" "$tools_source"

  dkms remove -m amneziawg -v 1.0.0 --all >/dev/null 2>&1 || true
  make -C "$module_source/src" dkms-install
  dkms add -m amneziawg -v 1.0.0
  dkms build -m amneziawg -v 1.0.0
  dkms install -m amneziawg -v 1.0.0
  make -C "$tools_source/src" -j"$(nproc)"
  make -C "$tools_source/src" install WITH_WGQUICK=yes WITH_SYSTEMDUNITS=yes

  install -d -m 0700 "$(dirname "$SOURCE_STAMP")"
  printf '%s\n' "$expected_stamp" >"$SOURCE_STAMP"
  chmod 0600 "$SOURCE_STAMP"
  rm -rf -- "$build_root"
  trap - EXIT
}

install_packages() {
  require_supported_debian
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg "linux-headers-$(uname -r)" nftables iproute2

  case "$VERSION_ID" in
    12) install_debian_12_packages ;;
    13) install_debian_13_packages ;;
  esac

  command -v awg >/dev/null
  command -v awg-quick >/dev/null
  modinfo amneziawg >/dev/null
  modprobe amneziawg || {
    echo "AWG_KERNEL_MODULE_UNAVAILABLE: the host kernel cannot load amneziawg" >&2
    exit 68
  }
}

configure_exit() {
  local environment_file=${1:?environment file is required}
  [[ -f $environment_file ]] || exit 66
  chmod 0600 "$environment_file"
  # This root-owned file is uploaded by the typed controller, not browser text.
  # shellcheck disable=SC1090
  source "$environment_file"
  trap "rm -f -- '$environment_file'" EXIT

  [[ ${EXIT_INTERFACE:-} =~ ^[A-Za-z0-9_.-]+$ ]]
  [[ ${REMOTE_ADDRESS:-} =~ ^[0-9.]+/[0-9]+$ ]]
  [[ ${LOCAL_ADDRESS:-} =~ ^[0-9.]+/[0-9]+$ ]]
  [[ ${CLIENT_SUBNET:-} =~ ^[0-9.]+/[0-9]+$ ]]
  [[ ${REMOTE_PORT:-} =~ ^[0-9]+$ ]]
  [[ ${LOCAL_PUBLIC_KEY:-} =~ ^[A-Za-z0-9+/=]+$ ]]

  install -d -m 0700 /etc/amnezia
  if [[ ! -s /etc/amnezia/exit-private.key ]]; then
    umask 077
    awg genkey >/etc/amnezia/exit-private.key
  fi
  local remote_private_key remote_public_key gateway_address
  remote_private_key=$(< /etc/amnezia/exit-private.key)
  remote_public_key=$(printf '%s\n' "$remote_private_key" | awg pubkey)
  gateway_address=${LOCAL_ADDRESS%/*}

  cat >/etc/amnezia/awg-exit.conf <<EOF
[Interface]
PrivateKey = $remote_private_key
Address = $REMOTE_ADDRESS
ListenPort = $REMOTE_PORT
Jc = 4
Jmin = 8
Jmax = 80
S1 = 25
S2 = 111
H1 = 234567
H2 = 345678
H3 = 456789
H4 = 567891

[Peer]
PublicKey = $LOCAL_PUBLIC_KEY
AllowedIPs = $LOCAL_ADDRESS, $CLIENT_SUBNET
EOF
  chmod 0600 /etc/amnezia/awg-exit.conf

  cat >/etc/systemd/system/awg-exit.service <<'EOF'
[Unit]
Description=AmneziaWG foreign exit
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/awg-quick up /etc/amnezia/awg-exit.conf
ExecStop=/usr/bin/awg-quick down /etc/amnezia/awg-exit.conf

[Install]
WantedBy=multi-user.target
EOF

  cat >/etc/sysctl.d/90-awg-exit.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
  sysctl --system >/dev/null
  local wan_interface
  wan_interface=$(ip -4 route show default | awk 'NR==1 {print $5}')
  [[ $wan_interface =~ ^[A-Za-z0-9_.-]+$ ]]
  cat >/etc/amnezia/awg-exit-nat.nft <<EOF
table ip awg_exit {
  chain forward {
    type filter hook forward priority filter; policy drop;
    iifname "$EXIT_INTERFACE" oifname "$wan_interface" ip saddr { $CLIENT_SUBNET, $gateway_address } accept
    iifname "$wan_interface" oifname "$EXIT_INTERFACE" ct state established,related accept
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "$wan_interface" ip saddr { $CLIENT_SUBNET, $gateway_address } masquerade
  }
}
EOF
  cat >/etc/systemd/system/awg-exit-routing.service <<EOF
[Unit]
Description=Route AmneziaWG clients to the Internet
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
  systemctl enable awg-exit.service awg-exit-routing.service
  systemctl restart awg-exit.service awg-exit-routing.service
  printf '%s\n' "$remote_public_key"
  rm -f -- "$environment_file"
  trap - EXIT
}

cleanup_exit() {
  systemctl disable --now awg-exit-routing.service awg-exit.service 2>/dev/null || true
  nft delete table ip awg_exit 2>/dev/null || true
  rm -f /etc/systemd/system/awg-exit.service \
    /etc/systemd/system/awg-exit-routing.service \
    /etc/amnezia/awg-exit.conf \
    /etc/amnezia/awg-exit-nat.nft \
    /etc/amnezia/exit-private.key
  systemctl daemon-reload
}

case "$MODE" in
  packages) install_packages ;;
  configure) configure_exit "${2:-}" ;;
  cleanup) cleanup_exit ;;
  *) echo "usage: $0 packages|configure ENV_FILE|cleanup" >&2; exit 64 ;;
esac
