from gateway.app.telegram_menu import TelegramMenu, format_exit_statuses, format_uplink_status


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


def test_client_menu_offers_configuration_and_qr_actions():
    menu = TelegramMenu()
    result = menu.handle("Клиенты", ["iphone"])
    assert result.kind == "choose_client"
    assert result.choices == ("iphone",)

    result = menu.handle("iphone", ["iphone"])
    assert result.kind == "client_actions"
    assert result.client_name == "iphone"

    result = menu.handle("Файл конфигурации", ["iphone"])
    assert result.kind == "download_config"
    assert result.client_name == "iphone"

    result = menu.handle("QR-код", ["iphone"])
    assert result.kind == "show_qr"
    assert result.client_name == "iphone"

    result = menu.handle("Назад", ["iphone"])
    assert result.kind == "choose_client"


def test_client_actions_can_be_cancelled():
    menu = TelegramMenu()
    menu.handle("Клиенты", ["iphone"])
    menu.handle("iphone", ["iphone"])
    assert menu.handle("Отмена", ["iphone"]).kind == "cancelled"
    assert menu.state == "idle"


def test_status_lists_each_configured_vps_with_name_ip_and_health():
    exits = [
        {"name": "Germany", "address": "203.0.113.10", "interface": "awg-de"},
        {"name": "Finland", "address": "203.0.113.11", "interface": "awg-fi"},
    ]
    outputs = {"awg-de": "peer-a\t970\n", "awg-fi": "peer-b\t100\n"}
    assert format_exit_statuses(exits, outputs, now=1000) == (
        "• Germany (203.0.113.10): доступен\n"
        "• Finland (203.0.113.11): недоступен"
    )
