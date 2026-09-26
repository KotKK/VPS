from ipaddress import IPv4Address
import threading
import time

from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.health import ExitProbe
from gateway.app.health_monitor import ExitHealthMonitor, HealthStateRepository
from gateway.app.notifications import (
    NotificationOutbox,
    NotificationService,
    TelegramSender,
)


class RecordingSender:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.calls = []

    def send(self, text, interfaces):
        self.calls.append((text, tuple(interfaces)))
        return self.succeeds and bool(interfaces)


class MutableProbe:
    def __init__(self, results):
        self.results = results

    def probe(self, record):
        return self.results[record.id]


class InterfaceRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(tuple(args))
        if args[0] == "curl" and args[args.index("--interface") + 1] == "awg-uplink":
            raise RuntimeError("first exit is down")


class BlockingSender:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def send(self, text, interfaces):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        return True


def ready_exit(repository, name, address):
    record = repository.create(name, IPv4Address(address))
    return repository.set_stage(record.id, ExitStatus.READY, "ready")


def test_notification_outbox_keeps_message_until_a_vpn_exit_can_deliver_it(tmp_path):
    outbox = NotificationOutbox(tmp_path / "notifications.sqlite3")
    sender = RecordingSender()
    notifications = NotificationService(outbox, sender)

    notifications.notify("Все VPS недоступны", ())

    assert [message.text for message in outbox.pending()] == [
        "Все VPS недоступны"
    ]
    assert sender.calls == []

    notifications.flush(("awg-uplink-2",))

    assert sender.calls == [
        ("Все VPS недоступны", ("awg-uplink-2",))
    ]
    assert outbox.pending() == []


def test_telegram_sender_switches_routes_and_falls_back_to_a_healthy_exit():
    runner = InterfaceRunner()
    sender = TelegramSender("token", "chat", runner=runner)

    delivered = sender.send("VPS down", ("awg-uplink", "awg-uplink-2"))

    assert delivered is True
    curl_interfaces = [
        call[call.index("--interface") + 1]
        for call in runner.calls
        if call[0] == "curl"
    ]
    assert curl_interfaces == ["awg-uplink", "awg-uplink-2"]
    assert any(
        call[:3] == ("ip", "route", "replace")
        and call[-2:] == ("dev", "awg-uplink-2")
        for call in runner.calls
    )


def test_two_processes_cannot_deliver_the_same_outbox_message_twice(tmp_path):
    database = tmp_path / "notifications.sqlite3"
    first = NotificationService(NotificationOutbox(database), BlockingSender())
    second = NotificationService(NotificationOutbox(database), first.sender)
    first.outbox.enqueue("Один алерт")

    first_thread = threading.Thread(target=first.flush, args=(("awg-uplink",),))
    second_thread = threading.Thread(target=second.flush, args=(("awg-uplink-2",),))
    first_thread.start()
    assert first.sender.entered.wait(timeout=1)
    second_thread.start()
    time.sleep(0.1)
    first.sender.release.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert first.sender.calls == 1
    assert first.outbox.pending() == []


def test_slow_telegram_delivery_does_not_block_new_alerts_from_being_queued(tmp_path):
    database = tmp_path / "notifications.sqlite3"
    sender = BlockingSender()
    notifications = NotificationService(NotificationOutbox(database), sender)
    notifications.outbox.enqueue("Первый алерт")
    delivery = threading.Thread(
        target=notifications.flush,
        args=(("awg-uplink",),),
    )
    delivery.start()
    assert sender.entered.wait(timeout=1)

    queued = threading.Event()

    def enqueue_second():
        NotificationOutbox(database).enqueue("Второй алерт")
        queued.set()

    enqueue = threading.Thread(target=enqueue_second)
    enqueue.start()
    queued_without_waiting_for_telegram = queued.wait(timeout=0.5)
    sender.release.set()
    delivery.join(timeout=2)
    enqueue.join(timeout=2)

    assert queued_without_waiting_for_telegram
    assert sender.calls == 2
    assert notifications.outbox.pending() == []


def test_monitor_checks_every_ready_vps_and_alerts_only_on_state_changes(tmp_path):
    exits = ExitRepository(tmp_path / "exits.sqlite3")
    first = ready_exit(exits, "Stockholm", "203.0.113.2")
    second = ready_exit(exits, "Helsinki", "203.0.113.3")
    sender = RecordingSender()
    notifications = NotificationService(
        NotificationOutbox(tmp_path / "notifications.sqlite3"), sender
    )
    probe = MutableProbe(
        {
            first.id: ExitProbe(True, "ok"),
            second.id: ExitProbe(True, "ok"),
        }
    )
    monitor = ExitHealthMonitor(
        exits,
        HealthStateRepository(tmp_path / "health.sqlite3"),
        probe,
        notifications,
    )

    monitor.run()
    assert sender.calls == []

    probe.results[first.id] = ExitProbe(False, "handshake устарел")
    monitor.run()
    monitor.run()

    assert sender.calls == [
        (
            "🔴 VPS «Stockholm» (203.0.113.2) недоступен: handshake устарел.",
            ("awg-uplink-2",),
        )
    ]

    probe.results[first.id] = ExitProbe(True, "ok")
    monitor.run()

    assert sender.calls[-1] == (
        "🟢 VPS «Stockholm» (203.0.113.2) снова работает.",
        ("awg-uplink", "awg-uplink-2"),
    )


def test_monitor_queues_outages_when_all_vps_are_down_and_sends_after_recovery(
    tmp_path,
):
    exits = ExitRepository(tmp_path / "exits.sqlite3")
    first = ready_exit(exits, "Stockholm", "203.0.113.2")
    second = ready_exit(exits, "Helsinki", "203.0.113.3")
    outbox = NotificationOutbox(tmp_path / "notifications.sqlite3")
    sender = RecordingSender()
    notifications = NotificationService(outbox, sender)
    probe = MutableProbe(
        {
            first.id: ExitProbe(True, "ok"),
            second.id: ExitProbe(True, "ok"),
        }
    )
    monitor = ExitHealthMonitor(
        exits,
        HealthStateRepository(tmp_path / "health.sqlite3"),
        probe,
        notifications,
    )
    monitor.run()

    probe.results = {
        first.id: ExitProbe(False, "timeout"),
        second.id: ExitProbe(False, "timeout"),
    }
    monitor.run()

    assert sender.calls == []
    assert len(outbox.pending()) == 2

    probe.results[first.id] = ExitProbe(True, "ok")
    monitor.run()

    assert [text for text, _interfaces in sender.calls] == [
        "🔴 VPS «Stockholm» (203.0.113.2) недоступен: timeout.",
        "🔴 VPS «Helsinki» (203.0.113.3) недоступен: timeout.",
        "🟢 VPS «Stockholm» (203.0.113.2) снова работает.",
    ]
    assert outbox.pending() == []
