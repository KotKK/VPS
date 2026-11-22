"""Monitor local VPN readiness, independently of foreign exit reachability."""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

RECOVERY_TEXTS = {
    "kernel-module": "модуль AmneziaWG для текущего ядра доступен",
    "kernel-headers": "автоматическое обновление заголовков ядра настроено",
    "next-kernel-module": "модули AmneziaWG для установленных ядер подготовлены",
    "forwarding": "пересылка IP-пакетов включена",
    "client-ingress": "клиентский VPN-туннель снова работает",
    "client-peers": "все клиентские подключения загружены корректно",
}


@dataclass(frozen=True)
class GatewayCheck:
    name: str
    healthy: bool
    detail: str


def run_command(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=8).stdout.strip()


class GatewayProbe:
    def __init__(self, state_dir: Path, runner: Callable[[list[str]], str] = run_command,
                 boot_image: Path = Path("/vmlinuz"), boot_directory: Path = Path("/boot")):
        self.state_dir, self.runner, self.boot_image = state_dir, runner, boot_image
        self.boot_directory = boot_directory

    def _read(self, args: list[str]) -> str | None:
        try:
            return self.runner(args).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def checks(self) -> list[GatewayCheck]:
        module = self._read(["modinfo", "amneziawg"])
        checks = [GatewayCheck("kernel-module", module is not None,
                               "модуль AmneziaWG для текущего ядра недоступен")]
        kernel = self._read(["uname", "-r"]) or ""
        header_meta = "linux-headers-cloud-amd64" if kernel.endswith("-cloud-amd64") else "linux-headers-amd64"
        installed = self._read(["dpkg-query", "-W", "-f=${db:Status-Status}", header_meta])
        checks.append(GatewayCheck("kernel-headers", installed == "installed",
                                   "не установлен пакет автоматического обновления заголовков ядра"))
        images = [self.boot_image] if self.boot_image.exists() else list(self.boot_directory.glob("vmlinuz-*"))
        missing_modules = []
        checked_kernels = []
        for image in images:
            filename = image.resolve().name
            next_kernel = filename.removeprefix("vmlinuz-")
            if filename.startswith("vmlinuz-") and re.fullmatch(r"[A-Za-z0-9.+_-]{1,100}", next_kernel):
                # Debian need not provide /vmlinuz; check installed current/newer
                # images directly and ignore old rollback kernels.
                if next_kernel != kernel and self._read(["dpkg", "--compare-versions", next_kernel, kernel, "gt"]) is None:
                    continue
                checked_kernels.append(next_kernel)
                ready = module is not None if next_kernel == kernel else self._read(
                    ["modinfo", "-k", next_kernel, "amneziawg"],
                ) is not None
                if not ready:
                    missing_modules.append(next_kernel)
        if checked_kernels:
            checks.append(GatewayCheck("next-kernel-module", not missing_modules,
                f"для установленного ядра ({', '.join(missing_modules)}) ещё нет модуля AmneziaWG; до исправления не перезагружайте VPS"))
        checks.append(GatewayCheck("forwarding", self._read(["sysctl", "-n", "net.ipv4.ip_forward"]) == "1",
                                   "отключена пересылка IP-пакетов"))
        loaded = self._read(["awg", "show", "awg-clients", "allowed-ips"])
        try:
            link = json.loads(self._read(["ip", "-j", "link", "show", "dev", "awg-clients"]) or "[]")
            is_up = bool(link and "UP" in link[0].get("flags", []))
        except (ValueError, TypeError, AttributeError, KeyError):
            is_up = False
        if loaded is None or not is_up:
            checks.append(GatewayCheck("client-ingress", False, "клиентский VPN-туннель не работает"))
            # Do not turn an absent interface into a misleading missing-peers alert too.
            return checks
        checks.append(GatewayCheck("client-ingress", True, "клиентский VPN-туннель не работает"))
        try:
            with sqlite3.connect(f"file:{(self.state_dir / 'clients.sqlite3').as_posix()}?mode=ro", uri=True, timeout=10) as conn:
                rows = conn.execute("SELECT public_key,address FROM clients WHERE profile <> ''").fetchall()
            actual = {}
            for line in loaded.splitlines():
                fields = line.split()
                if fields:
                    actual[fields[0]] = set("".join(fields[1:]).split(","))
            missing = 0
            for key, address in rows:
                address = f"{ipaddress.IPv4Address(address)}/32"
                if not key or address not in actual.get(key, set()):
                    missing += 1
            checks.append(GatewayCheck("client-peers", missing == 0,
                f"в туннеле отсутствуют или неверно настроены {missing} из {len(rows)} клиентских подключений"))
        except (sqlite3.Error, ValueError, TypeError):
            checks.append(GatewayCheck("client-peers", False, "не удалось проверить сохранённые клиентские подключения"))
        return checks


class GatewayHealthMonitor:
    def __init__(self, database: Path, probe: GatewayProbe, notifications, failure_checks: int = 2):
        self.database, self.probe, self.notifications = database, probe, notifications
        self.failure_checks = max(1, failure_checks)
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database, timeout=30) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS gateway_health ("
                         "name TEXT PRIMARY KEY, status TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0)")

    def run(self, interfaces: Sequence[str]) -> None:
        alerts = []
        checks = self.probe.checks()
        with sqlite3.connect(self.database, timeout=30) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for check in checks:
                row = conn.execute("SELECT status,failures FROM gateway_health WHERE name=?", (check.name,)).fetchone()
                previous, failures = row if row else ("up", 0)
                failures = 0 if check.healthy else failures + 1
                current = "up" if check.healthy else ("down" if failures >= self.failure_checks else previous)
                conn.execute("INSERT INTO gateway_health(name,status,failures) VALUES(?,?,?) "
                             "ON CONFLICT(name) DO UPDATE SET status=excluded.status,failures=excluded.failures",
                             (check.name, current, failures))
                if previous != current:
                    alerts.append((current, check))
        for state, check in alerts:
            message = (f"🔴 Российский VPS: {check.detail}." if state == "down"
                       else f"🟢 Российский VPS: {RECOVERY_TEXTS[check.name]}.")
            self.notifications.notify(message, interfaces)
        self.notifications.flush(interfaces)
