import httpx

from gateway.app.telegram_transport import post_form, upload_file


def test_upload_file_uses_bounded_multipart_request():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["content_type"] = request.headers["content-type"]
        observed["body"] = request.read()
        return httpx.Response(200, json={"ok": True})

    upload_file(
        "https://api.telegram.test/bot-token/sendDocument",
        chat_id="123",
        file_field="document",
        filename="iphone.conf",
        media_type="text/plain",
        payload=b"[Interface]\nPrivateKey = secret",
        transport=httpx.MockTransport(handler),
    )

    assert str(observed["content_type"]).startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in observed["body"]
    assert b'name="document"; filename="iphone.conf"' in observed["body"]
    assert b"[Interface]\nPrivateKey = secret" in observed["body"]


def test_post_form_applies_bounded_read_timeout_to_long_poll():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = request.read()
        observed["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"ok": True, "result": []})

    result = post_form(
        "https://api.telegram.test/bot-token/getUpdates",
        {"offset": "10", "timeout": "50"},
        read_timeout=60.0,
        transport=httpx.MockTransport(handler),
    )

    assert result == {"ok": True, "result": []}
    assert b"offset=10&timeout=50" == observed["body"]
    assert observed["timeout"]["read"] == 60.0
