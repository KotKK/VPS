from gateway.app.telegram_menu import TelegramMenu, format_uplink_status


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


def test_invalid_client_name_returns_validation_result_instead_of_crashing():
    menu = TelegramMenu()
    menu.handle("➕ Добавить клиента", [])
    result = menu.handle("../phone", [])
    assert result.kind == "invalid_name"
    assert menu.state == "await_name"


def test_delete_requires_selection_and_confirmation():
    menu = TelegramMenu()
    assert menu.handle("➖ Удалить клиента", ["iPhone"]).kind == "choose_delete"
    assert menu.handle("iPhone", ["iPhone"]).kind == "confirm_delete"
    result = menu.handle("Подтвердить", ["iPhone"])
    assert result.kind == "delete"
    assert result.client_name == "iPhone"


def test_status_and_panel_buttons_have_explicit_actions():
    menu = TelegramMenu()
    assert menu.handle("Статус", []).kind == "status"
    assert menu.handle("Панель", []).kind == "panel"


def test_button_text_ignores_telegram_variation_selector():
    menu = TelegramMenu()
    assert menu.handle("➕\ufe0f Добавить клиента", []).kind == "ask_name"


def test_uplink_status_reports_fresh_handshake():
    assert format_uplink_status("peer-key\t970\n", now=1000) == "доступен"
    assert format_uplink_status("peer-key\t100\n", now=1000) == "недоступен"
