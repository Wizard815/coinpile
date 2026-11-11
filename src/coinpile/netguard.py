"""Addresses a person types (a model server, a calendar, a chat webhook, the Plaid connector) are fetched by the server, so they must not reach
the server itself or the cloud metadata service. Your home network is allowed: that is where Ollama, CalDAV and the connector usually live.

Set ALLOW_LOOPBACK_URLS=1 if you really run a model or calendar inside the Coinpile container's own network namespace."""
import ipaddress
import os
import socket
import threading
from contextlib import contextmanager
from urllib.parse import urlparse

import httpcore
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


def _resolve(host: str, port: int | None) -> list[str]:
    """Every address the name points at (one DNS lookup). Tests replace this."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def check(url: str, resolve: bool = True) -> None:
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host:
        raise Blocked("that address has no host")
    if host in ("metadata.google.internal", "metadata") or ((host == "localhost" or host.endswith(".localhost")) and os.environ.get("ALLOW_LOOPBACK_URLS") != "1"):
        raise Blocked(f"{host} is not allowed here")
    if bad_ip(host):
        raise Blocked(f"{host} is not allowed here (the server itself or a link-local address)")
    if not resolve:
        return
    try:
        ips = _resolve(host, None)
    except socket.gaierror:
        return                                    # not resolvable: the request fails by itself
    for ip in ips:
        if bad_ip(ip):
            raise Blocked(f"{host} points at the server itself or a link-local address")


class _CheckedBackend(httpcore.SyncBackend):
    """Resolves the name once, checks every address it got, and connects to one of exactly those addresses.

    Without this, the check above and the connection would each ask DNS, and a name that answers "public" the first time and "127.0.0.1" the
    second (DNS rebinding) would get through. httpcore still uses the hostname from the URL for the TLS server name (SNI) and certificate check,
    and httpx sends it in the Host header, so only the address that is dialled changes."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.strip("[]")
        try:
            ips = [name] if _is_ip(name) else _resolve(name, port)
        except socket.gaierror as e:
            raise httpcore.ConnectError(f"could not resolve {name}: {e}")
        for ip in ips:
            if bad_ip(ip):
                raise Blocked(f"{name} points at the server itself or a link-local address")
        last: Exception | None = None
        for ip in dict.fromkeys(ips):
            try:
                return super().connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except httpcore.ConnectError as e:
                last = e
        raise last or httpcore.ConnectError(f"no address for {name}")


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def guarded_transport(**kw) -> httpx.HTTPTransport:
    """httpx's own transport, dialling through _CheckedBackend. Note: it never uses HTTP(S)_PROXY from the environment (a proxy would do its own
    DNS lookup, which could not be checked)."""
    t = httpx.HTTPTransport(**kw)
    pool = t._pool
    if not hasattr(pool, "_network_backend"):                 # an httpcore that changed shape: refuse rather than silently go unguarded
        raise RuntimeError("this httpcore version cannot be guarded against DNS rebinding")
    pool._network_backend = _CheckedBackend()
    return t


def _hook(request: httpx.Request) -> None:
    check(str(request.url), resolve=False)        # names and literal addresses; the DNS answer is checked when connecting (_CheckedBackend)


def client(transport=None, **kw) -> httpx.Client:
    """An httpx client for addresses a person typed. Every request (redirects too) is checked, and the connection goes to the very address that
    was checked. Tests with a stand-in transport skip both checks."""
    if transport is not None:
        return httpx.Client(transport=transport, **kw)
    return httpx.Client(transport=guarded_transport(), event_hooks={"request": [_hook]}, **kw)


# ---------------------------------------------------------------- how many slow outbound calls one person may have at once
PER_PERSON = 4          # AnyIO runs blocking endpoints on ~40 shared worker threads: one person must not be able to fill them all
WAIT = 0.5              # seconds an extra request waits for a free slot before it is turned away


class Busy(Exception):
    """Too many of this person's outbound calls are already running. web.py answers 429."""


class Slots:
    def __init__(self, n: int = PER_PERSON, wait: float = WAIT):
        self.sem, self.wait = threading.BoundedSemaphore(n), wait

    @contextmanager
    def hold(self):
        if not self.sem.acquire(timeout=self.wait):
            raise Busy("busy: you already have several requests to outside servers running. Try again in a moment.")
        try:
            yield
        finally:
            self.sem.release()
