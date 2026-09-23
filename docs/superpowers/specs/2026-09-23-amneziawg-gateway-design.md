# AmneziaWG gateway: technical design

## Goal

Provide one stable AmneziaWG endpoint on the Russian VPS (`191.44.45.36`).
Client devices connect only to it.  Their Internet traffic exits through one
of the managed foreign VPS nodes, initially `153.76.194.217`.  Adding or
removing a foreign exit must not require reconfiguring client devices.

## Boundaries and security model

The gateway is a Debian 13 host.  Management is deliberately not exposed to
the Internet: the web service binds to `127.0.0.1:8080` only.  The operator
opens it over an SSH local-forward:

```text
ssh -L 8080:127.0.0.1:8080 root@191.44.45.36
```

The panel is therefore reached at `http://127.0.0.1:8080`.  SSH keys should
replace password authentication after bootstrap.  Administrative passwords,
AmneziaWG private keys, the Telegram token, and Telegram chat ID live only in
root-owned configuration on the gateway or are entered interactively by the
installer; no secret is committed to this repository.

The setup opens only SSH and the configured UDP port for the client-facing
AmneziaWG listener on the Russian VPS.  Foreign nodes accept AmneziaWG only
from the Russian gateway where provider firewall rules permit it.

## Network topology

```text
AmneziaWG clients
       |
       | encrypted client tunnel
       v
Russian gateway (client listener, control panel, health checks)
       |                         |
       | encrypted exit tunnel   | encrypted exit tunnel (later)
       v                         v
Foreign exit A               Foreign exit B
153.76.194.217               additional node
       |                         |
       +------ NAT to Internet --+
```

Each foreign node has a dedicated AmneziaWG peer relationship with the Russian
gateway and performs source NAT for client traffic received on that tunnel.
The Russian host maintains policy-routing tables for every healthy exit.
Connection marks make the selected egress sticky for the lifetime of a flow;
new flows are distributed with configurable equal weights.  With one healthy
exit all traffic uses it.  A health-check failure removes an exit from new-flow
selection and emits a Telegram alert; recovery emits a second alert.  Existing
flows on a failed node can break and reconnect through a healthy node, which is
the correct transparent failover behavior without a third-party load balancer.

Management, server-to-server tunnel traffic, SSH, and the Russian gateway's
own outbound traffic are excluded from client egress marking to prevent
routing loops.

## AmneziaWG profiles and QR codes

Client profiles use AmneziaWG configuration syntax and include the generated
obfuscation parameters (`Jc`, `Jmin`, `Jmax`, `S1`, `S2`, `H1`–`H4`) together
with the normal interface, peer key, endpoint, allowed IPs, and persistent
keepalive.  The panel generates a QR representation of this complete profile
and a downloadable `.conf` file.  Both are intended for import through the
AmneziaWG app's configuration/QR importer, not a generic WireGuard-only
importer.

Each client gets a distinct key pair and a stable private tunnel address.
Deletion removes its gateway peer, generated profile record, and allocated
address; it does not retain a usable configuration.

## Control panel

The panel has four views with an Apple-inspired restrained visual language:
soft neutral background, high-contrast text, system-like typography, rounded
cards, a single blue action color, clear destructive red actions, and compact
status pills.

* Dashboard: total clients, healthy/failed exit count, current exits and
  recent health events.
* Clients: list, create client, explicit Cancel action while editing, QR modal,
  `.conf` download, and destructive delete confirmation.
* Exit VPS: list and health/state; Add opens a cancellable form with name, IP,
  fixed login `root`, and password.  Delete requires confirmation and refuses
  to remove the final healthy exit unless the operator explicitly confirms the
  loss of Internet egress.
* Settings: traffic weights, health-check interval and Telegram configuration
  status (the secret itself is never returned to the browser).

All forms validate before changes are applied.  Cancel discards drafts and
does not alter server state.  Server changes are transactional: configuration
is rendered and validated before the active network state is replaced, with a
rollback path on failure.

## Installer and operations

The installer is idempotent and runs locally on the Russian VPS as root.  It
installs the supported AmneziaWG kernel/userspace packages, the panel service,
systemd units, nftables policy-routing rules, and initial exit configuration.
It prompts for the Telegram bot token and chat ID, with an explicit skip
option; skipped notification settings can later be added in the panel.

It provisions the first foreign node over SSH using the supplied root account,
then verifies both tunnel handshakes, a routed Internet probe, local-only panel
binding, and QR/config generation.  Service logs are in journald; health state
and non-secret configuration are stored under a root-owned application data
directory.

## Acceptance checks

1. An AmneziaWG mobile client imports a generated QR profile and reaches the
   Internet through the first foreign exit.
2. `127.0.0.1:8080` is reachable through the SSH tunnel while the VPS public
   address has no panel listener.
3. The panel can create, cancel, display, download, and delete a client.
4. The panel can add, cancel, list, and delete a foreign exit, using `root` as
   the only accepted SSH login.
5. Health-check failure suppresses the exit from new flows and sends one
   Telegram down message; recovery sends one up message.
6. Introducing a second healthy exit distributes new connections while keeping
   individual flows pinned to their selected exit.
