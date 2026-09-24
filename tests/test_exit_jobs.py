import threading
import time
from ipaddress import IPv4Address

from gateway.app.apply import ApplyResult
from gateway.app.exit_jobs import ExitJobCoordinator
from gateway.app.exit_provisioner import ProvisionCancelled
from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.models import ExitCreate
from pydantic import SecretStr


class RecordingProvisioner:
    def __init__(self, delay: float = 0):
        self.delay = delay
        self._lock = threading.Lock()
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self.calls = []
        self.cleaned = []

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


class RecordingApplier:
    def __init__(self):
        self.desired_history = []

    def apply(self, _previous, desired):
        self.desired_history.append(desired)
        return ApplyResult(active=desired)


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
