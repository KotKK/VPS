"""Small SQLite-backed registry for generated client profiles."""

from ipaddress import IPv4Address, IPv4Interface, IPv4Network
import sqlite3
from pathlib import Path


CLIENT_NETWORK = IPv4Network("10.20.0.0/24")
CLIENT_GATEWAY = IPv4Address("10.20.0.1")


def _profile_address(profile: str) -> str | None:
    for line in profile.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "Address":
            continue
        for candidate in value.split(","):
            try:
                interface = IPv4Interface(candidate.strip())
            except ValueError:
                continue
            if interface.ip in CLIENT_NETWORK:
                return str(interface.ip)
    return None


class ClientRegistry:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS clients (name TEXT PRIMARY KEY, profile TEXT NOT NULL, public_key TEXT)")
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(clients)")
            }
            if "address" not in columns:
                conn.execute("ALTER TABLE clients ADD COLUMN address TEXT")
            claimed_addresses: set[str] = set()
            for name, profile, address in conn.execute(
                "SELECT name, profile, address FROM clients ORDER BY name"
            ):
                candidate = str(address or "") or _profile_address(str(profile))
                if not candidate:
                    continue
                if candidate in claimed_addresses:
                    conn.execute(
                        "UPDATE clients SET address = NULL WHERE name = ?",
                        (name,),
                    )
                    continue
                claimed_addresses.add(candidate)
                if candidate != address:
                    conn.execute(
                        "UPDATE clients SET address = ? WHERE name = ?",
                        (candidate, name),
                    )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS clients_address_unique "
                "ON clients(address) WHERE address IS NOT NULL AND address <> ''"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database)

    def put(self, name: str, profile: str, public_key: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO clients(name, profile, public_key, address) VALUES (?, ?, ?, ?)",
                (name, profile, public_key, _profile_address(profile)),
            )

    def reserve_address(self, name: str) -> str:
        """Atomically persist the first free client address before peer mutation."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assigned: set[IPv4Address] = set()
            for address, profile in conn.execute(
                "SELECT address, profile FROM clients"
            ):
                if address:
                    assigned.add(IPv4Address(str(address)))
                if parsed := _profile_address(str(profile)):
                    assigned.add(IPv4Address(parsed))
            address = next(
                (
                    candidate
                    for candidate in CLIENT_NETWORK.hosts()
                    if candidate != CLIENT_GATEWAY and candidate not in assigned
                ),
                None,
            )
            if address is None:
                raise ValueError("client address pool is exhausted")
            try:
                conn.execute(
                    "INSERT INTO clients(name, profile, public_key, address) VALUES (?, '', '', ?)",
                    (name, str(address)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("client name or address is already reserved") from exc
        return f"{address}/32"

    def complete_reservation(self, name: str, profile: str, public_key: str) -> None:
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE clients SET profile = ?, public_key = ? "
                "WHERE name = ? AND profile = ''",
                (profile, public_key, name),
            ).rowcount
        if changed != 1:
            raise ValueError("client address reservation is missing")

    def release_reservation(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM clients WHERE name = ? AND profile = ''",
                (name,),
            )

    def public_key(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT public_key FROM clients WHERE name = ?", (name,)).fetchone()
        return None if row is None else str(row[0] or "")

    def get(self, name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT profile FROM clients WHERE name = ?", (name,)).fetchone()
        return None if row is None else str(row[0])

    def names(self) -> list[str]:
        """Return issued profile names in deterministic display order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM clients WHERE profile <> '' ORDER BY name"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def delete(self, name: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM clients WHERE name = ?", (name,)).rowcount == 1
