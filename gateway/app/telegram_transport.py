"""Bounded Telegram Bot API file uploads."""

import httpx


def upload_file(
    url: str,
    *,
    chat_id: str,
    file_field: str,
    filename: str,
    media_type: str,
    payload: bytes,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Upload one file and fail within bounded connect/write/read timeouts."""
    timeout = httpx.Timeout(connect=10.0, write=10.0, read=20.0, pool=5.0)
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(
            url,
            data={"chat_id": chat_id},
            files={file_field: (filename, payload, media_type)},
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description", "Telegram API error")))
