from ipaddress import IPv4Address, IPv4Network

from gateway.app.exit_network import LocalUplinkManager, TunnelAllocation
from gateway.app.exit_store import ExitRepository


class RecordingRunner:
    def __init__(self):
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args: list[str], input_text: str | None = None) -> str:
        self.calls.append((args, input_text))
        if args == ["awg", "genkey"]:
            return "local-private\n"
        if args == ["awg", "pubkey"]:
            return "local-public\n"
        return ""


def test_slot_two_addresses_match_the_second_30():
    """An off-by-one allocation would make two tunnels share an address."""
    allocation = TunnelAllocation.for_slot(2)
    assert allocation.network == IPv4Network("10.200.0.4/30")
    assert allocation.remote_address == IPv4Address("10.200.0.5")
    assert allocation.local_address == IPv4Address("10.200.0.6")


def test_staged_local_uplink_contains_allocated_addresses_and_remote_endpoint(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    record = repo.create("nl", IPv4Address("203.0.113.3"))
    runner = RecordingRunner()
    manager = LocalUplinkManager(
        runner,
        config_dir=tmp_path / "amnezia",
        systemd_dir=tmp_path / "systemd",
    )

    keys = manager.generate_keys()
    manager.stage(record, keys.private_key, "remote-public")

    config = (tmp_path / "amnezia" / "awg-uplink.conf").read_text(encoding="utf-8")
    unit = (tmp_path / "systemd" / "awg-uplink.service").read_text(encoding="utf-8")
    assert "Address = 10.200.0.2/30" in config
    assert "Endpoint = 203.0.113.3:49001" in config
    assert "PrivateKey = local-private" in config
    assert "PublicKey = remote-public" in config
    assert "ExecStart=/usr/bin/awg-quick up /etc/amnezia/awg-uplink.conf" in unit


def test_existing_first_uplink_is_adopted_without_overwrite(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    record = repo.create("existing", IPv4Address("153.76.194.217"))
    config_dir = tmp_path / "amnezia"
    systemd_dir = tmp_path / "systemd"
    config_dir.mkdir()
    systemd_dir.mkdir()
    config_path = config_dir / "awg-uplink.conf"
    unit_path = systemd_dir / "awg-uplink.service"
    config_path.write_text("live-config\n", encoding="utf-8")
    unit_path.write_text("live-unit\n", encoding="utf-8")
    manager = LocalUplinkManager(
        RecordingRunner(), config_dir=config_dir, systemd_dir=systemd_dir
    )

    manager.stage(record, "replacement-private", "replacement-public")

    assert config_path.read_text(encoding="utf-8") == "live-config\n"
    assert unit_path.read_text(encoding="utf-8") == "live-unit\n"
