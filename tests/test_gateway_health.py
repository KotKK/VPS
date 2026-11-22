import json
import subprocess

from gateway.app.gateway_health import GatewayCheck, GatewayHealthMonitor, GatewayProbe
from gateway.app.store import ClientRegistry


class Notifications:
    def __init__(self):
        self.messages = []
    def notify(self, text, interfaces):
        self.messages.append((text, tuple(interfaces)))
    def flush(self, interfaces):
        pass


class Probe:
    def __init__(self, checks):
        self.results = checks
    def checks(self):
        return self.results


def healthy_runner(args):
    if args[0] == "modinfo":
        return "module exists"
    if args[0] == "uname":
        return "6.12-test"
    if args[0] == "dpkg-query":
        return "installed"
    if args[0] == "sysctl":
        return "1"
    if args[0] == "awg":
        return "key1\t10.20.0.2/32\nkey2\t10.20.0.10/32"
    if args[0] == "ip":
        return json.dumps([{"flags": ["UP", "POINTOPOINT"]}])
    raise AssertionError(args)


def registry(tmp_path):
    result = ClientRegistry(tmp_path / "clients.sqlite3")
    result.put("iphone", "[Interface]\nAddress = 10.20.0.2/24\n", "key1")
    result.put("router", "[Interface]\nAddress = 10.20.0.10/24\n", "key2")
    return result


def test_healthy_idle_clients_need_no_handshake_and_reservations_are_ignored(tmp_path):
    clients = registry(tmp_path)
    clients.reserve_address("pending")
    results = GatewayProbe(tmp_path, healthy_runner, boot_image=tmp_path / "absent").checks()
    assert all(check.healthy for check in results)


def test_missing_peer_is_detected_even_when_module_and_all_tunnels_exist(tmp_path):
    registry(tmp_path)
    def runner(args):
        return "key1\t10.20.0.2/32" if args[0] == "awg" else healthy_runner(args)
    check = next(c for c in GatewayProbe(tmp_path, runner).checks() if c.name == "client-peers")
    assert not check.healthy
    assert "1 из 2" in check.detail


def test_wrong_allowed_address_is_detected(tmp_path):
    registry(tmp_path)
    def runner(args):
        return "key1\t10.20.0.2/32\nkey2\t10.20.0.99/32" if args[0] == "awg" else healthy_runner(args)
    assert not next(c for c in GatewayProbe(tmp_path, runner).checks() if c.name == "client-peers").healthy


def test_future_kernel_without_awg_is_reported_before_reboot(tmp_path):
    registry(tmp_path)
    future_image = tmp_path / "vmlinuz-6.12-next"
    future_image.touch()
    def runner(args):
        if args[0] == "dpkg":
            return ""
        if args[:2] == ["modinfo", "-k"]:
            raise subprocess.CalledProcessError(1, args)
        return healthy_runner(args)
    results = GatewayProbe(tmp_path, runner, boot_image=future_image).checks()
    assert next(c for c in results if c.name == "kernel-module").healthy
    assert not next(c for c in results if c.name == "next-kernel-module").healthy


def test_new_installed_kernel_is_checked_without_vmlinuz_symlink(tmp_path):
    registry(tmp_path)
    boot_dir = tmp_path / "boot"
    boot_dir.mkdir()
    for version in ("6.12-old", "6.12-test", "6.12-next"):
        (boot_dir / ("vmlinuz-" + version)).touch()
    def runner(args):
        if args[0] == "dpkg":
            if args[2] == "6.12-old":
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[:2] == ["modinfo", "-k"]:
            assert args[2] == "6.12-next"
            raise subprocess.CalledProcessError(1, args)
        return healthy_runner(args)
    results = GatewayProbe(tmp_path, runner, boot_image=tmp_path / "absent", boot_directory=boot_dir).checks()
    check = next(c for c in results if c.name == "next-kernel-module")
    assert not check.healthy
    assert "6.12-next" in check.detail
    assert "6.12-old" not in check.detail


def test_missing_ingress_does_not_emit_a_second_missing_peers_alarm(tmp_path):
    registry(tmp_path)
    def runner(args):
        if args[0] in {"awg", "ip"}:
            raise subprocess.CalledProcessError(1, args)
        return healthy_runner(args)
    results = GatewayProbe(tmp_path, runner).checks()
    assert not next(c for c in results if c.name == "client-ingress").healthy
    assert not any(c.name == "client-peers" for c in results)


def test_link_down_is_reported_even_if_peers_remain_loaded(tmp_path):
    registry(tmp_path)
    def runner(args):
        return json.dumps([{"flags": ["POINTOPOINT"]}]) if args[0] == "ip" else healthy_runner(args)
    assert not next(c for c in GatewayProbe(tmp_path, runner).checks() if c.name == "client-ingress").healthy


def test_alert_state_survives_timer_restart_and_sends_once_per_edge(tmp_path):
    notifier = Notifications()
    probe = Probe([GatewayCheck("client-peers", False, "пропали клиентские подключения")])
    database = tmp_path / "health.sqlite3"
    GatewayHealthMonitor(database, probe, notifier).run(())
    assert notifier.messages == []  # Ignore a transient create/delete operation.
    GatewayHealthMonitor(database, probe, notifier).run(())
    assert len(notifier.messages) == 1
    assert notifier.messages[0][0].startswith("🔴 Российский VPS:")
    GatewayHealthMonitor(database, probe, notifier).run(())
    assert len(notifier.messages) == 1
    probe.results = [GatewayCheck("client-peers", True, "unused")]
    GatewayHealthMonitor(database, probe, notifier).run(("awg-uplink",))
    assert len(notifier.messages) == 2
    assert "🟢 Российский VPS: все клиентские подключения загружены корректно." == notifier.messages[-1][0]


def test_successful_check_clears_a_transient_failure_streak(tmp_path):
    notifier = Notifications()
    probe = Probe([GatewayCheck("forwarding", False, "disabled")])
    monitor = GatewayHealthMonitor(tmp_path / "health.sqlite3", probe, notifier)
    monitor.run(())
    probe.results = [GatewayCheck("forwarding", True, "unused")]
    monitor.run(())
    probe.results = [GatewayCheck("forwarding", False, "disabled")]
    monitor.run(())
    assert notifier.messages == []
