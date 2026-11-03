"""Persistent client assignments, serialized across panel and health processes."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from gateway.app.apply import ApplyResult, EgressApplier, SubprocessEgressRunner
from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.renderer import ClientRoute, ExitRoute, GatewayState


def assign_clients(clients, exits, previous):
    """Keep healthy assignments; place new/displaced clients on least-used exits."""
    available = {route.name for route in exits}
    counts = {route.name: 0 for route in exits}
    retained = {}
    for name, address in clients:
        old = previous.get(name)
        if old and old.address == address and old.exit_id in available:
            retained[name] = old
            counts[old.exit_id] += 1
    for name, address in clients:
        if name not in retained and counts:
            selected = min(counts, key=lambda exit_id: counts[exit_id])
            retained[name] = ClientRoute(name, address, selected)
            counts[selected] += 1
    return tuple(retained[name] for name, _ in clients if name in retained)


class ClientRoutingController:
    def __init__(self, state_dir: Path, applier=None):
        self.state_dir = state_dir
        self.applier = applier or EgressApplier(SubprocessEgressRunner())
        state_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS routing (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.state_dir / "routing.sqlite3", timeout=60)

    @staticmethod
    def _read_state(conn):
        row = conn.execute("SELECT state FROM routing WHERE id=1").fetchone()
        if not row:
            return GatewayState((), client_routing=True)
        value = json.loads(row[0])
        return GatewayState(
            tuple(ExitRoute(**item) for item in value["exits"]),
            tuple(ClientRoute(**item) for item in value["clients"]),
            True,
        )

    def active_state(self):
        with self._connect() as conn:
            return self._read_state(conn)

    def _healthy(self, desired):
        path = self.state_dir / "health.sqlite3"
        states = {}
        if path.exists():
            with sqlite3.connect(path, timeout=30) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='exit_health'").fetchone():
                    states = dict(conn.execute("SELECT exit_id,status FROM exit_health"))
        return tuple(route for route in desired.exits if states.get(route.name) != "down")

    def _clients(self):
        path = self.state_dir / "clients.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(path, timeout=30) as conn:
            return conn.execute("SELECT name,address FROM clients WHERE profile <> '' AND address IS NOT NULL AND address <> '' ORDER BY name").fetchall()

    def _sync_locked(self, conn, desired, force):
        previous = self._read_state(conn)
        exits = self._healthy(desired)
        clients = assign_clients(self._clients(), exits, {c.name: c for c in previous.clients})
        target = GatewayState(exits, clients, True)
        if target != previous or force:
            result = self.applier.apply(previous, target)
            if result.active != target:
                raise RuntimeError("Не удалось применить маршрутизацию клиентов")
            conn.execute("INSERT INTO routing VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (json.dumps(asdict(target)),))
        return target

    def sync(self, force=False):
        with self._connect() as conn:
            # SQLite provides one cross-process lock for both generated files,
            # route mutations and assignment commits, including client changes.
            conn.execute("BEGIN IMMEDIATE")
            routes = tuple(
                ExitRoute(r.id, r.interface, r.address, r.route_table, r.mark)
                for r in ExitRepository(self.state_dir / "exits.sqlite3").list()
                if r.status is ExitStatus.READY
            )
            return self._sync_locked(conn, GatewayState(routes), force)

    def apply(self, previous, desired):
        """Lifecycle adapter: report acceptance of desired nodes, even if unhealthy."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._sync_locked(conn, desired, True)
        return ApplyResult(active=desired)


def main():
    import os
    ClientRoutingController(Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))).sync(force=True)


if __name__ == "__main__":
    main()
