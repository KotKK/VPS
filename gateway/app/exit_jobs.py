"""Serialized background lifecycle for foreign VPS provisioning."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Callable, Protocol

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
    SSHCommandError,
    SSHCredentials,
    SSHTimeoutError,
)

LOGGER = logging.getLogger(__name__)


class Provisioner(Protocol):
    def provision(self, record, credentials, progress, cancelled) -> None: ...

    def cleanup_local(self, record) -> None: ...


class Applier(Protocol):
    def apply(self, previous: GatewayState, desired: GatewayState) -> ApplyResult: ...


class Notifier(Protocol):
    def notify(self, text: str, interfaces: tuple[str, ...]) -> None: ...


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
    if isinstance(exc, SSHCommandError):
        _prefix, _separator, detail = str(exc).partition(":")
        safe_detail = " ".join((detail or str(exc)).split())[:240]
        return f"Удалённая установка: {safe_detail}"
    return "Установка VPS завершилась ошибкой"


@dataclass
class ExitJobCoordinator:
    repository: ExitRepository
    provisioner: Provisioner
    applier: Applier
    notifier: Notifier | None = None
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

    def _ready_state(
        self,
        extra: ExitRecord | None = None,
        exclude_id: str | None = None,
    ) -> GatewayState:
        records = [
            record
            for record in self.repository.list()
            if record.status is ExitStatus.READY and record.id != exclude_id
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
        self._submit_task(record, credentials, self._install, record.id)

    def _submit_task(
        self,
        record: ExitRecord,
        credentials: SSHCredentials,
        function: Callable[..., None],
        *args,
    ) -> None:
        with self._lock:
            self._credentials[record.id] = credentials
            try:
                future = self._executor.submit(function, *args)
            except Exception:
                self._credentials.pop(record.id, None)
                raise
            self._futures[record.id] = future

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
        previous = self.repository.get(exit_id)
        if previous is None:
            raise KeyError(exit_id)
        record = self.repository.prepare_retry(exit_id)
        credentials = SSHCredentials(
            host=IPv4Address(record.address),
            username="root",
            password=password,
        )
        if previous.stage in {"local_cleanup", "restore_conflict"}:
            self._submit_task(
                record,
                credentials,
                self._retry_after_replacement_failure,
                record.id,
            )
        else:
            self._submit(record, credentials)
        return record

    def update(
        self,
        exit_id: str,
        name: str,
        address: IPv4Address,
        password: SecretStr | None,
    ) -> ExitRecord:
        record = self.repository.get(exit_id)
        if record is None:
            raise KeyError(exit_id)
        if record.status not in {ExitStatus.READY, ExitStatus.ERROR}:
            raise ValueError(
                "Редактировать можно только доступный VPS или VPS с ошибкой"
            )
        if str(address) == record.address:
            return self.repository.rename(exit_id, name)
        if password is None or not password.get_secret_value():
            raise ValueError("Для замены IP нужен пароль root нового VPS")
        replacement = self.repository.prepare_replacement(
            exit_id,
            name,
            address,
        )
        credentials = SSHCredentials(address, "root", password)
        try:
            self._submit_task(
                replacement,
                credentials,
                self._replace,
                record,
                replacement.id,
            )
        except Exception:
            self.repository.restore_replacement(record)
            raise
        return replacement

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
        last_stage = record.stage

        def progress(stage: str) -> None:
            nonlocal last_stage
            last_stage = stage
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
            ready_record = self.repository.set_stage(
                exit_id, ExitStatus.READY, "ready"
            )
            self._notify(
                f"✅ VPS «{ready_record.name}» ({ready_record.address}) "
                "добавлен и доступен.",
                (ready_record.interface,) + self._notification_interfaces(),
            )
        except ProvisionCancelled:
            self._cleanup_local_safely(record, "cancelled provisioning")
            self.repository.set_stage(
                exit_id,
                ExitStatus.ERROR,
                "cancelled",
                "Установка остановлена пользователем",
            )
        except Exception as exc:
            LOGGER.exception(
                "Exit provisioning failed for %s at stage %s",
                exit_id,
                last_stage,
            )
            self._cleanup_local_safely(record, "failed provisioning")
            failed = self.repository.set_stage(
                exit_id,
                ExitStatus.ERROR,
                last_stage,
                _public_error(exc),
            )
            self._notify(
                f"❌ VPS «{failed.name}» ({failed.address}): "
                f"установка не завершена. {failed.error}",
                self._notification_interfaces(),
            )
        finally:
            with self._lock:
                self._credentials.pop(exit_id, None)

    def _cleanup_local_safely(self, record: ExitRecord, context: str) -> bool:
        try:
            self.provisioner.cleanup_local(record)
        except Exception:
            LOGGER.exception("Local cleanup failed during %s for %s", context, record.id)
            return False
        return True

    def _notification_interfaces(self) -> tuple[str, ...]:
        return tuple(route.interface for route in self._active_state.exits)

    def _notify(self, text: str, interfaces: tuple[str, ...]) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify(text, tuple(dict.fromkeys(interfaces)))
        except Exception:
            LOGGER.exception("Telegram notification could not be queued")

    def _notify_replacement_failure(self, record: ExitRecord) -> None:
        detail = record.error or record.warning or record.stage
        self._notify(
            f"❌ VPS «{record.name}» ({record.address}): "
            f"замена не завершена. {detail}",
            self._notification_interfaces(),
        )

    def _retry_after_replacement_failure(self, exit_id: str) -> None:
        delegated_to_install = False
        try:
            record = self.repository.get(exit_id)
            if record is None:
                return
            if any(route.name == exit_id for route in self._active_state.exits):
                desired = self._ready_state(exclude_id=exit_id)
                result = self.applier.apply(self._active_state, desired)
                if result.active != desired:
                    failed = self.repository.set_stage(
                        exit_id,
                        ExitStatus.ERROR,
                        "restore_conflict",
                        "Не удалось исключить старый VPS из балансировки",
                    )
                    self._notify_replacement_failure(failed)
                    return
                self._active_state = desired
            if not self._cleanup_local_safely(record, "replacement retry"):
                failed = self.repository.set_stage(
                    exit_id,
                    ExitStatus.ERROR,
                    "local_cleanup",
                    "Не удалось отключить старый локальный туннель",
                )
                self._notify_replacement_failure(failed)
                return
            delegated_to_install = True
            self._install(exit_id)
        except Exception as exc:
            LOGGER.exception("Replacement retry failed for %s", exit_id)
            current = self.repository.get(exit_id)
            if current is not None and current.status is ExitStatus.INSTALLING:
                current = self.repository.set_stage(
                    exit_id,
                    ExitStatus.ERROR,
                    "replace_failed",
                    _public_error(exc),
                )
            if current is not None:
                self._notify_replacement_failure(current)
        finally:
            if not delegated_to_install:
                with self._lock:
                    self._credentials.pop(exit_id, None)

    def _replace(self, original: ExitRecord, exit_id: str) -> None:
        delegated_to_install = False
        try:
            if self.repository.cancel_requested(exit_id):
                self.repository.restore_replacement(original)
                return

            desired = self._ready_state(exclude_id=exit_id)
            if original.status is ExitStatus.READY:
                self.repository.set_stage(
                    exit_id,
                    ExitStatus.INSTALLING,
                    "replace_remove_balance",
                )
                result = self.applier.apply(self._active_state, desired)
                if result.active != desired:
                    restored = self.repository.restore_replacement(
                        original,
                        "Не удалось временно исключить VPS из балансировки",
                    )
                    self._notify_replacement_failure(restored)
                    return
                self._active_state = desired

            self.repository.set_stage(
                exit_id,
                ExitStatus.INSTALLING,
                "local_cleanup",
            )
            try:
                self.provisioner.cleanup_local(original)
            except Exception:
                LOGGER.exception("Local cleanup failed while replacing %s", exit_id)
                failed = self.repository.set_stage(
                    exit_id,
                    ExitStatus.ERROR,
                    "local_cleanup",
                    "Не удалось отключить старый локальный туннель",
                )
                self._notify_replacement_failure(failed)
                return

            delegated_to_install = True
            self._install(exit_id)
        except Exception as exc:
            LOGGER.exception("Replacement failed for %s", exit_id)
            current = self.repository.get(exit_id)
            if current is not None and current.status is ExitStatus.INSTALLING:
                current = self.repository.set_stage(
                    exit_id,
                    ExitStatus.ERROR,
                    "replace_failed",
                    _public_error(exc),
                )
            if current is not None:
                self._notify_replacement_failure(current)
        finally:
            if not delegated_to_install:
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
