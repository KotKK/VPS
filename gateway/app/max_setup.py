"""Interactive, root-only token entry and one-time operator pairing for MAX."""

from __future__ import annotations

import getpass
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from gateway.app.max_api import MaxAPI
from gateway.app.max_bot import event_actor, register_commands
from gateway.app.max_direct import default_direct_interface

ENV_FILE = Path("/etc/awg-gateway/secrets.env")


def save_settings(path: Path, values: dict[str, str]) -> None:
    """Replace only MAX keys, atomically, preserving unrelated settings verbatim."""
    for key, value in values.items():
        if not re.fullmatch(r"MAX_[A-Z_]+", key) or any(c in value for c in '\r\n\x00'):
            raise ValueError("invalid MAX setting")
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition("=")[0].strip() not in values]
    # systemd EnvironmentFile quoting, not shell interpolation.
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".max-secrets-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pair(api: MaxAPI, code: str, *, timeout: int = 600) -> tuple[str, str]:
    marker = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        params = {"timeout": 3, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        result = api.call("GET", "/updates", params=params)
        for update in result.get("updates", []):
            user, chat, text = event_actor(update)
            if (update.get("update_type") == "message_created" and user and chat
                    and secrets.compare_digest(text.encode(), code.encode())):
                return user, chat
        marker = result.get("marker", marker)
        time.sleep(1)
    raise TimeoutError("MAX pairing timed out")


def run(pairing_code: str | None = None) -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("Запустите настройку от root.")
    url = input("HTTPS webhook URL: ").strip()
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.path != "/max/webhook" or parsed.query or parsed.fragment):
        raise SystemExit("Нужен адрес https://ваш-домен/max/webhook на порту 443.")
    token = getpass.getpass("Токен MAX (скрытый ввод): ").strip()
    ca_file = os.getenv("MAX_CA_FILE") or "/etc/awg-gateway/max-ca.pem"
    direct_interface = default_direct_interface()
    api = MaxAPI(token, ca_file, direct_interface=direct_interface)
    print(f"MAX: прямой выход через {direct_interface}, прокси отключены.", flush=True)
    identity = api.call("GET", "/me")
    print(f"Бот: {identity.get('username') or identity.get('first_name')}", flush=True)
    subscriptions = api.call("GET", "/subscriptions").get("subscriptions", [])
    if subscriptions:
        raise SystemExit("У бота уже есть webhook. Сначала проверьте существующую интеграцию; она не изменена.")
    code = pairing_code or "Привязать " + secrets.token_hex(10)
    print(f"Отправьте этому боту в ЛИЧНОМ чате: {code}", flush=True)
    user_id, chat_id = pair(api, code)
    secret = secrets.token_urlsafe(32)
    save_settings(ENV_FILE, {"MAX_BOT_TOKEN": token, "MAX_USER_ID": user_id,
                           "MAX_CHAT_ID": chat_id, "MAX_WEBHOOK_SECRET": secret,
                           "MAX_WEBHOOK_URL": url, "MAX_CA_FILE": ca_file,
                           "MAX_DIRECT_INTERFACE": direct_interface})
    subprocess.run(["systemctl", "enable", "gateway-max.service"], check=True)
    subprocess.run(["systemctl", "restart", "gateway-max.service"], check=True)
    time.sleep(2)
    api.call("POST", "/subscriptions", body={"url": url, "secret": secret,
             "update_types": ["message_created", "message_callback", "bot_started"]})
    register_commands(api)
    subprocess.run(["systemctl", "restart", "gateway-web.service"], check=True)
    api.send(chat_id, "MAX подключён. Напишите /start, чтобы открыть меню управления AmneziaWG.")
    print("MAX настроен. Токен сохранён с правами 0600; управление привязано к вашему личному чату.")


if __name__ == "__main__":
    try:
        run()
    except (Exception, KeyboardInterrupt) as exc:
        raise SystemExit(f"Настройка MAX остановлена: {type(exc).__name__}. Токен не выводится.") from None
