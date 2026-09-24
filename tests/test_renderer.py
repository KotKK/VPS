from dataclasses import dataclass

from gateway.app.renderer import ExitRoute, GatewayState, render_egress_nft, render_policy_routes


@dataclass(frozen=True)
class TwoExitState:
    exits: tuple[ExitRoute, ...] = (
        ExitRoute("exit-1", "awg-uplink", "153.76.194.217", 101, 101),
        ExitRoute("exit-2", "awg-uplink-2", "203.0.113.2", 102, 102),
    )


def test_two_exits_receive_distinct_route_tables_and_marks():
    """Reusing a mark/table would send a flow to the wrong foreign exit."""
    commands = render_policy_routes(GatewayState(exits=TwoExitState().exits))
    assert ["ip", "route", "replace", "default", "dev", "awg-uplink", "table", "101"] in commands
    assert ["ip", "route", "replace", "default", "dev", "awg-uplink-2", "table", "102"] in commands
    assert ["ip", "rule", "replace", "fwmark", "0x65", "lookup", "101"] in commands
    assert ["ip", "rule", "replace", "fwmark", "0x66", "lookup", "102"] in commands


def test_egress_marks_exclude_ssh_and_foreign_endpoints():
    """A route loop must not capture SSH or an exit tunnel's UDP endpoint."""
    rules = render_egress_nft(GatewayState(exits=TwoExitState().exits))
    assert "tcp dport 22 return" in rules
    assert "ip daddr { 153.76.194.217, 203.0.113.2 } return" in rules
    assert "meta mark != 0 return" in rules


def test_existing_flow_restores_connection_mark():
    rules = render_egress_nft(GatewayState(exits=TwoExitState().exits))
    assert "ct mark != 0 meta mark set ct mark" in rules
    assert "ct state new ct mark 0 meta mark set numgen random mod 2" in rules
    assert "ct mark set meta mark" in rules


def test_explicit_table_does_not_change_when_first_exit_is_removed():
    remaining = GatewayState(
        exits=(ExitRoute("exit-2", "awg-uplink-2", "203.0.113.2", 102, 102),)
    )
    commands = render_policy_routes(remaining)
    assert ["ip", "route", "replace", "default", "dev", "awg-uplink-2", "table", "102"] in commands
    assert all("101" not in command for command in commands)


def test_empty_state_has_no_invalid_empty_endpoint_set():
    rules = render_egress_nft(GatewayState(exits=()))
    assert "ip daddr {  }" not in rules
    assert "    return" in rules
