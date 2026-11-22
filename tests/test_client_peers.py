import base64
import sqlite3

import pytest

from gateway.app.client_peers import restore_clients
from gateway.app.store import ClientRegistry


class Manager:
    def __init__(self):
        self.peers = {}
        self.calls = []

    def add(self, key, address):
        self.calls.append((key, address))
        self.peers[key] = address


def key(number):
    return base64.b64encode(bytes([number]) * 32).decode()


def test_restore_after_interface_recreation_retains_keys_and_exact_client_addresses(tmp_path):
    path = tmp_path / "clients.sqlite3"
    registry = ClientRegistry(path)
    registry.put("iphone", "[Interface]\nAddress = 10.20.0.2/24\nPrivateKey = confidential\n", key(1))
    registry.put("router", "[Interface]\nAddress = 10.20.0.10/24\nPrivateKey = confidential\n", key(2))
    registry.reserve_address("unfinished")
    manager = Manager()
    expected = {key(1): "10.20.0.2/32", key(2): "10.20.0.10/32"}
    assert restore_clients(path, manager) == 2
    assert manager.peers == expected
    manager.peers.clear()  # Recreating an AWG interface discards its runtime peers.
    assert restore_clients(path, manager) == 2
    assert manager.peers == expected
    assert "confidential" in registry.get("iphone")
    assert "unfinished" not in registry.names()


def test_deleted_clients_are_not_restored_and_repeated_restore_is_safe(tmp_path):
    path = tmp_path / "clients.sqlite3"
    registry = ClientRegistry(path)
    registry.put("removed", "[Interface]\nAddress = 10.20.0.2/24\n", key(1))
    registry.put("kept", "[Interface]\nAddress = 10.20.0.3/24\n", key(2))
    registry.delete("removed")
    manager = Manager()
    for _ in range(2):
        assert restore_clients(path, manager) == 1
    assert manager.peers == {key(2): "10.20.0.3/32"}


@pytest.mark.parametrize("bad_key,bad_address", [
    ("invalid", "10.20.0.4"), (key(3), "10.20.0.1"), (key(3), "192.168.0.2"),
    (key(3), "10.20.0.255"), (key(1), "10.20.0.4"), (key(3), None),
])
def test_invalid_registry_cannot_partially_change_live_peers(tmp_path, bad_key, bad_address):
    path = tmp_path / "clients.sqlite3"
    registry = ClientRegistry(path)
    registry.put("first", "[Interface]\nAddress = 10.20.0.2/24\n", key(1))
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO clients(name,profile,public_key,address) VALUES ('last','issued',?,?)", (bad_key,bad_address))
    manager = Manager()
    with pytest.raises((ValueError, TypeError)):
        restore_clients(path, manager)
    assert manager.calls == []


def test_fresh_install_does_not_create_registry_or_peers(tmp_path):
    path = tmp_path / "missing.sqlite3"
    manager = Manager()
    assert restore_clients(path, manager) == 0
    assert manager.calls == []
    assert not path.exists()
