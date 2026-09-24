from contextlib import contextmanager
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from pydantic import SecretStr

from gateway.app.exit_provisioner import (
    ExitProvisioner,
    ProvisionCancelled,
    UnsupportedRemoteOSError,
)
from gateway.app.exit_store import ExitRepository
from gateway.app.keygen import AwgKeyPair
from gateway.app.ssh_transport import SSHCredentials, SSHResult


class FakeSession:
    def __init__(self, os_release: str = 'ID=debian\nVERSION_ID="13"\n'):
        self.os_release = os_release
        self.commands: list[list[str]] = []
        self.uploads: list[tuple[str, bytes, int]] = []
        self.configured = False

    def run(self, args, stdin=None):
        command = list(args)
        self.commands.append(command)
        if command == ["cat", "/etc/os-release"]:
            return SSHResult(self.os_release, "", 0)
        if "configure" in command:
            self.configured = True
            return SSHResult("remote-public\n", "", 0)
        return SSHResult("", "", 0)

    def put_bytes(self, remote_path: str, content: bytes, mode: int) -> None:
        self.uploads.append((remote_path, content, mode))


class FakeTransport:
    def __init__(self, session: FakeSession):
        self.session = session

    @contextmanager
    def connect(self, _credentials):
        yield self.session


class FakeLocalManager:
    def __init__(self):
        self.staged = []
        self.started = []
        self.removed = []
        self.handshakes = []
        self.internet_checks = []

    def generate_keys(self):
        return AwgKeyPair("local-private", "local-public")

    def stage(self, record, private_key, remote_public_key):
        self.staged.append((record.id, private_key, remote_public_key))

    def start(self, record):
        self.started.append(record.id)

    def remove(self, record):
        self.removed.append(record.id)

    def wait_handshake(self, record, timeout):
        self.handshakes.append((record.id, timeout))

    def check_internet(self, record):
        self.internet_checks.append(record.id)


def build_record(tmp_path):
    return ExitRepository(tmp_path / "state.sqlite3").create(
        "de", IPv4Address("203.0.113.2")
    )


def credentials():
    return SSHCredentials(
        IPv4Address("203.0.113.2"), "root", SecretStr("secret")
    )


def test_provisioner_rejects_non_debian_13_before_install(tmp_path):
    session = FakeSession('ID=ubuntu\nVERSION_ID="24.04"\n')
    local = FakeLocalManager()
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    with pytest.raises(UnsupportedRemoteOSError):
        provisioner.provision(build_record(tmp_path), credentials(), lambda _stage: None, lambda: False)

    assert not any("packages" in command for command in session.commands)
    assert local.started == []


def test_cancel_after_remote_configuration_cleans_local_artifacts(tmp_path):
    session = FakeSession()
    local = FakeLocalManager()
    record = build_record(tmp_path)
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    with pytest.raises(ProvisionCancelled):
        provisioner.provision(
            record,
            credentials(),
            lambda _stage: None,
            lambda: session.configured,
        )

    assert local.removed == [record.id]
    assert local.started == []


def test_successful_provision_runs_all_checks_before_returning(tmp_path):
    session = FakeSession()
    local = FakeLocalManager()
    record = build_record(tmp_path)
    stages: list[str] = []
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    provisioner.provision(record, credentials(), stages.append, lambda: False)

    assert stages == [
        "ssh",
        "os_check",
        "packages",
        "remote_tunnel",
        "local_tunnel",
        "handshake",
        "internet_check",
    ]
    assert local.staged == [(record.id, "local-private", "remote-public")]
    assert local.started == [record.id]
    assert local.handshakes == [(record.id, 45)]
    assert local.internet_checks == [record.id]
    assert all(b"secret" not in content for _, content, _ in session.uploads)
