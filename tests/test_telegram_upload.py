import httpx

from gateway.app.telegram_transport import upload_file


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
