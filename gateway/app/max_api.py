"""Small MAX Bot API client; credentials never appear in URLs or error messages."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from gateway.app.max_direct import urlopen, validate_interface

from gateway.app.telegram_transport import multipart_form

API_URL = "https://platform-api2.max.ru"


class MaxAPIError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", code) else "api_error"
        super().__init__(self.code)


class MaxAPI:
    def __init__(self, token: str, ca_file: str | None = None, *, direct_interface: str | None = None):
        if not token.strip() or "\n" in token or "\r" in token:
            raise ValueError("MAX token is missing or invalid")
        self.token = token
        interface = os.getenv("MAX_DIRECT_INTERFACE", "") if direct_interface is None else direct_interface
        self.direct_interface = validate_interface(interface) if interface else ""
        self.context = ssl.create_default_context()
        if ca_file:
            self.context.load_verify_locations(cafile=ca_file)

    def _request(self, request: Request, timeout: int = 15) -> dict:
        try:
            with urlopen(request, timeout=timeout, context=self.context,
                         interface=self.direct_interface) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read(16384)).get("code", f"http_{exc.code}")
            except (ValueError, AttributeError):
                code = f"http_{exc.code}"
            raise MaxAPIError(str(code)) from None
        except (OSError, URLError, ValueError):
            raise MaxAPIError("transport_error") from None
        if not isinstance(result, dict):
            raise MaxAPIError("invalid_response")
        if result.get("success") is False or "code" in result:
            raise MaxAPIError(str(result.get("code", "request_failed")))
        return result

    def call(self, method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
        url = API_URL + path
        if params:
            url += "?" + urlencode(params)
        return self._request(Request(
            url, method=method,
            headers={"Authorization": self.token, "Content-Type": "application/json"},
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        ))

    def send(self, chat_id: str, text: str, attachments: list | None = None) -> dict:
        body: dict = {"text": text}
        if attachments is not None:
            body["attachments"] = attachments
        return self.call("POST", "/messages", params={"chat_id": chat_id}, body=body)

    def answer(self, callback_id: str) -> None:
        self.call("POST", "/answers", params={"callback_id": callback_id}, body={})

    def send_file(self, chat_id: str, filename: str, media_type: str, data: bytes,
                  *, image: bool = False, attachments: list | None = None) -> None:
        kind = "image" if image else "file"
        upload = self.call("POST", "/uploads", params={"type": kind})
        url = str(upload.get("url", ""))
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or not any(host == domain or host.endswith("." + domain)
                           for domain in ("max.ru", "oneme.ru", "okcdn.ru"))):
            raise MaxAPIError("invalid_upload_url")
        body, content_type = multipart_form({}, "data", filename, media_type, data)
        # Upload URLs are signed; do not forward the bot's API credential to media hosts.
        result = self._request(Request(url, data=body, method="POST",
                                       headers={"Content-Type": content_type}), timeout=30)
        token = result.get("token")
        if image and not token:
            token = next((photo.get("token") for photo in result.get("photos", {}).values()
                          if isinstance(photo, dict) and photo.get("token")), None)
        if not isinstance(token, str) or not token:
            raise MaxAPIError("missing_upload_token")
        media = [{"type": kind, "payload": {"token": token}}] + (attachments or [])
        for attempt in range(4):
            try:
                self.send(chat_id, filename, media)
                return
            except MaxAPIError as exc:
                if exc.code != "attachment.not.ready" or attempt == 3:
                    raise
                time.sleep(2 ** attempt)
