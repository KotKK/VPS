"""Persistent, non-secret state for foreign egress VPS nodes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from uuid import uuid4


class ExitStatus(StrEnum):
    INSTALLING = "installing"
    READY = "ready"
    ERROR = "error"
    DELETING = "deleting"


class DuplicateExitError(ValueError):
    """Raised when a name or address is already registered."""


@dataclass(frozen=True)
class ExitRecord:
    id: str
    name: str
    address: str
    status: ExitStatus
    stage: str
    error: str
    warning: str
    slot: int
    interface: str
    route_table: int
    mark: int
    tunnel_cidr: str
    remote_port: int
    cancel_requested: bool
    created_at: str
    updated_at: str


class ExitRepository:
    """SQLite repository that allocates stable tunnel identities atomically."""

    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exits (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    address TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    warning TEXT NOT NULL DEFAULT '',
                    slot INTEGER NOT NULL UNIQUE,
                    interface TEXT NOT NULL UNIQUE,
                    route_table INTEGER NOT NULL UNIQUE,
                    mark INTEGER NOT NULL UNIQUE,
                    tunnel_cidr TEXT NOT NULL UNIQUE,
                    remote_port INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ExitRecord:
        return ExitRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            address=str(row["address"]),
            status=ExitStatus(row["status"]),
            stage=str(row["stage"]),
            error=str(row["error"]),
            warning=str(row["warning"]),
            slot=int(row["slot"]),
            interface=str(row["interface"]),
            route_table=int(row["route_table"]),
            mark=int(row["mark"]),
            tunnel_cidr=str(row["tunnel_cidr"]),
            remote_port=int(row["remote_port"]),
            cancel_requested=bool(row["cancel_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _identity(slot: int) -> tuple[str, int, int, str, int]:
        network_base = int(IPv4Network("10.200.0.0/16").network_address)
        tunnel = IPv4Network((network_base + ((slot - 1) * 4), 30))
        interface = "awg-uplink" if slot == 1 else f"awg-uplink-{slot}"
        return interface, 100 + slot, 100 + slot, str(tunnel), 49000 + slot

    def create(self, name: str, address: IPv4Address) -> ExitRecord:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            used_slots = {
                int(row[0]) for row in connection.execute("SELECT slot FROM exits")
            }
            slot = 1
            while slot in used_slots:
                slot += 1
            interface, route_table, mark, tunnel_cidr, remote_port = self._identity(slot)
            exit_id = f"exit-{uuid4().hex[:12]}"
            try:
                connection.execute(
                    """
                    INSERT INTO exits (
                        id, name, address, status, stage, slot, interface,
                        route_table, mark, tunnel_cidr, remote_port,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        exit_id,
                        name,
                        str(address),
                        ExitStatus.INSTALLING.value,
                        "queued",
                        slot,
                        interface,
                        route_table,
                        mark,
                        tunnel_cidr,
                        remote_port,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateExitError("VPS с таким названием или IP уже существует") from exc
            row = connection.execute(
                "SELECT * FROM exits WHERE id = ?", (exit_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._record(row)

    def get(self, exit_id: str) -> ExitRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM exits WHERE id = ?", (exit_id,)
            ).fetchone()
        return None if row is None else self._record(row)

    def list(self) -> list[ExitRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM exits ORDER BY slot").fetchall()
        return [self._record(row) for row in rows]

    def set_stage(
        self,
        exit_id: str,
        status: ExitStatus,
        stage: str,
        error: str = "",
    ) -> ExitRecord:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE exits
                SET status = ?, stage = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, stage, error, now, exit_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(exit_id)
        record = self.get(exit_id)
        assert record is not None
        return record

    def request_cancel(self, exit_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE exits SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), exit_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(exit_id)

    def prepare_retry(self, exit_id: str) -> ExitRecord:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE exits
                SET status = ?, stage = 'queued', error = '', warning = '',
                    cancel_requested = 0, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ExitStatus.INSTALLING.value,
                    now,
                    exit_id,
                    ExitStatus.ERROR.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Повтор возможен только для VPS с ошибкой")
        record = self.get(exit_id)
        assert record is not None
        return record

    def cancel_requested(self, exit_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM exits WHERE id = ?", (exit_id,)
            ).fetchone()
        return bool(row[0]) if row is not None else False

    def delete(self, exit_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM exits WHERE id = ?", (exit_id,))

    def recover_interrupted(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE exits
                SET status = ?, stage = 'interrupted',
                    error = 'Установка прервана перезапуском панели', updated_at = ?
                WHERE status = ?
                """,
                (ExitStatus.ERROR.value, now, ExitStatus.INSTALLING.value),
            )
        return cursor.rowcount


__all__ = [
    "DuplicateExitError",
    "ExitRecord",
    "ExitRepository",
    "ExitStatus",
]
