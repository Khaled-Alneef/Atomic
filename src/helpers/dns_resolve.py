"""Hostname resolution over DNS-over-HTTPS, for sites the local
resolver will not answer for.

Measured before this was written, over the reading/anime sites the owner
named. The system resolver returns nothing for anime3rb.com,
witanime.com, witanime.quest and animerco.org, while Cloudflare's DoH
endpoint answers for all four and the servers themselves respond
normally once reached. Two others (animelux.tv, ww3.animerco.org) fail
on both, which is a different thing entirely - those domains are gone,
and no resolver can help. These sites rotate domains constantly, so
telling the two apart matters: one is worth routing around, the other
needs a new address.

DoH is the same mechanism browsers use by default; nothing here is
exotic. What it does mean is that a name your network declines to
resolve gets resolved anyway, so it is applied only to the sites the
user has configured for themselves, never as a blanket override.

The connection still goes to the real host over normal TLS - only the
*name lookup* changes. SNI and the Host header both keep the original
hostname, so certificate validation is unaffected and a wrong answer
from a DoH server cannot silently redirect anything.
"""

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.request

from . import net

# Two providers, tried in order. Both speak the same JSON API.
PROVIDERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

_UA = "Atomic/1.0"
TIMEOUT = 8
CACHE_TTL = 30 * 60

_cache = {}
_lock = threading.Lock()


def _cached(host):
    with _lock:
        row = _cache.get(host)
    if not row:
        return None
    address, at = row
    return address if time.time() - at < CACHE_TTL else None


def _store(host, address):
    with _lock:
        _cache[host] = (address, time.time())


def resolve(host: str, timeout: float = TIMEOUT):
    """The A record for `host`, via DoH. None when nothing answers.

    The system resolver is tried first: it is faster, it is what the
    machine is configured to use, and going around it when it works
    would be both slower and ruder."""
    host = (host or "").strip().lower()
    if not host:
        return None
    cached = _cached(host)
    if cached:
        return cached
    try:
        address = socket.gethostbyname(host)
        _store(host, address)
        return address
    except OSError:
        pass
    for provider in PROVIDERS:
        try:
            url = f"{provider}?name={urllib.parse.quote(host)}&type=A"
            request = urllib.request.Request(
                url, headers={"Accept": "application/dns-json", "User-Agent": _UA})
            with net.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except Exception:
            continue
        for answer in (body or {}).get("Answer") or []:
            # type 1 is A; CNAMEs in the chain are skipped rather than
            # followed, since the chain already ends in an A record here.
            if answer.get("type") == 1 and answer.get("data"):
                address = str(answer["data"])
                _store(host, address)
                return address
    return None


def blocked_locally(host: str) -> bool:
    """True when the local resolver refuses a name that DoH answers.

    This is the honest distinction: a domain that fails everywhere is
    dead and should be reported as such, not presented as censorship."""
    try:
        socket.gethostbyname(host)
        return False
    except OSError:
        return resolve(host) is not None


import urllib.parse  # noqa: E402  (used by resolve, kept below the docstring)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an already-resolved address while presenting the
    original hostname for SNI and certificate checks."""

    def __init__(self, host, address, **kwargs):
        super().__init__(host, **kwargs)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._address, self.port or 443), self.timeout)
        if self._tunnel_host:
            self._tunnel()
        context = self._context or ssl.create_default_context()
        # server_hostname stays the real name: the certificate must still
        # match it, so a DoH answer cannot point this at an impostor.
        self.sock = context.wrap_socket(self.sock, server_hostname=self.host)


class DoHHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that resolves through `resolve` before connecting."""

    def https_open(self, request):
        host = request.host.split(":")[0]
        address = resolve(host)
        if not address:
            return super().https_open(request)

        def build(host_and_port, **kwargs):
            kwargs.pop("context", None)
            return _PinnedHTTPSConnection(
                host, address, context=self._context, **kwargs)

        return self.do_open(build, request)


def opener():
    """A urllib opener that resolves names over DoH when the local
    resolver will not."""
    return urllib.request.build_opener(DoHHTTPSHandler())
