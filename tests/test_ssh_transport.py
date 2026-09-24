import logging
from io import BytesIO
from ipaddress import IPv4Address

import paramiko
import pytest
from pydantic import SecretStr

from gateway.app.ssh_transport import (
    HostKeyChangedError,
    SSHCommandError,
    SSHCredentials,
    SSHTransport,
    TrustOnFirstUsePolicy,
)


class FakeChannel:
    def __init__(self, exit_code: int = 0):
        self.exit_code = exit_code
        self.input = b""

    def sendall(self, content: bytes) -> None:
        self.input += content

    def shutdown_write(self) -> None:
        pass

    def recv_exit_status(self) -> int:
        return self.exit_code


class FakeStream(BytesIO):
    def __init__(self, content: bytes = b"", channel: FakeChannel | None = None):
        super().__init__(content)
        self.channel = channel or FakeChannel()


class FakeSSHClient:
    def __init__(self, *, exit_code: int = 0, stderr: bytes = b""):
        self.exit_code = exit_code
        self.stderr = stderr
        self.executed: list[str] = []
        self.policy = None
        self.closed = False

    def load_host_keys(self, _path: str) -> None:
        pass

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def connect(self, **_kwargs) -> None:
        pass

    def exec_command(self, command: str, timeout: int):
        self.executed.append(command)
        channel = FakeChannel(self.exit_code)
        return (
            FakeStream(channel=channel),
            FakeStream(b"ok", channel),
            FakeStream(self.stderr, channel),
        )

    def close(self) -> None:
        self.closed = True


def credentials(password: str = "secret") -> SSHCredentials:
    return SSHCredentials(
        host=IPv4Address("203.0.113.2"),
        username="root",
        password=SecretStr(password),
    )


def test_password_is_not_in_remote_command_or_logs(tmp_path, caplog):
    """Shell-shaped password text must remain only in Paramiko authentication memory."""
    password = "q'$(touch /tmp/no)\nsecret"
    fake = FakeSSHClient()
    transport = SSHTransport(tmp_path / "known_hosts", client_factory=lambda: fake)

    with caplog.at_level(logging.DEBUG):
        with transport.connect(credentials(password)) as session:
            result = session.run(["printf", "%s", "safe value"])

    assert result.stdout == "ok"
    assert fake.executed == ["printf %s 'safe value'"]
    assert password not in "".join(fake.executed)
    assert password not in caplog.text
    assert fake.closed is True


def test_changed_host_key_is_mapped_before_any_command(tmp_path):
    expected = paramiko.RSAKey.generate(1024)
    received = paramiko.RSAKey.generate(1024)

    class ChangedKeyClient(FakeSSHClient):
        def connect(self, **kwargs) -> None:
            raise paramiko.BadHostKeyException(kwargs["hostname"], received, expected)

    fake = ChangedKeyClient()
    transport = SSHTransport(tmp_path / "known_hosts", client_factory=lambda: fake)

    with pytest.raises(HostKeyChangedError, match="изменился"):
        with transport.connect(credentials()):
            pass

    assert fake.executed == []


def test_first_use_policy_persists_received_public_key(tmp_path):
    known_hosts = tmp_path / "known_hosts"
    key = paramiko.RSAKey.generate(1024)
    client = paramiko.SSHClient()
    policy = TrustOnFirstUsePolicy(known_hosts)

    policy.missing_host_key(client, "203.0.113.2", key)

    loaded = paramiko.HostKeys(str(known_hosts))
    assert loaded.check("203.0.113.2", key) is True


def test_command_failure_redacts_password_from_exception(tmp_path):
    password = "never-show-this"
    fake = FakeSSHClient(exit_code=1, stderr=f"failed: {password}".encode())
    transport = SSHTransport(tmp_path / "known_hosts", client_factory=lambda: fake)

    with transport.connect(credentials(password)) as session:
        with pytest.raises(SSHCommandError) as caught:
            session.run(["false"])

    assert password not in str(caught.value)
    assert "[скрыто]" in str(caught.value)
