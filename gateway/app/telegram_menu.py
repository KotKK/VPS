"""Small state machine for the Telegram client-management menu."""

from dataclasses import dataclass

from gateway.app.models import validate_name


@dataclass(frozen=True)
class MenuResult:
    kind: str
    client_name: str | None = None
    choices: tuple[str, ...] = ()


class TelegramMenu:
    def __init__(self) -> None:
        self.state = "idle"
        self.selected: str | None = None

    def handle(self, text: str, clients: list[str]) -> MenuResult:
        text = text.replace("\ufe0f", "")
        if text == "Отмена":
            self.state = "idle"
            self.selected = None
            return MenuResult("cancelled")
        if text == "➕ Добавить клиента":
            self.state = "await_name"
            return MenuResult("ask_name")
        if text == "➖ Удалить клиента":
            self.state = "choose_delete"
            return MenuResult("choose_delete", choices=tuple(clients))
        if self.state == "await_name":
            try:
                name = validate_name(text)
            except ValueError:
                return MenuResult("invalid_name")
            self.state = "idle"
            return MenuResult("create", client_name=name)
        if self.state == "choose_delete" and text in clients:
            self.selected = text
            self.state = "confirm_delete"
            return MenuResult("confirm_delete", client_name=text)
        if self.state == "confirm_delete" and text == "Подтвердить" and self.selected:
            name = self.selected
            self.state = "idle"
            self.selected = None
            return MenuResult("delete", client_name=name)
        if text == "Клиенты":
            return MenuResult("list", choices=tuple(clients))
        if text == "Статус":
            return MenuResult("status")
        if text == "Панель":
            return MenuResult("panel")
        return MenuResult("menu")


def format_uplink_status(output: str, now: int, max_age: int = 180) -> str:
    try:
        handshake = max(int(line.split()[1]) for line in output.splitlines() if line.split())
    except (ValueError, IndexError):
        return "недоступен"
    return "доступен" if handshake > now - max_age else "недоступен"
