from gateway.app.telegram_menu import TelegramMenu


def test_add_client_can_be_cancelled_without_action():
    menu = TelegramMenu()
    assert menu.handle("➕ Добавить клиента", ["iPhone"]).kind == "ask_name"
    result = menu.handle("Отмена", ["iPhone"])
    assert result.kind == "cancelled"
    assert result.client_name is None


def test_add_client_emits_create_only_after_name():
    menu = TelegramMenu()
    menu.handle("➕ Добавить клиента", [])
    result = menu.handle("iPad", [])
    assert result.kind == "create"
    assert result.client_name == "iPad"


def test_delete_requires_selection_and_confirmation():
    menu = TelegramMenu()
    assert menu.handle("➖ Удалить клиента", ["iPhone"]).kind == "choose_delete"
    assert menu.handle("iPhone", ["iPhone"]).kind == "confirm_delete"
    result = menu.handle("Подтвердить", ["iPhone"])
    assert result.kind == "delete"
    assert result.client_name == "iPhone"
