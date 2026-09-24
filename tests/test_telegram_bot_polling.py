import importlib
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

