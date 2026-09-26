"""Persistent Telegram notifications delivered through a healthy AWG exit."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence


TELEGRAM_IPV4_SUBNETS = (
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


@dataclass(frozen=True)
class PendingNotification:
    id: int
    text: str


class NotificationOutbox:
    """A small SQLite outbox shared by the panel and health timer."""

    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    claimed_until REAL NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(notifications)")
            }
            if "claimed_until" not in columns:
                connection.execute(
                    "ALTER TABLE notifications "
                    "ADD COLUMN claimed_until REAL NOT NULL DEFAULT 0"
                )

    def enqueue(self, text: str) -> None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute(
                "INSERT INTO notifications (text, created_at) VALUES (?, ?)",
                (text, datetime.now(UTC).isoformat()),
            )

    def pending(self) -> list[PendingNotification]:
        with sqlite3.connect(self.database, timeout=30) as connection:
            rows = connection.execute(
                "SELECT id, text FROM notifications ORDER BY id"
            ).fetchall()
        return [PendingNotification(int(row[0]), str(row[1])) for row in rows]

    def delete(self, notification_id: int) -> None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute(
                "DELETE FROM notifications WHERE id = ?", (notification_id,)
            )

    def claim(self, lease_seconds: int) -> PendingNotification | None:
        """Atomically lease the oldest message without holding a network lock."""
        now = time.time()
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, text, claimed_until FROM notifications "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None or float(row[2]) > now:
                return None
            connection.execute(
                "UPDATE notifications SET claimed_until = ? WHERE id = ?",
                (now + lease_seconds, int(row[0])),
            )
        return PendingNotification(int(row[0]), str(row[1]))

    def release(self, notification_id: int) -> None:
        with sqlite3.connect(self.database, timeout=30) as connection:
            connection.execute(
                "UPDATE notifications SET claimed_until = 0 WHERE id = ?",
                (notification_id,),
            )


class NotificationSender(Protocol):
    def send(self, text: str, interfaces: Sequence[str]) -> bool: ...


def _run_command(args: list[str]) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)


class TelegramSender:
    """Switch Telegram routes and try each known-good VPN interface."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        runner: Callable[[list[str]], None] = _run_command,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.runner = runner

    def send(self, text: str, interfaces: Sequence[str]) -> bool:
        if not self.token or not self.chat_id:
            return False
        for interface in dict.fromkeys(interfaces):
            try:
                for subnet in TELEGRAM_IPV4_SUBNETS:
                    self.runner(
                        ["ip", "route", "replace", subnet, "dev", interface]
                    )
                self.runner(
                    [
                        "curl",
                        "--interface",
                        interface,
                        "-4",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "10",
                        "-X",
                        "POST",
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        "-d",
                        f"chat_id={self.chat_id}",
                        "--data-urlencode",
                        f"text={text}",
                    ]
                )
                return True
            except (OSError, RuntimeError, subprocess.SubprocessError):
                continue
        return False


class NotificationService:
    def __init__(
        self,
        outbox: NotificationOutbox,
        sender: NotificationSender,
    ) -> None:
        self.outbox = outbox
        self.sender = sender

    def notify(self, text: str, interfaces: Sequence[str]) -> None:
        self.outbox.enqueue(text)
        self.flush(interfaces)

    def flush(self, interfaces: Sequence[str]) -> None:
        if not interfaces:
            return
        lease_seconds = 60 + (30 * len(interfaces))
        while notification := self.outbox.claim(lease_seconds):
            try:
                if not self.sender.send(notification.text, interfaces):
                    self.outbox.release(notification.id)
                    return
            except Exception:
                self.outbox.release(notification.id)
                raise
            self.outbox.delete(notification.id)


def production_notification_service(state_dir: Path) -> NotificationService:
    return NotificationService(
        NotificationOutbox(state_dir / "notifications.sqlite3"),
        TelegramSender(
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_CHAT_ID", ""),
        ),
    )


__all__ = [
    "NotificationOutbox",
    "NotificationService",
    "PendingNotification",
    "TelegramSender",
    "production_notification_service",
]
