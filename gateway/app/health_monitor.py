"""Probe every configured exit and notify only when its state changes."""

from __future__ import annotations

import os
import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Protocol
from collections.abc import Callable

from gateway.app.exit_store import ExitRecord, ExitRepository, ExitStatus as StoredStatus
from gateway.app.health import ExitProbe, ExitStatus, transition
from gateway.app.notifications import (
    NotificationService,
    production_notification_service,
)

LOGGER = logging.getLogger(__name__)
PROBE_URLS = (
    "https://api.ipify.org",
    "https://1.1.1.1/cdn-cgi/trace",
    "https://dns.google/resolve?name=example.com",
)


class HealthStateRepository:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exit_health (
                    exit_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    recovery_streak INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(exit_health)")}
            if "recovery_streak" not in columns:
                connection.execute(
                    "ALTER TABLE exit_health ADD COLUMN recovery_streak INTEGER NOT NULL DEFAULT 0"
                )

    def get(self, exit_id: str) -> ExitStatus | None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            row = connection.execute(
                "SELECT status FROM exit_health WHERE exit_id = ?", (exit_id,)
            ).fetchone()
        return None if row is None else ExitStatus(str(row[0]))

    def set(self, exit_id: str, status: ExitStatus) -> None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute(
                """
                INSERT INTO exit_health (exit_id, status) VALUES (?, ?)
                ON CONFLICT(exit_id) DO UPDATE SET
                    status = excluded.status, recovery_streak = 0
                """,
                (exit_id, status.value),
            )

    def observe(
        self, exit_id: str, healthy: bool, recovery_checks: int,
    ) -> tuple[ExitStatus | None, ExitStatus]:
        """Record one check, preserving a recovery streak across timer runs."""
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, recovery_streak FROM exit_health WHERE exit_id = ?",
                (exit_id,),
            ).fetchone()
            previous = ExitStatus(row[0]) if row else None
            current = ExitStatus.UP if healthy else ExitStatus.DOWN
            streak = 0
            if healthy and previous is ExitStatus.DOWN:
                streak = row[1] + 1
                if streak < recovery_checks:
                    current = ExitStatus.DOWN
                else:
                    streak = 0
            connection.execute(
                """
                INSERT INTO exit_health (exit_id, status, recovery_streak) VALUES (?, ?, ?)
                ON CONFLICT(exit_id) DO UPDATE SET
                    status = excluded.status, recovery_streak = excluded.recovery_streak
                """,
                (exit_id, current.value, streak),
            )
            return previous, current

    def delete_missing(self, exit_ids: set[str]) -> None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            rows = connection.execute("SELECT exit_id FROM exit_health").fetchall()
            for (exit_id,) in rows:
                if str(exit_id) not in exit_ids:
                    connection.execute(
                        "DELETE FROM exit_health WHERE exit_id = ?", (exit_id,)
                    )


class ExitProbeTransport(Protocol):
    def probe(self, record: ExitRecord) -> ExitProbe: ...


class SubprocessExitProbe:
    def __init__(
        self,
        max_handshake_age: int = 180,
        internet_attempts: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self.max_handshake_age = max_handshake_age
        self.internet_attempts = max(1, internet_attempts)
        self.retry_delay = max(0.0, retry_delay)

    def probe(self, record: ExitRecord) -> ExitProbe:
        try:
            result = subprocess.run(
                ["awg", "show", record.interface, "latest-handshakes"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            handshakes = [
                int(line.split()[1])
                for line in result.stdout.splitlines()
                if len(line.split()) >= 2 and line.split()[1].isdigit()
            ]
            if not handshakes or max(handshakes) <= int(time.time()) - self.max_handshake_age:
                return ExitProbe(False, "handshake устарел")
        except (OSError, subprocess.SubprocessError):
            return ExitProbe(False, "не удалось проверить туннель")

        failures = []
        for attempt in range(self.internet_attempts):
            url = PROBE_URLS[attempt % len(PROBE_URLS)]
            try:
                subprocess.run(
                    [
                        "curl",
                        "--interface",
                        record.interface,
                        "-4",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--connect-timeout",
                        "3",
                        "--max-time",
                        "6",
                        url,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return ExitProbe(True, "ok")
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None) or type(exc).__name__
                failures.append(f"{url}: {detail.strip()}")
                if attempt + 1 < self.internet_attempts and self.retry_delay:
                    time.sleep(self.retry_delay)

        LOGGER.warning("Exit %s (%s) probe failed: %s", record.address, record.interface, " | ".join(failures))
        return ExitProbe(False, "нет доступа в интернет")


class ExitHealthMonitor:
    def __init__(
        self,
        exits: ExitRepository,
        states: HealthStateRepository,
        probe: ExitProbeTransport,
        notifications: NotificationService,
        sync_routing: Callable[[], object] | None = None,
        recovery_checks: int = 3,
    ) -> None:
        self.exits = exits
        self.states = states
        self.probe = probe
        self.notifications = notifications
        self.sync_routing = sync_routing
        self.recovery_checks = max(1, recovery_checks)

    def run(self) -> None:
        records = self.exits.list()
        self.states.delete_missing({record.id for record in records})
        ready = [record for record in records if record.status is StoredStatus.READY]
        results = {record.id: self.probe.probe(record) for record in ready}
        successful = [record for record in ready if results[record.id].healthy]
        # Restore one working path immediately when waiting would leave all
        # clients offline. Otherwise require consecutive successful runs.
        first_recovery = None
        if successful and all(self.states.get(record.id) is ExitStatus.DOWN for record in successful):
            first_recovery = successful[0].id

        alerts = []
        healthy_interfaces = []
        for record in ready:
            probe = results[record.id]
            previous, current = self.states.observe(
                record.id, probe.healthy,
                1 if record.id == first_recovery else self.recovery_checks,
            )
            if current is ExitStatus.UP:
                healthy_interfaces.append(record.interface)
            alert = None
            if previous is None and current is ExitStatus.DOWN:
                alert = "down"
            elif previous is not None and previous is not current:
                state_change = transition(previous, probe)
                alert = None if state_change is None else state_change.kind
            alerts.append((record, probe, alert))

        if self.sync_routing is not None:
            self.sync_routing()

        interfaces = tuple(healthy_interfaces)
        for record, probe, alert in alerts:
            if alert == "down":
                self.notifications.notify(
                    f"🔴 VPS «{record.name}» ({record.address}) "
                    f"недоступен: {probe.reason}.",
                    interfaces,
                )
            elif alert == "up":
                self.notifications.notify(
                    f"🟢 VPS «{record.name}» ({record.address}) снова работает.",
                    interfaces,
                )

        self.notifications.flush(interfaces)


def main() -> None:
    from gateway.app.client_routing import ClientRoutingController
    state_dir = Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))
    ExitHealthMonitor(
        ExitRepository(state_dir / "exits.sqlite3"),
        HealthStateRepository(state_dir / "health.sqlite3"),
        SubprocessExitProbe(),
        production_notification_service(state_dir),
        sync_routing=ClientRoutingController(state_dir).sync,
    ).run()


if __name__ == "__main__":
    main()


__all__ = ["ExitHealthMonitor", "HealthStateRepository", "SubprocessExitProbe"]
