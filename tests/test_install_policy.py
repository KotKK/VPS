from pathlib import Path


def test_web_service_is_loopback_only():
    unit = Path("gateway/deploy/systemd/gateway-web.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8080" in unit


def test_installer_reads_telegram_secret_without_echoing_it():
    script = Path("gateway/deploy/install.sh").read_text(encoding="utf-8")
    assert "read -r -s TELEGRAM_BOT_TOKEN" in script
    assert 'echo "$TELEGRAM_BOT_TOKEN"' not in script
