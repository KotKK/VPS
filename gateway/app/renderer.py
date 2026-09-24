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
class GatewayState:
    exits: tuple[ExitRoute, ...]


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
                    "replace",
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
        "table inet awg_gateway {",
        "  chain mark_client_egress {",
        "    type filter hook prerouting priority mangle; policy accept;",
        "    iifname != \"awg-clients\" return",
        "    ip saddr 127.0.0.0/8 return",
        "    tcp dport 22 return",
        "    udp dport { 53, 123 } return",
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
