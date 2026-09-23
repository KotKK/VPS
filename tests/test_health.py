from gateway.app.health import ExitProbe, ExitStatus, transition


def test_down_alert_only_fires_on_the_down_transition():
    """Repeated probe failures must not spam the Telegram chat."""
    assert transition(ExitStatus.UP, ExitProbe(False, "timeout")).kind == "down"
    assert transition(ExitStatus.DOWN, ExitProbe(False, "timeout")) is None
