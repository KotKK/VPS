"""HTTPS transport pinned to the Russian VPS's physical egress, without proxies."""

from __future__ import annotations

import json
import re
import socket
import subprocess
from http.client import HTTPSConnection
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, build_opener


def validate_interface(interface: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)
            or interface.lower().startswith(("awg", "wg", "tun", "tap", "tailscale", "zt"))
            or interface == "lo"):
        raise ValueError("MAX requires a direct network interface")
    return interface


def default_direct_interface() -> str:
    routes = json.loads(subprocess.run(
        ["ip", "-j", "-4", "route", "show", "table", "main", "default"],
        check=True, capture_output=True, text=True, timeout=5,
    ).stdout)
    for route in sorted(routes, key=lambda item: item.get("metric", 0)):
        if route.get("dev"):
            return validate_interface(route["dev"])
    raise ValueError("No direct default route found")


def bound_connection(address, timeout, source_address, interface: str):
    """Never retry on an unbound socket when the direct interface fails."""
    last_error = None
    for family, kind, protocol, _, destination in socket.getaddrinfo(
        address[0], address[1], 0, socket.SOCK_STREAM,
    ):
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(timeout)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                  interface.encode("ascii") + b"\0")
            if source_address:
                connection.bind(source_address)
            connection.connect(destination)
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    raise last_error or OSError("No direct destination available")


class DirectHTTPSConnection(HTTPSConnection):
    def __init__(self, host, *, interface: str, **kwargs):
        super().__init__(host, **kwargs)
        self._create_connection = lambda address, timeout, source_address: bound_connection(
            address, timeout, source_address, interface,
        )


class DirectHTTPSHandler(HTTPSHandler):
    def __init__(self, context, interface: str):
        super().__init__(context=context)
        self.interface = interface

    def https_open(self, request):
        def connection(host, **kwargs):
            return DirectHTTPSConnection(host, interface=self.interface, **kwargs)
        return self.do_open(connection, request, context=self._context)


class HTTPSOnlyRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise URLError("MAX HTTPS downgrade rejected")
        return super().redirect_request(request, response, code, message, headers, new_url)


def urlopen(request, *, timeout, context, interface: str = ""):
    if urlsplit(request.full_url).scheme != "https":
        raise URLError("MAX requires HTTPS")
    handler = DirectHTTPSHandler(context, validate_interface(interface)) if interface else HTTPSHandler(context=context)
    # Explicitly ignore HTTP(S)_PROXY, ALL_PROXY and system proxy settings.
    opener = build_opener(ProxyHandler({}), handler, HTTPSOnlyRedirects())
    return opener.open(request, timeout=timeout)
