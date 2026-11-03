"""Pure renderers for foreign egress policy-routing state."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitRoute:
    """The non-secret routing identity of one foreign egress tunnel."""

    name: str
    interface: str
    endpoint: str
    route_table: int
    mark: int


@dataclass(frozen=True)
class ClientRoute:
    name: str
    address: str
    exit_id: str


@dataclass(frozen=True)
class GatewayState:
    exits: tuple[ExitRoute, ...]
    clients: tuple[ClientRoute, ...] = ()
    client_routing: bool = False


def render_policy_routes(state: GatewayState) -> list[list[str]]:
    """Render deterministic policy-route commands for each healthy egress."""
    commands: list[list[str]] = []
    for exit_route in state.exits:
        commands.extend(
            [
                [
                    "ip",
                    "route",
                    "replace",
                    "default",
                    "dev",
                    exit_route.interface,
                    "table",
                    str(exit_route.route_table),
                ],
                [
                    "ip",
                    "rule",
                    "add",
                    "fwmark",
                    hex(exit_route.mark),
                    "lookup",
                    str(exit_route.route_table),
                ],
            ]
        )
    return commands


def render_egress_nft(state: GatewayState) -> str:
    """Render a chain that marks client flows without capturing control traffic."""
    if state.client_routing:
        return render_client_egress_nft(state)
    endpoints = ", ".join(exit_route.endpoint for exit_route in state.exits)
    weighted_map = ", ".join(
        f"{index} : {hex(exit_route.mark)}"
        for index, exit_route in enumerate(state.exits)
    )
    mark_line = (
        "    ct state new ct mark 0 meta mark set "
        f"numgen random mod {len(state.exits)} map {{ {weighted_map} }}"
        if state.exits
        else "    return"
    )
    lines = [
        "table inet awg_gateway",
        "flush table inet awg_gateway",
        "table inet awg_gateway {",
        "  chain mark_client_egress {",
        "    type filter hook prerouting priority mangle; policy accept;",
        "    iifname != \"awg-clients\" return",
        "    fib daddr type local return",
        "    ip saddr 127.0.0.0/8 return",
    ]
    if endpoints:
        lines.append(f"    ip daddr {{ {endpoints} }} return")
    lines.extend(
        [
            "    ct mark != 0 meta mark set ct mark",
            "    meta mark != 0 return",
            mark_line,
        ]
    )
    if state.exits:
        lines.append("    ct mark set meta mark")
    lines.extend(["  }", "}"])
    return "\n".join(lines)


def render_client_egress_nft(state: GatewayState) -> str:
    """Replace legacy filtering and route every packet using its client's assignment.

    Replacing stale connection marks permits failover without flushing unrelated
    conntrack entries. Unknown clients and an empty healthy pool fail closed.
    """
    marks = {route.name: route.mark for route in state.exits}
    entries = ", ".join(
        f"{client.address} : {hex(marks[client.exit_id])}"
        for client in state.clients if client.exit_id in marks
    )
    lines = [
        "table ip awg_gateway",
        "delete table ip awg_gateway",
        "table inet awg_gateway",
        "flush table inet awg_gateway",
        "table inet awg_gateway {",
        "  chain mark_client_egress {",
        "    type filter hook prerouting priority mangle; policy accept;",
        '    iifname != "awg-clients" return',
        "    fib daddr type local return",
        "    meta mark set 0",
    ]
    if entries:
        lines.append(f"    meta mark set ip saddr map {{ {entries} }}")
    lines.extend([
        "    meta mark 0 drop",
        "    ct mark set meta mark",
        "  }",
        "  chain forward {",
        "    type filter hook forward priority filter; policy drop;",
    ])
    for route in state.exits:
        lines.extend([
            f'    iifname "awg-clients" oifname "{route.interface}" ip saddr 10.20.0.0/24 meta mark {hex(route.mark)} accept',
            f'    iifname "{route.interface}" oifname "awg-clients" ip daddr 10.20.0.0/24 ct state established,related accept',
        ])
    lines.extend(["  }", "}"])
    return "\n".join(lines)
