from fastapi.testclient import TestClient
from ipaddress import IPv4Address
from pathlib import Path

from gateway.app.main import StateStore, create_app
from gateway.app.main import seed_existing_exit
from gateway.app.exit_store import ExitRepository, ExitStatus
from gateway.app.models import ExitCreate
from gateway.app.store import ClientRegistry


def profile_factory(name: str) -> str:
    return f"[Interface]\nPrivateKey = test-{name}\nJc = 5\n"


def build_client() -> tuple[TestClient, StateStore]:
    store = StateStore(profile_factory=profile_factory)
    return TestClient(create_app(store)), store


class FakeExitJobs:
    def __init__(self, repository: ExitRepository):
        self.repository = repository
        self.started = []
        self.cancelled = []
        self.deleted = []
        self.retried = []
        self.updated = []

    def start(self, request):
        self.started.append(request)
        record = self.repository.create(request.name, request.host)
        return self.repository.set_stage(
            record.id, ExitStatus.INSTALLING, "ssh"
        )

    def cancel(self, exit_id):
        self.cancelled.append(exit_id)
        self.repository.request_cancel(exit_id)

    def shutdown(self):
        pass

    def retry(self, exit_id, password):
        self.retried.append((exit_id, password))
        return self.repository.prepare_retry(exit_id)

    def delete(self, exit_id, password, acknowledge):
        self.deleted.append((exit_id, password, acknowledge))
        return self.repository.get(exit_id)

    def update(self, exit_id, name, address, password):
        self.updated.append((exit_id, name, address, password))
        record = self.repository.get(exit_id)
        if str(address) == record.address:
            return self.repository.rename(exit_id, name)
        return self.repository.prepare_replacement(exit_id, name, address)


def test_create_exit_calls_provisioner_before_it_appears_in_panel():
    """An exit is listed only after its SSH provisioning succeeds."""
    calls: list[ExitCreate] = []
    store = StateStore(exit_provisioner=lambda request: calls.append(request) or {"id": "exit-de", "name": request.name, "address": str(request.host), "healthy": True})
    client = TestClient(create_app(store))
    response = client.post("/exits/new", data={"action": "create", "name": "Germany", "address": "203.0.113.2", "password": "secret"}, follow_redirects=False)
    assert response.status_code == 303
    assert calls[0].login == "root"
    assert store.exits[0]["id"] == "exit-de"


def test_invalid_exit_form_returns_to_panel_with_friendly_error():
    client, _store = build_client()

    response = client.post(
        "/exits/new",
        data={
            "action": "create",
            "name": "bad/name",
            "address": "203.0.113.2",
            "password": "secret",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/exits?error=")
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Проверьте название, IP-адрес и пароль" in page.text


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


def test_deleting_an_exit_calls_network_remover_before_removing_it_from_panel():
    removed: list[str] = []
    store = StateStore(exit_remover=lambda node: removed.append(node["id"]))
    store.exits.append({"id": "exit-a", "name": "Germany", "healthy": False})
    response = TestClient(create_app(store)).post("/exits/exit-a/delete", follow_redirects=False)
    assert response.status_code == 303
    assert removed == ["exit-a"]
    assert store.exits == []


def test_exits_page_exposes_a_confirmed_delete_button():
    client, store = build_client()
    store.exits.append({"id": "exit-a", "name": "Germany", "address": "203.0.113.2", "healthy": False})
    page = client.get("/exits").text
    assert 'action="/exits/exit-a/delete"' in page
    assert "Удалить VPS" in page


def test_exits_page_has_collapsible_create_and_edit_forms(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("Germany", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    client = TestClient(create_app(StateStore(), exit_jobs=FakeExitJobs(repository)))

    page = client.get("/exits").text

    assert '<details class="card create-exit">' in page
    assert '<summary>Добавить выходной VPS</summary>' in page
    assert f'action="/exits/{record.id}/edit"' in page
    assert 'value="Germany"' in page
    assert 'value="203.0.113.2"' in page
    assert "Редактировать" in page
    assert 'name="action" value="cancel"' in page


def test_edit_exit_sends_validated_values_to_the_job_coordinator(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("Germany", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    jobs = FakeExitJobs(repository)
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    response = client.post(
        f"/exits/{record.id}/edit",
        data={
            "action": "save",
            "name": "Finland",
            "address": "203.0.113.9",
            "password": "new-secret",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/exits"
    exit_id, name, address, password = jobs.updated[0]
    assert exit_id == record.id
    assert name == "Finland"
    assert address == IPv4Address("203.0.113.9")
    assert password.get_secret_value() == "new-secret"


def test_edit_exit_cancel_makes_no_changes(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("Germany", IPv4Address("203.0.113.2"))
    jobs = FakeExitJobs(repository)
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    response = client.post(
        f"/exits/{record.id}/edit",
        data={"action": "cancel"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert jobs.updated == []


def test_edit_exit_requires_new_root_password_when_ip_changes(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("Germany", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    jobs = FakeExitJobs(repository)
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    response = client.post(
        f"/exits/{record.id}/edit",
        data={
            "action": "save",
            "name": "Finland",
            "address": "203.0.113.9",
            "password": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert jobs.updated == []


def test_create_exit_redirects_immediately_and_shows_installing_stage(tmp_path):
    jobs = FakeExitJobs(ExitRepository(tmp_path / "exits.sqlite3"))
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    response = client.post(
        "/exits/new",
        data={
            "action": "create",
            "name": "Германия",
            "address": "203.0.113.2",
            "password": "secret",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/exits"
    assert jobs.started[0].login == "root"
    page = client.get("/exits").text
    assert "Установка" in page
    assert "Подключение по SSH" in page
    assert 'http-equiv="refresh" content="3"' in page


def test_exit_password_never_returns_in_html(tmp_path):
    secret = "visible-only-once"
    jobs = FakeExitJobs(ExitRepository(tmp_path / "exits.sqlite3"))
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    client.post(
        "/exits/new",
        data={
            "action": "create",
            "name": "de",
            "address": "203.0.113.2",
            "password": secret,
        },
    )

    assert secret not in client.get("/exits").text


def test_existing_live_uplink_is_seeded_once_without_reallocation(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    config = tmp_path / "awg-uplink.conf"
    config.write_text("live", encoding="utf-8")

    first = seed_existing_exit(repository, config)
    second = seed_existing_exit(repository, config)

    assert first is not None
    assert first.id == second.id
    assert first.slot == 1
    assert first.interface == "awg-uplink"
    assert first.status is ExitStatus.READY


def test_legacy_live_uplink_config_is_migrated(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    current_config = tmp_path / "awg-uplink.conf"
    legacy_config = tmp_path / "russia-exit.conf"
    legacy_config.write_text("live", encoding="utf-8")

    record = seed_existing_exit(repository, current_config, legacy_config)

    assert record is not None
    assert record.slot == 1
    assert record.interface == "awg-uplink"
    assert record.status is ExitStatus.READY


def test_job_backed_final_ready_exit_requires_russian_acknowledgement(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.READY, "ready")
    client = TestClient(create_app(StateStore(), exit_jobs=FakeExitJobs(repository)))

    response = client.post(
        f"/exits/{record.id}/delete",
        data={"password": "secret"},
    )

    assert response.status_code == 409
    assert "последний доступный VPS" in response.text


def test_retry_requires_a_fresh_password(tmp_path):
    repository = ExitRepository(tmp_path / "exits.sqlite3")
    record = repository.create("de", IPv4Address("203.0.113.2"))
    repository.set_stage(record.id, ExitStatus.ERROR, "failed", "Ошибка")
    jobs = FakeExitJobs(repository)
    client = TestClient(create_app(StateStore(), exit_jobs=jobs))

    response = client.post(
        f"/exits/{record.id}/retry",
        data={"password": ""},
    )

    assert response.status_code == 422
    assert jobs.retried == []


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
