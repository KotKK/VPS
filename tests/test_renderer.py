from dataclasses import dataclass

from gateway.app.renderer import ExitRoute, GatewayState, render_egress_nft, render_policy_routes


@dataclass(frozen=True)
class TwoExitState:
    exits: tuple[ExitRoute, ...] = (
        ExitRoute("exit-1", "awg-exit-1", "153.76.194.217"),
        ExitRoute("exit-2", "awg-exit-2", "203.0.113.2"),
    )


def test_two_exits_receive_distinct_route_tables_and_marks():
    """Reusing a mark/table would send a flow to the wrong foreign exit."""
    commands = render_policy_routes(GatewayState(exits=TwoExitState().exits))
    assert "ip route replace default dev awg-exit-1 table 101" in commands
    assert "ip route replace default dev awg-exit-2 table 102" in commands
    assert "ip rule replace fwmark 0x65 lookup 101" in commands
    assert "ip rule replace fwmark 0x66 lookup 102" in commands


def test_egress_marks_exclude_ssh_and_foreign_endpoints():
    """A route loop must not capture SSH or an exit tunnel's UDP endpoint."""
    rules = render_egress_nft(GatewayState(exits=TwoExitState().exits))
    assert "tcp dport 22 return" in rules
    assert "ip daddr { 153.76.194.217, 203.0.113.2 } return" in rules
    assert "meta mark != 0 return" in rules
