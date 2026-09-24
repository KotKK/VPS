"""Serialized background lifecycle for foreign VPS provisioning."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Protocol

from pydantic import SecretStr

from gateway.app.apply import ApplyResult
from gateway.app.exit_provisioner import (
    KernelModuleUnavailableError,
    ProvisionCancelled,
    UnsupportedRemoteOSError,
)
from gateway.app.exit_store import ExitRecord, ExitRepository, ExitStatus
from gateway.app.models import ExitCreate
from gateway.app.renderer import ExitRoute, GatewayState
from gateway.app.ssh_transport import (
    HostKeyChangedError,
    SSHAuthenticationError,
    SSHCredentials,
    SSHTimeoutError,
)


class Provisioner(Protocol):
    def provision(self, record, credentials, progress, cancelled) -> None: ...

    def cleanup_local(self, record) -> None: ...


class Applier(Protocol):
    def apply(self, previous: GatewayState, desired: GatewayState) -> ApplyResult: ...


def _public_error(exc: Exception) -> str:
    if isinstance(exc, SSHAuthenticationError):
        return "Неверный пароль root"
    if isinstance(exc, HostKeyChangedError):
        return "SSH host key изменился; подключение заблокировано"
    if isinstance(exc, UnsupportedRemoteOSError):
        return "Поддерживаются только чистые Debian 12 и Debian 13"
    if isinstance(exc, KernelModuleUnavailableError):
        return str(exc)
    if isinstance(exc, SSHTimeoutError):
        return "VPS не ответил вовремя"
    return "Установка VPS завершилась ошибкой"


@dataclass
class ExitJobCoordinator:
    repository: ExitRepository
    provisioner: Provisioner
    applier: Applier
    max_workers: int = 1
    _executor: ThreadPoolExecutor = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    _credentials: dict[str, SSHCredentials] = field(init=False, repr=False)
    _futures: dict[str, Future[None]] = field(init=False, repr=False)
    _active_state: GatewayState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="awg-exit",
        )
        self._lock = threading.Lock()
        self._credentials = {}
        self._futures = {}
        self.repository.recover_interrupted()
        ready = self._ready_state()
        self._active_state = GatewayState(())
        if ready.exits:
            result = self.applier.apply(self._active_state, ready)
            self._active_state = result.active or self._active_state

    @property
    def active_state(self) -> GatewayState:
        return self._active_state

    def _ready_state(self, extra: ExitRecord | None = None) -> GatewayState:
        records = [
            record
            for record in self.repository.list()
            if record.status is ExitStatus.READY
        ]
        if extra is not None and all(record.id != extra.id for record in records):
            records.append(extra)
        records.sort(key=lambda record: record.slot)
        return GatewayState(
            tuple(
                ExitRoute(
                    record.id,
                    record.interface,
                    record.address,
                    record.route_table,
                    record.mark,
                )
                for record in records
            )
        )

    def _submit(self, record: ExitRecord, credentials: SSHCredentials) -> None:
        with self._lock:
            self._credentials[record.id] = credentials
            self._futures[record.id] = self._executor.submit(
                self._install,
                record.id,
            )

    def start(self, request: ExitCreate) -> ExitRecord:
        record = self.repository.create(request.name, request.host)
        credentials = SSHCredentials(
            request.host,
            request.login,
            request.password,
        )
        self._submit(record, credentials)
        return record

    def retry(self, exit_id: str, password: SecretStr) -> ExitRecord:
        record = self.repository.prepare_retry(exit_id)
        credentials = SSHCredentials(
            host=IPv4Address(record.address),
            username="root",
            password=password,
        )
        self._submit(record, credentials)
        return record

    def cancel(self, exit_id: str) -> None:
        self.repository.request_cancel(exit_id)

    def delete(
        self,
        exit_id: str,
        password: SecretStr | None,
        acknowledge: bool,
    ) -> ExitRecord:
        record = self.repository.get(exit_id)
        if record is None:
            raise KeyError(exit_id)
        ready = [
            item for item in self.repository.list() if item.status is ExitStatus.READY
        ]
        if record.status is ExitStatus.READY and len(ready) == 1 and not acknowledge:
            raise ValueError("Нужно подтвердить: это последний доступный VPS")
        if record.status in {ExitStatus.INSTALLING, ExitStatus.DELETING}:
            raise ValueError("VPS уже выполняет сетевую операцию")

        discard_warning = record.stage == "remote_cleanup"
        deleting = self.repository.set_stage(
            exit_id,
            ExitStatus.DELETING,
            "remove_balance",
        )
        credentials = None
        if password is not None and password.get_secret_value():
            credentials = SSHCredentials(
                IPv4Address(record.address), "root", password
            )
        with self._lock:
            if credentials is not None:
                self._credentials[exit_id] = credentials
            self._futures[exit_id] = self._executor.submit(
                self._delete,
                deleting,
                discard_warning,
            )
        return deleting

    def _install(self, exit_id: str) -> None:
        record = self.repository.get(exit_id)
        if record is None:
            return
        with self._lock:
            credentials = self._credentials[exit_id]

        def progress(stage: str) -> None:
            self.repository.set_stage(exit_id, ExitStatus.INSTALLING, stage)

        try:
            self.provisioner.provision(
                record,
                credentials,
                progress,
                lambda: self.repository.cancel_requested(exit_id),
            )
            desired = self._ready_state(extra=record)
            result = self.applier.apply(self._active_state, desired)
            if result.active != desired:
                raise RuntimeError("Не удалось применить балансировку")
            self._active_state = desired
            self.repository.set_stage(exit_id, ExitStatus.READY, "ready")
        except ProvisionCancelled:
            self.provisioner.cleanup_local(record)
            self.repository.set_stage(
                exit_id,
                ExitStatus.ERROR,
                "cancelled",
                "Установка остановлена пользователем",
            )
        except Exception as exc:
            self.provisioner.cleanup_local(record)
            self.repository.set_stage(
                exit_id,
                ExitStatus.ERROR,
                "failed",
                _public_error(exc),
            )
        finally:
            with self._lock:
                self._credentials.pop(exit_id, None)

    def _delete(self, record: ExitRecord, discard_warning: bool) -> None:
        with self._lock:
            credentials = self._credentials.get(record.id)
        try:
            desired = self._ready_state()
            result = self.applier.apply(self._active_state, desired)
            if result.active != desired:
                raise RuntimeError("Не удалось исключить VPS из балансировки")
            self._active_state = desired
            self.provisioner.cleanup_local(record)

            warning = None
            if credentials is not None:
                warning = self.provisioner.cleanup_remote(record, credentials)
            elif not discard_warning:
                warning = "Удалённая очистка не завершена"

            if warning:
                self.repository.set_warning(record.id, "remote_cleanup", warning)
            else:
                self.repository.delete(record.id)
        except Exception as exc:
            self.repository.set_stage(
                record.id,
                ExitStatus.ERROR,
                "delete_failed",
                _public_error(exc),
            )
        finally:
            with self._lock:
                self._credentials.pop(record.id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)


__all__ = ["ExitJobCoordinator"]
