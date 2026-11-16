import io
import json
import sqlite3
import threading
from ipaddress import IPv4Address
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from gateway.app import max_api, max_bot
from gateway.app.max_api import MaxAPI, MaxAPIError
from gateway.app.max_bot import MaxBot, event_actor
from gateway.app.max_setup import pair, save_settings
from gateway.app.max_webhook import EventInbox, create_app
from gateway.app.exit_store import ExitStatus
from gateway.app.health import ExitStatus as HealthStatus
from gateway.app.notifications import MultiNotificationService, NotificationOutbox, NotificationService


def event(text, mid="m1", user="42", chat="100", chat_type="dialog"):
    return {"update_type": "message_created", "timestamp": 123,
            "message": {"sender": {"user_id": user, "is_bot": False},
                        "recipient": {"chat_id": chat, "chat_type": chat_type},
                        "body": {"mid": mid, "text": text}}}


class FakeAPI:
    def __init__(self):
        self.messages, self.files, self.answers = [], [], []

    def send(self, chat, text, attachments=None):
        self.messages.append((chat, text, attachments))

    def send_file(self, *args, **kwargs):
        self.files.append((args, kwargs))

    def answer(self, callback_id):
        self.answers.append(callback_id)


def bot(tmp_path):
    return MaxBot(FakeAPI(), tmp_path, "42", "100")


@pytest.mark.parametrize("user,chat,kind", [("43", "100", "dialog"), ("42", "200", "dialog"), ("42", "100", "chat")])
def test_other_accounts_and_groups_cannot_control_bot(tmp_path, user, chat, kind):
    controller = bot(tmp_path)
    controller.process(event("Клиенты", user=user, chat=chat, chat_type=kind))
    assert controller.api.messages == []


def test_callback_uses_clicker_not_message_sender(tmp_path):
    controller = bot(tmp_path)
    callback = event("ignored")
    callback["update_type"] = "message_callback"
    callback["message"]["sender"] = {"user_id": "bot", "is_bot": True}
    callback["callback"] = {"user": {"user_id": "42"}, "callback_id": "cb", "payload": "stale"}
    controller.process(callback)
    assert controller.api.answers == ["cb"]
    assert "устарела" in controller.api.messages[-1][1]
    callback["callback"]["user"]["user_id"] = "stranger"
    assert not controller.authorized(callback)


def test_client_create_cancel_and_delete_confirmation(tmp_path, monkeypatch):
    controller = bot(tmp_path)
    calls = []
    monkeypatch.setattr(max_bot, "panel_post", lambda *args: calls.append(args))
    controller.process(event("➕ Добавить клиента"))
    controller.process(event("Отмена"))
    assert calls == []
    controller.process(event("➕ Добавить клиента"))
    controller.process(event("iPad"))
    assert calls == [("/clients/new", {"action": "create", "name": "iPad"})]
    controller.registry.put("iPad", "[Interface]\nPrivateKey = secret\n")
    controller.process(event("➖ Удалить клиента"))
    controller.process(event("iPad"))
    assert len(calls) == 1
    controller.process(event("Отмена"))
    assert len(calls) == 1
    controller.process(event("➖ Удалить клиента"))
    controller.process(event("iPad"))
    controller.process(event("Подтвердить"))
    assert calls[-1] == ("/clients/iPad/delete", {})


def test_configuration_and_qr_are_actual_profile(tmp_path):
    controller = bot(tmp_path)
    profile = "[Interface]\nPrivateKey = secret\nJc = 4\n"
    controller.registry.put("iPad", profile)
    for text in ("Клиенты", "iPad", "Файл конфигурации", "QR-код"):
        controller.process(event(text))
    assert controller.api.files[0][0][1] == "iPad.conf"
    assert controller.api.files[0][0][3] == profile.encode()
    assert controller.api.files[1][0][3].startswith(b"\x89PNG\r\n\x1a\n")
    assert controller.api.files[1][1]["image"]


def test_status_command_during_name_input_does_not_create_client(tmp_path, monkeypatch):
    controller = bot(tmp_path)
    calls = []
    monkeypatch.setattr(max_bot, "panel_post", lambda *args: calls.append(args))
    controller.process(event("➕ Добавить клиента"))
    controller.process(event("Статус"))
    assert calls == []
    assert "VPS" in controller.api.messages[-1][1]


def test_webhook_auth_dedup_and_no_public_panel(tmp_path):
    controller = bot(tmp_path)
    inbox = EventInbox(tmp_path / "inbox.sqlite3")
    secret = "s" * 32
    with TestClient(create_app(controller, inbox, secret, run_worker=False)) as client:
        assert client.post("/max/webhook", json=event("Клиенты")).status_code == 403
        headers = {"X-Max-Bot-Api-Secret": secret}
        for _ in range(2):
            assert client.post("/max/webhook", json=event("Клиенты"), headers=headers).status_code == 200
        assert client.post("/max/webhook", json=event("Клиенты", mid="intruder", user="43"), headers=headers).status_code == 200
        assert client.get("/clients").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.post("/max/webhook", json=[], headers=headers).status_code == 400
        assert client.post("/max/webhook", content=b"x" * 262145, headers=headers).status_code == 413
    first = inbox.take()
    assert first[1]["message"]["body"]["mid"] == "m1"
    assert inbox.take() is None
    inbox.done(first[0])
    inbox.put(event("Клиенты"))
    assert inbox.take() is None


def test_interrupted_mutation_is_not_replayed_after_restart(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = EventInbox(path)
    inbox.put(event("Подтвердить"))
    assert inbox.take()
    restarted = EventInbox(path)
    assert restarted.take() is None


def test_api_header_auth_upload_and_attachment_readiness(monkeypatch):
    requests = []
    responses = [{"url": "https://fu.oneme.ru/upload?signature=secret"},
                 {"fileId": 1, "token": "file-token"}, "not_ready", {"message": {}}]

    def open_request(request, timeout, context):
        requests.append((request, timeout))
        value = responses.pop(0)
        if value == "not_ready":
            raise HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b'{"code":"attachment.not.ready"}'))
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(max_api, "urlopen", open_request)
    monkeypatch.setattr(max_api.time, "sleep", lambda _: None)
    api = MaxAPI("test-secret")
    api.send_file("100", "ipad.conf", "text/plain", b"private-profile")
    assert requests[0][0].get_header("Authorization") == "test-secret"
    assert "test-secret" not in requests[0][0].full_url
    assert requests[1][0].get_header("Authorization") is None
    assert b'filename="ipad.conf"' in requests[1][0].data
    assert b"private-profile" in requests[1][0].data
    sent = json.loads(requests[-1][0].data)
    assert sent["attachments"][0] == {"type": "file", "payload": {"token": "file-token"}}
    assert all(timeout <= 30 for _, timeout in requests)


def test_api_exception_does_not_disclose_credentials(monkeypatch):
    def fail(request, **kwargs):
        raise HTTPError(request.full_url, 400, "secret", {}, io.BytesIO(b'{"code":"bad secret TOKEN"}'))
    monkeypatch.setattr(max_api, "urlopen", fail)
    with pytest.raises(MaxAPIError) as caught:
        MaxAPI("TOKEN").send("100", "profile")
    assert str(caught.value) == "api_error"


def test_separate_delivery_receipts_and_max_works_without_exits(tmp_path):
    class Sender:
        def __init__(self, works):
            self.works, self.calls = works, []
        def send(self, text, interfaces):
            self.calls.append(text)
            return self.works
    tg_sender, mx_sender = Sender(False), Sender(True)
    tg = NotificationService(NotificationOutbox(tmp_path / "tg.sqlite3"), tg_sender)
    mx = NotificationService(NotificationOutbox(tmp_path / "mx.sqlite3"), mx_sender, require_interfaces=False)
    notifier = MultiNotificationService([tg, mx])
    notifier.notify("VPS недоступен", ())
    assert mx_sender.calls == ["VPS недоступен"]
    assert not mx.outbox.pending()
    assert len(tg.outbox.pending()) == 1
    notifier.flush(("awg-up",))
    assert mx_sender.calls == ["VPS недоступен"]
    tg_sender.works = True
    notifier.flush(("awg-up",))
    assert not tg.outbox.pending()


def test_pairing_requires_secret_in_private_chat(monkeypatch):
    class PairAPI:
        def call(self, method, path, params):
            return {"updates": [event("Привязать code", user="bad", chat_type="chat"),
                                event("wrong code", user="bad"),
                                event("Привязать code")], "marker": 3}
    assert pair(PairAPI(), "Привязать code", timeout=1) == ("42", "100")


def test_settings_preserve_telegram_and_restrict_permissions(tmp_path, monkeypatch):
    # fchmod is Linux-specific; the production setup runs on the Debian gateway.
    monkeypatch.setattr("gateway.app.max_setup.os.fchmod", lambda *_: None, raising=False)
    path = tmp_path / "secrets.env"
    path.write_text("TELEGRAM_BOT_TOKEN=preserved\nMAX_CHAT_ID=old\n")
    save_settings(path, {"MAX_CHAT_ID": "100", "MAX_BOT_TOKEN": "hidden"})
    text = path.read_text()
    assert "TELEGRAM_BOT_TOKEN=preserved\n" in text
    assert text.count("MAX_CHAT_ID=") == 1
    assert 'MAX_CHAT_ID="100"' in text
    assert list(tmp_path.glob(".max-secrets-*")) == []


def test_pagination_and_old_keyboard_invalidation(tmp_path):
    controller = bot(tmp_path)
    controller.choices = [f"client-{number}" for number in range(45)]
    controller.menu.state = "choose_client"
    controller.show_choices()
    first_buttons = set(controller.buttons)
    assert len(controller.api.messages[-1][2][0]["payload"]["buttons"]) <= 30
    controller.command("Следующие →")
    assert controller.page == 1
    assert not first_buttons.intersection(controller.buttons)


def test_worker_handles_commands_after_response_and_survives_failure(tmp_path):
    controller = bot(tmp_path)
    inbox = EventInbox(tmp_path / "inbox.sqlite3")
    handled = threading.Event()
    commands = []

    def process(update):
        commands.append(event_actor(update)[2])
        if len(commands) == 1:
            raise TimeoutError()
        handled.set()

    controller.process = process
    with TestClient(create_app(controller, inbox, "s" * 32)) as client:
        headers = {"X-Max-Bot-Api-Secret": "s" * 32}
        assert client.post("/max/webhook", json=event("Статус", mid="1"), headers=headers).status_code == 200
        assert client.post("/max/webhook", json=event("Клиенты", mid="2"), headers=headers).status_code == 200
        assert handled.wait(3)
    assert commands == ["Статус", "Клиенты"]


def test_status_lists_each_vps_and_uses_monitor_results(tmp_path, monkeypatch):
    controller = bot(tmp_path)
    first = controller.exits.create("Хельсинки", IPv4Address("203.0.113.10"))
    second = controller.exits.create("Стокгольм", IPv4Address("203.0.113.20"))
    # Use the same persisted installation transitions as the provisioner.
    controller.exits.set_stage(first.id, status=ExitStatus.READY, stage="ready")
    controller.exits.set_stage(second.id, status=ExitStatus.READY, stage="ready")
    controller.health.set(first.id, HealthStatus.DOWN)
    monkeypatch.setattr(max_bot.subprocess, "run", lambda *args, **kwargs: type("Result", (), {
        "stdout": f"public-key\t{int(max_bot.time.time())}\n"})())
    text = controller.status()
    assert "Хельсинки (203.0.113.10) — недоступен" in text
    assert "Стокгольм (203.0.113.20) — доступен" in text


def test_image_upload_extracts_photo_token(monkeypatch):
    api = MaxAPI("token")
    messages = []
    monkeypatch.setattr(api, "call", lambda *args, **kwargs: {"url": "https://iu.oneme.ru/uploadImage?sig=signed"})
    monkeypatch.setattr(api, "_request", lambda *args, **kwargs: {"photos": {"123": {"token": "qr-image-token"}}})
    monkeypatch.setattr(api, "send", lambda *args: messages.append(args))
    api.send_file("100", "qr.png", "image/png", b"png", image=True)
    assert messages[0][2][0] == {"type": "image", "payload": {"token": "qr-image-token"}}
