"""Probe every configured exit and notify only when its state changes."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Protocol

from gateway.app.exit_store import ExitRecord, ExitRepository, ExitStatus as StoredStatus
from gateway.app.health import ExitProbe, ExitStatus, transition
from gateway.app.notifications import (
    NotificationService,
    production_notification_service,
)


class HealthStateRepository:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exit_health (
                    exit_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                )
                """
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
                ON CONFLICT(exit_id) DO UPDATE SET status = excluded.status
                """,
                (exit_id, status.value),
            )

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
    def __init__(self, max_handshake_age: int = 180) -> None:
        self.max_handshake_age = max_handshake_age

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
            subprocess.run(
                [
                    "curl",
                    "--interface",
                    record.interface,
                    "-4",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "10",
                    "https://api.ipify.org",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return ExitProbe(True, "ok")
        except (OSError, subprocess.SubprocessError):
            return ExitProbe(False, "нет доступа в интернет")


class ExitHealthMonitor:
    def __init__(
        self,
        exits: ExitRepository,
        states: HealthStateRepository,
        probe: ExitProbeTransport,
        notifications: NotificationService,
    ) -> None:
        self.exits = exits
        self.states = states
        self.probe = probe
        self.notifications = notifications

    def run(self) -> None:
        records = self.exits.list()
        self.states.delete_missing({record.id for record in records})
        ready = [record for record in records if record.status is StoredStatus.READY]
        results = {record.id: self.probe.probe(record) for record in ready}
        healthy_interfaces = tuple(
            record.interface for record in ready if results[record.id].healthy
        )

        for record in ready:
            probe = results[record.id]
            current = ExitStatus.UP if probe.healthy else ExitStatus.DOWN
            previous = self.states.get(record.id)
            alert = None
            if previous is None and not probe.healthy:
                alert = "down"
            elif previous is not None:
                state_change = transition(previous, probe)
                alert = None if state_change is None else state_change.kind
            self.states.set(record.id, current)

            if alert == "down":
                self.notifications.notify(
                    f"🔴 VPS «{record.name}» ({record.address}) "
                    f"недоступен: {probe.reason}.",
                    healthy_interfaces,
                )
            elif alert == "up":
                self.notifications.notify(
                    f"🟢 VPS «{record.name}» ({record.address}) снова работает.",
                    healthy_interfaces,
                )

        self.notifications.flush(healthy_interfaces)


def main() -> None:
    state_dir = Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway"))
    ExitHealthMonitor(
        ExitRepository(state_dir / "exits.sqlite3"),
        HealthStateRepository(state_dir / "health.sqlite3"),
        SubprocessExitProbe(),
        production_notification_service(state_dir),
    ).run()


if __name__ == "__main__":
    main()


__all__ = ["ExitHealthMonitor", "HealthStateRepository", "SubprocessExitProbe"]
