"""Give mpv a stream it can actually seek for the engine's local URLs.

**The vendored libmpv cannot seek HTTP at all, and that is the owner's
"it does not play the video until it reaches 100%".** Measured 30 August
2026 with mpv's own verbose log, against a byte-perfect local server
(200 + Accept-Ranges, textbook 206s):

    v ffmpeg: stream level seek from 32768 to 216954178
    v ffmpeg: Seek failed (to 216954178, size 217220531)
    error ffmpeg/demuxer: mov,mp4,m4a,3gp,3g2,mj2: moov atom not found

The seek never reached the network - the server saw exactly one request.
This is a property of the DLL itself (vendor/libmpv-source.json: it is
Stremio's stremio-shell-ng build, whose http layer is a callback stream
with no seek - their shell only ever streams sequentially), reproduced
with bare python-mpv in a clean process, no app code loaded, and with
every app mpv option removed one at a time. Consequences measured before
this patch, 217MB moov-at-end MP4, one seeder at 7.5MB/s:

    * a fresh play issued ONE request and read the entire file looking
      for the index: no tracks, no frame, nothing until ~100% - then it
      played (33s for 217MB; a 1.5GB episode is minutes);
    * every "Skipping to X" over an engine URL was a read-through to the
      target, which is the family of resume complaints behind
      regression_fixes_144-152.

So engine playback stops going through that broken layer: this module
registers an `atomic://` stream protocol on every player handle -
python-mpv's client-API stream, with a real `seek` - and the player
rewrites the engine's `http://127.0.0.1:<port>/<hash>/<n>` URL to
`atomic://...` at load time. Reads become ranged GETs against the same
local handler as before (each seek opens a fresh request, which is
exactly one reader window in torrent_engine._serve, same as a browser
would); a read blocks while the swarm fetches, which is the handler's
designed behaviour and what mpv's cache expects of a network stream.

Direct addon/debrid HTTPS URLs are left on the built-in path on purpose:
they carry per-host header requirements this shim does not speak, and
being remote they were never the reported case. The durable fix for
those is an upstream libmpv build - a packaging decision, not a runtime
patch.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

_INSTALLED = False
_PATCHED = set()

# The engine's URL shape and nothing else - see torrent_engine.stream_url.
_ENGINE_URL = re.compile(
    r"^http://127\.0\.0\.1:\d+/[0-9a-f]{40}/\d+$")

SCHEME = "atomic"

# Opening the stream HEADs the URL for its size; the handler answers from
# metadata it already holds, so this is bounded and quick.
OPEN_TIMEOUT_S = 10.0

# **A read waits out silence, not the transfer.** It used to pass
# `timeout=None`, on the reasoning that the handler deliberately blocks
# while pieces arrive. True - and it also meant a handler that stopped
# sending for any other reason blocked mpv's stream thread for ever,
# which blocks mpv's play loop, which is what answers every property the
# UI thread reads (torrent_engine._Torrent.wait_for's docstring spells
# out that chain). Two unbounded waits, stacked, one at each end of the
# same socket: the owner's "the video player freeze after playing for
# sometime", 2 September 2026.
#
# A socket timeout bounds one recv rather than the transfer, which is
# exactly the right shape here - every byte that arrives restarts it, so
# a slow swarm is never cut off and a silent one is. Longer than the
# engine's own STREAM_STALL_S so the server ends the response tidily
# first; this is the backstop for a server that cannot.
READ_SILENCE_S = 75.0

# **How long a read may go on reopening a response that keeps ending
# with nothing in it before the stream is genuinely ended** - see
# _EngineStream.read. Two whole server patiences (PIECE_WAIT_S is 45s a
# piece), so a swarm that is merely slow at a seek target is waited for
# through the server ending the response and the reader opening it
# again, while a swarm that has died is not waited for for ever. Longer
# than the player's own SEAT_GIVE_UP_S (45s), so the player says "Playing
# From Here" before this ever has to speak.
REOPEN_SILENCE_S = 100.0
# Between two reopens. A server that ends a response the instant it is
# opened (nothing to send, reader not yet re-aimed) would otherwise be
# asked in a tight loop; a quarter second is invisible against a piece.
REOPEN_PAUSE_S = 0.25


class _EngineStream:
    """One mpv-facing stream over the local torrent server."""

    def __init__(self, url):
        self._url = url
        self._lock = threading.Lock()
        self._resp = None
        self._pos = 0
        self._cancelled = False
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=OPEN_TIMEOUT_S) as resp:
            self.size = int(resp.headers.get("Content-Length") or 0)
        if self.size <= 0:
            raise ValueError("engine stream reports no size")

    def _open_from(self, pos):
        request = urllib.request.Request(
            self._url, headers={"Range": f"bytes={int(pos)}-"})
        return urllib.request.urlopen(request, timeout=READ_SILENCE_S)

    def read(self, length):
        """Bytes at the current position - and b"" only for a cancelled
        stream or the end of the file, never for a gap in delivery.

        **b"" is EOF to mpv, not "wait", and the server ends responses
        on purpose.** torrent_engine._serve returns early whenever the
        piece a read is blocked on has not arrived inside PIECE_WAIT_S,
        or nothing was sent for STREAM_STALL_S - by design, so the reader
        reopens from where it stopped and the engine re-aims at that
        offset. urllib does not raise for that: a body announced as N
        bytes and closed after fewer answers `read()` with b"" and no
        exception (measured 5 September 2026 against a local server that
        declared 100 bytes and sent 10: b'0123456789', then b'', then
        b''). The previous version reopened only on an *exception*, so a
        tidily ended response was handed to mpv as end-of-file. With
        `keep-open=yes` (video_backend) mpv then pauses on its last
        frame - which is the owner's "seeking on the swarm path ... not
        pressing any buttons to play each time": every seek that waited
        past the server's patience ended with the picture stopped until
        he pressed play, and pressing play at an EOF mpv believes in
        restarts from the head.

        So an empty read before `size` is a dropped connection, whatever
        caused it: the response is closed and a new Range is opened from
        the same offset, as many times as it takes, until the stream is
        cancelled, the file genuinely ends, the server says the torrent
        is gone (404 - a released torrent, which is a real stop), or the
        swarm has sent nothing at all for REOPEN_SILENCE_S. mpv sits in
        `paused-for-cache` for the wait, which the player narrates as
        Buffering, and resumes on its own when the bytes land - no press
        needed. A short sleep between reopens keeps a server answering
        instantly with nothing from being asked in a tight loop."""
        with self._lock:
            if self._cancelled or self._pos >= self.size:
                return b""
            resp = self._resp
        silent_since = time.monotonic()
        reopens = 0
        while True:
            data = b""
            gone = False
            try:
                if resp is None:
                    resp = self._open_from(self._pos)
                    with self._lock:
                        if self._cancelled:
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return b""
                        self._resp = resp
                data = resp.read(max(1, int(length)))
            except urllib.error.HTTPError as error:
                # The torrent is no longer served at this address - a
                # switch or a close released it. That is a stop, not a
                # stall, and retrying would only delay the next source.
                gone = getattr(error, "code", 0) == 404
            except Exception:
                pass
            if data:
                with self._lock:
                    self._pos += len(data)
                return data
            # Nothing came: the response is finished for us, whether it
            # ended tidily or died. Reopen from where we are.
            with self._lock:
                self._resp = None
                cancelled = self._cancelled or self._pos >= self.size
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
                resp = None
            if cancelled or gone:
                return b""
            reopens += 1
            if time.monotonic() - silent_since > REOPEN_SILENCE_S:
                try:
                    from . import logs
                    logs.info(f"engine stream: nothing arrived at byte "
                              f"{self._pos} in {REOPEN_SILENCE_S:.0f}s over "
                              f"{reopens} reopens - ending the stream")
                except Exception:
                    pass
                return b""
            time.sleep(REOPEN_PAUSE_S)

    def seek(self, pos):
        pos = max(0, min(int(pos), self.size))
        with self._lock:
            resp, self._resp = self._resp, None
            self._pos = pos
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        return pos

    def cancel(self):
        with self._lock:
            self._cancelled = True
            resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def close(self):
        self.cancel()


def _open(uri):
    # atomic://127.0.0.1:PORT/HASH/N  ->  the engine's http URL.
    rest = str(uri or "")
    if rest.startswith(f"{SCHEME}://"):
        rest = rest[len(SCHEME) + 3:]
    url = "http://" + rest
    if not _ENGINE_URL.match(url):
        raise ValueError(f"not an engine URL: {uri}")
    return _EngineStream(url)


# ---- direct/debrid HTTP(S), the other half of the same DLL bug ---------
#
# The owner runs Real-Debrid, and debrid wins most races - so most
# playback was never on the engine URL this module fixed first. A direct
# HTTPS link rides the vendored libmpv's own http, which cannot seek at
# all (the module docstring has the measurement), so an MKV whose Cues
# sit at the end read the ENTIRE file before opening: his log, 30 August
# 2026, Angel Next Door S1E8 - first pick 18:07:39, chapter markers (the
# real open) 18:09:27, one hundred and eight seconds, which at his
# 27MB/s is the whole 1.4GB file. That is "the buffering gets stuck on
# 99% until it 100% loads", now from the debrid side.

DIRECT_SCHEME = "atomicd"
DIRECT_PROBE_TIMEOUT_S = 8.0
DIRECT_READ_TIMEOUT_S = 30.0
# How many times a direct read reopens a connection that answered
# nothing before believing the file has ended - see _DirectReader.read.
DIRECT_REOPENS = 3

_UA = "Mozilla/5.0 PC-App/1.0"


class _DirectReader:
    """Shared read plumbing for the two direct variants."""

    def __init__(self, url, headers, size):
        self._url = url
        self._headers = dict(headers or {})
        self._headers.setdefault("User-Agent", _UA)
        self.size = size
        self._lock = threading.Lock()
        self._resp = None
        self._pos = 0
        self._cancelled = False

    def _open_from(self, pos):
        headers = dict(self._headers)
        if pos:
            headers["Range"] = f"bytes={int(pos)}-"
        request = urllib.request.Request(self._url, headers=headers)
        return urllib.request.urlopen(request,
                                      timeout=DIRECT_READ_TIMEOUT_S)

    def read(self, length):
        with self._lock:
            if self._cancelled:
                return b""
            if self.size is not None and self._pos >= self.size:
                return b""
            resp = self._resp
        # Retries from the current position: debrid hosts drop idle
        # connections, and a paused player's next read used to be the
        # whole failure. **An empty read counts as a drop too**, not only
        # an exception - urllib answers b"" and raises nothing for a body
        # cut short (see _EngineStream.read for the measurement), and
        # b"" is EOF to mpv, which then pauses under keep-open. A remote
        # host can genuinely end early, so the reopens are counted rather
        # than timed: DIRECT_REOPENS, then the honest EOF.
        reopens = 0
        while True:
            data = b""
            try:
                if resp is None:
                    resp = self._open_from(self._pos)
                    with self._lock:
                        if self._cancelled:
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return b""
                        self._resp = resp
                data = resp.read(max(1, int(length)))
            except Exception:
                pass
            if data:
                with self._lock:
                    self._pos += len(data)
                return data
            with self._lock:
                self._resp = None
                cancelled = self._cancelled or (
                    self.size is not None and self._pos >= self.size)
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
                resp = None
            if cancelled or reopens >= DIRECT_REOPENS:
                return b""
            reopens += 1
            time.sleep(REOPEN_PAUSE_S)

    def cancel(self):
        with self._lock:
            self._cancelled = True
            resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def close(self):
        self.cancel()


class _DirectSeekable(_DirectReader):
    def seek(self, pos):
        pos = max(0, int(pos))
        if self.size is not None:
            pos = min(pos, self.size)
        with self._lock:
            resp, self._resp = self._resp, None
            self._pos = pos
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        return pos


def _open_direct(uri):
    """Probe the host once and answer with the right stream shape.

    Runs on mpv's stream thread, never the UI. A host that honours
    ranges (Real-Debrid does) gets the seekable stream; anything else -
    no range support, a failed probe - gets the sequential one, which
    is byte-for-byte the old behaviour. Opening can therefore never be
    worse than before this module existed."""
    rest = str(uri or "")
    if rest.startswith(f"{DIRECT_SCHEME}://"):
        rest = rest[len(DIRECT_SCHEME) + 3:]
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(rest.encode("ascii")).decode("utf-8"))
        url = payload["url"]
        headers = payload.get("headers") or {}
    except Exception as error:
        raise ValueError(f"bad direct uri: {error}")

    size = None
    seekable = False
    try:
        probe_headers = dict(headers)
        probe_headers.setdefault("User-Agent", _UA)
        probe_headers["Range"] = "bytes=0-0"
        request = urllib.request.Request(url, headers=probe_headers)
        with urllib.request.urlopen(
                request, timeout=DIRECT_PROBE_TIMEOUT_S) as resp:
            if resp.status == 206:
                content_range = resp.headers.get("Content-Range") or ""
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    size = int(total)
                    seekable = size > 0
            elif resp.status == 200:
                length = resp.headers.get("Content-Length")
                if length and str(length).isdigit():
                    size = int(length)
    except Exception:
        pass
    if seekable:
        return _DirectSeekable(url, headers, size)
    return _DirectReader(url, headers, size)


def direct_uri(url, headers=None):
    """The atomicd:// form of a direct http(s) URL, or None.

    HLS is deliberately left alone: an .m3u8 is a playlist whose seeks
    are playlist arithmetic, and its segment fetches must stay on
    lavf's own http."""
    url = str(url or "")
    if not url.lower().startswith(("http://", "https://")):
        return None
    if _ENGINE_URL.match(url):
        return None                     # the engine scheme owns these
    bare = url.split("?", 1)[0].lower()
    if bare.endswith((".m3u8", ".m3u")):
        return None
    payload = json.dumps({"url": url, "headers": dict(headers or {})})
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{DIRECT_SCHEME}://{encoded}"


def attach(handle):
    """Register both protocols on one mpv handle. Safe to call twice.

    **Returns whether mpv can actually open these schemes**, and the
    caller must not rewrite a URL when it cannot. This used to return
    nothing and swallow the failure, so when registration broke - mpv
    moved to its own process and a Python opener stopped being something
    that could be handed over (helpers/mpv_proxy) - the load path went on
    rewriting every URL to `atomic://` regardless. mpv was then asked for
    a scheme it had never heard of, so **every source of every episode
    failed to open** while the log recorded one line per attempt. The
    owner: "it does not play videos, get stuck in sourcing."

    A false here is not a disaster: the URL stays http:// and playback
    falls back to the vendored libmpv's own path, which is the seekless
    behaviour this module was written to improve on - slow, but playing.
    """
    try:
        if getattr(handle, "_atomic_seekable_stream", False):
            return True
        handle.register_stream_protocol(SCHEME, _open)
        handle.register_stream_protocol(DIRECT_SCHEME, _open_direct)
        handle._atomic_seekable_stream = True
        return True
    except Exception:
        from . import logs
        logs.exception("could not register the seekable engine stream")
        return False


def engine_uri(url):
    """The atomic:// form of an engine URL, or None when `url` is not one."""
    url = str(url or "")
    if _ENGINE_URL.match(url):
        return f"{SCHEME}://" + url[len("http://"):]
    return None


def _patch_player(module):
    key = ("seekable-stream", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    Page = module.PlayerPage
    old_load = Page._load_into_mpv

    def load_into_mpv(self, stream, resume_at=None):
        # Only rewrite when mpv has the schemes - see attach().
        ready = attach(getattr(self, "handle", None))
        url = (stream or {}).get("url")
        uri = engine_uri(url) if ready else None
        if uri is None and ready:
            uri = direct_uri(url, (stream or {}).get("headers"))
        if uri:
            # A copy, not the live dict: the source list, the panel and
            # the release records all hold the http form, and only mpv
            # needs the atomic form.
            stream = dict(stream)
            stream["url"] = uri
        return old_load(self, stream, resume_at)

    Page._load_into_mpv = load_into_mpv


# **mpv's cache knobs were tried here and measured as doing nothing -
# do not re-add them without a number.** The theory was that the long
# freeze came from mpv refusing to resume until the whole
# `demuxer_readahead_secs=20` target had refilled (the cache time did
# jump 6.6s -> 31.6s at the moment it resumed). Setting
# `demuxer_readahead_secs=5` + `cache_pause_wait=0.5` changed the run
# not at all: the same stall, 14.18s -> 33.9s, to the tenth of a second.
# The real cause was on the torrent side - the container tail holding
# playback's own priority (see torrent_engine._apply_windows) - and
# with that fixed the same swarm plays through with zero stalls. A
# smaller readahead would only have cost resilience for nothing.


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    # Chained onto the shared windows.player hook - a second finder on an
    # already-hooked module silently shadows the rest (helpers/__init__).
    # Installed after player_resume_latency_patch, so this wrapper runs
    # FIRST on a load and the deferred-resume wrapper sees the rewritten
    # stream copy - its seat poll reads only info_hash, which survives.
    try:
        from . import requested_fixes_patch as requested
        previous = requested._patch_player

        def chained(module):
            previous(module)
            _patch_player(module)

        requested._patch_player = chained
        loaded = sys.modules.get("windows.player")
        if loaded is not None:
            _patch_player(loaded)
    except Exception:
        pass
