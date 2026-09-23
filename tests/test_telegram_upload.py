from gateway.app.telegram_transport import multipart_form


def test_multipart_form_contains_configuration_file_and_chat_id():
    body, content_type = multipart_form(
        {"chat_id": "123"},
        file_field="document",
        filename="iphone.conf",
        media_type="text/plain",
        payload=b"[Interface]\nPrivateKey = secret",
        boundary="test-boundary",
    )

    assert content_type == "multipart/form-data; boundary=test-boundary"
    assert b'name="chat_id"\r\n\r\n123' in body
    assert b'name="document"; filename="iphone.conf"' in body
    assert b"Content-Type: text/plain" in body
    assert b"[Interface]\nPrivateKey = secret" in body
