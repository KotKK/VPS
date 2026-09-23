"""Long-polling Telegram menu restricted to the configured operator chat."""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gateway.app.store import ClientRegistry
from gateway.app.telegram_menu import TelegramMenu

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"
PANEL = "http://127.0.0.1:8080"
registry = ClientRegistry(Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway")) / "clients.sqlite3")
menus: dict[str, TelegramMenu] = {}


def api(method: str, data: dict[str, str]) -> dict:
    request = Request(f"{API}/{method}", data=urlencode(data).encode(), method="POST")
    with urlopen(request, timeout=70) as response:
        return json.load(response)


def keyboard(rows: list[list[str]]) -> str:
    return json.dumps({"keyboard": [[{"text": value} for value in row] for row in rows], "resize_keyboard": True})


def send(text: str, rows: list[list[str]] | None = None) -> None:
    data = {"chat_id": CHAT_ID, "text": text}
    if rows is not None:
        data["reply_markup"] = keyboard(rows)
    api("sendMessage", data)


def panel_post(path: str, values: dict[str, str]) -> None:
    request = Request(PANEL + path, data=urlencode(values).encode(), method="POST")
    with urlopen(request, timeout=45) as response:
        response.read()


def process(text: str) -> None:
    menu = menus.setdefault(CHAT_ID, TelegramMenu())
    clients = registry.names()
    try:
        result = menu.handle(text, clients)
        if result.kind == "ask_name":
            send("Введите имя нового клиента.", [["Отмена"]])
        elif result.kind == "choose_delete":
            send("Выберите клиента.", [[name] for name in result.choices] + [["Отмена"]])
        elif result.kind == "confirm_delete":
            send(f"Удалить клиента {result.client_name}?", [["Подтвердить", "Отмена"]])
        elif result.kind == "create":
            panel_post("/clients/new", {"action": "create", "name": result.client_name or ""})
            send(f"Клиент {result.client_name} создан. Файл и QR доступны в панели.", main_keyboard())
        elif result.kind == "delete":
            panel_post(f"/clients/{result.client_name}/delete", {})
            send(f"Клиент {result.client_name} удалён, его ключ отозван.", main_keyboard())
        elif result.kind == "list":
            send("Клиенты:\n" + ("\n".join(result.choices) or "нет"), main_keyboard())
        elif result.kind == "cancelled":
            send("Действие отменено.", main_keyboard())
        else:
            send("Выберите действие.", main_keyboard())
    except Exception as exc:
        send(f"Операция не выполнена: {type(exc).__name__}", main_keyboard())


def main_keyboard() -> list[list[str]]:
    return [["Клиенты", "Статус"], ["➕ Добавить клиента", "➖ Удалить клиента"], ["Панель"]]


def run() -> None:
    offset = 0
    send("Меню управления AmneziaWG готово.", main_keyboard())
    while True:
        try:
            result = api("getUpdates", {"offset": str(offset), "timeout": "50"})
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) == CHAT_ID and isinstance(message.get("text"), str):
                    process(message["text"])
        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    run()
