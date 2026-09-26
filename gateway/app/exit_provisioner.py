"""Orchestrate one foreign Debian 12/13 AmneziaWG exit installation."""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Callable, Protocol

from gateway.app.exit_network import LocalUplinkManager, TunnelAllocation
from gateway.app.exit_store import ExitRecord
from gateway.app.ssh_transport import (
    HostKeyChangedError,
    SSHAuthenticationError,
    SSHCommandError,
    SSHCredentials,
    SSHTimeoutError,
    SSHTransport,
    SSHTransportError,
)


class UnsupportedRemoteOSError(RuntimeError):
    pass


class KernelModuleUnavailableError(RuntimeError):
    pass


class ProvisionCancelled(RuntimeError):
    pass


class UplinkManager(Protocol):
    def generate_keys(self): ...
    def stage(self, record, private_key, remote_public_key): ...
    def start(self, record): ...
    def remove(self, record): ...
    def wait_handshake(self, record, timeout): ...
    def check_internet(self, record): ...


def _parse_os_release(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


class ExitProvisioner:
    def __init__(
        self,
        transport: SSHTransport,
        local_manager: UplinkManager,
        remote_script: Path,
        reboot_timeout: int = 240,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.transport = transport
        self.local_manager = local_manager
        self.remote_script = remote_script
        self.reboot_timeout = reboot_timeout
        self._sleep = sleeper

    @staticmethod
    def _check_cancel(cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise ProvisionCancelled("Установка остановлена пользователем")

    @staticmethod
    def _environment(record: ExitRecord, local_public_key: str) -> bytes:
        allocation = TunnelAllocation.from_record(record)
        values = {
            "EXIT_INTERFACE": "awg-exit",
            "REMOTE_ADDRESS": f"{allocation.remote_address}/30",
            "LOCAL_ADDRESS": f"{allocation.local_address}/32",
            "CLIENT_SUBNET": "10.20.0.0/24",
            "REMOTE_PORT": str(record.remote_port),
            "LOCAL_PUBLIC_KEY": local_public_key,
        }
        return "".join(
            f"{name}={shlex.quote(value)}\n" for name, value in values.items()
        ).encode()

    @staticmethod
    def _boot_id(session) -> str:
        boot_id = session.run(
            ["cat", "/proc/sys/kernel/random/boot_id"]
        ).stdout.strip()
        if not boot_id:
            raise RuntimeError("VPS не вернул идентификатор загрузки")
        return boot_id

    def _wait_for_reboot(
        self,
        credentials: SSHCredentials,
        previous_boot_id: str,
        cancelled: Callable[[], bool],
    ) -> None:
        deadline = time.monotonic() + self.reboot_timeout
        while time.monotonic() < deadline:
            self._check_cancel(cancelled)
            try:
                with self.transport.connect(credentials) as session:
                    if self._boot_id(session) != previous_boot_id:
                        return
            except (SSHAuthenticationError, HostKeyChangedError):
                raise
            except SSHTransportError:
                pass
            self._sleep(3)
        raise SSHTimeoutError("VPS не вернулся после перезагрузки ядра")

    def provision(
        self,
        record: ExitRecord,
        credentials: SSHCredentials,
        progress: Callable[[str], None],
        cancelled: Callable[[], bool],
    ) -> None:
        script_path = "/tmp/awg-gateway-remote-exit.sh"
        environment_path = "/tmp/awg-gateway-exit.env"
        rebooted = False
        try:
            while True:
                progress("ssh")
                self._check_cancel(cancelled)
                reboot_boot_id = None
                with self.transport.connect(credentials) as session:
                    progress("os_check")
                    self._check_cancel(cancelled)
                    os_release = _parse_os_release(
                        session.run(["cat", "/etc/os-release"]).stdout
                    )
                    if (
                        os_release.get("ID") != "debian"
                        or os_release.get("VERSION_ID") not in {"12", "13"}
                    ):
                        raise UnsupportedRemoteOSError(
                            "Поддерживаются только чистые Debian 12 и Debian 13"
                        )
                    boot_id = self._boot_id(session)

                    session.put_bytes(
                        script_path, self.remote_script.read_bytes(), 0o700
                    )
                    progress("packages")
                    self._check_cancel(cancelled)
                    try:
                        session.run(["bash", script_path, "packages"])
                    except SSHCommandError as exc:
                        detail = str(exc)
                        if "AWG_REBOOT_REQUIRED" in detail:
                            if rebooted:
                                raise KernelModuleUnavailableError(
                                    "после перезагрузки заголовки запущенного "
                                    "ядра VPS всё ещё недоступны"
                                ) from exc
                            try:
                                session.run(["systemctl", "reboot"])
                            except SSHCommandError:
                                raise
                            except SSHTransportError:
                                # A disconnect is the expected result of reboot.
                                pass
                            reboot_boot_id = boot_id
                        elif "AWG_KERNEL_MODULE_UNAVAILABLE" in detail:
                            raise KernelModuleUnavailableError(
                                "ядро VPS не может загрузить модуль AmneziaWG; "
                                "для LXC модуль должен быть разрешён на хосте"
                            ) from exc
                        else:
                            raise

                    if reboot_boot_id is None:
                        keys = self.local_manager.generate_keys()
                        progress("remote_tunnel")
                        self._check_cancel(cancelled)
                        session.put_bytes(
                            environment_path,
                            self._environment(record, keys.public_key),
                            0o600,
                        )
                        remote_public_key = session.run(
                            ["bash", script_path, "configure", environment_path]
                        ).stdout.strip()
                        if not remote_public_key:
                            raise RuntimeError(
                                "Зарубежный VPS не вернул публичный ключ"
                            )
                        self._check_cancel(cancelled)

                        progress("local_tunnel")
                        self.local_manager.stage(
                            record, keys.private_key, remote_public_key
                        )
                        self.local_manager.start(record)
                        self._check_cancel(cancelled)

                        progress("handshake")
                        self.local_manager.wait_handshake(record, 45)
                        self._check_cancel(cancelled)

                        progress("internet_check")
                        self.local_manager.check_internet(record)
                        return

                assert reboot_boot_id is not None
                progress("reboot")
                self._wait_for_reboot(credentials, reboot_boot_id, cancelled)
                rebooted = True
        except ProvisionCancelled:
            self.local_manager.remove(record)
            raise

    def cleanup_remote(
        self,
        record: ExitRecord,
        credentials: SSHCredentials,
    ) -> str | None:
        script_path = "/tmp/awg-gateway-remote-exit.sh"
        try:
            with self.transport.connect(credentials) as session:
                session.put_bytes(script_path, self.remote_script.read_bytes(), 0o700)
                session.run(["bash", script_path, "cleanup"])
        except SSHTransportError:
            return "Удалённая очистка не завершена"
        return None

    def cleanup_local(self, record: ExitRecord) -> None:
        self.local_manager.remove(record)


__all__ = [
    "ExitProvisioner",
    "KernelModuleUnavailableError",
    "ProvisionCancelled",
    "UnsupportedRemoteOSError",
]
