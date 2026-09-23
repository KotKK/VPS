"""Small SQLite-backed registry for generated client profiles."""

import sqlite3
from pathlib import Path


class ClientRegistry:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS clients (name TEXT PRIMARY KEY, profile TEXT NOT NULL, public_key TEXT)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database)

    def put(self, name: str, profile: str, public_key: str = "") -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO clients(name, profile, public_key) VALUES (?, ?, ?)", (name, profile, public_key))

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
            rows = conn.execute("SELECT name FROM clients ORDER BY name").fetchall()
        return [str(row[0]) for row in rows]

    def delete(self, name: str) -> bool:
        with self._connect() as conn:
            return conn.execute("DELETE FROM clients WHERE name = ?", (name,)).rowcount == 1
