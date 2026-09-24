from pathlib import Path

from gateway.app.apply import ApplyResult, EgressApplier, apply_state
from gateway.app.renderer import ExitRoute, GatewayState


class ScriptedRunner:
    def __init__(self, failing_command: str):
        self.failing_command = failing_command
        self.calls: list[str] = []

    def run(self, command: str) -> bool:
        self.calls.append(command)
        return command != self.failing_command


def test_failed_activation_restores_previous_generation():
    """A failed reload must leave the last working routing generation active."""
    runner = ScriptedRunner("activate staged")
    result = apply_state("staged", "previous", runner)
    assert result == ApplyResult(active_generation="previous", rolled_back=True)
    assert runner.calls == ["validate staged", "activate staged", "restore previous", "activate previous"]


class RecordingEgressRunner:
    def __init__(self, failing_command: tuple[str, ...] | None = None):
        self.failing_command = failing_command
        self.calls: list[tuple[str, ...]] = []
        self.files: dict[Path, str] = {}

    def write_atomic(self, path: Path, content: str) -> None:
        self.files[path] = content

    def run(self, args: list[str]) -> bool:
        command = tuple(args)
        self.calls.append(command)
        return command != self.failing_command


def one_exit_state() -> GatewayState:
    return GatewayState(
        exits=(ExitRoute("exit-1", "awg-uplink", "153.76.194.217", 101, 101),)
    )


def two_exit_state() -> GatewayState:
    return GatewayState(
        exits=(
            ExitRoute("exit-1", "awg-uplink", "153.76.194.217", 101, 101),
            ExitRoute("exit-2", "awg-uplink-2", "203.0.113.2", 102, 102),
        )
    )


def test_invalid_new_nft_keeps_previous_generation_without_mutation():
    validation = ("nft", "-c", "-f", "/run/awg-gateway/next.nft")
    runner = RecordingEgressRunner(failing_command=validation)

    result = EgressApplier(runner).apply(one_exit_state(), two_exit_state())

    assert result.active == one_exit_state()
    assert result.rolled_back is False
    assert runner.calls == [validation]


def test_removed_exit_is_cleaned_only_after_new_rules_activate():
    runner = RecordingEgressRunner()

    result = EgressApplier(runner).apply(two_exit_state(), one_exit_state())

    assert result.active == one_exit_state()
    activate_index = runner.calls.index(("nft", "-f", "/run/awg-gateway/next.nft"))
    delete_index = runner.calls.index(("ip", "rule", "del", "fwmark", "0x66", "lookup", "102"))
    assert activate_index < delete_index


def test_existing_policy_rule_is_deleted_before_supported_add_command():
    runner = RecordingEgressRunner()

    result = EgressApplier(runner).apply(GatewayState(()), one_exit_state())

    assert result.active == one_exit_state()
    delete = ("ip", "rule", "del", "fwmark", "0x65", "lookup", "101")
    add = ("ip", "rule", "add", "fwmark", "0x65", "lookup", "101")
    assert runner.calls.index(delete) < runner.calls.index(add)
    assert not any(call[:3] == ("ip", "rule", "replace") for call in runner.calls)


def test_legacy_source_rule_is_removed_only_after_new_generation_activates():
    runner = RecordingEgressRunner()

    EgressApplier(runner).apply(GatewayState(()), one_exit_state())

    activate = ("nft", "-f", "/run/awg-gateway/next.nft")
    legacy = ("ip", "rule", "del", "from", "10.20.0.0/24", "lookup", "101")
    assert runner.calls.index(activate) < runner.calls.index(legacy)
