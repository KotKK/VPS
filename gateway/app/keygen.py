"""Narrow interface for generating AmneziaWG key pairs."""

from dataclasses import dataclass
from typing import Protocol


class AwgRunner(Protocol):
    def run(self, args: list[str], input_text: str | None = None) -> str: ...


@dataclass(frozen=True)
class AwgKeyPair:
    private_key: str
    public_key: str


class AwgKeyGenerator:
    def __init__(self, runner: AwgRunner):
        self._runner = runner

    def generate(self) -> AwgKeyPair:
        private_key = self._runner.run(["awg", "genkey"]).strip()
        public_key = self._runner.run(["awg", "pubkey"], input_text=f"{private_key}\n").strip()
        if not private_key or not public_key:
            raise RuntimeError("awg did not generate a complete key pair")
        return AwgKeyPair(private_key, public_key)
