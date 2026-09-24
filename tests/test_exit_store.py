import sqlite3
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address

import pytest

from gateway.app.exit_store import DuplicateExitError, ExitRepository, ExitStatus


def test_concurrent_creates_allocate_distinct_network_identity(tmp_path):
    """A race must not assign two foreign VPS nodes the same tunnel slot."""
    repo = ExitRepository(tmp_path / "state.sqlite3")

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(
            pool.map(
                lambda pair: repo.create(pair[0], IPv4Address(pair[1])),
                [("de", "203.0.113.2"), ("nl", "203.0.113.3")],
            )
        )

    assert {record.slot for record in records} == {1, 2}
    assert {record.interface for record in records} == {"awg-uplink", "awg-uplink-2"}
    assert {record.route_table for record in records} == {101, 102}
    assert {record.mark for record in records} == {101, 102}
    assert {record.tunnel_cidr for record in records} == {
        "10.200.0.0/30",
        "10.200.0.4/30",
    }


def test_schema_never_contains_password_column(tmp_path):
    """A future schema change must not accidentally persist SSH credentials."""
    repo = ExitRepository(tmp_path / "state.sqlite3")
    repo.create("de", IPv4Address("203.0.113.2"))

    with sqlite3.connect(repo.database) as connection:
        columns = connection.execute("PRAGMA table_info(exits)").fetchall()

    assert "password" not in {column[1] for column in columns}


def test_restart_recovers_interrupted_deletion(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("Germany", IPv4Address("203.0.113.10"))
    repository.set_stage(record.id, ExitStatus.DELETING, "remove_balance")

    assert repository.recover_interrupted() == 1

    recovered = repository.get(record.id)
    assert recovered is not None
    assert recovered.status is ExitStatus.ERROR
    assert recovered.stage == "interrupted"
    assert "перезапуском" in recovered.error


def test_restart_marks_installing_record_as_error(tmp_path):
    """A crashed installation must never become a routable exit after restart."""
    repo = ExitRepository(tmp_path / "state.sqlite3")
    record = repo.create("de", IPv4Address("203.0.113.2"))
    repo.set_stage(record.id, ExitStatus.INSTALLING, "remote_packages")

    assert repo.recover_interrupted() == 1
    recovered = repo.get(record.id)
    assert recovered is not None
    assert recovered.status is ExitStatus.ERROR
    assert recovered.stage == "interrupted"


def test_duplicate_name_or_address_is_rejected_before_a_slot_is_consumed(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    repo.create("de", IPv4Address("203.0.113.2"))

    with pytest.raises(DuplicateExitError):
        repo.create("de", IPv4Address("203.0.113.3"))
    with pytest.raises(DuplicateExitError):
        repo.create("nl", IPv4Address("203.0.113.2"))

    second = repo.create("nl", IPv4Address("203.0.113.3"))
    assert second.slot == 2


def test_cancel_and_delete_mutate_only_the_selected_record(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    first = repo.create("de", IPv4Address("203.0.113.2"))
    second = repo.create("nl", IPv4Address("203.0.113.3"))

    repo.request_cancel(first.id)
    assert repo.cancel_requested(first.id) is True
    assert repo.cancel_requested(second.id) is False

    repo.delete(first.id)
    assert repo.get(first.id) is None
    assert [record.id for record in repo.list()] == [second.id]
