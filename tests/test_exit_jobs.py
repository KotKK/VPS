import threading
import time
from ipaddress import IPv4Address

import pytest

from gateway.app.apply import ApplyResult
from gateway.app.exit_jobs import ExitJobCoordinator
from gateway.app.exit_provisioner import ProvisionCancelled
from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.models import ExitCreate
from gateway.app.ssh_transport import SSHCommandError
from pydantic import SecretStr


class RecordingProvisioner:
    def __init__(self, delay: float = 0):
        self.delay = delay
        self._lock = threading.Lock()
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self.calls = []
        self.cleaned = []
        self.cleanup_warning = None

    def provision(self, record, credentials, progress, cancelled):
        with self._lock:
            self.concurrent_calls += 1
            self.max_concurrent_calls = max(
                self.max_concurrent_calls, self.concurrent_calls
            )
        try:
            self.calls.append(record.id)
            progress("ssh")
            if cancelled():
                raise ProvisionCancelled()
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.concurrent_calls -= 1

    def cleanup_local(self, record):
        self.cleaned.append(record.id)

    def cleanup_remote(self, record, credentials):
        return self.cleanup_warning


class RecordingApplier:
    def __init__(self):
        self.desired_history = []

    def apply(self, _previous, desired):
        self.desired_history.append(desired)
        return ApplyResult(active=desired)


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def notify(self, text, interfaces):
        self.events.append((text, tuple(interfaces)))


def request(name: str, address: str, password: str = "secret") -> ExitCreate:
    return ExitCreate(name=name, host=address, login="root", password=password)


def build_coordinator(tmp_path, *, delay: float = 0):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    provisioner = RecordingProvisioner(delay)
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, provisioner, applier)
    return coordinator, repository, provisioner, applier


def test_password_is_destroyed_after_success(tmp_path):
    coordinator, repository, _provisioner, _applier = build_coordinator(tmp_path)

    record = coordinator.start(request("de", "203.0.113.2"))
    coordinator.shutdown()

    assert record.id not in coordinator._credentials
    assert repository.get(record.id).status is ExitStatus.READY


def test_successful_vps_install_notifies_with_name_ip_and_working_interface(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    notifier = RecordingNotifier()
    coordinator = ExitJobCoordinator(
        repository,
        RecordingProvisioner(),
        RecordingApplier(),
        notifier=notifier,
    )

    coordinator.start(request("Helsinki", "203.0.113.2"))
    coordinator.shutdown()

    assert notifier.events == [
        (
            "✅ VPS «Helsinki» (203.0.113.2) добавлен и доступен.",
            ("awg-uplink",),
        )
    ]


def test_failed_vps_install_queues_safe_error_through_existing_exit(tmp_path):
    class FailingProvisioner(RecordingProvisioner):
        def provision(self, record, credentials, progress, cancelled):
            progress("ssh")
            raise SSHCommandError("Ошибка удалённой команды: access denied")

    repository = ExitRepository(tmp_path / "state.sqlite3")
    existing = repository.create("Stockholm", IPv4Address("203.0.113.10"))
    repository.set_stage(existing.id, ExitStatus.READY, "ready")
    notifier = RecordingNotifier()
    coordinator = ExitJobCoordinator(
        repository,
        FailingProvisioner(),
        RecordingApplier(),
        notifier=notifier,
    )

    coordinator.start(request("Helsinki", "203.0.113.2"))
    coordinator.shutdown()

    assert notifier.events == [
        (
            "❌ VPS «Helsinki» (203.0.113.2): установка не завершена. "
            "Удалённая установка: access denied",
            ("awg-uplink",),
        )
    ]


def test_jobs_are_serialized_and_each_ready_exit_enters_balancing(tmp_path):
    coordinator, repository, provisioner, applier = build_coordinator(
        tmp_path, delay=0.03
    )

    first = coordinator.start(request("de", "203.0.113.2"))
    second = coordinator.start(request("nl", "203.0.113.3"))
    coordinator.shutdown()

    assert provisioner.max_concurrent_calls == 1
    assert repository.get(first.id).status is ExitStatus.READY
    assert repository.get(second.id).status is ExitStatus.READY
    assert [len(state.exits) for state in applier.desired_history] == [1, 2]


def test_restart_marks_interrupted_exit_error_and_does_not_balance_it(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.INSTALLING, "packages")
    provisioner = RecordingProvisioner()
    applier = RecordingApplier()

    coordinator = ExitJobCoordinator(repository, provisioner, applier)
    coordinator.shutdown()

    assert repository.get(record.id).status is ExitStatus.ERROR
    assert coordinator.active_state.exits == ()
    assert applier.desired_history == []


def test_failed_remote_command_preserves_stage_and_safe_detail(tmp_path):
    class FailingProvisioner(RecordingProvisioner):
        def provision(self, record, credentials, progress, cancelled):
            progress("packages")
            raise SSHCommandError(
                "Ошибка удалённой команды: пакет ядра недоступен"
            )

    repository = ExitRepository(tmp_path / "state.sqlite3")
    provisioner = FailingProvisioner()
    coordinator = ExitJobCoordinator(repository, provisioner, RecordingApplier())

    record = coordinator.start(request("debian-12", "203.0.113.2"))
    coordinator.shutdown()

    failed = repository.get(record.id)
    assert failed.status is ExitStatus.ERROR
    assert failed.stage == "packages"
    assert failed.error == "Удалённая установка: пакет ядра недоступен"


def test_cancelled_job_cleans_local_state_and_never_enters_balancing(tmp_path):
    coordinator, repository, provisioner, applier = build_coordinator(
        tmp_path, delay=0.03
    )
    first = coordinator.start(request("de", "203.0.113.2"))
    second = coordinator.start(request("nl", "203.0.113.3"))

    coordinator.cancel(second.id)
    coordinator.shutdown()

    cancelled = repository.get(second.id)
    assert cancelled.status is ExitStatus.ERROR
    assert cancelled.stage == "cancelled"
    assert second.id in provisioner.cleaned
    assert all(second.id not in {route.name for route in state.exits} for state in applier.desired_history)
    assert repository.get(first.id).status is ExitStatus.READY


def test_retry_reuses_allocation_with_a_fresh_in_memory_password(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.ERROR, "failed", "Неверный пароль root")
    provisioner = RecordingProvisioner()
    coordinator = ExitJobCoordinator(repository, provisioner, RecordingApplier())

    retried = coordinator.retry(record.id, SecretStr("fresh-password"))
    coordinator.shutdown()

    assert retried.slot == record.slot
    assert repository.get(record.id).status is ExitStatus.READY
    assert record.id not in coordinator._credentials


def test_unreachable_remote_is_removed_from_balancing_and_kept_as_warning(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    provisioner = RecordingProvisioner()
    provisioner.cleanup_warning = "Удалённая очистка не завершена"
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, provisioner, applier)

    coordinator.delete(
        record.id,
        SecretStr("fresh-password"),
        acknowledge=True,
    )
    coordinator.shutdown()

    retained = repository.get(record.id)
    assert retained is not None
    assert retained.status is ExitStatus.ERROR
    assert retained.stage == "remote_cleanup"
    assert retained.warning == "Удалённая очистка не завершена"
    assert coordinator.active_state.exits == ()
    assert record.id in provisioner.cleaned


def test_final_ready_exit_requires_acknowledgement_before_mutation(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    coordinator = ExitJobCoordinator(
        repository, RecordingProvisioner(), RecordingApplier()
    )

    with pytest.raises(ValueError, match="последний доступный VPS"):
        coordinator.delete(record.id, SecretStr("secret"), acknowledge=False)
    coordinator.shutdown()

    assert repository.get(record.id).status is ExitStatus.READY


def test_renaming_ready_exit_does_not_reprovision_or_change_balancing(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("old-name", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    provisioner = RecordingProvisioner()
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, provisioner, applier)

    updated = coordinator.update(
        record.id,
        "new-name",
        IPv4Address("203.0.113.2"),
        None,
    )
    coordinator.shutdown()

    assert updated.name == "new-name"
    assert provisioner.calls == []
    assert [len(state.exits) for state in applier.desired_history] == [1]


def test_replacing_ready_exit_reuses_slot_and_reenters_balancing(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("old-vps", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    provisioner = RecordingProvisioner()
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, provisioner, applier)

    replacement = coordinator.update(
        record.id,
        "new-vps",
        IPv4Address("203.0.113.9"),
        SecretStr("fresh-password"),
    )
    coordinator.shutdown()

    saved = repository.get(record.id)
    assert replacement.slot == record.slot
    assert saved.address == "203.0.113.9"
    assert saved.status is ExitStatus.READY
    assert provisioner.cleaned == [record.id]
    assert provisioner.calls == [record.id]
    assert [len(state.exits) for state in applier.desired_history] == [1, 0, 1]


def test_replacement_network_changes_run_on_the_serial_job_worker(tmp_path):
    class ThreadRecordingApplier(RecordingApplier):
        def __init__(self):
            super().__init__()
            self.thread_names = []

        def apply(self, previous, desired):
            self.thread_names.append(threading.current_thread().name)
            return super().apply(previous, desired)

    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("old-vps", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    applier = ThreadRecordingApplier()
    coordinator = ExitJobCoordinator(repository, RecordingProvisioner(), applier)

    coordinator.update(
        record.id,
        "new-vps",
        IPv4Address("203.0.113.9"),
        SecretStr("fresh-password"),
    )
    coordinator.shutdown()

    assert applier.thread_names[0] == "MainThread"
    assert all(name.startswith("awg-exit") for name in applier.thread_names[1:])


def test_duplicate_replacement_is_rejected_without_touching_balancing(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    first = repository.create("first", IPv4Address("203.0.113.2"))
    second = repository.create("second", IPv4Address("203.0.113.3"))
    repository.set_stage(first.id, ExitStatus.READY, "ready")
    repository.set_stage(second.id, ExitStatus.READY, "ready")
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, RecordingProvisioner(), applier)

    with pytest.raises(ValueError, match="существует"):
        coordinator.update(
            first.id,
            "replacement",
            IPv4Address("203.0.113.3"),
            SecretStr("fresh-password"),
        )
    coordinator.shutdown()

    assert repository.get(first.id).address == "203.0.113.2"
    assert coordinator.active_state.exits
    assert [len(state.exits) for state in applier.desired_history] == [2]


def test_cleanup_failure_leaves_replacement_retryable_and_forgets_password(tmp_path):
    class CleanupFailureProvisioner(RecordingProvisioner):
        def cleanup_local(self, record):
            raise RuntimeError("cleanup failed")

    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("old-vps", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    coordinator = ExitJobCoordinator(
        repository,
        CleanupFailureProvisioner(),
        RecordingApplier(),
    )

    coordinator.update(
        record.id,
        "new-vps",
        IPv4Address("203.0.113.9"),
        SecretStr("fresh-password"),
    )
    coordinator.shutdown()

    failed = repository.get(record.id)
    assert failed.status is ExitStatus.ERROR
    assert failed.stage == "local_cleanup"
    assert record.id not in coordinator._credentials


def test_submit_failure_restores_original_record_and_forgets_password(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("old-vps", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, RecordingProvisioner(), applier)
    coordinator.shutdown()

    with pytest.raises(RuntimeError):
        coordinator.update(
            record.id,
            "new-vps",
            IPv4Address("203.0.113.9"),
            SecretStr("fresh-password"),
        )

    restored = repository.get(record.id)
    assert restored.name == "old-vps"
    assert restored.address == "203.0.113.2"
    assert restored.status is ExitStatus.READY
    assert record.id not in coordinator._credentials
    assert [len(state.exits) for state in applier.desired_history] == [1]


def test_failed_install_reaches_error_even_when_cleanup_also_fails(tmp_path):
    class DoubleFailureProvisioner(RecordingProvisioner):
        def provision(self, record, credentials, progress, cancelled):
            progress("handshake")
            raise RuntimeError("handshake failed")

        def cleanup_local(self, record):
            raise RuntimeError("cleanup also failed")

    repository = ExitRepository(tmp_path / "state.sqlite3")
    coordinator = ExitJobCoordinator(
        repository,
        DoubleFailureProvisioner(),
        RecordingApplier(),
    )

    record = coordinator.start(request("new-vps", "203.0.113.9"))
    coordinator.shutdown()

    failed = repository.get(record.id)
    assert failed.status is ExitStatus.ERROR
    assert failed.stage == "handshake"
    assert record.id not in coordinator._credentials


def test_retry_after_local_cleanup_error_cleans_before_provisioning(tmp_path):
    class OrderedProvisioner(RecordingProvisioner):
        def __init__(self):
            super().__init__()
            self.events = []

        def cleanup_local(self, record):
            self.events.append("cleanup")

        def provision(self, record, credentials, progress, cancelled):
            self.events.append("provision")
            super().provision(record, credentials, progress, cancelled)

    repository = ExitRepository(tmp_path / "state.sqlite3")
    record = repository.create("replacement", IPv4Address("203.0.113.9"))
    repository.set_stage(
        record.id,
        ExitStatus.ERROR,
        "local_cleanup",
        "Не удалось отключить старый локальный туннель",
    )
    provisioner = OrderedProvisioner()
    coordinator = ExitJobCoordinator(repository, provisioner, RecordingApplier())

    coordinator.retry(record.id, SecretStr("fresh-password"))
    coordinator.shutdown()

    assert provisioner.events == ["cleanup", "provision"]
    assert repository.get(record.id).status is ExitStatus.READY


def test_retry_after_restore_conflict_removes_old_route_before_provisioning(tmp_path):
    repository = ExitRepository(tmp_path / "state.sqlite3")
    original = repository.create("old-vps", IPv4Address("203.0.113.2"))
    original = repository.set_stage(original.id, ExitStatus.READY, "ready")
    provisioner = RecordingProvisioner()
    applier = RecordingApplier()
    coordinator = ExitJobCoordinator(repository, provisioner, applier)

    repository.prepare_replacement(
        original.id,
        "new-vps",
        IPv4Address("203.0.113.9"),
    )
    repository.create("old-vps", IPv4Address("203.0.113.2"))
    conflicted = repository.restore_replacement(original)
    assert conflicted.stage == "restore_conflict"

    coordinator.retry(original.id, SecretStr("fresh-password"))
    coordinator.shutdown()

    saved = repository.get(original.id)
    assert saved.status is ExitStatus.READY
    assert saved.address == "203.0.113.9"
    assert provisioner.cleaned == [original.id]
    assert provisioner.calls == [original.id]
    assert [len(state.exits) for state in applier.desired_history] == [1, 0, 1]
