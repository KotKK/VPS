from gateway.app.keygen import AwgKeyGenerator


class Runner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args: list[str], input_text: str | None = None) -> str:
        self.calls.append(args)
        return "public-key\n" if args[-1] == "pubkey" else "private-key\n"


def test_key_generator_uses_awg_and_never_shells_out():
    """Replacing awg with a shell string could expose private material to injection."""
    runner = Runner()
    keys = AwgKeyGenerator(runner).generate()
    assert keys.private_key == "private-key"
    assert keys.public_key == "public-key"
    assert runner.calls == [["awg", "genkey"], ["awg", "pubkey"]]
