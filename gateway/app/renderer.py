"""Pure renderers for foreign egress policy-routing state."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitRoute:
    """The non-secret routing identity of one foreign egress tunnel."""

    name: str
    interface: str
    endpoint: str


@dataclass(frozen=True)
class GatewayState:
    exits: tuple[ExitRoute, ...]


def _table(index: int) -> int:
    return 101 + index


def render_policy_routes(state: GatewayState) -> list[str]:
    """Render deterministic policy-route commands for each healthy egress."""
    commands: list[str] = []
    for index, exit_route in enumerate(state.exits):
        table = _table(index)
        commands.extend(
            [
                f"ip route replace default dev {exit_route.interface} table {table}",
                f"ip rule replace fwmark 0x{table:x} lookup {table}",
            ]
        )
    return commands


def render_egress_nft(state: GatewayState) -> str:
    """Render a chain that marks client flows without capturing control traffic."""
    endpoints = ", ".join(exit_route.endpoint for exit_route in state.exits)
    weighted_map = ", ".join(
        f"{index} : 0x{_table(index):x}" for index in range(len(state.exits))
    )
    mark_line = (
        f"    meta mark set numgen random mod {len(state.exits)} map {{ {weighted_map} }}"
        if state.exits
        else "    return"
    )
    return "\n".join(
        [
            "table inet awg_gateway {",
            "  chain mark_client_egress {",
            "    type filter hook prerouting priority mangle; policy accept;",
            "    iifname != \"awg-clients\" return",
            "    ip saddr 127.0.0.0/8 return",
            "    tcp dport 22 return",
            "    udp dport { 53, 123 } return",
            f"    ip daddr {{ {endpoints} }} return",
            "    meta mark != 0 return",
            mark_line,
            "  }",
            "}",
        ]
    )
