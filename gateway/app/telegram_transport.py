"""Bounded Telegram Bot API file uploads."""

import httpx


def build_client() -> httpx.Client:
    """Create the process-wide Telegram client with IPv4 and connect retries."""
    return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2))


def post_form(
    url: str,
    data: dict[str, str],
    *,
    read_timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Post a Telegram form with explicit connect/write/read time bounds."""
    timeout = httpx.Timeout(connect=10.0, write=10.0, read=read_timeout, pool=5.0)
    if client is None:
        with httpx.Client(timeout=timeout, transport=transport) as temporary_client:
            response = temporary_client.post(url, data=data)
    else:
        response = client.post(url, data=data, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description", "Telegram API error")))
    return result


def upload_file(
    url: str,
    *,
    chat_id: str,
    file_field: str,
    filename: str,
    media_type: str,
    payload: bytes,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Upload one file and fail within bounded connect/write/read timeouts."""
    timeout = httpx.Timeout(connect=10.0, write=10.0, read=20.0, pool=5.0)
    request = {
        "data": {"chat_id": chat_id},
        "files": {file_field: (filename, payload, media_type)},
        "timeout": timeout,
    }
    if client is None:
        with httpx.Client(timeout=timeout, transport=transport) as temporary_client:
            response = temporary_client.post(url, **request)
    else:
        response = client.post(url, **request)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description", "Telegram API error")))
