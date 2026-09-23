"""Health state transitions, isolated from probe transport and Telegram delivery."""

from dataclasses import dataclass
from enum import StrEnum


class ExitStatus(StrEnum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class ExitProbe:
    healthy: bool
    reason: str


@dataclass(frozen=True)
class Alert:
    kind: str
    reason: str


def transition(previous: ExitStatus, probe: ExitProbe) -> Alert | None:
    """Emit once for each state edge so a persistent outage does not spam alerts."""
    if previous is ExitStatus.UP and not probe.healthy:
        return Alert("down", probe.reason)
    if previous is ExitStatus.DOWN and probe.healthy:
        return Alert("up", probe.reason)
    return None
