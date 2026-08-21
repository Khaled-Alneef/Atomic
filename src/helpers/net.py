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
