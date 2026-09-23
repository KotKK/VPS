"""AmneziaWG client profile and QR payload generation."""

from dataclasses import dataclass
from io import BytesIO

import qrcode


@dataclass(frozen=True)
class AwgParameters:
    jc: int
    jmin: int
    jmax: int
    s1: int
    s2: int
    h1: int
    h2: int
    h3: int
    h4: int


def build_client_profile(
    client_name: str,
    client_private_key: str,
    address: str,
    server_public_key: str,
    endpoint: str,
    parameters: AwgParameters,
) -> str:
    """Build the full profile imported by AmneziaWG applications."""
    return "\n".join(
        [
            f"# {client_name}",
            "[Interface]",
            f"PrivateKey = {client_private_key}",
            f"Address = {address}",
            "DNS = 1.1.1.1, 2606:4700:4700::1111",
            f"Jc = {parameters.jc}",
            f"Jmin = {parameters.jmin}",
            f"Jmax = {parameters.jmax}",
            f"S1 = {parameters.s1}",
            f"S2 = {parameters.s2}",
            f"H1 = {parameters.h1}",
            f"H2 = {parameters.h2}",
            f"H3 = {parameters.h3}",
            f"H4 = {parameters.h4}",
            "",
            "[Peer]",
            f"PublicKey = {server_public_key}",
            "AllowedIPs = 0.0.0.0/0, ::/0",
            f"Endpoint = {endpoint}",
            "PersistentKeepalive = 25",
            "",
        ]
    )


def qr_png(profile: str) -> bytes:
    """Encode a complete text profile into a portable PNG QR image."""
    image = qrcode.make(profile)
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()
