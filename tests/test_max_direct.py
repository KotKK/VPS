import json
import socket
from urllib.error import URLError
from urllib.request import Request, ProxyHandler

import pytest

from gateway.app import max_direct
from gateway.app.max_api import MaxAPI


@pytest.mark.parametrize("interface", ["awg-uplink", "wg0", "tun0", "tap0", "lo", "", "ens3\n", "x" * 16])
def test_vpn_interfaces_are_rejected(interface):
    with pytest.raises(ValueError):
        max_direct.validate_interface(interface)


def test_direct_socket_is_bound_before_connect_and_does_not_fallback(monkeypatch):
    calls = []
    class Connection:
        def settimeout(self, value):
            calls.append(("timeout", value))
        def setsockopt(self, *args):
            calls.append(("bind_device", args))
        def connect(self, destination):
            calls.append(("connect", destination))
            raise OSError("Direct network unavailable")
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 443)),
    ])
    monkeypatch.setattr(socket, "socket", lambda *args: Connection())
    with pytest.raises(OSError, match="Direct network unavailable"):
        max_direct.bound_connection(("api.example", 443), 15, None, "ens3")
    assert [call[0] for call in calls] == ["timeout", "bind_device", "connect", "close"]
    assert calls[1][1] == (socket.SOL_SOCKET, 25, b"ens3\0")


def test_max_proxy_environment_is_ignored(monkeypatch):
    import ssl
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    observed = []
    class Opener:
        def open(self, request, timeout):
            return "direct"
    def build(*handlers):
        observed.extend(handlers)
        return Opener()
    monkeypatch.setattr(max_direct, "build_opener", build)
    result = max_direct.urlopen(Request("https://platform-api2.max.ru/me"),
                                timeout=15, context=ssl.create_default_context(), interface="ens3")
    assert result == "direct"
    assert isinstance(observed[0], ProxyHandler)
    assert observed[0].proxies == {}
    assert isinstance(observed[1], max_direct.DirectHTTPSHandler)
    assert observed[1].interface == "ens3"


def test_redirects_cannot_downgrade_https():
    with pytest.raises(URLError):
        max_direct.HTTPSOnlyRedirects().redirect_request(
            Request("https://max.ru"), None, 302, "redirect", {}, "http://max.ru",
        )


def test_api_uses_direct_interface_from_production_settings(monkeypatch):
    monkeypatch.setenv("MAX_DIRECT_INTERFACE", "ens3")
    assert MaxAPI("token").direct_interface == "ens3"
    monkeypatch.setenv("MAX_DIRECT_INTERFACE", "awg-uplink")
    with pytest.raises(ValueError):
        MaxAPI("token")


def test_setup_selects_main_physical_route(monkeypatch):
    def run(args, **kwargs):
        assert args == ["ip", "-j", "-4", "route", "show", "table", "main", "default"]
        return type("Result", (), {"stdout": json.dumps([{"dev": "ens3", "metric": 100}])})()
    monkeypatch.setattr(max_direct.subprocess, "run", run)
    assert max_direct.default_direct_interface() == "ens3"
