from pathlib import Path


def test_web_service_is_loopback_only():
    unit = Path("gateway/deploy/systemd/gateway-web.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8080" in unit


def test_installer_reads_telegram_secret_without_echoing_it():
    script = Path("gateway/deploy/install.sh").read_text(encoding="utf-8")
    assert "read -r -s TELEGRAM_BOT_TOKEN" in script
    assert 'echo "$TELEGRAM_BOT_TOKEN"' not in script


def test_exit_routing_provisioner_persists_forwarding_nat_and_client_route():
    """Every foreign exit needs the return route that fixed the live outage."""
    script = Path("gateway/deploy/provision-exit-routing.sh").read_text(encoding="utf-8")
    assert '[[ $(id -u) -eq 0 ]] || exit 64' in script
    assert "net.ipv4.ip_forward = 1" in script
    assert 'ip route replace "$CLIENT_SUBNET" dev "$EXIT_INTERFACE"' in script
    assert "GATEWAY_UPLINK_ADDRESS=${AWG_GATEWAY_UPLINK_ADDRESS:-10.200.0.2}" in script
    assert 'ip saddr { $CLIENT_SUBNET, $GATEWAY_UPLINK_ADDRESS } accept' in script
    assert 'ip saddr { $CLIENT_SUBNET, $GATEWAY_UPLINK_ADDRESS } masquerade' in script
    assert "ExecStartPre=-/usr/sbin/nft delete table ip awg_exit" in script


def test_telegram_is_routed_through_awg_uplink_before_bot_starts():
    route_script = Path("gateway/deploy/telegram-vpn-route.sh").read_text(encoding="utf-8")
    route_unit = Path(
        "gateway/deploy/systemd/gateway-telegram-route.service"
    ).read_text(encoding="utf-8")
    bot_unit = Path("gateway/deploy/systemd/gateway-telegram.service").read_text(
        encoding="utf-8"
    )

    telegram_ipv4_subnets = (
        "91.108.56.0/22",
        "91.108.4.0/22",
        "91.108.8.0/22",
        "91.108.16.0/22",
        "91.108.12.0/22",
        "149.154.160.0/20",
        "91.105.192.0/23",
        "91.108.20.0/22",
        "185.76.151.0/24",
    )
    for subnet in telegram_ipv4_subnets:
        assert subnet in route_script

    assert 'ip route replace "$subnet" dev "$UPLINK_INTERFACE"' in route_script
    assert "Requires=awg-uplink.service" in route_unit
    assert "Before=gateway-telegram.service" in route_unit
    assert "Requires=gateway-telegram-route.service" in bot_unit
    assert "After=gateway-telegram-route.service" in bot_unit


def test_health_timer_runs_monitor_without_exposing_telegram_secrets():
    script = Path("gateway/deploy/health-check.sh").read_text(encoding="utf-8")
    unit = Path("gateway/deploy/systemd/gateway-health.service").read_text(encoding="utf-8")
    timer = Path("gateway/deploy/systemd/gateway-health.timer").read_text(encoding="utf-8")
    assert "awg show awg-uplink latest-handshakes" in script
    assert "TELEGRAM_BOT_TOKEN" in script
    assert "ExecStart=/opt/awg-gateway/gateway/deploy/health-check.sh" in unit
    assert "OnUnitActiveSec=60" in timer


def test_web_service_allows_outbound_ssh_but_remains_loopback_only():
    unit = Path("gateway/deploy/systemd/gateway-web.service").read_text(
        encoding="utf-8"
    )
    assert "--host 127.0.0.1 --port 8080" in unit
    assert "IPAddressDeny=any" not in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in unit
    assert "ReadWritePaths=/var/lib/awg-gateway /etc/amnezia /etc/systemd/system /run/awg-gateway" in unit


def test_installer_prepares_root_only_ssh_state_and_remote_script():
    script = Path("gateway/deploy/install.sh").read_text(encoding="utf-8")
    assert "install -d -m 0700 /var/lib/awg-gateway /run/awg-gateway" in script
    assert "touch /var/lib/awg-gateway/known_hosts" in script
    assert "chmod 0600 /var/lib/awg-gateway/known_hosts" in script
    assert "install -m 0700 gateway/deploy/remote-exit.sh" in script


def test_debian_12_remote_install_is_pinned_to_verified_official_sources():
    script = Path("gateway/deploy/remote-exit.sh").read_text(encoding="utf-8")
    assert "ae0924ca700520ca34c5bdbcfd05b2f683ea9353" in script
    assert "61e741780e8465a67a7d7fb6cffe14a8a15d624a" in script
    assert "https://github.com/amnezia-vpn/amneziawg-linux-kernel-module.git" in script
    assert "https://github.com/amnezia-vpn/amneziawg-tools.git" in script
    assert "dkms install -m amneziawg -v 1.0.0" in script


def test_remote_configuration_cleanup_does_not_defer_a_local_variable():
    script = Path("gateway/deploy/remote-exit.sh").read_text(encoding="utf-8")
    assert "trap 'rm -f \"$environment_file\"' EXIT" not in script
    assert "trap - EXIT" in script


def test_remote_reconfiguration_restarts_active_oneshot_services():
    """A retry must apply newly generated peer keys to the live interface."""
    script = Path("gateway/deploy/remote-exit.sh").read_text(encoding="utf-8")
    assert "systemctl enable awg-exit.service awg-exit-routing.service" in script
    assert "systemctl restart awg-exit.service awg-exit-routing.service" in script


def test_operations_doc_uses_a_password_placeholder_only():
    text = Path("docs/panel-vps-operations.md").read_text(encoding="utf-8")
    assert "<ПАРОЛЬ_НОВОГО_VPS>" in text
    assert "пароль не сохраняется" in text.lower()
