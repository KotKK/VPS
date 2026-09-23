from gateway.app.apply import ApplyResult, apply_state


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
