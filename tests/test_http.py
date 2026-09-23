from fastapi.testclient import TestClient

from gateway.app.main import StateStore, create_app


def profile_factory(name: str) -> str:
    return f"[Interface]\nPrivateKey = test-{name}\nJc = 5\n"


def build_client() -> tuple[TestClient, StateStore]:
    store = StateStore(profile_factory=profile_factory)
    return TestClient(create_app(store)), store


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


def test_clients_page_has_cancellable_create_form():
    """The UI exposes a real Cancel action before any client is persisted."""
    client, _ = build_client()
    response = client.get("/clients")
    assert response.status_code == 200
    assert 'name="action" value="cancel"' in response.text
    assert "Добавить клиента" in response.text
