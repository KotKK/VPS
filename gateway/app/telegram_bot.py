"""Long-polling Telegram menu restricted to the configured operator chat."""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gateway.app.store import ClientRegistry
from gateway.app.telegram_menu import TelegramMenu, format_uplink_status

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"
PANEL = "http://127.0.0.1:8080"
POLL_TIMEOUT_SECONDS = 3
API_HTTP_TIMEOUT_SECONDS = 8
registry = ClientRegistry(Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway")) / "clients.sqlite3")
menus: dict[str, TelegramMenu] = {}


def api(method: str, data: dict[str, str]) -> dict:
    request = Request(f"{API}/{method}", data=urlencode(data).encode(), method="POST")
    with urlopen(request, timeout=API_HTTP_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("description", "Telegram API error")))
    return payload


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


def get_updates(offset: int) -> list[dict]:
    """Use short polling because long-lived responses are buffered by the VPN path."""
    result = api(
        "getUpdates",
        {"offset": str(offset), "timeout": str(POLL_TIMEOUT_SECONDS)},
    )
    return list(result.get("result", []))


def process(text: str) -> None:
    print(f"telegram command: {text!r}", flush=True)
    menu = menus.setdefault(CHAT_ID, TelegramMenu())
    clients = registry.names()
    try:
        result = menu.handle(text, clients)
        if result.kind == "ask_name":
            send("Введите имя нового клиента.", [["Отмена"]])
        elif result.kind == "invalid_name":
            send("Имя: от 1 до 63 символов. Можно использовать русские и латинские буквы, цифры, пробел, '.', '_' и '-'.", [["Отмена"]])
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
        elif result.kind == "status":
            output = subprocess.run(["awg", "show", "awg-uplink", "latest-handshakes"], text=True, capture_output=True, check=True).stdout
            send("Зарубежный VPS: " + format_uplink_status(output, int(time.time())), main_keyboard())
        elif result.kind == "panel":
            send("Панель доступна через SSH-туннель: http://127.0.0.1:8080", main_keyboard())
        elif result.kind == "cancelled":
            send("Действие отменено.", main_keyboard())
        else:
            send("Выберите действие.", main_keyboard())
    except Exception as exc:
        print(f"telegram operation failed: {type(exc).__name__}: {exc}", flush=True)
        send(f"Операция не выполнена: {type(exc).__name__}", main_keyboard())


def main_keyboard() -> list[list[str]]:
    return [["Клиенты", "Статус"], ["➕ Добавить клиента", "➖ Удалить клиента"], ["Панель"]]


def run() -> None:
    offset = 0
    send("Меню управления AmneziaWG готово.", main_keyboard())
    while True:
        try:
            for update in get_updates(offset):
                offset = int(update["update_id"]) + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) == CHAT_ID and isinstance(message.get("text"), str):
                    process(message["text"])
        except Exception as exc:
            print(f"telegram polling failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
