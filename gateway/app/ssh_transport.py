"""Password-safe SSH boundary for provisioning foreign exit nodes."""

from __future__ import annotations

import logging
import os
import shlex
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Callable, Iterator, Literal, Sequence

import paramiko
from pydantic import SecretStr

LOGGER = logging.getLogger(__name__)


class SSHTransportError(RuntimeError):
    """Base class for safe errors exposed to the provisioning layer."""


class SSHAuthenticationError(SSHTransportError):
    pass


class HostKeyChangedError(SSHTransportError):
    pass


class SSHTimeoutError(SSHTransportError):
    pass


class SSHCommandError(SSHTransportError):
    pass


@dataclass(frozen=True)
class SSHCredentials:
    host: IPv4Address
    username: Literal["root"]
    password: SecretStr


@dataclass(frozen=True)
class SSHResult:
    stdout: str
    stderr: str
    exit_code: int


class TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Persist the first key, but never accept a second key for that host."""

    def __init__(self, known_hosts: Path):
        self.known_hosts = known_hosts

    def missing_host_key(
        self,
        client: paramiko.SSHClient,
        hostname: str,
        key: paramiko.PKey,
    ) -> None:
        hosts = paramiko.HostKeys()
        if self.known_hosts.exists() and self.known_hosts.stat().st_size:
            hosts.load(str(self.known_hosts))
        if hosts.lookup(hostname):
            raise HostKeyChangedError("SSH host key изменился; подключение заблокировано")

        hosts.add(hostname, key.get_name(), key)
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.known_hosts.with_name(f".{self.known_hosts.name}.tmp")
        hosts.save(str(temporary))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.known_hosts)
        client.get_host_keys().add(hostname, key.get_name(), key)


def _redact(value: str, secrets: Sequence[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[скрыто]")
    return redacted


class SSHSession:
    def __init__(
        self,
        client: paramiko.SSHClient,
        command_timeout: int,
        secrets: Sequence[str],
    ):
        self._client = client
        self._command_timeout = command_timeout
        self._secrets = tuple(secrets)

    def run(self, args: Sequence[str], stdin: bytes | None = None) -> SSHResult:
        command = shlex.join([str(value) for value in args])
        try:
            stdin_stream, stdout_stream, stderr_stream = self._client.exec_command(
                command,
                timeout=self._command_timeout,
            )
            if stdin is not None:
                stdin_stream.channel.sendall(stdin)
                stdin_stream.channel.shutdown_write()
            stdout = stdout_stream.read().decode("utf-8", errors="replace")
            stderr = stderr_stream.read().decode("utf-8", errors="replace")
            exit_code = int(stdout_stream.channel.recv_exit_status())
        except (TimeoutError, socket.timeout) as exc:
            raise SSHTimeoutError("SSH-команда превысила допустимое время") from exc
        except paramiko.SSHException as exc:
            raise SSHTransportError("Ошибка SSH-сеанса") from exc

        safe_stdout = _redact(stdout, self._secrets)
        safe_stderr = _redact(stderr, self._secrets)
        LOGGER.debug("SSH command completed with exit code %d", exit_code)
        if exit_code != 0:
            detail = safe_stderr.strip() or "удалённая команда завершилась с ошибкой"
            raise SSHCommandError(f"Ошибка удалённой команды: {detail}")
        return SSHResult(safe_stdout, safe_stderr, exit_code)

    def put_bytes(self, remote_path: str, content: bytes, mode: int) -> None:
        try:
            sftp = self._client.open_sftp()
            try:
                with sftp.file(remote_path, "wb") as remote:
                    remote.write(content)
                sftp.chmod(remote_path, mode)
            finally:
                sftp.close()
        except (TimeoutError, socket.timeout) as exc:
            raise SSHTimeoutError("Передача файла по SSH превысила допустимое время") from exc
        except paramiko.SSHException as exc:
            raise SSHTransportError("Не удалось передать файл по SSH") from exc


class SSHTransport:
    def __init__(
        self,
        known_hosts: Path,
        connect_timeout: int = 15,
        command_timeout: int = 300,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
    ):
        self.known_hosts = known_hosts
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.client_factory = client_factory

    @contextmanager
    def connect(self, credentials: SSHCredentials) -> Iterator[SSHSession]:
        client = self.client_factory()
        password = credentials.password.get_secret_value()
        try:
            if self.known_hosts.exists() and self.known_hosts.stat().st_size:
                client.load_host_keys(str(self.known_hosts))
            client.set_missing_host_key_policy(TrustOnFirstUsePolicy(self.known_hosts))
            try:
                client.connect(
                    hostname=str(credentials.host),
                    username=credentials.username,
                    password=password,
                    timeout=self.connect_timeout,
                    banner_timeout=self.connect_timeout,
                    auth_timeout=self.connect_timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            except paramiko.BadHostKeyException as exc:
                raise HostKeyChangedError(
                    "SSH host key изменился; подключение заблокировано"
                ) from exc
            except paramiko.AuthenticationException as exc:
                raise SSHAuthenticationError("Неверный пароль root") from exc
            except (
                TimeoutError,
                socket.timeout,
                paramiko.ssh_exception.NoValidConnectionsError,
            ) as exc:
                raise SSHTimeoutError("VPS не ответил по SSH вовремя") from exc
            except paramiko.SSHException as exc:
                raise SSHTransportError("Не удалось установить SSH-соединение") from exc

            yield SSHSession(client, self.command_timeout, (password,))
        finally:
            client.close()


__all__ = [
    "HostKeyChangedError",
    "SSHAuthenticationError",
    "SSHCommandError",
    "SSHCredentials",
    "SSHResult",
    "SSHSession",
    "SSHTimeoutError",
    "SSHTransport",
    "SSHTransportError",
    "TrustOnFirstUsePolicy",
]
