from contextlib import contextmanager
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from pydantic import SecretStr

from gateway.app.exit_provisioner import (
    ExitProvisioner,
    KernelModuleUnavailableError,
    ProvisionCancelled,
    UnsupportedRemoteOSError,
)
from gateway.app.exit_store import ExitRepository
from gateway.app.keygen import AwgKeyPair
from gateway.app.ssh_transport import (
    SSHCommandError,
    SSHCredentials,
    SSHResult,
    SSHTransportError,
)


class FakeSession:
    def __init__(
        self,
        os_release: str = 'ID=debian\nVERSION_ID="13"\n',
        *,
        boot_id: str = "boot-current",
        package_error: Exception | None = None,
    ):
        self.os_release = os_release
        self.boot_id = boot_id
        self.package_error = package_error
        self.commands: list[list[str]] = []
        self.uploads: list[tuple[str, bytes, int]] = []
        self.configured = False

    def run(self, args, stdin=None):
        command = list(args)
        self.commands.append(command)
        if command == ["cat", "/etc/os-release"]:
            return SSHResult(self.os_release, "", 0)
        if command == ["cat", "/proc/sys/kernel/random/boot_id"]:
            return SSHResult(f"{self.boot_id}\n", "", 0)
        if "packages" in command and self.package_error is not None:
            raise self.package_error
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


class SequencedTransport:
    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.connect_count = 0

    @contextmanager
    def connect(self, _credentials):
        self.connect_count += 1
        attempt = self.attempts.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        yield attempt


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


def test_provisioner_rejects_unsupported_os_before_install(tmp_path):
    session = FakeSession('ID=ubuntu\nVERSION_ID="24.04"\n')
    local = FakeLocalManager()
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    with pytest.raises(UnsupportedRemoteOSError):
        provisioner.provision(build_record(tmp_path), credentials(), lambda _stage: None, lambda: False)

    assert not any("packages" in command for command in session.commands)
    assert local.started == []


@pytest.mark.parametrize("version", ["12", "13"])
def test_provisioner_accepts_supported_debian_versions(tmp_path, version):
    session = FakeSession(f'ID=debian\nVERSION_ID="{version}"\n')
    local = FakeLocalManager()
    record = build_record(tmp_path)
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    provisioner.provision(record, credentials(), lambda _stage: None, lambda: False)

    assert any("packages" in command for command in session.commands)
    assert local.started == [record.id]


def test_provisioner_rejects_debian_11_before_install(tmp_path):
    session = FakeSession('ID=debian\nVERSION_ID="11"\n')
    local = FakeLocalManager()
    provisioner = ExitProvisioner(
        FakeTransport(session), local, Path("gateway/deploy/remote-exit.sh")
    )

    with pytest.raises(
        UnsupportedRemoteOSError,
        match="Debian 12.*Debian 13",
    ):
        provisioner.provision(
            build_record(tmp_path), credentials(), lambda _stage: None, lambda: False
        )

    assert not any("packages" in command for command in session.commands)


def test_provisioner_explains_when_host_kernel_cannot_load_module(tmp_path):
    class ModuleFailureSession(FakeSession):
        def run(self, args, stdin=None):
            if "packages" in args:
                raise SSHCommandError(
                    "Ошибка удалённой команды: AWG_KERNEL_MODULE_UNAVAILABLE"
                )
            return super().run(args, stdin)

    session = ModuleFailureSession('ID=debian\nVERSION_ID="12"\n')
    provisioner = ExitProvisioner(
        FakeTransport(session), FakeLocalManager(), Path("gateway/deploy/remote-exit.sh")
    )

    with pytest.raises(RuntimeError, match="ядро VPS не может загрузить"):
        provisioner.provision(
            build_record(tmp_path), credentials(), lambda _stage: None, lambda: False
        )


def test_provisioner_reboots_outdated_debian_kernel_and_resumes(tmp_path):
    reboot_required = SSHCommandError(
        "Ошибка удалённой команды: AWG_REBOOT_REQUIRED"
    )
    before_reboot = FakeSession(boot_id="boot-old", package_error=reboot_required)
    after_reboot_probe = FakeSession(boot_id="boot-new")
    resumed = FakeSession(boot_id="boot-new")
    transport = SequencedTransport(
        [
            before_reboot,
            SSHTransportError("VPS перезагружается"),
            after_reboot_probe,
            resumed,
        ]
    )
    local = FakeLocalManager()
    record = build_record(tmp_path)
    stages: list[str] = []
    provisioner = ExitProvisioner(
        transport,
        local,
        Path("gateway/deploy/remote-exit.sh"),
        reboot_timeout=1,
        sleeper=lambda _seconds: None,
    )

    provisioner.provision(record, credentials(), stages.append, lambda: False)

    assert ["systemctl", "reboot"] in before_reboot.commands
    assert stages.count("reboot") == 1
    assert local.started == [record.id]
    assert resumed.configured is True
    assert transport.connect_count == 4


def test_provisioner_stops_if_kernel_is_still_outdated_after_reboot(tmp_path):
    reboot_required = SSHCommandError(
        "Ошибка удалённой команды: AWG_REBOOT_REQUIRED"
    )
    before_reboot = FakeSession(boot_id="boot-old", package_error=reboot_required)
    after_reboot_probe = FakeSession(boot_id="boot-new")
    still_outdated = FakeSession(boot_id="boot-new", package_error=reboot_required)
    transport = SequencedTransport([before_reboot, after_reboot_probe, still_outdated])
    provisioner = ExitProvisioner(
        transport,
        FakeLocalManager(),
        Path("gateway/deploy/remote-exit.sh"),
        reboot_timeout=1,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(KernelModuleUnavailableError, match="после перезагрузки"):
        provisioner.provision(
            build_record(tmp_path),
            credentials(),
            lambda _stage: None,
            lambda: False,
        )

    assert before_reboot.commands.count(["systemctl", "reboot"]) == 1
    assert ["systemctl", "reboot"] not in still_outdated.commands


def test_provisioner_reports_reboot_command_failure_without_polling(tmp_path):
    class RebootCommandFailureSession(FakeSession):
        def run(self, args, stdin=None):
            if list(args) == ["systemctl", "reboot"]:
                raise SSHCommandError(
                    "Ошибка удалённой команды: systemctl reboot failed"
                )
            return super().run(args, stdin)

    session = RebootCommandFailureSession(
        boot_id="boot-old",
        package_error=SSHCommandError(
            "Ошибка удалённой команды: AWG_REBOOT_REQUIRED"
        ),
    )
    transport = FakeTransport(session)
    provisioner = ExitProvisioner(
        transport,
        FakeLocalManager(),
        Path("gateway/deploy/remote-exit.sh"),
        reboot_timeout=0,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(SSHCommandError, match="systemctl reboot failed"):
        provisioner.provision(
            build_record(tmp_path),
            credentials(),
            lambda _stage: None,
            lambda: False,
        )


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
