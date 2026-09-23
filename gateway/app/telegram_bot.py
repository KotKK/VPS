"""Long-polling Telegram menu restricted to the configured operator chat."""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gateway.app.profiles import qr_png
from gateway.app.store import ClientRegistry
from gateway.app.telegram_menu import TelegramMenu, format_exit_statuses
from gateway.app.telegram_transport import post_form, upload_file

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"
PANEL = "http://127.0.0.1:8080"
registry = ClientRegistry(Path(os.getenv("AWG_GATEWAY_STATE_DIR", "/var/lib/awg-gateway")) / "clients.sqlite3")
menus: dict[str, TelegramMenu] = {}
DEFAULT_EXITS = [
    {"name": "Зарубежный VPS 01", "address": "153.76.194.217", "interface": "awg-uplink"}
]


def api(method: str, data: dict[str, str]) -> dict:
    read_timeout = 60.0 if method == "getUpdates" else 20.0
    return post_form(f"{API}/{method}", data, read_timeout=read_timeout)


def keyboard(rows: list[list[str]]) -> str:
    return json.dumps({"keyboard": [[{"text": value} for value in row] for row in rows], "resize_keyboard": True})


def send(text: str, rows: list[list[str]] | None = None) -> None:
    data = {"chat_id": CHAT_ID, "text": text}
    if rows is not None:
        data["reply_markup"] = keyboard(rows)
    api("sendMessage", data)


def send_file(method: str, file_field: str, filename: str, media_type: str, payload: bytes) -> None:
    upload_file(
        f"{API}/{method}",
        chat_id=CHAT_ID,
        file_field=file_field,
        filename=filename,
        media_type=media_type,
        payload=payload,
    )


def configured_exits() -> list[dict[str, str]]:
    """Load exit metadata, allowing additional interfaces through one JSON environment value."""
    raw = os.getenv("AWG_EXITS_JSON")
    if not raw:
        return DEFAULT_EXITS
    value = json.loads(raw)
    if not isinstance(value, list) or not all(
        isinstance(node, dict) and all(isinstance(node.get(key), str) for key in ("name", "address", "interface"))
        for node in value
    ):
        raise ValueError("AWG_EXITS_JSON must contain a list of exit objects")
    return value


def panel_post(path: str, values: dict[str, str]) -> None:
    request = Request(PANEL + path, data=urlencode(values).encode(), method="POST")
    with urlopen(request, timeout=45) as response:
        response.read()


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
        elif result.kind == "choose_client":
            rows = [[name] for name in result.choices] + [["Отмена"]]
            send("Выберите клиента." if result.choices else "Клиентов пока нет.", rows)
        elif result.kind == "client_actions":
            send(f"Клиент: {result.client_name}", [["Файл конфигурации", "QR-код"], ["Назад", "Отмена"]])
        elif result.kind in {"download_config", "show_qr"}:
            client_name = result.client_name or ""
            profile = registry.get(client_name)
            if profile is None:
                raise ValueError("client profile not found")
            if result.kind == "download_config":
                send_file("sendDocument", "document", f"{client_name}.conf", "text/plain", profile.encode("utf-8"))
            else:
                send_file("sendPhoto", "photo", f"{client_name}-qr.png", "image/png", qr_png(profile))
            send(f"Клиент: {client_name}", [["Файл конфигурации", "QR-код"], ["Назад", "Отмена"]])
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
        elif result.kind == "status":
            exits = configured_exits()
            outputs: dict[str, str] = {}
            for node in exits:
                command = subprocess.run(
                    ["awg", "show", node["interface"], "latest-handshakes"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                outputs[node["interface"]] = command.stdout if command.returncode == 0 else ""
            send("Зарубежные VPS:\n" + format_exit_statuses(exits, outputs, int(time.time())), main_keyboard())
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
            result = api("getUpdates", {"offset": str(offset), "timeout": "50"})
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                message = update.get("message", {})
                if str(message.get("chat", {}).get("id")) == CHAT_ID and isinstance(message.get("text"), str):
                    process(message["text"])
        except Exception as exc:
            print(f"telegram polling failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
