"""Operator-only MAX menu sharing the Telegram client-management state machine."""

from __future__ import annotations

import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.health import ExitStatus as HealthStatus
from gateway.app.health_monitor import HealthStateRepository
from gateway.app.max_api import MaxAPI
from gateway.app.profiles import qr_png
from gateway.app.store import ClientRegistry
from gateway.app.telegram_menu import TelegramMenu, format_exit_statuses

MAIN_ROWS = [["Клиенты", "Статус"], ["➕ Добавить клиента", "➖ Удалить клиента"], ["Панель"]]
CLIENT_ROWS = [["Файл конфигурации", "QR-код"], ["Назад", "Отмена"]]


def event_actor(update: dict) -> tuple[str, str, str]:
    """Return the actual author, destination and command (never a forwarded author)."""
    kind = update.get("update_type")
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    if kind == "bot_started":
        user, chat, text = update.get("user") or {}, update.get("chat_id"), "/start"
    elif kind == "message_created":
        if recipient.get("chat_type") != "dialog":
            return "", "", ""
        user, chat = message.get("sender") or {}, recipient.get("chat_id")
        text = (message.get("body") or {}).get("text")
    elif kind == "message_callback":
        if recipient.get("chat_type") != "dialog":
            return "", "", ""
        callback = update.get("callback") or {}
        user, chat, text = callback.get("user") or {}, recipient.get("chat_id"), callback.get("payload")
    else:
        return "", "", ""
    if user.get("is_bot") or not isinstance(text, str):
        return "", "", ""
    return str(user.get("user_id", "")), str(chat or ""), text


def panel_post(path: str, values: dict[str, str]) -> None:
    request = Request("http://127.0.0.1:8080" + path,
                      data=urlencode(values).encode(), method="POST")
    with urlopen(request, timeout=45) as response:
        response.read()


class MaxBot:
    def __init__(self, api: MaxAPI, state_dir: Path, user_id: str, chat_id: str):
        self.api, self.user_id, self.chat_id = api, user_id, chat_id
        self.registry = ClientRegistry(state_dir / "clients.sqlite3")
        self.exits = ExitRepository(state_dir / "exits.sqlite3")
        self.health = HealthStateRepository(state_dir / "health.sqlite3")
        self.menu = TelegramMenu()
        self.buttons: dict[str, str] = {}
        self.choices: list[str] = []
        self.page = 0

    def authorized(self, update: dict) -> bool:
        user, chat, _ = event_actor(update)
        return bool(self.user_id and self.chat_id and user == self.user_id and chat == self.chat_id)

    def keyboard(self, rows: list[list[str]]) -> list[dict]:
        self.buttons = {secrets.token_hex(12): text for row in rows for text in row}
        tokens = {text: key for key, text in self.buttons.items()}
        return [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "callback", "text": text, "payload": tokens[text]} for text in row]
            for row in rows
        ]}}]

    def send(self, text: str, rows: list[list[str]] | None = None) -> None:
        self.api.send(self.chat_id, text, self.keyboard(rows or MAIN_ROWS))

    def show_choices(self) -> None:
        rows = [[name] for name in self.choices[self.page * 20:(self.page + 1) * 20]]
        nav = []
        if self.page:
            nav.append("← Предыдущие")
        if (self.page + 1) * 20 < len(self.choices):
            nav.append("Следующие →")
        self.send("Выберите клиента." if self.choices else "Клиентов пока нет.",
                  rows + ([nav] if nav else []) + [["Отмена"]])

    def status(self) -> str:
        def read(interface: str) -> str:
            return subprocess.run(["awg", "show", interface, "latest-handshakes"],
                                  capture_output=True, text=True, check=True, timeout=5).stdout
        lines = []
        now = int(time.time())
        for record in self.exits.list():
            if record.status is ExitStatus.READY and self.health.get(record.id) is HealthStatus.DOWN:
                lines.append(f"• {record.name} ({record.address}) — недоступен по результатам мониторинга")
            else:
                lines.extend(format_exit_statuses([record], read, now).splitlines()[1:])
        return "Зарубежные VPS:\n" + ("\n".join(lines) or "нет")

    def process(self, update: dict) -> None:
        if not self.authorized(update):
            return
        _, _, text = event_actor(update)
        if update["update_type"] == "message_callback":
            # A failed callback acknowledgement must never block the command.
            try:
                self.api.answer(str(update["callback"]["callback_id"]))
            except Exception:
                pass
            if text not in self.buttons:
                self.menu = TelegramMenu()
                self.send("Эта кнопка устарела. Выберите действие в новом меню.")
                return
            text = self.buttons[text]
        try:
            self.command(text)
        except Exception as exc:
            # Profile contents, upload URLs and credentials must not reach the journal.
            print(f"MAX operation failed: {type(exc).__name__}", flush=True)
            self.menu = TelegramMenu()
            self.send("Операция не выполнена. Проверьте результат в панели и повторите действие.")

    def command(self, text: str) -> None:
        if text in {"/start", "/menu", "Меню"}:
            self.menu = TelegramMenu()
            self.send("Управление AmneziaWG. Выберите действие.")
            return
        # Global commands work even while a client name is being requested.
        if text in {"Клиенты", "Статус", "Панель"}:
            self.menu = TelegramMenu()
        if text in {"← Предыдущие", "Следующие →"} and self.menu.state in {"choose_client", "choose_delete"}:
            self.page += -1 if text.startswith("←") else 1
            self.page = max(0, min(self.page, max(0, (len(self.choices) - 1) // 20)))
            self.show_choices()
            return
        result = self.menu.handle(text, self.registry.names())
        if result.kind == "ask_name":
            self.send("Введите имя нового клиента.", [["Отмена"]])
        elif result.kind == "invalid_name":
            self.send("Имя: 1–63 символа. Допустимы буквы, цифры, пробел, '.', '_' и '-'.", [["Отмена"]])
        elif result.kind in {"choose_client", "choose_delete"}:
            self.choices, self.page = list(result.choices), 0
            self.show_choices()
        elif result.kind == "confirm_delete":
            self.send(f"Удалить клиента «{result.client_name}» и отозвать его ключ?", [["Подтвердить", "Отмена"]])
        elif result.kind == "create":
            panel_post("/clients/new", {"action": "create", "name": result.client_name or ""})
            self.menu.state, self.menu.selected = "client_actions", result.client_name
            self.send(f"Клиент «{result.client_name}» создан.", CLIENT_ROWS)
        elif result.kind == "delete":
            panel_post(f"/clients/{quote(result.client_name or '', safe='')}/delete", {})
            self.send(f"Клиент «{result.client_name}» удалён, ключ отозван.")
        elif result.kind == "client_actions":
            self.send(f"Клиент: {result.client_name}", CLIENT_ROWS)
        elif result.kind in {"download_config", "show_qr"}:
            name = result.client_name or ""
            profile = self.registry.get(name)
            if not profile:
                raise ValueError("profile unavailable")
            image = result.kind == "show_qr"
            self.api.send_file(self.chat_id, f"{name}-qr.png" if image else f"{name}.conf",
                               "image/png" if image else "text/plain; charset=utf-8",
                               qr_png(profile) if image else profile.encode(), image=image,
                               attachments=self.keyboard(CLIENT_ROWS))
        elif result.kind == "status":
            # MAX limits message text to 4000 characters.
            lines, chunk = self.status().splitlines(), ""
            for line in lines:
                if len(chunk) + len(line) > 3800:
                    self.api.send(self.chat_id, chunk)
                    chunk = ""
                chunk += line + "\n"
            self.send(chunk)
        elif result.kind == "panel":
            self.send("Панель через SSH-туннель: http://127.0.0.1:28080")
        elif result.kind == "cancelled":
            self.send("Действие отменено.")
        else:
            self.send("Выберите действие.")
