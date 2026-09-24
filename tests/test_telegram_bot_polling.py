import importlib
import io
import sys


def test_polling_window_is_short_for_vpn_routed_telegram(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("AWG_GATEWAY_STATE_DIR", str(tmp_path))
    sys.modules.pop("gateway.app.telegram_bot", None)
    bot = importlib.import_module("gateway.app.telegram_bot")

    calls = []
    bot.api = lambda method, data: calls.append((method, data)) or {
        "ok": True,
        "result": [],
    }

    assert bot.get_updates(17) == []
    assert calls == [("getUpdates", {"offset": "17", "timeout": "3"})]


def test_telegram_http_request_cannot_block_menu_for_a_minute(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("AWG_GATEWAY_STATE_DIR", str(tmp_path))
    sys.modules.pop("gateway.app.telegram_bot", None)
    bot = importlib.import_module("gateway.app.telegram_bot")

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
