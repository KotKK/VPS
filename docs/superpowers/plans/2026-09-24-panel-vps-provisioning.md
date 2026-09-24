# Panel VPS Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в закрытую веб-панель надёжную фоновую установку, повтор, остановку и удаление зарубежных Debian 13 VPS с автоматическим подключением к AmneziaWG-балансировке.

**Architecture:** SQLite хранит только несекретное состояние выходов и стабильные сетевые идентификаторы. Однопоточный координатор держит SSH-пароль только в памяти, управляет Paramiko-транспортом, идемпотентным удалённым установщиком, локальными AWG-интерфейсами и атомарным применением правил балансировки. FastAPI создаёт задачи и отображает их состояние, но не удерживает HTTP-запрос во время установки.

**Tech Stack:** Python 3.12+, FastAPI, Jinja2, Pydantic 2, Paramiko 4, SQLite, pytest, Debian 13, AmneziaWG, systemd, nftables, iproute2.

**Spec:** `docs/superpowers/specs/2026-09-24-panel-vps-provisioning-design.md`

## Global Constraints

- Панель слушает только `127.0.0.1:8080` и доступна через SSH-туннель.
- Поддерживаемая удалённая ОС — чистая Debian 13; SSH-логин всегда `root`.
- SSH-пароль существует только в памяти задачи и отсутствует в SQLite, HTML, логах, исключениях и аргументах процессов.
- Первый host key принимается один раз; изменение сохранённого ключа блокирует подключение.
- Только выходы со статусом `ready` входят в балансировку.
- Первый существующий выход сохраняет интерфейс `awg-uplink`, таблицу `101`, mark `0x65` и сеть `10.200.0.0/30`; обновление не должно прерывать работающий VPN.
- Один исполнитель выполняет не более одной установки или сетевого удаления одновременно.
- Отмена проверяется между этапами и всегда удаляет незавершённое локальное состояние.
- Удаление последнего доступного выхода требует отдельного подтверждения.
- Любое применение nftables сначала проходит `nft -c`; ошибка активации восстанавливает последнюю рабочую конфигурацию.

## Review Focus

- Одновременные запросы добавления должны получить разные `slot`, интерфейс, /30-сеть, route table и mark; проверяет Task 1.
- Пароль с кавычками, переводами строк и shell-метасимволами не должен попасть в хранилище, команду или журнал; проверяют Tasks 3 и 6.
- Изменившийся SSH host key должен завершить установку безопасной ошибкой до выполнения удалённой команды; проверяет Task 3.
- Перезапуск панели во время установки должен оставить рабочую балансировку неизменной и перевести незавершённую запись в `error`; проверяет Task 5.
- Недоступный удаляемый VPS должен быть исключён из локальной балансировки, а неполная удалённая очистка должна отображаться предупреждением; проверяет Task 7.

---

## File Structure

```text
gateway/app/exit_store.py       # SQLite-модель, переходы состояний и стабильные slot
gateway/app/exit_network.py     # Расчёт адресов и локальные AWG-артефакты
gateway/app/ssh_transport.py    # Paramiko, TOFU known_hosts, безопасный запуск команд
gateway/app/exit_provisioner.py # Идемпотентные этапы remote/local provisioning и проверки
gateway/app/exit_jobs.py        # Однопоточная очередь, секреты в памяти, отмена/повтор
gateway/app/renderer.py         # Стабильные policy routes и sticky nft marks
gateway/app/apply.py            # Проверка и атомарное применение поколения балансировки
gateway/app/main.py             # HTTP-команды и production wiring
gateway/templates/exits.html    # Форма, статусы, прогресс и действия
gateway/static/app.css          # Визуальные статусы и кнопки
gateway/deploy/remote-exit.sh   # Идемпотентная настройка чистого Debian 13 exit
gateway/deploy/systemd/gateway-web.service # Права для SSH и локальной сети
tests/                          # Модульные и интеграционные проверки каждого интерфейса
```

### Task 1: Persistent exit records and collision-free allocation

**Files:**
- Create: `gateway/app/exit_store.py`
- Create: `tests/test_exit_store.py`

**Interfaces:**
- Produces: `ExitStatus(StrEnum)`, `ExitRecord`, `ExitRepository(database: Path)`.
- Produces: `ExitRepository.create(name: str, address: IPv4Address) -> ExitRecord`, `get(exit_id: str) -> ExitRecord | None`, `list() -> list[ExitRecord]`, `set_stage(exit_id: str, status: ExitStatus, stage: str, error: str = "") -> ExitRecord`, `request_cancel(exit_id: str) -> None`, `cancel_requested(exit_id: str) -> bool`, `delete(exit_id: str) -> None`, `recover_interrupted() -> int`.
- Later tasks consume immutable `slot`, `interface`, `route_table`, `mark`, `tunnel_cidr`, `remote_port` fields from `ExitRecord`.

- [ ] **Step 1: Write failing repository tests**

```python
def test_concurrent_creates_allocate_distinct_network_identity(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(
            lambda pair: repo.create(pair[0], IPv4Address(pair[1])),
            [("de", "203.0.113.2"), ("nl", "203.0.113.3")],
        ))
    assert {record.slot for record in records} == {1, 2}
    assert {record.interface for record in records} == {"awg-uplink", "awg-uplink-2"}
    assert {record.route_table for record in records} == {101, 102}
    assert {record.tunnel_cidr for record in records} == {"10.200.0.0/30", "10.200.0.4/30"}

def test_schema_never_contains_password_column(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    repo.create("de", IPv4Address("203.0.113.2"))
    columns = sqlite3.connect(repo.database).execute("PRAGMA table_info(exits)").fetchall()
    assert "password" not in {column[1] for column in columns}

def test_restart_marks_installing_record_as_error(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    record = repo.create("de", IPv4Address("203.0.113.2"))
    repo.set_stage(record.id, ExitStatus.INSTALLING, "remote_packages")
    assert repo.recover_interrupted() == 1
    assert repo.get(record.id).status is ExitStatus.ERROR
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_store.py --basetemp .pytest-exit-store-red -p no:cacheprovider -q`

Expected: FAIL because `gateway.app.exit_store` does not exist.

- [ ] **Step 3: Implement the repository and allocation transaction**

```python
class ExitStatus(StrEnum):
    INSTALLING = "installing"
    READY = "ready"
    ERROR = "error"
    DELETING = "deleting"

@dataclass(frozen=True)
class ExitRecord:
    id: str
    name: str
    address: str
    status: ExitStatus
    stage: str
    error: str
    warning: str
    slot: int
    interface: str
    route_table: int
    mark: int
    tunnel_cidr: str
    remote_port: int
    cancel_requested: bool
```

Within `BEGIN IMMEDIATE`, reject duplicate `name` and `address`, choose the smallest unused positive slot, and derive slot 1 as `awg-uplink`, table/mark 101, `10.200.0.0/30`; later slots use `awg-uplink-{slot}`, table/mark `100 + slot`, successive `/30` networks from `10.200.0.0/16`, and port `49000 + slot`. Map SQLite integrity errors to `DuplicateExitError` without echoing submitted secrets.

- [ ] **Step 4: Run repository tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_store.py --basetemp .pytest-exit-store-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/exit_store.py tests/test_exit_store.py
git commit -m "Add persistent exit state"
```

### Task 2: Stable sticky balancing and atomic local apply

**Files:**
- Modify: `gateway/app/renderer.py`
- Modify: `gateway/app/apply.py`
- Modify: `tests/test_renderer.py`
- Modify: `tests/test_apply.py`

**Interfaces:**
- Consumes: `ExitRecord.route_table`, `mark`, `interface`, and `address` from Task 1.
- Produces: `ExitRoute(name: str, interface: str, endpoint: str, route_table: int, mark: int)`.
- Produces: `render_policy_routes(state: GatewayState) -> list[list[str]]`, `render_egress_nft(state: GatewayState) -> str`.
- Produces: `EgressApplier.apply(previous: GatewayState, desired: GatewayState) -> ApplyResult`.

- [ ] **Step 1: Extend failing renderer and rollback tests**

```python
def test_existing_flow_restores_connection_mark(two_exit_state):
    rules = render_egress_nft(two_exit_state)
    assert "ct mark != 0 meta mark set ct mark" in rules
    assert "ct state new ct mark 0 meta mark set numgen random mod 2" in rules
    assert "ct mark set meta mark" in rules

def test_explicit_tables_do_not_change_when_an_exit_is_removed():
    remaining = GatewayState((ExitRoute("nl", "awg-uplink-2", "203.0.113.3", 102, 102),))
    assert ["ip", "route", "replace", "default", "dev", "awg-uplink-2", "table", "102"] in render_policy_routes(remaining)

def test_invalid_new_nft_keeps_previous_generation():
    runner = RecordingRunner(fail=("nft", "-c", "-f", "/run/awg-gateway/next.nft"))
    result = EgressApplier(runner).apply(one_exit_state(), two_exit_state())
    assert result.active == one_exit_state()
    assert runner.mutations == []
```

- [ ] **Step 2: Run targeted tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_renderer.py tests/test_apply.py --basetemp .pytest-egress-red -p no:cacheprovider -q`

Expected: FAIL because routes are strings with index-derived tables and the nft output does not persist `ct mark`.

- [ ] **Step 3: Implement stable rendering and apply**

Render command argument lists, not shell strings. The nft chain must restore an existing `ct mark`, exclude loopback/SSH/DNS/NTP/all exit endpoints, choose an equal-weight mark only for new unmarked client flows, copy `meta mark` to `ct mark`, and restore the mark for later packets. `EgressApplier` writes `/run/awg-gateway/next.nft`, calls `nft -c -f`, installs desired routes/rules, activates nft, and only then removes stale routes/rules. On activation failure it reapplies the previous state once.

```python
for route in desired.exits:
    runner.run(["ip", "route", "replace", "default", "dev", route.interface,
                "table", str(route.route_table)])
    runner.run(["ip", "rule", "replace", "fwmark", hex(route.mark),
                "lookup", str(route.route_table)])
```

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_renderer.py tests/test_apply.py --basetemp .pytest-egress-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/renderer.py gateway/app/apply.py tests/test_renderer.py tests/test_apply.py
git commit -m "Make exit balancing stable and sticky"
```

### Task 3: Password-safe SSH transport with TOFU host keys

**Files:**
- Create: `gateway/app/ssh_transport.py`
- Create: `tests/test_ssh_transport.py`

**Interfaces:**
- Produces: `SSHCredentials(host: IPv4Address, username: Literal["root"], password: SecretStr)`.
- Produces: `SSHResult(stdout: str, stderr: str, exit_code: int)`.
- Produces: `SSHTransport(known_hosts: Path, connect_timeout: int = 15, command_timeout: int = 300)` with context-managed `connect(credentials)`, `run(args: Sequence[str], stdin: bytes | None = None) -> SSHResult`, and `put_bytes(remote_path: str, content: bytes, mode: int) -> None`.
- Task 4 consumes only this interface and never Paramiko objects directly.

- [ ] **Step 1: Write failing transport tests with fake Paramiko clients**

```python
def test_password_is_not_in_command_or_logs(tmp_path, caplog):
    password = "q'$(touch /tmp/no)\nsecret"
    fake = FakeSSHClient()
    transport = SSHTransport(tmp_path / "known_hosts", client_factory=lambda: fake)
    with transport.connect(SSHCredentials(IPv4Address("203.0.113.2"), "root", SecretStr(password))) as session:
        session.run(["printf", "%s", "safe value"])
    assert password not in repr(fake.calls)
    assert password not in caplog.text
    assert fake.executed == ["printf %s 'safe value'"]

def test_changed_host_key_blocks_before_command(tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("203.0.113.2 ssh-ed25519 OLD\n", encoding="utf-8")
    fake = FakeSSHClient(server_key="NEW")
    with pytest.raises(HostKeyChangedError):
        SSHTransport(known_hosts, client_factory=lambda: fake).connect(credentials())
    assert fake.executed == []
```

- [ ] **Step 2: Run transport tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ssh_transport.py --basetemp .pytest-ssh-red -p no:cacheprovider -q`

Expected: FAIL because `gateway.app.ssh_transport` does not exist.

- [ ] **Step 3: Implement transport**

Use Paramiko `RejectPolicy` for known hosts and a custom first-use policy that atomically writes only the received public host key to a root-owned `0600` file. Pass the password only to `SSHClient.connect(password=...)`. Convert argument arrays with `shlex.join`; never accept a raw remote command from HTTP. Redact host-independent Paramiko authentication text to Russian domain exceptions: `SSHAuthenticationError`, `HostKeyChangedError`, `SSHTimeoutError`, `SSHCommandError`.

```python
command = shlex.join([str(value) for value in args])
stdin_stream, stdout, stderr = self._client.exec_command(command, timeout=self.command_timeout)
if stdin is not None:
    stdin_stream.channel.sendall(stdin)
    stdin_stream.channel.shutdown_write()
```

- [ ] **Step 4: Run transport tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ssh_transport.py --basetemp .pytest-ssh-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/ssh_transport.py tests/test_ssh_transport.py
git commit -m "Add safe SSH transport for exits"
```

### Task 4: Idempotent Debian 13 provisioning and local AWG uplinks

**Files:**
- Create: `gateway/app/exit_network.py`
- Create: `gateway/app/exit_provisioner.py`
- Create: `gateway/deploy/remote-exit.sh`
- Create: `tests/test_exit_network.py`
- Create: `tests/test_exit_provisioner.py`
- Modify: `tests/test_install_policy.py`

**Interfaces:**
- Consumes: `ExitRecord` from Task 1 and `SSHTransport` from Task 3.
- Produces: `TunnelAllocation.from_record(record)`, including local address `network[2]`, remote address `network[1]`, interface, port, table and mark.
- Produces: `LocalUplinkManager.stage(record, local_private_key: str, remote_public_key: str) -> None`, `start(record) -> None`, `remove(record) -> None`.
- Produces: `ExitProvisioner.provision(record, credentials, progress: Callable[[str], None], cancelled: Callable[[], bool]) -> None`, `cleanup_remote(record, credentials) -> str | None`.

- [ ] **Step 1: Write failing allocation, stage, cancellation and OS tests**

```python
def test_slot_two_addresses_match_the_second_30():
    allocation = TunnelAllocation.for_slot(2)
    assert allocation.network == IPv4Network("10.200.0.4/30")
    assert allocation.remote_address == IPv4Address("10.200.0.5")
    assert allocation.local_address == IPv4Address("10.200.0.6")

def test_provisioner_rejects_non_debian_13_before_install(fake_session, record):
    fake_session.results[("cat", "/etc/os-release")] = SSHResult('ID=ubuntu\nVERSION_ID="24.04"\n', "", 0)
    with pytest.raises(UnsupportedRemoteOSError):
        provisioner(fake_session).provision(record, credentials(), stages.append, lambda: False)
    assert not any("apt-get" in call for call in fake_session.commands)

def test_cancel_between_remote_and_local_stages_removes_local_artifacts(fake_session, record):
    cancelled = iter([False, False, True])
    with pytest.raises(ProvisionCancelled):
        provisioner(fake_session).provision(record, credentials(), stages.append, lambda: next(cancelled))
    assert local_manager.removed == [record.id]
```

- [ ] **Step 2: Run provisioner tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_network.py tests/test_exit_provisioner.py tests/test_install_policy.py --basetemp .pytest-provision-red -p no:cacheprovider -q`

Expected: FAIL because the new modules and `remote-exit.sh` do not exist.

- [ ] **Step 3: Implement remote installer and local manager**

`remote-exit.sh` must check `ID=debian` and `VERSION_ID=13`, install `ca-certificates curl gnupg linux-headers-$(uname -r) nftables iproute2`, import key `0x57290828` from `https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x57290828` into `/usr/share/keyrings/amnezia-ppa.gpg`, configure the signed `https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu noble main` source, and install `amneziawg-dkms amneziawg-tools`. It then verifies `awg`, `awg-quick`, and `modinfo amneziawg`, writes `/etc/amnezia/awg-exit.conf` mode `0600`, creates `awg-exit.service`, and applies NAT for both `10.20.0.0/24` and the allocated Russian uplink address. Inputs arrive as a root-owned environment file uploaded through SFTP, never as command arguments; a trap removes it.

`LocalUplinkManager` writes `/etc/amnezia/<interface>.conf` and `/etc/systemd/system/<interface>.service` atomically. Slot 1 adopts the existing `awg-uplink` artifacts instead of replacing or restarting them. Later slots use their allocated names. `ExitProvisioner` emits only these stages: `ssh`, `os_check`, `packages`, `remote_tunnel`, `local_tunnel`, `handshake`, `internet_check`; it checks cancellation between every stage, waits at most 45 seconds for `awg show <interface> latest-handshakes`, and verifies internet with `curl --interface <interface> -4 --fail --max-time 10 https://api.ipify.org`.

- [ ] **Step 4: Run provisioner tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_network.py tests/test_exit_provisioner.py tests/test_install_policy.py --basetemp .pytest-provision-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/exit_network.py gateway/app/exit_provisioner.py gateway/deploy/remote-exit.sh tests/test_exit_network.py tests/test_exit_provisioner.py tests/test_install_policy.py
git commit -m "Provision Debian exit nodes over SSH"
```

### Task 5: Serialized background jobs, cancellation, retry and restart recovery

**Files:**
- Create: `gateway/app/exit_jobs.py`
- Create: `tests/test_exit_jobs.py`

**Interfaces:**
- Consumes: `ExitRepository`, `ExitProvisioner`, `EgressApplier` and `SSHCredentials`.
- Produces: `ExitJobCoordinator(repository, provisioner, applier, max_workers=1)`.
- Produces: `start(request: ExitCreate) -> ExitRecord`, `cancel(exit_id: str) -> None`, `retry(exit_id: str, password: SecretStr) -> ExitRecord`, `delete(exit_id: str, password: SecretStr | None, acknowledge: bool) -> ExitRecord`, `shutdown() -> None`.
- Secrets live in a locked `_credentials: dict[str, SSHCredentials]` and are removed in a `finally` block.

- [ ] **Step 1: Write failing job lifecycle tests**

```python
def test_password_is_destroyed_after_success(coordinator, request):
    record = coordinator.start(request)
    coordinator.wait(record.id)
    assert record.id not in coordinator._credentials
    assert coordinator.repository.get(record.id).status is ExitStatus.READY

def test_jobs_are_serialized(coordinator, request_factory):
    first = coordinator.start(request_factory("de", "203.0.113.2"))
    second = coordinator.start(request_factory("nl", "203.0.113.3"))
    coordinator.wait_all()
    assert coordinator.provisioner.max_concurrent_calls == 1
    assert [call.id for call in coordinator.applier.desired_history] == [first.id, second.id]

def test_restart_does_not_add_interrupted_exit_to_balancing(tmp_path):
    repo = ExitRepository(tmp_path / "state.sqlite3")
    record = repo.create("de", IPv4Address("203.0.113.2"))
    repo.set_stage(record.id, ExitStatus.INSTALLING, "packages")
    coordinator = build_coordinator(repo)
    assert repo.get(record.id).status is ExitStatus.ERROR
    assert coordinator.applier.current.exits == ()
```

- [ ] **Step 2: Run job tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_jobs.py --basetemp .pytest-jobs-red -p no:cacheprovider -q`

Expected: FAIL because `gateway.app.exit_jobs` does not exist.

- [ ] **Step 3: Implement coordinator state machine**

Use `ThreadPoolExecutor(max_workers=1)`. `start` validates and persists the record before storing credentials under a lock and submitting `_install`. `_install` updates each stage through the progress callback; after provisioning it applies a desired state built from existing `ready` exits plus the new record and only then transitions it to `ready`. On failure it cleans incomplete local artifacts and writes a redacted Russian error. On cancellation it cleans locally and stores `error` with `stage="cancelled"`. `retry` accepts a fresh password and reuses the same allocation. Constructor calls `recover_interrupted()` and applies only persisted `ready` exits.

```python
try:
    self.provisioner.provision(record, credentials, progress, cancelled)
    self.applier.apply(self._ready_state(), self._ready_state(extra=record))
    self.repository.set_stage(record.id, ExitStatus.READY, "ready")
except Exception as exc:
    self.repository.set_stage(record.id, ExitStatus.ERROR, "failed", self.errors.public_message(exc))
finally:
    with self._lock:
        self._credentials.pop(record.id, None)
```

- [ ] **Step 4: Run job tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_jobs.py --basetemp .pytest-jobs-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/exit_jobs.py tests/test_exit_jobs.py
git commit -m "Run exit provisioning as serialized jobs"
```

### Task 6: Wire the panel to real background provisioning

**Files:**
- Modify: `gateway/app/main.py`
- Modify: `gateway/templates/exits.html`
- Modify: `gateway/static/app.css`
- Modify: `tests/test_http.py`

**Interfaces:**
- Consumes: `ExitJobCoordinator` from Task 5.
- Produces routes: `POST /exits/new`, `POST /exits/{id}/cancel`, `POST /exits/{id}/retry`.
- `create_app(store, exit_jobs: ExitJobCoordinator | None = None) -> FastAPI` remains injectable for tests.

- [ ] **Step 1: Replace stub expectations with failing background-flow tests**

```python
def test_create_redirects_immediately_and_shows_installing(fake_jobs):
    client = TestClient(create_app(build_store(), exit_jobs=fake_jobs))
    response = client.post("/exits/new", data={
        "action": "create", "name": "Германия", "address": "203.0.113.2", "password": "secret"
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/exits"
    assert fake_jobs.started[0].login == "root"
    page = client.get("/exits").text
    assert "Установка" in page and "Подключение по SSH" in page

def test_password_never_returns_in_html(fake_jobs):
    secret = "visible-only-once"
    client = TestClient(create_app(build_store(), exit_jobs=fake_jobs))
    client.post("/exits/new", data={"action": "create", "name": "de", "address": "203.0.113.2", "password": secret})
    assert secret not in client.get("/exits").text
```

- [ ] **Step 2: Run HTTP tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_http.py --basetemp .pytest-http-provision-red -p no:cacheprovider -q`

Expected: FAIL because production wiring still uses an in-memory hard-coded exit and no coordinator.

- [ ] **Step 3: Implement HTTP wiring and status UI**

`production_store()` creates the SQLite repository at `/var/lib/awg-gateway/exits.sqlite3`, seeds the existing `153.76.194.217` exit as slot 1 only when the database is empty and `awg-uplink` exists, then constructs transport, provisioner, applier and coordinator. The web service startup owns the coordinator and shutdown calls `shutdown()`.

The template retains fields `name`, `address`, `password`, shows login `root` readonly, and never repopulates password. Each record displays Russian status/stage, safe error, IP, and contextual actions. While any record installs, add `<meta http-equiv="refresh" content="3">`; do not introduce a public API or client-side secret storage.

- [ ] **Step 4: Run HTTP tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_http.py --basetemp .pytest-http-provision-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/main.py gateway/templates/exits.html gateway/static/app.css tests/test_http.py
git commit -m "Connect panel to VPS provisioning jobs"
```

### Task 7: Safe removal, remote cleanup and retry UX

**Files:**
- Modify: `gateway/app/exit_jobs.py`
- Modify: `gateway/app/main.py`
- Modify: `gateway/templates/exits.html`
- Modify: `tests/test_exit_jobs.py`
- Modify: `tests/test_http.py`

**Interfaces:**
- Consumes existing `delete`, `retry`, `cleanup_remote` interfaces.
- Produces route: `POST /exits/{id}/delete` with fields `acknowledge: bool` and optional `password` used only for remote cleanup.

- [ ] **Step 1: Write failing deletion and retry tests**

```python
def test_unreachable_remote_is_still_removed_from_local_balancing(coordinator, ready_record):
    coordinator.provisioner.cleanup_error = SSHTimeoutError()
    coordinator.delete(ready_record.id, SecretStr("fresh-password"), acknowledge=True)
    coordinator.wait(ready_record.id)
    assert ready_record.id not in {route.name for route in coordinator.applier.current.exits}
    assert coordinator.repository.get(ready_record.id).warning == "Удалённая очистка не завершена"

def test_final_ready_exit_needs_explicit_acknowledgement(client, ready_record):
    response = client.post(f"/exits/{ready_record.id}/delete", data={"acknowledge": "false"})
    assert response.status_code == 409
    assert "последний доступный VPS" in response.text

def test_retry_requires_fresh_password(client, error_record):
    response = client.post(f"/exits/{error_record.id}/retry", data={"password": ""})
    assert response.status_code == 422
```

- [ ] **Step 2: Run deletion tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_jobs.py tests/test_http.py --basetemp .pytest-delete-red -p no:cacheprovider -q`

Expected: FAIL because removal currently mutates an in-memory list synchronously and cannot preserve warnings.

- [ ] **Step 3: Implement ordered deletion and retry actions**

For a ready record, transition to `deleting`, atomically apply the desired state without it, remove the local interface/unit, then attempt remote cleanup if a fresh password was provided. Delete the database row after full cleanup; if remote cleanup fails, retain a non-routing `error` record with `stage="remote_cleanup"` and the fixed warning. A second delete without password removes that retained record locally. Refuse final-ready deletion before any mutation unless `acknowledge=True`. Retry clears the safe error, preserves slot/allocation, and submits the same `_install` path with a fresh in-memory credential.

- [ ] **Step 4: Run deletion tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_exit_jobs.py tests/test_http.py --basetemp .pytest-delete-green -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/app/exit_jobs.py gateway/app/main.py gateway/templates/exits.html tests/test_exit_jobs.py tests/test_http.py
git commit -m "Add safe VPS retry and removal"
```

### Task 8: Service permissions, migration and live acceptance

**Files:**
- Modify: `gateway/deploy/systemd/gateway-web.service`
- Modify: `gateway/deploy/install.sh`
- Modify: `tests/test_install_policy.py`
- Create: `docs/panel-vps-operations.md`

**Interfaces:**
- Consumes all previous tasks.
- Produces an idempotent update path for the running Russian gateway and an operator acceptance checklist.

- [ ] **Step 1: Write failing service-policy tests**

```python
def test_web_service_can_reach_configured_foreign_ssh_hosts_but_stays_loopback_only():
    unit = Path("gateway/deploy/systemd/gateway-web.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8080" in unit
    assert "IPAddressDeny=any" not in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "/etc/systemd/system" in unit

def test_operations_doc_never_embeds_a_real_password():
    text = Path("docs/panel-vps-operations.md").read_text(encoding="utf-8")
    assert "<ПАРОЛЬ_НОВОГО_VPS>" in text
    assert "C7bV" not in text and "8c68" not in text
```

- [ ] **Step 2: Run deployment tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_install_policy.py --basetemp .pytest-deploy-red -p no:cacheprovider -q`

Expected: FAIL because the current service denies every non-loopback address and the operations document is absent.

- [ ] **Step 3: Update deployment artifacts and migration documentation**

Keep Uvicorn bound to loopback. Remove `IPAddressDeny=any`/`IPAddressAllow=localhost`, add `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, and extend `ReadWritePaths` to `/var/lib/awg-gateway /etc/amnezia /etc/systemd/system /run/awg-gateway`. The installer creates `/var/lib/awg-gateway/known_hosts` mode `0600`, `/run/awg-gateway`, and installs `remote-exit.sh` mode `0700`. Document backup of SQLite and `/etc/amnezia`, update commands, rollback to the preceding Git commit, adding a clean Debian 13 VPS, interpreting each status, retry, stop, delete, and verifying `awg show`, `ip rule`, `nft list table inet awg_gateway`.

- [ ] **Step 4: Run the complete local verification**

Run: `.venv\Scripts\python.exe -m pytest --basetemp .pytest-vps-provisioning-final -p no:cacheprovider -q`

Expected: all tests PASS with zero failures.

- [ ] **Step 5: Commit deployment integration**

```bash
git add gateway/deploy/systemd/gateway-web.service gateway/deploy/install.sh tests/test_install_policy.py docs/panel-vps-operations.md
git commit -m "Deploy panel VPS provisioning safely"
```

- [ ] **Step 6: Install on the Russian gateway without changing the live exit**

Run through the existing SSH session:

```bash
cd /opt/awg-gateway
git pull --ff-only
.venv/bin/pip install -e .
install -m 0644 gateway/deploy/systemd/gateway-web.service /etc/systemd/system/
install -m 0700 gateway/deploy/remote-exit.sh /opt/awg-gateway/gateway/deploy/remote-exit.sh
install -d -m 0700 /var/lib/awg-gateway /run/awg-gateway
touch /var/lib/awg-gateway/known_hosts
chmod 0600 /var/lib/awg-gateway/known_hosts
systemctl daemon-reload
systemctl restart gateway-web.service
systemctl --no-pager --full status gateway-web.service
```

Expected: `gateway-web.service` is `active (running)`; `awg-uplink` remains up and existing clients retain internet access.

- [ ] **Step 7: Execute live acceptance with a fresh test VPS**

From the SSH-tunnel-only panel, add a clean Debian 13 VPS and observe every stage. On the Russian gateway run:

```bash
awg show
ip rule list
nft list table inet awg_gateway
sqlite3 /var/lib/awg-gateway/exits.sqlite3 'select name,address,status,stage,slot from exits order by slot;'
```

Expected: the new interface has a recent handshake; a distinct table/mark exists; both exits are `ready`; the database contains no password column. Open multiple new client connections and confirm both public exit IPs appear. Restart `gateway-web.service` and confirm both records remain. Delete the test VPS, confirm it disappears from new-flow balancing, and confirm the original `awg-uplink` and client traffic remain active.

## Plan Self-Review

- Spec coverage: persistent states and restart recovery are Tasks 1 and 5; SSH security is Task 3; Debian provisioning and health validation are Task 4; balancing is Task 2; UI/cancellation/retry are Tasks 5–7; safe deletion is Task 7; deployment and live checks are Task 8.
- Placeholder scan: the plan contains no deferred implementation markers; each behavior has a named interface, test command, expected failure and expected success.
- Type consistency: `ExitRecord` is produced once in Task 1; Tasks 2–7 consume its stable fields. `SSHTransport` is isolated behind Task 3. `ExitJobCoordinator` is the only mutating interface consumed by FastAPI.
- Existing first exit: Task 6 explicitly adopts `awg-uplink` as slot 1 and Task 8 verifies it is not restarted or renumbered.
- Review Focus coverage: concurrent allocation (Task 1), hostile password (Task 3/6), changed host key (Task 3), interrupted installation (Task 5), and unreachable cleanup (Task 7) each have an explicit failing test.
