"""Encoding helpers for Telegram file uploads."""

from collections.abc import Mapping
from uuid import uuid4


def multipart_form(
    fields: Mapping[str, str],
    file_field: str,
    filename: str,
    media_type: str,
    payload: bytes,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Build a small multipart/form-data request body without extra dependencies."""
    boundary = boundary or f"awg-gateway-{uuid4().hex}"
    marker = boundary.encode("ascii")
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8") + b"\r\n",
            ]
        )

    chunks.extend(
        [
            b"--" + marker + b"\r\n",
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
            payload + b"\r\n",
            b"--" + marker + b"--\r\n",
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
