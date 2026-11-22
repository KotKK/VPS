"""Restore issued client peers after the ingress interface is recreated."""

from __future__ import annotations

import base64
import os
import sqlite3
from ipaddress import IPv4Address
from pathlib import Path

from gateway.app.peers import AwgPeerManager
from gateway.app.store import CLIENT_GATEWAY, CLIENT_NETWORK


def restore_clients(database: Path, manager: AwgPeerManager | None = None) -> int:
    if not database.is_file():
        # A fresh installation has no clients yet. Never create an empty registry here.
        return 0
    manager = manager or AwgPeerManager()
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT public_key,address FROM clients WHERE profile <> '' ORDER BY name"
        ).fetchall()
        peers = []
        addresses, keys = set(), set()
        # Validate the complete snapshot before changing any live peer.
        for key, value in rows:
            if not isinstance(key, str) or len(base64.b64decode(key, validate=True)) != 32:
                raise ValueError("Invalid persisted client public key")
            address = IPv4Address(value)
            if (address not in CLIENT_NETWORK
                    or address in {CLIENT_NETWORK.network_address, CLIENT_NETWORK.broadcast_address, CLIENT_GATEWAY}
                    or address in addresses or key in keys):
                raise ValueError("Invalid persisted client allocation")
            addresses.add(address)
            keys.add(key)
            peers.append((key, f"{address}/32"))
        # Only add/update issued peers. The runtime interface may already contain peers.
        for key, address in peers:
            manager.add(key, address)
    return len(peers)


def main() -> None:
    state_dir = Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))
    count = restore_clients(state_dir / "clients.sqlite3")
    print(f"Restored {count} persisted AmneziaWG client peers", flush=True)


if __name__ == "__main__":
    main()
