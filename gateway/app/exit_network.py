"""Deterministic tunnel allocation and local AmneziaWG uplink lifecycle."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Protocol

from gateway.app.exit_store import ExitRecord
from gateway.app.keygen import AwgKeyGenerator, AwgKeyPair


class TextRunner(Protocol):
    def run(self, args: list[str], input_text: str | None = None) -> str: ...


class SubprocessTextRunner:
    def run(self, args: list[str], input_text: str | None = None) -> str:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout


@dataclass(frozen=True)
class TunnelAllocation:
    network: IPv4Network
    remote_address: IPv4Address
    local_address: IPv4Address

    @classmethod
    def for_slot(cls, slot: int) -> "TunnelAllocation":
        if slot < 1:
            raise ValueError("slot must be positive")
        base = int(IPv4Network("10.200.0.0/16").network_address)
        network = IPv4Network((base + ((slot - 1) * 4), 30))
        return cls(network, network.network_address + 1, network.network_address + 2)

    @classmethod
    def from_record(cls, record: ExitRecord) -> "TunnelAllocation":
        network = IPv4Network(record.tunnel_cidr)
        return cls(network, network.network_address + 1, network.network_address + 2)


@dataclass
class LocalUplinkManager:
    runner: TextRunner
    config_dir: Path = Path("/etc/amnezia")
    systemd_dir: Path = Path("/etc/systemd/system")

    def generate_keys(self) -> AwgKeyPair:
        return AwgKeyGenerator(self.runner).generate()

    @staticmethod
    def _write_atomic(path: Path, content: str, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)

    def stage(
        self,
        record: ExitRecord,
        local_private_key: str,
        remote_public_key: str,
    ) -> None:
        allocation = TunnelAllocation.from_record(record)
        config_path = self.config_dir / f"{record.interface}.conf"
        unit_path = self.systemd_dir / f"{record.interface}.service"
        if record.slot == 1 and config_path.exists() and unit_path.exists():
            return

        config = "\n".join(
            [
                "[Interface]",
                f"PrivateKey = {local_private_key}",
                f"Address = {allocation.local_address}/30",
                "Table = off",
                "Jc = 4",
                "Jmin = 8",
                "Jmax = 80",
                "S1 = 25",
                "S2 = 111",
                "H1 = 234567",
                "H2 = 345678",
                "H3 = 456789",
                "H4 = 567891",
                "",
                "[Peer]",
                f"PublicKey = {remote_public_key}",
                "AllowedIPs = 0.0.0.0/0",
                f"Endpoint = {record.address}:{record.remote_port}",
                "PersistentKeepalive = 25",
                "",
            ]
        )
        unit = "\n".join(
            [
                "[Unit]",
                f"Description=AmneziaWG uplink for {record.name}",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=oneshot",
                "RemainAfterExit=yes",
                f"ExecStart=/usr/bin/awg-quick up /etc/amnezia/{record.interface}.conf",
                f"ExecStop=/usr/bin/awg-quick down /etc/amnezia/{record.interface}.conf",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )
        self._write_atomic(config_path, config, 0o600)
        self._write_atomic(unit_path, unit, 0o644)

    def start(self, record: ExitRecord) -> None:
        self.runner.run(["systemctl", "daemon-reload"])
        self.runner.run(["systemctl", "enable", "--now", f"{record.interface}.service"])

    def wait_handshake(self, record: ExitRecord, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.runner.run(
                ["awg", "show", record.interface, "latest-handshakes"]
            )
            handshakes = [
                int(line.rsplit("\t", 1)[-1])
                for line in output.splitlines()
                if line.rsplit("\t", 1)[-1].isdigit()
            ]
            if any(timestamp > 0 for timestamp in handshakes):
                return
            time.sleep(1)
        raise TimeoutError("AmneziaWG handshake не появился")

    def check_internet(self, record: ExitRecord) -> None:
        self.runner.run(
            [
                "curl",
                "--interface",
                record.interface,
                "-4",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "10",
                "https://api.ipify.org",
            ]
        )

    def remove(self, record: ExitRecord) -> None:
        try:
            self.runner.run(
                ["systemctl", "disable", "--now", f"{record.interface}.service"]
            )
        except (OSError, subprocess.SubprocessError):
            pass
        (self.config_dir / f"{record.interface}.conf").unlink(missing_ok=True)
        (self.systemd_dir / f"{record.interface}.service").unlink(missing_ok=True)
        self.runner.run(["systemctl", "daemon-reload"])


__all__ = [
    "LocalUplinkManager",
    "SubprocessTextRunner",
    "TunnelAllocation",
]
