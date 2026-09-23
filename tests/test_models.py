import pytest

from gateway.app.models import ExitCreate, ValidationError, validate_name


def test_exit_create_rejects_non_root_login():
    """Changing the fixed administrator login must reject the exit request."""
    with pytest.raises(ValidationError, match="root"):
        ExitCreate(name="Germany", host="153.76.194.217", login="admin", password="x")


def test_validate_name_rejects_configuration_injection():
    """A newline in a display name must not become a generated config field."""
    with pytest.raises(ValueError):
        validate_name("phone\nPrivateKey = injected")


def test_validate_name_accepts_russian_client_name():
    """A Russian UI must allow an operator to name a client in Russian."""
    assert validate_name("Айфон Мамы") == "Айфон Мамы"
