"""Addresses a person types (a model server, a calendar, a chat webhook, the Plaid connector) are fetched by the server, so they must not reach
the server itself or the cloud metadata service. Your home network is allowed: that is where Ollama, CalDAV and the connector usually live.

Set ALLOW_LOOPBACK_URLS=1 if you really run a model or calendar inside the Coinpile container's own network namespace."""
import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx


class Blocked(httpx.HTTPError):
    pass


def bad_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    loop = a.is_loopback or a.is_unspecified
    return a.is_link_local or a.is_multicast or a.is_reserved or (loop and os.environ.get("ALLOW_LOOPBACK_URLS") != "1")


def check(url: str) -> None:
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host:
        raise Blocked("that address has no host")
    if host in ("metadata.google.internal", "metadata") or ((host == "localhost" or host.endswith(".localhost")) and os.environ.get("ALLOW_LOOPBACK_URLS") != "1"):
        raise Blocked(f"{host} is not allowed here")
    if bad_ip(host):
        raise Blocked(f"{host} is not allowed here (the server itself or a link-local address)")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return                                    # not resolvable: the request fails by itself
    for info in infos:
        if bad_ip(info[4][0]):
            raise Blocked(f"{host} points at the server itself or a link-local address")


def _hook(request: httpx.Request) -> None:
    check(str(request.url))


def client(transport=None, **kw) -> httpx.Client:
    """An httpx client for addresses a person typed. Every request (redirects too) is checked; tests with a stand-in transport skip the DNS check."""
    hooks = {} if transport is not None else {"request": [_hook]}
    return httpx.Client(transport=transport, event_hooks=hooks, **kw)
