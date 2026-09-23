"""Safe AmneziaWG peer mutations for the local control plane."""

import subprocess
from dataclasses import dataclass

from gateway.app.keygen import AwgKeyGenerator


@dataclass
class AwgPeerManager:
    interface: str = "awg-clients"

    def _run(self, args: list[str], input_text: str | None = None) -> str:
        result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=True)
        return result.stdout

    def create_keys(self):
        return AwgKeyGenerator(self).generate()

    def add(self, public_key: str, address: str) -> None:
        self._run(["awg", "set", self.interface, "peer", public_key, "allowed-ips", address])

    def remove(self, public_key: str) -> None:
        self._run(["awg", "set", self.interface, "peer", public_key, "remove"])
