import sqlite3

from gateway.app.store import ClientRegistry


def test_legacy_duplicate_profiles_do_not_block_registry_migration(tmp_path):
    database = tmp_path / "clients.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE clients ("
            "name TEXT PRIMARY KEY, profile TEXT NOT NULL, public_key TEXT)"
        )
        connection.executemany(
            "INSERT INTO clients(name, profile, public_key) VALUES (?, ?, ?)",
            [
                ("first", "[Interface]\nAddress = 10.20.0.9/24\n", "first-key"),
                ("second", "[Interface]\nAddress = 10.20.0.9/24\n", "second-key"),
            ],
        )

    registry = ClientRegistry(database)

    assert registry.names() == ["first", "second"]
    assert registry.reserve_address("new") == "10.20.0.2/32"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM clients WHERE address = '10.20.0.9'"
        ).fetchone()[0] == 1


def test_duplicate_profile_keeps_address_unavailable_after_canonical_row_is_deleted(tmp_path):
    database = tmp_path / "clients.sqlite3"
    rows = [
        (f"device-{host}", f"[Interface]\nAddress = 10.20.0.{host}/24\n", f"key-{host}")
        for host in range(2, 9)
    ]
    rows.extend(
        [
            ("first-nine", "[Interface]\nAddress = 10.20.0.9/24\n", "first-key"),
            ("second-nine", "[Interface]\nAddress = 10.20.0.9/24\n", "second-key"),
        ]
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE clients ("
            "name TEXT PRIMARY KEY, profile TEXT NOT NULL, public_key TEXT)"
        )
        connection.executemany(
            "INSERT INTO clients(name, profile, public_key) VALUES (?, ?, ?)",
            rows,
        )

    registry = ClientRegistry(database)
    registry.delete("first-nine")

    assert registry.reserve_address("new") == "10.20.0.10/32"
