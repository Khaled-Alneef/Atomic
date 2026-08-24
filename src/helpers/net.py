"""Bounded HTTP body reads, shared by every module that fetches one.

`urlopen(timeout=...)` bounds each individual socket operation, not the
transfer as a whole - so a host that sends one byte every couple of
seconds resets that timer forever and a plain `resp.read()` never
returns. Measured against exactly that while fixing anime_sites: a local
server dribbling a chunked body held a lookup thread for over 180s with
no sign of stopping.

That matters more here than it did there. anilist/stremio/tvmaze/
mangadex/images are the page-load path, and they run on lookup_pool's
four shared workers - four stuck reads drain the pool completely and
every other entry's lookup queues behind them forever, with the page's
own refresh counters waiting on results that can never arrive.

The pattern lives here rather than being copied per module because it
was already copied once (anime_sites, then manga_sites) and the second
copy is how the first fix failed to reach the other five files.
"""

import time
import urllib.parse

# Generous for what any of these actually return - AniList's largest
# response is a few hundred KB, a cover a couple of MB - and low enough
# that a host answering with an endless body is cut off rather than
# read into memory until the app dies.
MAX_RESPONSE_BYTES = 5_000_000

# **A page image is not an API response and must not be read at that
# ceiling.** A webtoon strip is one 800px-wide image tens of thousands
# of pixels tall, and it goes past 5MB routinely. windows.reader has
# carried its own 16MB cap for this since the reader was written; the
# *downloader* did not, and read pages at MAX_RESPONSE_BYTES with an
# `except Exception: continue` around it, so an oversized page was
# dropped from the .cbz without a word.
#
# Measured 24 August 2026 on the owner's own report - The Eternal
# Supreme chapter 550, lavascans.com. Seven pages; the first is
# **5,007,791 bytes** (800x17103), 7,791 bytes over the cap, and the
# other six are 1.6-2.5MB. So the saved chapter was six pages long and
# started on page two. That is his "ch 550 was not fetched correctly,
# look at the size!!!".
#
# One number in one place now, because two copies is how the downloader
# came to be missing the fix the reader already had.
MAX_IMAGE_BYTES = 16_000_000

# Small on purpose: read1() returns whatever has arrived rather than
# waiting to fill the buffer, which is what lets the deadline below be
# checked while a slow sender is still dribbling.
READ_CHUNK = 65536


def read_bytes(resp, deadline: float, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """The response body, given a size ceiling and a wall-clock deadline.

    read1() rather than read(): read() waits until it has the full amount
    asked for, so a deadline checked around it is never reached while the
    dribble continues. read1() comes back with whatever has arrived, so
    the check below actually gets a turn.

    Raises rather than returning short - every caller here is wrapped in
    a fail-soft `except`, so a truncated body must not be mistaken for a
    complete one and parsed as the real answer."""
    chunks, total = [], 0
    while True:
        chunk = resp.read1(READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response body over the size cap")
        if time.monotonic() > deadline:
            raise TimeoutError("response body over the time budget")
    return b"".join(chunks)


def read_text(resp, deadline: float, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    """read_bytes as UTF-8. "replace" rather than strict: a mojibake
    character in a title is a cosmetic problem, a raised UnicodeDecode-
    Error is a lookup that silently returns nothing."""
    return read_bytes(resp, deadline, max_bytes).decode("utf-8", "replace")


# Characters that already mean something in a URL and must survive being
# quoted; `%` is among them so a URL that is *already* percent-encoded
# isn't encoded a second time ("%20" -> "%2520").
_URL_SAFE = "/%:@&=+$,;~!*'()?[]#"


def ascii_url(url: str) -> str:
    """`url` with any non-ASCII character percent-encoded.

    urllib will not send one otherwise: http.client encodes the request
    line as ASCII and raises UnicodeEncodeError before a connection is
    even opened - a failure that looks like "the host said no" to every
    fail-soft caller here. **Measured 21 August 2026 on two of the
    owner's blank Discover tiles**: the covers were found and were real,
    but Mangalek names its files
    "large_o-o-u-u-o-o³u-956_20260410200043-1.webp" and
    "boukoku-no-oujo-ga-negau-no-wa-1-١-110x150.jpg", so downloading them
    raised and the tile stayed empty with the art sitting right there.

    The host is left as it is: a non-ASCII domain needs IDNA rather than
    percent-encoding, and no source here has one."""
    if not url or url.isascii():
        return url
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe=_URL_SAFE),
        urllib.parse.quote(parts.query, safe=_URL_SAFE),
        urllib.parse.quote(parts.fragment, safe=_URL_SAFE)))


def deadline_in(timeout: float) -> float:
    """The wall-clock deadline `timeout` seconds from now. Named so call
    sites read as a budget rather than a bare arithmetic expression."""
    return time.monotonic() + timeout


# Below this there is no point starting another request: DNS plus a TCP
# handshake to a host that has already proven slow will not finish, and
# the attempt still costs a connection. Give up honestly instead.
MIN_STEP_SECONDS = 1.0


def step_timeout(deadline, timeout: float):
    """The timeout for the next request in a chain, or None when the
    chain's own deadline leaves too little to bother.

    `deadline` of None means an uncapped caller - the old behaviour, one
    full timeout per request. This is what makes "three engines at 6s
    each" a 6s-ish bound rather than an 18s one."""
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining < MIN_STEP_SECONDS:
        return None
    return min(timeout, remaining)


# ---------------------------------------------------------------------
# One connection pool for the whole app
#
# **Measured 21 August 2026 from the owner's own connection, before any
# of this existed:**
#
#   six GETs to one host, a new connection each   31.6 s
#   the same six over one kept-alive connection    0.72 s
#
# and, over 24 cold handshakes to the same host, a median of 178ms with
# a **p90 of 10.2s and a worst case of 20.2s** - roughly one connection
# in ten stalls outright and is then waited out in full. Both numbers
# have one cause: `urlopen` opens a fresh TCP+TLS connection for every
# request and then waits however long that takes.
#
# That is the whole of "the app is slow when it comes to the integrated
# parts". It is not the language, not Qt, and not the owner's line - the
# median handshake proves the line is fine. It is that a lookup fanning
# out over six sites paid six handshakes, and one of them would stall
# for ten seconds with the page sat waiting on it. A search measured at
# 9.37s ("kingdom", the note above SEARCH_TIMEOUT in manga_sites) against
# a 6s budget gets cut off - which is why a *longer* query could come
# back with *fewer* results than its own prefix.
#
# So, two changes, both here because every module already reads its
# bodies through this file:
#
#   * connections are kept and reused per host, so the second request to
#     a host costs one round trip instead of three plus a handshake;
#   * a handshake that has not completed in CONNECT_ATTEMPT_S is
#     abandoned and retried rather than waited out. Over the same 24
#     connections that moves p90 from 10.2s to 1.4s and the worst case
#     from 20.2s to 2.6s, and it cost **zero** extra attempts in that
#     run - a stall here is a dropped handshake, not a busy server, so
#     the retry normally connects immediately.
#
# `urlopen` below is a drop-in for `urllib.request.urlopen` at every one
# of this app's call sites: same Request in, same `with ... as resp`,
# same `urllib.error.HTTPError` out. Anything it cannot honestly serve -
# a configured proxy, a scheme that is not http(s) - is handed straight
# back to urllib rather than reimplemented badly.

import email.parser
import http.client
import io
import socket
import ssl
import threading
import urllib.error
import urllib.request
import zlib

# Long enough that an ordinary handshake (178ms median, 290ms worst
# among the healthy ones) is never cut off, short enough that a stalled
# one is abandoned while the user is still watching.
CONNECT_ATTEMPT_S = 2.0
CONNECT_ATTEMPTS = 3

# How long an idle connection is offered again before being dropped.
# Well under the 60s most of these hosts hold one for: losing that race
# costs a request that has to be retried on a fresh connection.
IDLE_TTL_S = 45.0
MAX_IDLE_PER_HOST = 6

# Redirect depth, matching urllib's own default.
MAX_REDIRECTS = 5

# An error body is drained - so the connection can be kept - only while
# it stays small. A 500 page that streams forever is not worth one.
MAX_ERROR_BODY = 65536

_ssl_context = None
_ssl_lock = threading.Lock()


def ssl_context():
    """The one TLS context, built once.

    `ssl.create_default_context()` reads the Windows certificate store
    and costs **47ms every time it is called** (measured). Sharing one
    context also lets TLS session tickets be reused between connections
    to the same host, which is the difference between a full handshake
    and a resumed one."""
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context()
        return _ssl_context


_idle = {}
_idle_lock = threading.Lock()


def _pool_key(scheme, host, port):
    return (scheme, host.lower(), port)


def _take_idle(key):
    """An idle connection for `key`, or None. Stale ones are dropped on
    the way past rather than swept by a timer - this is the only moment
    their age matters."""
    now = time.monotonic()
    with _idle_lock:
        bucket = _idle.get(key)
        while bucket:
            conn, returned_at = bucket.pop()
            if now - returned_at < IDLE_TTL_S:
                return conn
            try:
                conn.close()
            except Exception:
                pass
    return None


def _give_back(key, conn):
    with _idle_lock:
        bucket = _idle.setdefault(key, [])
        if len(bucket) >= MAX_IDLE_PER_HOST:
            try:
                bucket.pop(0)[0].close()
            except Exception:
                pass
        bucket.append((conn, time.monotonic()))


def close_idle_connections():
    """Drop every pooled connection. Nothing in the app needs this - it
    exists so a test can prove the pool is what made a measurement
    fast."""
    with _idle_lock:
        for bucket in _idle.values():
            for conn, _at in bucket:
                try:
                    conn.close()
                except Exception:
                    pass
        _idle.clear()


def _new_connection(scheme, host, port, timeout):
    """A connected http.client connection, retrying a stalled handshake
    instead of waiting it out (see CONNECT_ATTEMPT_S).

    The short window covers the connect only. Once the connection is up
    the socket gets the caller's whole timeout back, because a slow
    *body* is a different problem and read_bytes' deadline is what
    bounds that one."""
    deadline = time.monotonic() + timeout
    last = None
    for attempt in range(CONNECT_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # The final attempt gets whatever is left rather than the short
        # window: the budget is already spent, so waiting is all that is
        # left to try.
        step = (remaining if attempt == CONNECT_ATTEMPTS - 1
                else min(CONNECT_ATTEMPT_S, remaining))
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=step, context=ssl_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=step)
        try:
            conn.connect()
        except (socket.timeout, TimeoutError, ssl.SSLError, OSError) as exc:
            last = exc
            try:
                conn.close()
            except Exception:
                pass
            continue
        conn.sock.settimeout(max(1.0, deadline - time.monotonic()))
        conn.timeout = timeout
        return conn
    raise last or TimeoutError("could not connect to %s:%s" % (host, port))


class _PooledResponse:
    """What `urlopen` hands back: the http.client response, plus the
    bookkeeping that decides whether its connection is worth keeping.

    A connection returns to the pool only if the body was read all the
    way to the end and the server did not ask for it to be closed.
    Anything else and the next request on it would read the tail of this
    body as its own headers - which is worse than a slow app."""

    def __init__(self, raw, conn, key, url, decoder=None):
        self._raw = raw
        self._conn = conn
        self._key = key
        self._url = url
        self._decoder = decoder
        self._eof = False
        self._closed = False
        self.status = raw.status
        self.code = raw.status
        self.headers = raw.headers
        self.reason = raw.reason

    def read1(self, amount=-1):
        if self._eof:
            return b""
        while True:
            chunk = self._raw.read1(amount if amount and amount > 0 else 65536)
            if not chunk:
                self._eof = True
                if self._decoder is not None:
                    try:
                        return self._decoder.flush()
                    except Exception:
                        return b""
                return b""
            if self._decoder is None:
                return chunk
            # A compressed chunk can decode to nothing while the stream
            # is very much alive, and returning that empty result would
            # look like EOF to read_bytes and silently truncate the
            # body. Keep pulling until there is something to hand back.
            out = self._decoder.decompress(chunk)
            if out:
                return out

    def read(self, amount=-1):
        if amount is not None and amount >= 0:
            return self.read1(amount)
        chunks = []
        while True:
            chunk = self.read1()
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def geturl(self):
        return self._url

    def info(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def close(self):
        if self._closed:
            return
        self._closed = True
        reusable = self._eof and not self._raw.will_close
        try:
            self._raw.close()
        except Exception:
            reusable = False
        if reusable:
            _give_back(self._key, self._conn)
        else:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# Headers the transport owns; a caller's copy would either duplicate or
# contradict what http.client writes for itself.
_TRANSPORT_HEADERS = {"host", "connection", "content-length",
                      "transfer-encoding", "proxy-connection"}


def _headers_for(request):
    headers = {}
    for name, value in request.header_items():
        if name.lower() in _TRANSPORT_HEADERS:
            continue
        headers[name] = value
    return headers


def _decoder_for(headers):
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if encoding == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    return None


def _drain_error(raw):
    """Whatever the error body holds, up to MAX_ERROR_BODY, and whether
    the connection survived being read to the end."""
    chunks, total = [], 0
    try:
        while True:
            chunk = raw.read1(65536)
            if not chunk:
                return b"".join(chunks), not raw.will_close
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ERROR_BODY:
                return b"".join(chunks), False
    except Exception:
        return b"".join(chunks), False


def _release(key, conn, raw):
    """Finish with a response whose body this module read itself."""
    _payload, keep = _drain_error(raw)
    try:
        raw.close()
    except Exception:
        keep = False
    if keep:
        _give_back(key, conn)
    else:
        try:
            conn.close()
        except Exception:
            pass
    return _payload


def _proxied(scheme):
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return True     # unreadable settings: let urllib decide
    return bool(proxies.get(scheme) or proxies.get("all"))


def urlopen(request, timeout=None, **kwargs):
    """`urllib.request.urlopen`, over the pool above.

    Deliberately the same signature and the same failure shape, so this
    is a one-word change at each call site and nothing downstream has to
    know: 4xx/5xx still raise `urllib.error.HTTPError` carrying `.code`,
    which is what `anilist.RateLimited` and the updater read.

    Falls back to urllib untouched for anything it should not be
    guessing at - a configured proxy, a scheme that is not http(s), a
    request shape it does not recognise."""
    if kwargs or timeout is None:
        return urllib.request.urlopen(request, timeout=timeout, **kwargs)
    if isinstance(request, str):
        request = urllib.request.Request(request)
    if not isinstance(request, urllib.request.Request):
        return urllib.request.urlopen(request, timeout=timeout)

    url = request.full_url
    method = request.get_method()
    body = request.data
    headers = _headers_for(request)
    if not any(name.lower() == "accept-encoding" for name in headers):
        headers["Accept-Encoding"] = "gzip, deflate"

    for _hop in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or ""
        if scheme not in ("http", "https") or not host or _proxied(scheme):
            return urllib.request.urlopen(request, timeout=timeout)
        port = parts.port or (443 if scheme == "https" else 80)
        key = _pool_key(scheme, host, port)
        path = urllib.parse.urlunsplit(("", "", parts.path or "/",
                                        parts.query, ""))

        raw = conn = None
        # One retry, and only for a connection taken from the pool: the
        # server is free to have dropped it since, and that must not
        # surface as "the host said no" when a fresh connection would
        # have worked.
        for reused in (True, False):
            conn = _take_idle(key) if reused else None
            if reused and conn is None:
                continue
            if conn is None:
                conn = _new_connection(scheme, host, port, timeout)
            try:
                conn.request(method, path, body=body, headers=headers)
                raw = conn.getresponse()
                break
            except (http.client.HTTPException, socket.timeout, TimeoutError,
                    ConnectionError, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                if reused:
                    raw = conn = None
                    continue
                raise
        if raw is None:
            raise http.client.HTTPException("no response from %s" % host)

        if raw.status in (301, 302, 303, 307, 308):
            location = raw.headers.get("Location")
            status = raw.status
            _release(key, conn, raw)
            if not location:
                break
            url = urllib.parse.urljoin(url, location)
            # 303, and the 301/302 every browser treats as one, become a
            # GET with no body - urllib's own HTTPRedirectHandler does
            # the same, and a POST replayed against the redirect target
            # is how one search turns into two submissions.
            if status in (301, 302, 303) and method == "POST":
                method, body = "GET", None
                headers = {k: v for k, v in headers.items()
                           if k.lower() != "content-type"}
            continue

        if raw.status >= 400:
            status, reason, error_headers = raw.status, raw.reason, raw.headers
            payload = _release(key, conn, raw)
            raise urllib.error.HTTPError(
                url, status, reason, error_headers, io.BytesIO(payload))

        return _PooledResponse(raw, conn, key, url, _decoder_for(raw.headers))

    # Out of redirect hops - urllib raises here too, rather than handing
    # back the last hop as though it were the answer.
    raise urllib.error.HTTPError(url, 310, "too many redirects",
                                 email.parser.HeaderParser().parsestr(""),
                                 io.BytesIO(b""))
