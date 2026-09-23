"""Small, testable transaction boundary for privileged state activation."""

from dataclasses import dataclass
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, command: str) -> bool: ...


@dataclass(frozen=True)
class ApplyResult:
    active_generation: str
    rolled_back: bool


def apply_state(staged: str, previous: str, runner: CommandRunner) -> ApplyResult:
    """Validate then activate staged artifacts, restoring the prior generation on failure."""
    if not runner.run(f"validate {staged}"):
        return ApplyResult(active_generation=previous, rolled_back=False)
    if runner.run(f"activate {staged}"):
        return ApplyResult(active_generation=staged, rolled_back=False)
    runner.run(f"restore {previous}")
    runner.run(f"activate {previous}")
    return ApplyResult(active_generation=previous, rolled_back=True)
