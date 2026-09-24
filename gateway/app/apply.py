"""Small, testable transaction boundary for privileged state activation."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Protocol

from gateway.app.renderer import GatewayState, render_egress_nft, render_policy_routes


class CommandRunner(Protocol):
    def run(self, command: str) -> bool: ...


@dataclass(frozen=True)
class ApplyResult:
    active_generation: str = ""
    rolled_back: bool = False
    active: GatewayState | None = None


def apply_state(staged: str, previous: str, runner: CommandRunner) -> ApplyResult:
    """Validate then activate staged artifacts, restoring the prior generation on failure."""
    if not runner.run(f"validate {staged}"):
        return ApplyResult(active_generation=previous, rolled_back=False)
    if runner.run(f"activate {staged}"):
        return ApplyResult(active_generation=staged, rolled_back=False)
    runner.run(f"restore {previous}")
    runner.run(f"activate {previous}")
    return ApplyResult(active_generation=previous, rolled_back=True)


class EgressCommandRunner(Protocol):
    def write_atomic(self, path: Path, content: str) -> None: ...

    def run(self, args: list[str]) -> bool: ...


class SubprocessEgressRunner:
    def write_atomic(self, path: Path | PurePosixPath, content: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, target)

    def run(self, args: list[str]) -> bool:
        return subprocess.run(args, capture_output=True, text=True).returncode == 0


@dataclass
class EgressApplier:
    """Validate and activate one complete egress-routing generation."""

    runner: EgressCommandRunner
    runtime_dir: PurePosixPath = PurePosixPath("/run/awg-gateway")

    def _write(self, name: str, state: GatewayState) -> PurePosixPath:
        path = self.runtime_dir / name
        self.runner.write_atomic(path, render_egress_nft(state))
        return path

    def _install_routes(self, state: GatewayState) -> bool:
        for route in state.exits:
            # `ip rule` has no portable `replace` operation. Remove a possible
            # prior copy first so repeated panel starts remain idempotent.
            self.runner.run(
                [
                    "ip",
                    "rule",
                    "del",
                    "fwmark",
                    hex(route.mark),
                    "lookup",
                    str(route.route_table),
                ]
            )
        return all(self.runner.run(command) for command in render_policy_routes(state))

    def _restore(self, previous: GatewayState) -> None:
        previous_path = self._write("previous.nft", previous)
        self._install_routes(previous)
        self.runner.run(["nft", "-f", str(previous_path)])

    def apply(self, previous: GatewayState, desired: GatewayState) -> ApplyResult:
        next_path = self._write("next.nft", desired)
        if not self.runner.run(["nft", "-c", "-f", str(next_path)]):
            return ApplyResult(active=previous)

        if not self._install_routes(desired):
            self._restore(previous)
            return ApplyResult(active=previous, rolled_back=True)

        if not self.runner.run(["nft", "-f", str(next_path)]):
            self._restore(previous)
            return ApplyResult(active=previous, rolled_back=True)

        # Older installations routed the whole client subnet directly to
        # table 101. It has a higher priority than the per-flow fwmark rules,
        # so leave it in place until the replacement generation is active,
        # then remove it to enable balancing.
        self.runner.run(
            [
                "ip",
                "rule",
                "del",
                "from",
                "10.20.0.0/24",
                "lookup",
                "101",
            ]
        )

        desired_tables = {route.route_table for route in desired.exits}
        for stale in previous.exits:
            if stale.route_table in desired_tables:
                continue
            self.runner.run(
                [
                    "ip",
                    "rule",
                    "del",
                    "fwmark",
                    hex(stale.mark),
                    "lookup",
                    str(stale.route_table),
                ]
            )
            self.runner.run(["ip", "route", "flush", "table", str(stale.route_table)])

        return ApplyResult(active=desired)
