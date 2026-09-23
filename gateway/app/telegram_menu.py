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
            name = validate_name(text)
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
        return MenuResult("menu")
