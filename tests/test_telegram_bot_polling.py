import importlib
import io
import sys


def load_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("AWG_GATEWAY_STATE_DIR", str(tmp_path))
    sys.modules.pop("gateway.app.telegram_bot", None)
    return importlib.import_module("gateway.app.telegram_bot")


def test_polling_window_is_short_for_vpn_routed_telegram(monkeypatch, tmp_path):
    bot = load_bot(monkeypatch, tmp_path)

    calls = []
    bot.api = lambda method, data: calls.append((method, data)) or {
        "ok": True,
        "result": [],
    }

    assert bot.get_updates(17) == []
    assert calls == [("getUpdates", {"offset": "17", "timeout": "3"})]


def test_telegram_http_request_cannot_block_menu_for_a_minute(monkeypatch, tmp_path):
    bot = load_bot(monkeypatch, tmp_path)

    observed = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(_request, timeout):
        observed["timeout"] = timeout
        return Response(b'{"ok": true, "result": []}')

    monkeypatch.setattr(bot, "urlopen", fake_urlopen)

    bot.api("getUpdates", {"offset": "0", "timeout": "3"})

    assert observed["timeout"] == 8


def test_telegram_file_upload_is_multipart_and_bounded(monkeypatch, tmp_path):
    bot = load_bot(monkeypatch, tmp_path)
    observed = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["content_type"] = request.get_header("Content-type")
        observed["body"] = request.data
        observed["timeout"] = timeout
        return Response(b'{"ok": true, "result": {}}')

    monkeypatch.setattr(bot, "urlopen", fake_urlopen)

    bot.send_file(
        "sendDocument",
        "document",
        "iphone.conf",
        "text/plain",
        b"[Interface]\nPrivateKey = secret\n",
    )

    assert observed["url"].endswith("/sendDocument")
    assert observed["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in observed["body"]
    assert b"123" in observed["body"]
    assert b'filename="iphone.conf"' in observed["body"]
    assert b"PrivateKey = secret" in observed["body"]
    assert observed["timeout"] == 8


def test_client_actions_send_configuration_and_qr(monkeypatch, tmp_path):
    bot = load_bot(monkeypatch, tmp_path)
    bot.menus.clear()
    bot.registry.put("iphone", "[Interface]\nPrivateKey = secret\n")
    messages = []
    uploads = []
    monkeypatch.setattr(bot, "send", lambda text, rows=None: messages.append((text, rows)))
    monkeypatch.setattr(bot, "send_file", lambda *args: uploads.append(args))

    bot.process("Клиенты")
    bot.process("iphone")
    bot.process("Файл конфигурации")
    bot.process("QR-код")

    assert messages[0] == ("Выберите клиента.", [["iphone"], ["Отмена"]])
    assert messages[1] == (
        "Клиент: iphone",
        [["Файл конфигурации", "QR-код"], ["Назад", "Отмена"]],
    )
    assert uploads[0] == (
        "sendDocument",
        "document",
        "iphone.conf",
        "text/plain; charset=utf-8",
        b"[Interface]\nPrivateKey = secret\n",
    )
    assert uploads[1][0:4] == ("sendPhoto", "photo", "iphone-qr.png", "image/png")
    assert uploads[1][4].startswith(b"\x89PNG\r\n\x1a\n")


def test_upload_timeout_returns_menu_instead_of_crashing(monkeypatch, tmp_path):
    bot = load_bot(monkeypatch, tmp_path)
    bot.menus.clear()
    bot.registry.put("iphone", "[Interface]\nPrivateKey = secret\n")
    messages = []
    monkeypatch.setattr(bot, "send", lambda text, rows=None: messages.append((text, rows)))
    monkeypatch.setattr(bot, "send_file", lambda *_args: (_ for _ in ()).throw(TimeoutError()))

    bot.process("Клиенты")
    bot.process("iphone")
    bot.process("Файл конфигурации")

    assert messages[-1] == ("Операция не выполнена: TimeoutError", bot.main_keyboard())
