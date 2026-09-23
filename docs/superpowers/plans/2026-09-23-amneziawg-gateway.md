# AmneziaWG Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Deploy a Russian AmneziaWG gateway with a localhost-only management panel, foreign egress nodes, sticky per-flow balancing, and Telegram health alerts.

**Architecture:** A FastAPI panel binds only to the Russian gateway loopback interface. A privileged local service renders and transactionally applies validated AmneziaWG, nftables, and policy-routing state; the web application cannot run arbitrary commands. Foreign egress nodes are provisioned over SSH as literal \`root\`.

**Tech Stack:** Debian 13; AmneziaWG 3.x (\`awg\`, \`awg-quick\`); Python 3.13; FastAPI; Jinja2; qrcode; pytest; systemd; nftables; iproute2; OpenSSH.

**Spec:** \`docs/superpowers/specs/2026-09-23-amneziawg-gateway-design.md\`

## Global Constraints

- Never commit or log VPS passwords, private keys, a Telegram bot token, or a Telegram chat ID.
- The web listener is exactly \`127.0.0.1:8080\`; no public TCP/8080 firewall rule exists.
- Profiles and QR payloads include AmneziaWG obfuscation fields, not plain WireGuard-only syntax.
- The renderer excludes SSH, server endpoints, health probes, and already marked packets from client egress marking.
- All client and exit forms have a Cancel action that causes no persistent or network change.
- Foreign-node login is exactly \`root\`; a different login is rejected.
- The installer stops unless a usable AmneziaWG module plus \`awg\` and \`awg-quick\` are present.
- State changes validate staged configuration and restore the previous active generation on an activation failure.

## Review Focus

- Injection-shaped client names, exit names, IPs, and passwords must be rejected or safely handled by Tasks 1 and 4.
- The final healthy exit must not be removable without an explicit acknowledgement (Task 4).
- A failed reload must restore the prior active configuration (Task 3).
- Marking must never route the gateway's own SSH or endpoint traffic into an exit (Task 2).
- A remote request cannot reach the panel (Task 5).

---

## File Structure

\`\`\`text
gateway/
  app/models.py       # Validated desired-state models
  app/profiles.py     # AWG profile and QR generation
  app/renderer.py     # Pure AWG, nftables and policy-route renderers
  app/apply.py        # Privileged staging, activation and rollback
  app/health.py       # Probe transitions and Telegram notifier
  app/main.py         # FastAPI routes
  templates/          # Dashboard and cancellable forms
  static/app.css      # Apple-inspired panel styling
  deploy/install.sh   # Interactive Russian-host bootstrap
  deploy/provision-exit.sh
  deploy/systemd/
  deploy/nftables/
tests/
pyproject.toml
README.md
\`\`\`

### Task 1: Safe domain model and AmneziaWG client profiles

**Files:**
- Create: \`pyproject.toml\`
- Create: \`gateway/app/models.py\`
- Create: \`gateway/app/profiles.py\`
- Create: \`tests/test_models.py\`
- Create: \`tests/test_profiles.py\`

**Interfaces:**
- Produces: \`validate_name(value: str) -> str\`, \`ExitCreate(name: str, host: IPv4Address, login: Literal["root"], password: SecretStr)\`, \`build_client_profile(...) -> str\`, \`qr_png(profile: str) -> bytes\`.

- [ ] **Step 1: Write failing model and profile tests**

\`\`\`python
def test_non_root_exit_login_is_rejected():
    with pytest.raises(ValidationError, match="root"):
        ExitCreate(name="Germany", host="153.76.194.217", login="admin", password="x")

def test_profile_contains_amneziawg_parameters():
    profile = build_client_profile("phone", "private", "10.77.0.2/32", "public", "191.44.45.36:585", AwgParameters(5, 8, 80, 31, 97, 11, 12, 13, 14))
    assert "Jc = 5" in profile and "H4 = 14" in profile
    assert qr_png(profile).startswith(b"\\x89PNG")
\`\`\`

- [ ] **Step 2: Run tests to verify RED**

Run: \`python -m pytest tests/test_models.py tests/test_profiles.py -v\`

Expected: FAIL with missing \`gateway.app\` modules.

- [ ] **Step 3: Implement the minimal models and profile renderer**

Use a 1–63-character safe-name regular expression, \`IPv4Address\`, \`Literal["root"]\`, and \`SecretStr\`. Render a complete profile: Interface fields \`PrivateKey\`, \`Address\`, \`DNS\`, \`Jc\`, \`Jmin\`, \`Jmax\`, \`S1\`, \`S2\`, \`H1\`–\`H4\`; peer fields \`PublicKey\`, \`AllowedIPs = 0.0.0.0/0, ::/0\`, \`Endpoint\`, and \`PersistentKeepalive = 25\`. Generate its PNG with \`qrcode\`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: \`python -m pytest tests/test_models.py tests/test_profiles.py -v\`

Expected: PASS.

- [ ] **Step 5: Commit**

\`\`\`bash
git add pyproject.toml gateway/app/models.py gateway/app/profiles.py tests/test_models.py tests/test_profiles.py
git commit -m "feat: generate validated AmneziaWG client profiles"
\`\`\`

### Task 2: Egress state and policy-routing renderer

**Files:**
- Create: \`gateway/app/renderer.py\`
- Create: \`tests/test_renderer.py\`

**Interfaces:**
- Consumes: \`GatewayState\` from Task 1.
- Produces: \`render_awg_interface(state) -> str\`, \`render_egress_nft(state) -> str\`, \`render_policy_routes(state) -> list[str]\`.

- [ ] **Step 1: Write failing routing tests**

\`\`\`python
def test_two_exits_get_distinct_route_tables_and_marks(two_exit_state):
    commands = render_policy_routes(two_exit_state)
    assert "ip route replace default dev awg-exit-1 table 101" in commands
    assert "ip route replace default dev awg-exit-2 table 102" in commands

def test_marks_exclude_ssh_and_exit_endpoints(two_exit_state):
    rules = render_egress_nft(two_exit_state)
    assert "tcp dport 22 return" in rules
    assert "ip daddr { 153.76.194.217, 203.0.113.2 } return" in rules
\`\`\`

- [ ] **Step 2: Run tests to verify RED**

Run: \`python -m pytest tests/test_renderer.py -v\`

Expected: FAIL because \`gateway.app.renderer\` does not exist.

- [ ] **Step 3: Implement pure renderers**

Allocate table and mark values as \`101 + index\`. Render an nft chain that returns for loopback, SSH, health/DNS probes, each exit endpoint, and already marked packets before applying an equal-weight map to unmarked packets entering the client-facing interface. Render matching \`ip route\` and \`ip rule fwmark\` commands. Construct all values from typed state; no raw browser string may be concatenated into a command.

- [ ] **Step 4: Run tests to verify GREEN**

Run: \`python -m pytest tests/test_renderer.py -v\`

Expected: PASS.

- [ ] **Step 5: Commit**

\`\`\`bash
git add gateway/app/renderer.py tests/test_renderer.py
git commit -m "feat: render sticky egress routing"
\`\`\`

### Task 3: Transactional apply, exit provisioning, and health transitions

**Files:**
- Create: \`gateway/app/apply.py\`
- Create: \`gateway/app/health.py\`
- Create: \`gateway/deploy/provision-exit.sh\`
- Create: \`gateway/deploy/systemd/gateway-apply.service\`
- Create: \`tests/test_apply.py\`
- Create: \`tests/test_health.py\`

**Interfaces:**
- Consumes: renderers from Task 2.
- Produces: \`apply_state(state, runner) -> ApplyResult\`, \`probe_exit(exit, runner) -> ExitProbe\`, \`transition(previous, probe) -> Alert | None\`.

- [ ] **Step 1: Write failing rollback, health, and provisioning-policy tests**

\`\`\`python
def test_failed_reload_restores_previous_generation(tmp_path):
    runner = ScriptedRunner(fail_on=["systemctl reload nftables"])
    assert apply_state(state_with_one_exit(), runner).rolled_back is True
    assert "restore /var/lib/awg-gateway/previous" in runner.calls

def test_down_notification_is_not_repeated_until_recovery():
    assert transition(ExitStatus.UP, ExitProbe(False, "timeout")).kind == "down"
    assert transition(ExitStatus.DOWN, ExitProbe(False, "timeout")) is None

def test_exit_provisioner_requires_root():
    assert '[[ "$SSH_LOGIN" == "root" ]] || exit 64' in Path("gateway/deploy/provision-exit.sh").read_text()
\`\`\`

- [ ] **Step 2: Run tests to verify RED**

Run: \`python -m pytest tests/test_apply.py tests/test_health.py -v\`

Expected: FAIL because apply, health and provisioner files are absent.

- [ ] **Step 3: Implement staged state and exit health**

Stage generated artifacts under \`/var/lib/awg-gateway/generations/<generation>\`, validate nftables with \`nft -c -f\` and interface syntax with \`awg-quick strip\`, and activate with argument-array subprocess calls, never \`shell=True\`. If activation fails, restore and reactivate the prior generation once. The provisioner rejects non-root login, installs verified AmneziaWG dependencies, enables IPv4 forwarding, and applies NAT from the Russian tunnel. Probe latest AWG handshake plus a table-bound HTTPS request. Telegram sends only UP→DOWN and DOWN→UP notifications and never stores passwords.

- [ ] **Step 4: Run tests to verify GREEN**

Run: \`python -m pytest tests/test_apply.py tests/test_health.py -v\`

Expected: PASS.

- [ ] **Step 5: Commit**

\`\`\`bash
git add gateway/app/apply.py gateway/app/health.py gateway/deploy tests/test_apply.py tests/test_health.py
git commit -m "feat: apply state and monitor foreign exits"
\`\`\`

### Task 4: Management panel and cancellable operations

**Files:**
- Create: \`gateway/app/main.py\`
- Create: \`gateway/templates/base.html\`
- Create: \`gateway/templates/dashboard.html\`
- Create: \`gateway/templates/clients.html\`
- Create: \`gateway/templates/exits.html\`
- Create: \`gateway/static/app.css\`
- Create: \`tests/test_http.py\`

**Interfaces:**
- Consumes: Task 1 profiles, Task 3 apply and health functions.
- Produces: \`create_app(state_store) -> FastAPI\`; routes \`/\`, \`/clients\`, \`/clients/{id}/qr\`, \`/exits\`, and \`/settings\`.

- [ ] **Step 1: Write failing panel tests**

\`\`\`python
def test_cancelled_exit_does_not_apply_or_persist(client, state_store):
    assert client.post("/exits/new", data={"action": "cancel"}).status_code == 303
    assert state_store.read().exits == []

def test_removing_final_healthy_exit_requires_acknowledgement(client):
    response = client.post("/exits/exit-a/delete", data={})
    assert response.status_code == 409
    assert "acknowledge" in response.text
\`\`\`

- [ ] **Step 2: Run tests to verify RED**

Run: \`python -m pytest tests/test_http.py -v\`

Expected: FAIL because \`create_app\` is absent.

- [ ] **Step 3: Implement panel and UI**

Serve server-rendered dashboard, clients, exits, and settings. On both create forms, a visible Cancel posts \`action=cancel\` and returns before state write or \`apply_state\`. Return complete-profile QR PNG and downloadable \`.conf\` only for the selected client. Use CSRF tokens, same-site cookies, escaped template output, and deletion confirmations. The UI uses neutral system typography, rounded cards, blue \`#007AFF\` primary actions, red destructive controls, and reduced-motion support.

- [ ] **Step 4: Run tests to verify GREEN**

Run: \`python -m pytest tests/test_http.py -v\`

Expected: PASS.

- [ ] **Step 5: Commit**

\`\`\`bash
git add gateway/app/main.py gateway/templates gateway/static/app.css tests/test_http.py
git commit -m "feat: add local gateway control panel"
\`\`\`

### Task 5: Hardened installer, services, and live verification

**Files:**
- Create: \`gateway/deploy/install.sh\`
- Create: \`gateway/deploy/systemd/gateway-web.service\`
- Create: \`gateway/deploy/systemd/gateway-health.service\`
- Create: \`gateway/deploy/nftables/awg-gateway.nft\`
- Create: \`tests/test_install_policy.py\`
- Create: \`README.md\`

**Interfaces:**
- Consumes: all prior application and deployment artifacts.
- Produces: an idempotent \`install.sh\` that prompts for secrets without echoing them and deploys the first exit.

- [ ] **Step 1: Write failing deployment-policy tests**

\`\`\`python
def test_web_service_is_loopback_only():
    unit = Path("gateway/deploy/systemd/gateway-web.service").read_text()
    assert "--host 127.0.0.1 --port 8080" in unit

def test_installer_reads_token_without_echoing_it():
    script = Path("gateway/deploy/install.sh").read_text()
    assert "read -r -s TELEGRAM_BOT_TOKEN" in script
    assert 'echo "$TELEGRAM_BOT_TOKEN"' not in script
\`\`\`

- [ ] **Step 2: Run tests to verify RED**

Run: \`python -m pytest tests/test_install_policy.py -v\`

Expected: FAIL because deployment artifacts do not exist.

- [ ] **Step 3: Implement installer and units**

Require Debian 13; install Python, nftables, iproute2, kernel headers and a verified AmneziaWG package/toolchain. Abort unless \`modinfo amneziawg\`, \`awg\`, and \`awg-quick\` work. Prompt via \`read -r -s\` for first-exit password, Telegram token and chat ID, accepting explicit \`skip\` only for Telegram. Write secrets mode 0600. The web unit must use \`User=awg-panel\`, \`NoNewPrivileges=yes\`, \`ProtectSystem=strict\`, \`PrivateTmp=yes\`, \`IPAddressDeny=any\`, \`IPAddressAllow=localhost\`, and \`--host 127.0.0.1 --port 8080\`. Document SSH tunnel, client QR import in AmneziaWG, adding/removing exits, password/key rotation, backup, and rollback.

- [ ] **Step 4: Run all tests to verify GREEN**

Run: \`python -m pytest -v\`

Expected: PASS.

- [ ] **Step 5: Commit and deploy**

\`\`\`bash
git add gateway/deploy tests/test_install_policy.py README.md
git commit -m "feat: install hardened AmneziaWG gateway"
# On the Russian VPS, run install.sh interactively; do not pass secrets as CLI arguments.
\`\`\`

- [ ] **Step 6: Execute live acceptance checks**

Run on the Russian VPS: \`awg show\`, \`nft list ruleset\`, \`ip rule list\`, \`ss -ltnp | grep 8080\`; verify the listener is loopback only. Open \`ssh -L 8080:127.0.0.1:8080 root@191.44.45.36\`, create a test client, import its QR into AmneziaWG, and confirm the observed public egress IP is the foreign VPS. Temporarily stop the exit interface, confirm exactly one Telegram down alert and removal from new-flow routing, restore it, and confirm one recovery alert.

## Plan self-review

- Spec coverage: Tasks 1 and 4 implement profiles, QR codes, client lifecycle and cancellation. Tasks 2 and 3 implement weighted sticky routing, foreign nodes, health, failover and Telegram. Task 5 implements loopback-only deployment, secret prompts and live acceptance checks.
- Placeholder scan: no step silently substitutes plain WireGuard; package compatibility is an explicit gate.
- Interface consistency: state models → profile/renderers → transactional apply/health → panel → installer.
- Review-focus coverage: each listed risk has a named test in its owning task.

