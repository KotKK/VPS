from collections import Counter
from ipaddress import IPv4Address
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.app.apply import ApplyResult
from gateway.app.client_routing import ClientRoutingController
from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.health import ExitStatus as HealthStatus
from gateway.app.health import ExitProbe
from gateway.app.health_monitor import HealthStateRepository, ExitHealthMonitor
from gateway.app.renderer import render_egress_nft
from gateway.app.store import ClientRegistry


class Applier:
    def __init__(self):
        self.calls = []
        self.fail = False

    def apply(self, previous, desired):
        self.calls.append((previous, desired))
        return ApplyResult(active=previous if self.fail else desired)


def setup_pool(tmp_path):
    repo = ExitRepository(tmp_path / "exits.sqlite3")
    nodes = []
    for number in range(3):
        node = repo.create(f"VPS {number}", IPv4Address(f"203.0.113.{number+1}"))
        nodes.append(repo.set_stage(node.id, ExitStatus.READY, "ready"))
    clients = ClientRegistry(tmp_path / "clients.sqlite3")
    for number in range(9):
        clients.put(f"client {number}", f"[Interface]\nAddress = 10.20.0.{number+2}/24\n")
    applier = Applier()
    return nodes, clients, applier, ClientRoutingController(tmp_path, applier)


def test_recovered_exit_rebalances_with_minimal_moves_and_then_stays_stable(tmp_path):
    nodes, clients, applier, controller = setup_pool(tmp_path)
    initial = controller.sync()
    assert sorted(Counter(c.exit_id for c in initial.clients).values()) == [3, 3, 3]
    assert ClientRoutingController(tmp_path, applier).sync() == initial
    assert len(applier.calls) == 1

    health = HealthStateRepository(tmp_path / "health.sqlite3")
    health.set(nodes[0].id, HealthStatus.DOWN)
    failed = controller.sync()
    assert nodes[0].id not in {c.exit_id for c in failed.clients}
    assert sorted(Counter(c.exit_id for c in failed.clients).values()) == [4, 5]
    before = {c.name: c for c in initial.clients}
    assert all(c == before[c.name] for c in failed.clients if before[c.name].exit_id != nodes[0].id)

    health.set(nodes[0].id, HealthStatus.UP)
    recovered = controller.sync()
    assert sorted(Counter(c.exit_id for c in recovered.clients).values()) == [3, 3, 3]
    failed_assignments = {c.name: c.exit_id for c in failed.clients}
    assert sum(c.exit_id != failed_assignments[c.name] for c in recovered.clients) == 3
    calls = len(applier.calls)
    assert ClientRoutingController(tmp_path, applier).sync() == recovered
    assert len(applier.calls) == calls
    clients.put("new device", "Address = 10.20.0.100/24\n")
    assert controller.sync().clients[-1].exit_id == nodes[0].id


def test_all_clients_on_one_exit_are_shared_after_confirmed_recovery(tmp_path):
    nodes, clients, applier, controller = setup_pool(tmp_path)
    clients.put("client 9", "Address = 10.20.0.11/24\n")
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    repository.set_stage(nodes[2].id, ExitStatus.DELETING, "remove_balance")
    health = HealthStateRepository(tmp_path / "health.sqlite3")
    health.set(nodes[0].id, HealthStatus.UP)
    health.set(nodes[1].id, HealthStatus.DOWN)
    initial = controller.sync()
    assert Counter(c.exit_id for c in initial.clients) == {nodes[0].id: 10}

    class Probe:
        def probe(self, record):
            return ExitProbe(True, "ok")

    class Notifications:
        def notify(self, text, interfaces):
            pass

        def flush(self, interfaces):
            pass

    monitor = ExitHealthMonitor(
        repository, health, Probe(), Notifications(), sync_routing=controller.sync,
    )
    for _ in range(2):
        monitor.run()
        assert controller.active_state() == initial
    monitor.run()
    recovered = controller.active_state()
    assert Counter(c.exit_id for c in recovered.clients) == {
        nodes[0].id: 5, nodes[1].id: 5,
    }
    assert sum(c.exit_id != nodes[0].id for c in recovered.clients) == 5
    calls = len(applier.calls)
    monitor.run()
    assert controller.active_state() == recovered
    assert len(applier.calls) == calls


def test_failed_activation_does_not_publish_unapplied_assignments(tmp_path):
    nodes, _clients, applier, controller = setup_pool(tmp_path)
    initial = controller.sync()
    HealthStateRepository(tmp_path / "health.sqlite3").set(nodes[0].id, HealthStatus.DOWN)
    applier.fail = True
    with pytest.raises(RuntimeError):
        controller.sync()
    assert controller.active_state() == initial


def test_all_down_blocks_egress_including_unknown_clients_and_ipv6(tmp_path):
    nodes, _clients, _applier, controller = setup_pool(tmp_path)
    controller.sync()
    health = HealthStateRepository(tmp_path / "health.sqlite3")
    for node in nodes:
        health.set(node.id, HealthStatus.DOWN)
    state = controller.sync()
    assert state.clients == ()
    rules = render_egress_nft(state)
    assert "meta mark 0 drop" in rules
    assert "policy drop" in rules
    assert "accept" not in rules.split("chain forward {")[1]
    # Flushing leaves the legacy base chain's policy=drop in force.
    assert "delete table ip awg_gateway" in rules


def test_deleted_client_and_deleted_exit_are_removed_from_assignments(tmp_path):
    nodes, clients, _applier, controller = setup_pool(tmp_path)
    controller.sync()
    clients.delete("client 0")
    repo = ExitRepository(tmp_path / "exits.sqlite3")
    repo.set_stage(nodes[1].id, ExitStatus.DELETING, "remove_balance")
    state = controller.sync()
    assert len(state.clients) == 8
    assert all(c.name != "client 0" and c.exit_id != nodes[1].id for c in state.clients)


def test_two_process_controllers_share_assignment_and_activation_lock(tmp_path):
    _nodes, _clients, applier, controller = setup_pool(tmp_path)
    other = ClientRoutingController(tmp_path, applier)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda c: c.sync(), [controller, other]))
    assert results[0] == results[1]
    assert len(applier.calls) == 1


def test_per_client_rules_replace_stale_flow_marks_and_allow_all_active_uplinks(tmp_path):
    _nodes, _clients, _applier, controller = setup_pool(tmp_path)
    state = controller.sync()
    rules = render_egress_nft(state)
    assert "numgen" not in rules
    assert "ct mark !=" not in rules
    assert "meta mark set ip saddr map" in rules
    for route in state.exits:
        assert f'oifname "{route.interface}"' in rules
    for client in state.clients:
        assert f"{client.address} : " in rules


def test_health_monitor_reassigns_clients_before_sending_down_notification(tmp_path):
    nodes, _clients, _applier, controller = setup_pool(tmp_path)
    controller.sync()
    states = HealthStateRepository(tmp_path / "health.sqlite3")
    for node in nodes:
        states.set(node.id, HealthStatus.UP)

    class Probe:
        def probe(self, record):
            return ExitProbe(record.id != nodes[1].id, "test outage")

    class Notifications:
        calls = []

        def notify(self, text, interfaces):
            assert nodes[1].id not in {c.exit_id for c in controller.active_state().clients}
            self.calls.append(text)

        def flush(self, interfaces):
            pass

    notifications = Notifications()
    monitor = ExitHealthMonitor(
        ExitRepository(tmp_path / "exits.sqlite3"), states, Probe(),
        notifications, sync_routing=controller.sync,
    )
    monitor.run()
    assert len(notifications.calls) == 1
    assert len(controller.active_state().clients) == 9


def test_panel_displays_the_persisted_client_exit(tmp_path):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from gateway.app.main import StateStore, create_app

    nodes, clients, _applier, controller = setup_pool(tmp_path)
    controller.sync()
    app = create_app(
        StateStore(registry=clients, routing=controller),
        exit_jobs=SimpleNamespace(repository=ExitRepository(tmp_path / "exits.sqlite3")),
    )
    response = TestClient(app).get('/clients')
    assert response.status_code == 200
    for node in nodes:
        assert node.address in response.text
