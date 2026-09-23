from fastapi.testclient import TestClient
from pathlib import Path

from gateway.app.main import StateStore, create_app
from gateway.app.models import ExitCreate
from gateway.app.store import ClientRegistry


def profile_factory(name: str) -> str:
    return f"[Interface]\nPrivateKey = test-{name}\nJc = 5\n"


def build_client() -> tuple[TestClient, StateStore]:
    store = StateStore(profile_factory=profile_factory)
    return TestClient(create_app(store)), store


def test_create_exit_calls_provisioner_before_it_appears_in_panel():
    """An exit is listed only after its SSH provisioning succeeds."""
    calls: list[ExitCreate] = []
    store = StateStore(exit_provisioner=lambda request: calls.append(request) or {"id": "exit-de", "name": request.name, "address": str(request.host), "healthy": True})
    client = TestClient(create_app(store))
    response = client.post("/exits/new", data={"action": "create", "name": "Germany", "address": "203.0.113.2", "password": "secret"}, follow_redirects=False)
    assert response.status_code == 303
    assert calls[0].login == "root"
    assert store.exits[0]["id"] == "exit-de"


def test_cancelled_exit_form_does_not_persist_an_exit():
    """Cancel must discard a draft rather than make a network change."""
    client, store = build_client()
    response = client.post("/exits/new", data={"action": "cancel"}, follow_redirects=False)
    assert response.status_code == 303
    assert store.exits == []


def test_deleting_the_final_healthy_exit_requires_acknowledgement():
    """An accidental delete must not strand all VPN clients without egress."""
    client, store = build_client()
    store.exits.append({"id": "exit-a", "name": "Germany", "healthy": True})
    response = client.post("/exits/exit-a/delete", data={})
    assert response.status_code == 409
    assert "acknowledge" in response.text


def test_cancelled_client_form_does_not_persist_a_client():
    """Canceling client creation must not consume an address or issue a profile."""
    client, store = build_client()
    response = client.post("/clients/new", data={"action": "cancel"}, follow_redirects=False)
    assert response.status_code == 303
    assert store.clients == []


def test_profile_is_not_issued_without_a_configured_key_generator():
    """A production panel must never fall back to placeholder VPN credentials."""
    store = StateStore()
    store.clients.append({"name": "iPhone"})
    assert TestClient(create_app(store)).get("/clients/iPhone.conf").status_code == 503


def test_created_client_has_qr_download_and_can_be_deleted():
    """A created device must receive a full profile and revocation must remove it."""
    client, store = build_client()
    response = client.post("/clients/new", data={"action": "create", "name": "iPhone"}, follow_redirects=False)
    assert response.status_code == 303
    assert store.clients[0]["name"] == "iPhone"
    profile = client.get("/clients/iPhone.conf")
    assert profile.headers["content-type"].startswith("text/plain")
    assert "Jc = " in profile.text
    assert client.get("/clients/iPhone/qr").content.startswith(b"\x89PNG")
    assert client.post("/clients/iPhone/delete", follow_redirects=False).status_code == 303
    assert store.clients == []


def test_persisted_client_is_listed_and_its_profile_survives_a_panel_restart(tmp_path: Path):
    """Restarting the web service must not hide or orphan an issued profile."""
    registry = ClientRegistry(tmp_path / "clients.sqlite3")
    registry.put("iPhone", "[Interface]\nPrivateKey = saved\n", "public-key")
    restarted_store = StateStore(registry=registry)
    app = TestClient(create_app(restarted_store))

    assert "iPhone" in app.get("/clients").text
    assert app.get("/clients/iPhone.conf").text == "[Interface]\nPrivateKey = saved\n"


def test_clients_page_has_cancellable_create_form():
    """The UI exposes a real Cancel action before any client is persisted."""
    client, _ = build_client()
    response = client.get("/clients")
    assert response.status_code == 200
    assert 'name="action" value="cancel"' in response.text
    assert "Добавить клиента" in response.text


def test_clients_page_exposes_a_delete_action_for_each_created_client():
    """The existing revocation endpoint must be reachable from the panel UI."""
    client, _ = build_client()
    client.post("/clients/new", data={"action": "create", "name": "iPhone"})

    page = client.get("/clients").text
    assert 'class="action-link" href="/clients/iPhone.conf"' in page
    assert 'class="action-link" href="/clients/iPhone/qr"' in page
    assert 'action="/clients/iPhone/delete"' in page
    assert "Удалить" in page


def test_destructive_client_action_uses_the_panel_danger_style():
    """Revocation must not look like the primary create action."""
    stylesheet = Path("gateway/static/app.css").read_text(encoding="utf-8")
    assert ".danger" in stylesheet
    assert "#ff3b30" in stylesheet


def test_panel_styles_define_the_dark_appearance_tokens():
    """The dark interface needs explicit background, card and text contrast."""
    stylesheet = Path("gateway/static/app.css").read_text(encoding="utf-8")
    assert "background:#000" in stylesheet
    assert "#1c1c1e" in stylesheet
    assert "#f5f5f7" in stylesheet
