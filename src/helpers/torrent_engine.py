"""Atomic's own torrent streaming engine, so playback needs nothing
installed alongside it.

This replaces the Stremio streaming server. That server worked, and is
still the fallback, but it meant Atomic could only play anything on a
machine with Stremio installed - which is not what "plays locally" is
supposed to mean.

Two halves:

  * **libtorrent** does the actual swarm work - trackers, DHT, peers,
    pieces. It is the same library the streaming clients use, including
    the one this replaces. Wheels exist for CPython 3.9-3.13, which is
    why this project builds on 3.13 (3.15 is a beta and has none).
  * **A tiny local HTTP server** turns that into something mpv can
    open. mpv cannot play a magnet; it can play `http://127.0.0.1:PORT/
    <id>/<file>`, and this serves exactly that, honouring Range so
    seeking works.

The part that makes it *streaming* rather than downloading:

  * `sequential_download` plus a deliberately prioritised head. Without
    this libtorrent fetches the rarest pieces first - excellent for
    completing a download, useless for watching, because the opening
    seconds arrive last.
  * A Range request that lands in the middle (a seek) re-points the
    priorities at that offset and sets piece *deadlines*, rather than
    waiting for everything in between.

Nothing here raises into the UI: every entry point returns None or an
error string, and the player says so.
"""

import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import logs

try:
    import libtorrent as lt
except Exception:                                       # pragma: no cover
    lt = None


def available() -> bool:
    return lt is not None


def unavailable_reason():
    if lt is not None:
        return None
    return ("The built-in torrent engine (libtorrent) is not installed "
            "in this build.")


# Where pieces land. A real directory rather than memory: a 1080p
# episode is well over a gigabyte and libtorrent streams to disk, which
# is also what lets the HTTP side just read the file back.
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "atomic-stream-cache")

# Fixed so the Windows Firewall rule the user grants once keeps applying;
# see the session settings for why 0 (random) re-prompts every launch.
LISTEN_PORT = 47600

# How much of the front of the file to demand before anything else.
# mpv wants the container header and the first frames; 12MB covers the
# moov atom of a big mp4 and a comfortable read-ahead on an mkv.
HEAD_BYTES = 12 * 1024 * 1024
# Kept ready ahead of wherever playback currently is.
READAHEAD_BYTES = 24 * 1024 * 1024

# The band that gets full priority and real deadlines - deliberately
# small. This is what playback is blocked on, and concentrating the
# swarm on it is the difference between a piece completing in seconds
# and 80MB of half-finished pieces. Rounded up to at least two pieces,
# so a torrent with very large pieces still gets one ready and one
# following.
URGENT_BYTES = 8 * 1024 * 1024
# **How much of the file is wanted at all, at any one moment.**
#
# Everything outside this window is priority 0 rather than 1, which is
# what turned a 36.8-second read of the first 16MB into a 9.3-second
# read of the first 50MB (see focus() for both measurements). The size
# is a compromise measured on the owner's connection: small enough that
# libtorrent's ~46-95 peers are all working on pieces the reader will
# reach within the minute, large enough to hold minutes of video so the
# window going idle is never something the viewer sees.
STREAM_WINDOW_BYTES = 48 * 1024 * 1024
# ...and a floor in *pieces*, because a release with 256KB pieces would
# otherwise put 192 of them in the window and be back to spreading thin.
STREAM_WINDOW_MIN_PIECES = 12
# How far the reader may advance before the window is slid forward.
# Sliding on every served byte would rewrite a 40,000-entry priority
# array continuously; sliding only when a piece is *missing* (which is
# what used to happen) means the window never moves until playback has
# already stalled.
STREAM_REFOCUS_BYTES = 8 * 1024 * 1024

# The window that belongs to nobody in particular - what add(),
# set_start_seconds() and a bare focus() move. Every live HTTP read
# holds one of its own beside it.
PRIMARY_READER = "primary"

# **The end of the file, wanted from the moment the torrent is added.**
#
# This is the owner's "buffering takes more than 60 seconds", and it was
# not the swarm. Measured 22 August 2026 with real libmpv against Bleach
# TYBW S01E12 (556MB, 2122 pieces), logging every Range request the
# demuxer made:
#
#     @9.17s  bytes=556132206-   100.0% of file   blocked 6.40s
#     @9.70s  bytes=0-             0.0% of file   blocked 6.94s
#     @10.02s bytes=8067-          0.0% of file   blocked 0.85s
#
# mpv's **first** request is the last byte of the file. Matroska writes
# its Cues (the seek index) at the end, and libavformat reads the
# SeekHead and jumps there before it will decode a single frame - an mp4
# whose `moov` atom was not front-loaded behaves the same way. `add()`
# primed only `focus(0, HEAD_BYTES)`, so those pieces sat at priority 1
# and the player waited on a part of the file nothing had been told to
# fetch; worse, serving that request called `focus(tail)`, which
# rewrites the whole priority array and so *dropped the head* it had
# just spent seconds fetching. Head and tail then took turns.
#
# 4MB covers a Cues element for a feature-length file with room to
# spare, and it is small enough that fetching it costs a second at the
# start rather than a stall in the middle. Kept wanted for the whole
# session, not just at add(): see _tail_pieces, which every focus()
# re-raises so a seek can never demote the index again.
TAIL_BYTES = 4 * 1024 * 1024

# **Where playback is going to start, when that is not the beginning.**
#
# Resuming seeks to an arbitrary offset, and until this existed nothing
# told the swarm about it: add() primed the head and the index, mpv drew
# the first frame at 0, *then* seeked - and the pieces at the resume
# offset were priority 1 like the rest of the file, so the player sat on
# them. The owner's report, 22 August 2026: "when the alert 'resumed
# from 9:23' appears it does play with it, it takes ~10-30 sec then the
# vid plays". Measured on Frieren S01E02 (EMBER): first picture at
# 31.0s, picture at 9:23 at **69.4s** - the seek alone cost 38.4s.
#
# The offset is read out of the container's own Cues (see
# _matroska_layout), so it is the byte the demuxer will actually ask
# for and the band can be small - two pieces of margin either side
# rather than a guess wide enough to cover being wrong. Armed only once
# the player has its url (arm_start_band): during prepare the only
# things that matter are the first piece and the index, and everything
# wanted competes.
#
# The guess this replaced: position/duration of the file's bytes. Its
# measured error on Frieren S01E02 was **51MB** (9:23 is 36.1% of the
# runtime and 28.9% of the bytes), a 64MB band around it missed
# completely, and it still cost 3.5s on the read of the file's opening.
RESUME_LOOKBACK_BYTES = 2 * 1024 * 1024
RESUME_BAND_BYTES = 16 * 1024 * 1024

_VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts")
_EPISODE_RE = re.compile(r"s(\d{1,2})[\s._-]?e(\d{1,3})", re.I)

_session = None
_session_lock = threading.Lock()
_torrents = {}          # info_hash -> _Torrent
_server = None
_server_port = None


def _make_session():
    """A session tuned for watching rather than archiving.

    DHT and the fallback trackers matter more here than they would in a
    downloader: a lot of what these indexers return carries no trackers
    at all, and without peer discovery the swarm is invisible however
    many seeders it claims."""
    settings = {
        # A fixed listen port, not 0. Port 0 asks the OS for a random
        # one every launch, and Windows Firewall keys its remembered
        # allow-rule on the port - so a new port each session means a new
        # firewall prompt every single time the player opens. A fixed
        # port is prompted once and then never again. 47600 is high,
        # unprivileged and not in any well-known service's range.
        "listen_interfaces": f"0.0.0.0:{LISTEN_PORT},[::]:{LISTEN_PORT}",
        # storage_notification is here for read_piece_alert - see
        # _Torrent.read_piece, which is the only way to read a piece
        # without racing libtorrent's disk writes.
        "alert_mask": lt.alert.category_t.error_notification
                      | lt.alert.category_t.status_notification
                      | lt.alert.category_t.storage_notification,
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        # Announce to a decent number of peers quickly; the default is
        # conservative in a way that shows up as a slow start.
        "connections_limit": 500,
        # **This has to be at least as wide as the race.**
        # streams.prepare_fastest deliberately starts RACE_WIDTH releases
        # at once and keeps replacing the failures, so at 8 a session
        # that still held a couple of losers from the previous attempt
        # put the newest lanes over the limit - and libtorrent's queue
        # *pauses* a torrent past it, which then sits out its whole data
        # wait without ever being started and is reported as a dead
        # swarm. Measured on the owner's Bleach S01E12, 22 August 2026:
        # ten consecutive releases answered `no-peers`, each after ~7s.
        # See also the auto_managed flag in add(), which is the other
        # half of the same fix.
        "active_downloads": 32,
        "active_limit": 64,
        "active_seeds": 0,
        # Announce to every tracker in every tier at once, rather than
        # libtorrent's default of one per tier in order. Metadata comes
        # from peers, so how fast peers are found *is* how fast playback
        # starts - and these releases carry a couple of dozen trackers
        # of wildly varying health. Waiting politely for a dead one to
        # time out before trying the next is most of a slow start.
        "announce_to_all_trackers": True,
        "announce_to_all_tiers": True,
        # Ask more peers for metadata concurrently.
        "torrent_connect_boost": 100,
        "connection_speed": 500,
        # Do not spend the opening seconds on handshakes that will not
        # answer; failing fast leaves room for one that will.
        "peer_connect_timeout": 6,
        "handshake_timeout": 8,
        "request_timeout": 12,
        # We are watching, not seeding a library.
        "seed_time_limit": 0,
        "share_ratio_limit": 1,
        "strict_end_game_mode": False,
        "user_agent": "Atomic/1.0 libtorrent/2.1",
    }
    session = lt.session(settings)
    for router in (("router.bittorrent.com", 6881),
                   ("router.utorrent.com", 6881),
                   ("dht.transmissionbt.com", 6881),
                   ("router.bitcomet.com", 6881)):
        try:
            session.add_dht_router(*router)
        except Exception:
            pass
    return session


# Pieces asked for through read_piece, waiting on their alert.
# (info_hash, piece) -> [threading.Event, bytes-or-None]
_reads = {}
_reads_lock = threading.Lock()
_pump_started = False


def _alert_pump():
    """Drain libtorrent's alert queue and answer read_piece requests.

    Nothing popped alerts before this; they simply accumulated until
    libtorrent dropped them. It exists now because read_piece_alert is
    the only way to get a piece's bytes without racing the disk - see
    _Torrent.read_piece."""
    while True:
        current = session()
        if current is None:
            return
        try:
            current.wait_for_alert(500)
            alerts = current.pop_alerts()
        except Exception:
            time.sleep(0.2)
            continue
        for alert in alerts:
            if not isinstance(alert, lt.read_piece_alert):
                continue
            try:
                info_hash = str(alert.handle.info_hash()).lower()
            except Exception:
                continue
            key = (info_hash, int(alert.piece))
            with _reads_lock:
                waiter = _reads.get(key)
            if waiter is None:
                continue
            try:
                waiter[1] = None if alert.error.value() else bytes(alert.buffer)
            except Exception:
                waiter[1] = None
            waiter[0].set()


def session():
    global _session, _pump_started
    with _session_lock:
        if _session is None and lt is not None:
            _session = _make_session()
            if not _pump_started:
                _pump_started = True
                threading.Thread(target=_alert_pump, daemon=True,
                                 name="atomic-torrent-alerts").start()
        return _session


# How much watched-but-not-kept video to leave lying around. Streaming
# writes every episode to disk and nothing ever removed it: a single
# session of testing left 24 files and ~10GB behind, and it only ever
# grew. A download the user actually asked for is copied to their own
# folder, so everything in here is scratch by definition.
CACHE_LIMIT_BYTES = 6 * 1024 * 1024 * 1024


def trim_cache(limit: int = CACHE_LIMIT_BYTES) -> int:
    """Delete the least recently used scratch files until under `limit`.

    Returns the bytes freed. Oldest-touched first, and never a file
    belonging to a torrent this session still has open - that would be
    deleting the thing currently playing."""
    if not os.path.isdir(_CACHE_DIR):
        return 0
    in_use = set()
    for torrent in list(_torrents.values()):
        try:
            in_use.add(os.path.normcase(torrent.file_path()))
        except Exception:
            pass

    entries = []
    total = 0
    for root, _dirs, files in os.walk(_CACHE_DIR):
        for name in files:
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            # st_blocks is not on Windows; st_size over-reports a sparse
            # file, which is the safe direction - it only makes trimming
            # more eager, never less.
            entries.append((stat.st_atime, stat.st_size, path))
            total += stat.st_size
    if total <= limit:
        return 0

    freed = 0
    for _atime, size, path in sorted(entries):
        if total - freed <= limit:
            break
        if os.path.normcase(path) in in_use:
            continue
        try:
            os.remove(path)
            freed += size
        except OSError:
            continue
    # Directories a torrent made for a multi-file release are left empty
    # behind their contents; sweep them so the cache does not fill with
    # husks.
    for root, dirs, _files in os.walk(_CACHE_DIR, topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    return freed


_last_trim = 0.0
_trim_lock = threading.Lock()

# How often the scratch may be re-measured while the app is running.
# Cheap enough to do often, but it walks the whole cache directory, so
# not on every single release during a six-wide race.
TRIM_INTERVAL_S = 60.0


def maybe_trim(interval: float = TRIM_INTERVAL_S):
    """Trim the scratch cache if it has not been trimmed recently, on a
    background thread.

    **Trimming used to happen only at launch**, inside prewarm(), and
    that is not often enough: streaming writes every episode watched to
    disk, so a single long session grows the cache without any ceiling
    at all until the next restart. Measured 22 August 2026 on the
    owner's machine - C: at **0.0 GB free of 237.6 GB**, with the
    scratch at 7.00 GB against its own 6 GB limit and nothing due to
    trim it until the app was next opened.

    A full disk is not just a space problem here: it is what corrupts
    the image cache (see images.download), which is the "images
    disappear" report. So this now runs while the app is open, not only
    when it starts."""
    global _last_trim
    with _trim_lock:
        if time.time() - _last_trim < interval:
            return
        _last_trim = time.time()
    threading.Thread(target=trim_cache, daemon=True,
                     name="atomic-torrent-trim").start()


def prewarm():
    """Bring the session and DHT up before anything is played.

    Bootstrapping the DHT and binding the listen sockets costs several
    seconds, and paying it at the moment someone presses play makes the
    first torrent of a session look slower than every later one. Called
    from a background thread when the player page opens, so by the time
    a source is chosen the session is already routable.

    Cheap to call more than once - the session is built once.

    **Measured 21 August 2026 on the owner's machine**, because "several
    seconds" was doing a lot of work in the paragraph above: constructing
    the session costs 16ms and the DHT it needs is empty at that moment -
    0 nodes, 4 at 0.23s, 30 at 1.33s, **50 at 2.44s**. Peer discovery is
    how a magnet finds anyone to ask for metadata, so until that table
    exists the first release simply waits, and every release after it in
    the same run does not. That is the owner's "it gets stuck in the same
    spot for a few secs then goes super fast".

    It is called from `main()` now as well as from the player, so the
    2.44s is spent while the app is being opened rather than while
    someone is watching a loading screen."""
    if lt is None:
        return
    # Trimming rides along with the prewarm rather than getting its own
    # trigger: it is disk work, it must not happen on the UI thread, and
    # the moment the player opens is exactly when last session's scratch
    # has stopped being interesting.
    threading.Thread(target=lambda: (session(), start_server(), trim_cache()),
                     daemon=True, name="atomic-torrent-prewarm").start()


class _Torrent:
    """One torrent being streamed, plus what the HTTP side needs."""

    def __init__(self, handle, info_hash):
        self.handle = handle
        self.info_hash = info_hash
        self.lock = threading.Lock()
        self.file_index = None
        self.last_touched = time.time()
        # Byte offset playback is going to *start* at, when a resume
        # point says it is not zero. Kept wanted through every focus()
        # the same way the index is - see RESUME_BAND_BYTES.
        self.start_offset = None
        self.start_seconds = None
        # Every live read's window, by reader key - see focus(). mpv
        # keeps more than one connection open and last-one-wins zeroed
        # the pieces the other one was blocked on.
        self.readers = {PRIMARY_READER: (0, HEAD_BYTES, 0)}
        self.last_offset = 0
        # Which window was pointed most recently. The newest read is the
        # one somebody is waiting on - see _apply_windows.
        self._seq = 0
        # The resume band is not wanted until the player actually has a
        # url - see arm_start_band.
        self.start_armed = False
        # Set once a *download* claims this torrent. focus() then stops
        # rewriting piece priorities altogether: the download owns them
        # (download_whole/_want_pieces), and a streaming window would
        # otherwise zero everything it is trying to fetch.
        self.want_whole = False

    # -- geometry -----------------------------------------------------
    @property
    def info(self):
        return self.handle.torrent_file()

    def file_size(self) -> int:
        return self.info.files().file_size(self.file_index)

    def file_offset(self) -> int:
        return self.info.files().file_offset(self.file_index)

    def piece_length(self) -> int:
        return self.info.piece_length()

    def piece_at(self, offset_in_file: int) -> int:
        return int((self.file_offset() + offset_in_file) // self.piece_length())

    def have(self, piece: int) -> bool:
        try:
            return self.handle.have_piece(piece)
        except Exception:
            return False

    def _tail_pieces(self, span_bytes: int = None):
        """The pieces holding the last TAIL_BYTES of the chosen file.

        The container's seek index lives here and the demuxer reads it
        before the first frame - see TAIL_BYTES for the measurement.
        Returns an empty range once they are all on disk, so a torrent
        that has the index stops spending priority on it."""
        try:
            size = self.file_size()
            if size <= 0:
                return []
            span = TAIL_BYTES if span_bytes is None else span_bytes
            first = self.piece_at(max(size - span, 0))
            last = self.piece_at(size - 1)
            return [p for p in range(first, last + 1) if not self.have(p)]
        except Exception:
            return []

    def _start_pieces(self):
        """The pieces holding the resume band, minus whatever is already
        on disk. Empty when playback starts at the beginning, or once
        the band has landed - so it stops costing priority the moment it
        has stopped being useful, exactly like _tail_pieces."""
        if self.start_offset is None or not self.start_armed:
            return []
        try:
            size = self.file_size()
            if size <= 0:
                return []
            first_byte = max(int(self.start_offset) - RESUME_LOOKBACK_BYTES, 0)
            last_byte = min(first_byte + RESUME_BAND_BYTES, size - 1)
            first = self.piece_at(first_byte)
            last = self.piece_at(last_byte)
            return [p for p in range(first, last + 1) if not self.have(p)]
        except Exception:
            return []

    def read_piece(self, piece: int, timeout: float = 10.0):
        """A piece's real bytes, from libtorrent rather than from the
        file on disk.

        **have_piece() runs ahead of the data being readable, and this
        is a corruption bug, not a slow one.** A torrent's file is
        sparse: reading a hole succeeds and returns zeros. Measured 22
        August 2026 on Frieren S01E02, sampling 64KB from the middle of
        each piece the instant have_piece() turned true - **10 of 13
        pieces read back as 64KB of zeros**, and all ten had real data
        half a second later. Those zeros were being served to mpv as
        video: two runs out of eight died with "mkv: Error parsing
        element Attachments" and never drew a frame, because the header
        they parsed was a hole.
        (`disk_write_mode = always_pwrite` was measured too, in case it
        was Windows mmap coherency. It is not - 12 of 12 still zero.
        libtorrent hashes a piece from the blocks in memory and marks it
        complete before its write job has finished.)

        read_piece asks libtorrent, which answers out of its own buffers
        or the disk, whichever holds the truth. Returns None on failure,
        and the caller falls back to the plain file read - no worse than
        what shipped."""
        key = (self.info_hash, int(piece))
        waiter = [threading.Event(), None]
        with _reads_lock:
            existing = _reads.get(key)
            if existing is not None:
                waiter = existing
            else:
                _reads[key] = waiter
        try:
            if existing is None:
                self.handle.read_piece(int(piece))
            if not waiter[0].wait(timeout):
                return None
            return waiter[1]
        except Exception:
            return None
        finally:
            # Popped whether or not the alert came. Leaving a
            # never-set waiter behind would make every later read of
            # this piece join it and wait for an alert that has already
            # been and gone.
            if existing is None:
                with _reads_lock:
                    if _reads.get(key) is waiter:
                        _reads.pop(key, None)

    def read_present(self, offset: int, length: int):
        """Bytes from the file, but only where the pieces are actually
        on disk. A torrent's file is sparse: reading a hole succeeds and
        returns zeros, which would parse as garbage rather than fail."""
        try:
            size = self.file_size()
            offset = max(0, min(int(offset), max(size - 1, 0)))
            length = int(min(length, size - offset))
            if length <= 0:
                return None
            first = self.piece_at(offset)
            last = self.piece_at(offset + length - 1)
            for piece in range(first, last + 1):
                if not self.have(piece):
                    return None
            # Through libtorrent for the same reason _serve does: a
            # freshly completed piece reads back as zeros from the file,
            # and zeros parse as a broken container rather than failing.
            piece_length = self.piece_length()
            out = bytearray()
            for piece in range(first, last + 1):
                blob = self.read_piece(piece)
                if blob is None:
                    out = None
                    break
                piece_start = piece * piece_length - self.file_offset()
                begin = max(offset - piece_start, 0)
                end = min(piece_start + len(blob), offset + length) - piece_start
                out += blob[begin:end]
            if out is not None and len(out) == length:
                return bytes(out)
            with open(self.file_path(), "rb") as handle:
                handle.seek(offset)
                data = handle.read(length)
            return data if len(data) == length else None
        except Exception:
            return None

    def resolve_start(self, seconds):
        """Turn a resume *time* into the byte the demuxer will read.

        Answers None unless it is certain: not Matroska, no Cues, or the
        bytes holding them not on disk yet all mean "prime nothing",
        because a wrong offset costs bandwidth the first frame needs and
        buys nothing (see the measurement above _matroska_layout)."""
        head = self.read_present(0, 256 * 1024)
        if not head:
            return None
        layout = _matroska_layout(head)
        if not layout:
            return None
        segment_start, cues_at, scale = layout
        # The Cues element's own header, then the element itself. Read
        # the header first so only as much as it actually spans is
        # required to be on disk.
        header = self.read_present(cues_at, 16)
        if not header:
            return None
        try:
            element, pos = _ebml_id(header, 0)
            size, pos = _ebml_size(header, pos)
        except (IndexError, ValueError):
            return None
        if element != _EBML_CUES or not size or size > 32 * 1024 * 1024:
            return None
        cues = self.read_present(cues_at + pos, size)
        if not cues:
            return None
        return _cue_byte_for(head, cues, segment_start, scale, seconds)

    def set_start_seconds(self, seconds):
        """Say where playback will begin, in seconds into the file.

        Kept as a time rather than a byte because only resolve_start can
        turn one into the other, and it needs the container's index -
        which is not on disk when the caller knows the time."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return False
        if seconds <= 0:
            return False
        self.start_seconds = seconds
        return True

    # -- prioritising -------------------------------------------------
    def focus(self, offset_in_file: int, span_bytes: int = READAHEAD_BYTES,
              reader=PRIMARY_READER):
        """Point the download at this offset.

        **Piece priorities, not just deadlines - this is what makes a
        seek work at all.** The first version set deadlines and left
        `sequential_download` on, and sequential mode pins libtorrent's
        picker to the front of the file: a seek to the middle then waits
        for pieces that are never chosen, the read times out, and mpv
        goes on showing what it already had. Measured exactly that -
        the player reported 74 minutes in and was still displaying the
        opening title card.

        **`reader` exists because mpv reads from several connections at
        once**, and once the pieces outside a window are priority 0
        rather than 1, "point the download at this offset" written as
        last-one-wins is a deadlock: the header reader and the seek that
        went looking for the Cues each zeroed the other's pieces, both
        waited forever, and playback never started at all (measured -
        the demuxer reported a duration at 11.1s and then sat for three
        minutes without a frame). So every live read holds a window and
        the priorities are the union of them."""
        with self.lock:
            if reader != PRIMARY_READER:
                # **A real read has taken over, so the placeholder window
                # at the start of the file stops being wanted.** It is
                # what add() and the index prime point at, and leaving it
                # pinned to offset 0 for the session meant every seek was
                # racing twelve pieces of the opening that nobody was
                # going to read again.
                self.readers.pop(PRIMARY_READER, None)
            self._seq += 1
            self.readers[reader] = (int(offset_in_file), int(span_bytes),
                                    self._seq)
            self.last_offset = int(offset_in_file)
            windows = list(self.readers.values())
        self._apply_windows(windows)

    def end_read(self, reader):
        """This reader has finished; stop keeping its window wanted."""
        with self.lock:
            self.readers.pop(reader, None)
            if not self.readers:
                # Nothing is reading. Keep wanting where the last read
                # got to rather than snapping back to the opening.
                self._seq += 1
                self.readers[PRIMARY_READER] = (self.last_offset,
                                                READAHEAD_BYTES, self._seq)
            windows = list(self.readers.values())
        self._apply_windows(windows)

    def _apply_windows(self, windows):
        try:
            handle = self.handle
            info = self.info
            total_pieces = info.num_pieces()
            file_first = self.piece_at(0)
            file_last = self.piece_at(max(self.file_size() - 1, 0))
            # **Newest read first.** mpv abandons a connection when it
            # seeks, but the handler serving it is blocked in a write or
            # a piece wait and does not notice for a while - so its
            # window goes on holding the best deadlines while the read
            # the viewer is actually waiting for queues behind it.
            # Measured: one resume seek cost **13.5s** with a zombie
            # window still ordered ahead of it, against 0.16-1.21s when
            # it was not.
            windows = sorted(windows, key=lambda w: -w[2])
            # **A narrow urgent band, not a wide one.** Marking the whole
            # readahead window "top priority" is the same as marking none
            # of it: libtorrent spreads requests across every urgent
            # piece at once, so with 16.8MB pieces and a 24-piece window
            # it has 400MB in flight and completes nothing. Measured
            # exactly that - 2.7MB/s sustained for 30 seconds, 80MB
            # pulled, and the *first* piece still unfinished, because its
            # last blocks were never the ones being asked for.
            #
            # Playback only needs the piece it is about to read. So a
            # couple of pieces are urgent, the rest of the window is
            # next, and everything outside it is not wanted at all.
            urgent_count = max(2, -(-URGENT_BYTES // self.piece_length()))

            if self.want_whole:
                # A *download* owns this torrent's priorities (see
                # download_whole/_want_pieces). Do not rewrite them -
                # just order the pieces the readers are about to want.
                for offset, _span, _seq in windows:
                    start = max(file_first, self.piece_at(offset))
                    for index, piece in enumerate(
                            range(start,
                                  min(file_last, start + urgent_count - 1) + 1)):
                        try:
                            handle.set_piece_deadline(piece, 200 + index * 400)
                        except Exception:
                            pass
                self.last_touched = time.time()
                return

            # **Everything outside the window is priority 0, and that is
            # the single biggest thing measured on 22 August 2026.**
            #
            # It used to be 1 - "wanted, but only once the window is
            # satisfied". libtorrent does not read it that way. With the
            # other 164 pieces of the file merely *wanted*, ~46 peers at
            # 15-22MB/s filled the connection with pieces from all over
            # the file and the pieces the player was blocked on crawled:
            # Frieren S01E02 (EMBER, 716MB, 4.19MB pieces), cold, with
            # priorities honoured -
            #
            #     rest at 1:  384MB of the file downloaded by 30s, and
            #                 mpv's read of the FIRST 16MB took 36.8s
            #                 (0.44MB/s); index at 16.9s
            #     rest at 0:  first 50MB contiguous at 9.3s, index at
            #                 8.3s, 6.8% of received bytes unused
            #
            # Same swarm, same release, same minute. It is not bandwidth
            # and it is not the peers: it is how many distinct pieces
            # libtorrent is allowed to work on at once. A bounded window
            # means every peer is working on something the reader will
            # want within the minute.
            #
            # The window goes idle once it is full (peers drop to 0 -
            # measured, and expected: there is nothing left to ask for).
            # That is safe because it holds ~48MB, which at these
            # bitrates is minutes of video, and _serve slides it forward
            # as it streams - see STREAM_REFOCUS_BYTES.
            window_pieces = max(STREAM_WINDOW_MIN_PIECES,
                                -(-STREAM_WINDOW_BYTES // self.piece_length()))

            priorities = [0] * total_pieces
            urgent_bands = []
            for offset, span, _seq in windows:
                start = max(file_first, self.piece_at(offset))
                span_last = min(file_last,
                                self.piece_at(min(offset + span,
                                                  max(self.file_size() - 1, 0))))
                band_last = min(file_last, start + urgent_count - 1, span_last)
                window_last = min(file_last, start + window_pieces - 1)
                for piece in range(start, window_last + 1):
                    if priorities[piece] < 4:
                        priorities[piece] = 4
                for piece in range(start, band_last + 1):
                    priorities[piece] = 7
                urgent_bands.append((start, window_last))

            # **The seek index stays wanted through every re-aim.** This
            # array is absolute - it replaces whatever the last focus()
            # said - so without these lines a seek away from the tail
            # demoted the Cues back to priority 1, and the next thing
            # the demuxer did was ask for them again. Re-raised here
            # rather than once at add() for exactly that reason.
            #
            # **7, the same as the urgent band, and that was measured.**
            # At 6 the index lost to the head: Frieren S01E02, cold, the
            # tail's first piece arrived instantly but the rest trickled
            # in behind the head band streaming at 2.7MB/s, and the
            # demuxer's seek still cost **8.10s** (whole open 18.3s). It
            # is not a competition worth having - nothing decodes until
            # the index is read, so 4MB of tail *is* the critical path,
            # and _tail_pieces goes empty the moment it lands, which
            # hands the bandwidth straight back to the head.
            tail = self._tail_pieces()
            for piece in tail:
                if 0 <= piece < total_pieces:
                    priorities[piece] = 7

            # **And where playback is going to start**, when a resume
            # point says that is not the beginning. Full priority,
            # because this offset is read out of the container's Cues
            # and is therefore the byte the demuxer will ask for - not a
            # guess. Tried at priority 2 first, on the reasoning that it
            # should only spend spare capacity: it never landed in time,
            # and the resume seek still cost 6.02s. Re-raised on every
            # focus() for the same reason the index is: this array is
            # absolute.
            start_band = self._start_pieces()
            for piece in start_band:
                if 0 <= piece < total_pieces:
                    priorities[piece] = 7
            handle.prioritize_pieces(priorities)

            # **A deadline on every piece of the window, in order.**
            #
            # Priority says *whether* to fetch a piece; only a deadline
            # says in what order. With deadlines on the first two pieces
            # and the other ten merely priority 4, libtorrent filled all
            # twelve at once and the piece the player was actually
            # blocked on - the first one - finished nearly last: 45MB of
            # the 50MB window had arrived at 4.37s and piece 0 of the
            # file only completed at **4.57s** (measured 22 August 2026,
            # Frieren S01E02). Stating the order costs nothing and is
            # the whole point of the window.
            # Rank 0 is the newest window and gets the real deadlines;
            # an older one is pushed far enough back that it cannot get
            # in front of it (see the sort above).
            for rank, (band_first, band_last) in enumerate(urgent_bands):
                base = 200 + rank * 20000
                for index, piece in enumerate(range(band_first, band_last + 1)):
                    try:
                        handle.set_piece_deadline(piece, base + index * 400)
                    except Exception:
                        pass
            # The index gets deadlines interleaved with the head's, not
            # behind them: a priority alone only tells the picker what to
            # prefer, and the measured stall was the demuxer blocked on
            # precisely these pieces before frame one. Starting at 300
            # leaves the file's very first piece (deadline 200) ahead -
            # that one is what has_data() gates on, so putting the index
            # in front of it would move the wait rather than remove it.
            for index, piece in enumerate(tail):
                try:
                    handle.set_piece_deadline(piece, 300 + index * 120)
                except Exception:
                    pass
            # The resume band's deadlines sit behind the first few head
            # pieces and ahead of the rest of the window. mpv reads
            # roughly the first 20MB of the file (header plus attached
            # fonts, measured), *then* jumps - so nothing between there
            # and the resume offset is going to be read at all, and it
            # should not be ordered in front of the place playback is
            # about to start.
            for index, piece in enumerate(start_band):
                try:
                    handle.set_piece_deadline(piece, 1600 + index * 200)
                except Exception:
                    pass
        except Exception:
            pass
        self.last_touched = time.time()

    def wait_for(self, piece: int, timeout: float = 45.0) -> bool:
        """Block until this piece is on disk, or give up.

        A timeout rather than forever: a swarm can simply not have the
        data, and an HTTP handler that never returns is a player that
        hangs with no way out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.have(piece):
                return True
            # **Give up the moment this torrent is released.** The HTTP
            # handler blocks in here, mpv blocks on that read, and mpv's
            # play loop is what answers every property get and command
            # the UI thread makes - so a wait that runs its full 45
            # seconds freezes the *interface*, not just the picture.
            # That is the owner's "the next ep button is lagging, it
            # glitched and did not take me to the next ep until its
            # loading finished": pressing Next has to touch mpv, and mpv
            # was stuck reading pieces for the episode being left
            # behind. Releasing a torrent now ends its reads at once
            # (see player._release_playing_torrent).
            if _torrents.get(self.info_hash) is not self:
                return False
            try:
                self.handle.set_piece_deadline(piece, 200)
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def file_path(self) -> str:
        storage_path = self.handle.status().save_path
        return os.path.join(storage_path, self.info.files().file_path(self.file_index))


# ------------------------------------------------- reading the Cues
#
# **Where a timestamp lives in the file is a fact, not an estimate, and
# the fact is already on this machine.** Resuming needs one number: the
# byte offset playback will start at. position/duration was tried first
# and measured badly wrong - Frieren S01E02 (EMBER, 716MB, 1561s), resume
# at 9:23 is 36.1% of the runtime and mpv read at **28.9% of the bytes**,
# 51MB out, because a VBR encode does not spend its bits evenly. Priming
# a 64MB band around that guess missed entirely *and* cost 3.5s on the
# read of the file's opening, because everything wanted competes.
#
# Matroska writes a Cues element - the same seek index the demuxer reads
# before it decodes a frame, which await_start already fetches - and it
# maps timestamps to cluster positions exactly. Parsing it is ~60 lines
# of EBML and turns a 64MB bet into two pieces that are certainly right.
#
# Everything here returns None rather than raising or guessing: an mp4,
# a file whose Cues are not on disk yet, an unfamiliar layout - all of
# them mean "prime nothing", which is what happened before any of this
# existed.

_EBML_SEGMENT = 0x18538067
_EBML_SEEKHEAD = 0x114D9B74
_EBML_SEEK = 0x4DBB
_EBML_SEEKID = 0x53AB
_EBML_SEEKPOS = 0x53AC
_EBML_CUES = 0x1C53BB6B
_EBML_CUEPOINT = 0xBB
_EBML_CUETIME = 0xB3
_EBML_CUETRACKPOS = 0xB7
_EBML_CUECLUSTERPOS = 0xF1
_EBML_INFO = 0x1549A966
_EBML_TIMECODESCALE = 0x2AD7B1


def _ebml_id(data, pos):
    """(id, next) - an EBML element id keeps its length marker bits."""
    first = data[pos]
    if first & 0x80:
        length = 1
    elif first & 0x40:
        length = 2
    elif first & 0x20:
        length = 3
    elif first & 0x10:
        length = 4
    else:
        raise ValueError("bad ebml id")
    return int.from_bytes(data[pos:pos + length], "big"), pos + length


def _ebml_size(data, pos):
    """(value, next) - an EBML size drops its length marker bit."""
    first = data[pos]
    if first == 0:
        raise ValueError("bad ebml size")
    length = 1
    mask = 0x80
    while not (first & mask):
        length += 1
        mask >>= 1
    value = first & (mask - 1)
    for byte in data[pos + 1:pos + length]:
        value = (value << 8) | byte
    # All-ones is "unknown length", which only a Segment or Cluster uses.
    if value == (1 << (7 * length)) - 1:
        value = None
    return value, pos + length


def _ebml_uint(data, pos, length):
    return int.from_bytes(data[pos:pos + length], "big")


def _ebml_children(data, pos, end):
    """Yield (id, data_start, data_end) for each element in [pos, end)."""
    while pos < end:
        try:
            element, pos = _ebml_id(data, pos)
            size, pos = _ebml_size(data, pos)
        except (IndexError, ValueError):
            return
        stop = end if size is None else min(pos + size, end)
        yield element, pos, stop
        if size is None:
            return
        pos = stop


def _matroska_layout(head: bytes):
    """(segment_data_start, cues_offset, timecode_scale) from the first
    bytes of a Matroska file, or None.

    `cues_offset` is absolute in the file and comes from the SeekHead -
    scanning the tail for the Cues id instead would hit the same four
    bytes inside frame data sooner or later, and a wrong offset here is
    a wrong resume point."""
    try:
        pos = 0
        segment_start = None
        segment_end = None
        for element, start, stop in _ebml_children(head, 0, len(head)):
            if element == _EBML_SEGMENT:
                segment_start, segment_end = start, stop
                break
            pos = stop
        if segment_start is None:
            return None
        cues = None
        scale = 1000000
        for element, start, stop in _ebml_children(head, segment_start,
                                                   min(segment_end or len(head),
                                                       len(head))):
            if element == _EBML_SEEKHEAD:
                for sub, sub_start, sub_stop in _ebml_children(head, start, stop):
                    if sub != _EBML_SEEK:
                        continue
                    seek_id = seek_pos = None
                    for leaf, leaf_start, leaf_stop in _ebml_children(
                            head, sub_start, sub_stop):
                        if leaf == _EBML_SEEKID:
                            seek_id = _ebml_uint(head, leaf_start,
                                                 leaf_stop - leaf_start)
                        elif leaf == _EBML_SEEKPOS:
                            seek_pos = _ebml_uint(head, leaf_start,
                                                  leaf_stop - leaf_start)
                    if seek_id == _EBML_CUES and seek_pos is not None:
                        cues = segment_start + seek_pos
            elif element == _EBML_INFO:
                for leaf, leaf_start, leaf_stop in _ebml_children(head, start, stop):
                    if leaf == _EBML_TIMECODESCALE:
                        scale = _ebml_uint(head, leaf_start,
                                           leaf_stop - leaf_start) or 1000000
            if cues is not None and scale != 1000000:
                break
        if cues is None:
            return None
        return segment_start, cues, scale
    except Exception:
        return None


def _cue_byte_for(head: bytes, cues: bytes, segment_start: int, scale: int,
                  seconds: float):
    """The byte a seek to `seconds` will actually read, or None.

    The last cue at or before the target, which is where a demuxer lands
    - so this is the offset mpv is about to ask for, not an estimate of
    it."""
    try:
        target_ns = float(seconds) * 1e9
        best = None
        for element, start, stop in _ebml_children(cues, 0, len(cues)):
            if element != _EBML_CUEPOINT:
                continue
            cue_time = cluster = None
            for leaf, leaf_start, leaf_stop in _ebml_children(cues, start, stop):
                if leaf == _EBML_CUETIME:
                    cue_time = _ebml_uint(cues, leaf_start, leaf_stop - leaf_start)
                elif leaf == _EBML_CUETRACKPOS:
                    for sub, sub_start, sub_stop in _ebml_children(
                            cues, leaf_start, leaf_stop):
                        if sub == _EBML_CUECLUSTERPOS:
                            cluster = _ebml_uint(cues, sub_start,
                                                 sub_stop - sub_start)
            if cue_time is None or cluster is None:
                continue
            if cue_time * scale <= target_ns:
                best = max(best or 0, segment_start + cluster)
        return best
    except Exception:
        return None


def _piece_priorities(handle) -> list:
    """The handle's *piece* priorities, whichever name this binding uses.
    Empty when the binding exposes neither - callers treat that as
    "cannot tell" and carry on."""
    for name in ("get_piece_priorities", "piece_priorities"):
        getter = getattr(handle, name, None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                return []
    return []


# How long to wait for libtorrent to finish applying `prioritize_files`
# before writing the streaming priorities on top of it.
#
# **This is the biggest single thing that was wrong with playback**, and
# it is invisible from reading the code. `prioritize_files()` is
# asynchronous, and it is applied about a *second* after the call
# returns - long after the synchronous-looking `prioritize_pieces()` in
# focus(). Applying it rewrites every piece of the chosen file to
# priority 7, which erases the entire scheme focus() exists to express.
# Read back live, 22 August 2026, Frieren S01E02 (EMBER, 716MB file,
# 4.19MB pieces):
#
#     right after add():          head=[7,7,4,4,1,1,1,1] mid=1 tail=[7,7]
#     one second later:           head=[7,7,7,7,7,7,7,7] mid=7 tail=[7,7]
#     after an explicit focus(0): head=[7,7,4,4,1,1,1,1] mid=1 tail=[7,7]
#
# With all 172 pieces equally urgent, libtorrent spreads ~100 peers
# across the whole file and completes about one piece a second, so the
# one piece the player is blocked on lands whenever it happens to land.
# Measured on that release with the whole file at 7: **21MB/s sustained,
# 291MB downloaded in 21 seconds, and the 0.2MB the demuxer was waiting
# for (the Matroska Cues, at the end of the file) took 19.6s**, then the
# resume seek took another 38.4s. Nothing about that is the swarm.
#
# So the file priorities are given their moment to land *before* the
# piece priorities are written. Polling rather than sleeping: it is
# usually much quicker than the cap, and the cap only applies to a
# binding whose apply is genuinely slow.
FILE_PRIORITY_SETTLE_S = 2.0


def _settle_file_priorities(handle, first_piece, last_piece,
                            timeout: float = FILE_PRIORITY_SETTLE_S) -> float:
    """Block until a prioritize_files() call has actually been applied.

    "Applied" is observable: before it lands every piece carries
    libtorrent's default priority (4), and after it lands the chosen
    file's pieces read back as 7. Returns the seconds spent, or -1 when
    the binding cannot be asked (in which case the caller carries on and
    accepts the old behaviour rather than sleeping blind)."""
    started = time.time()
    if not _piece_priorities(handle):
        return -1.0
    deadline = started + max(0.0, timeout)
    probe = [first_piece, (first_piece + last_piece) // 2, last_piece]
    while time.time() < deadline:
        current = _piece_priorities(handle)
        if current and all(0 <= p < len(current) and current[p] == 7
                           for p in probe):
            break
        time.sleep(0.02)
    return time.time() - started


def _video_files(info) -> list:
    """(index, name, size) for every video file, or every file when the
    torrent carries no recognised video at all."""
    files = info.files()
    candidates = []
    for index in range(files.num_files()):
        name = files.file_path(index)
        candidates.append((index, name, files.file_size(index)))
    return [c for c in candidates
            if c[1].lower().endswith(_VIDEO_SUFFIXES)] or candidates


def _episode_file_index(info, season, episode):
    """The file whose *name* states this episode, or None. Deliberately
    no largest-file fallback here: callers asking "does this pack hold
    episode N" must get an honest no, not the biggest file wearing N's
    number (see file_index_for)."""
    if not (season and episode):
        return None
    for index, name, _size in _video_files(info):
        match = _EPISODE_RE.search(os.path.basename(name))
        if (match and int(match.group(1)) == int(season)
                and int(match.group(2)) == int(episode)):
            return index
    return None


def _pick_file(info, season=None, episode=None) -> int:
    """Which file in the torrent to play - the episode if its name says
    so, else the largest video."""
    matched = _episode_file_index(info, season, episode)
    if matched is not None:
        return matched
    return max(_video_files(info), key=lambda c: c[2])[0]


def add(info_hash: str, *, trackers=(), season=None, episode=None,
        file_index=None, metadata_timeout: float = 45.0,
        start_at=None):
    """Start streaming a torrent; returns its id, or None.

    `trackers` matters more than it looks. Some indexers return a bare
    info hash with no announce list at all, and DHT alone can take
    minutes to find a swarm that trackers would surface in a second.

    `start_at`, when playback is going to resume part-way in, is where
    it will start, in seconds. Nothing is fetched for it here - the byte
    that time maps to is only knowable once the container's index is on
    disk, which is what arm_start_band does later."""
    if lt is None:
        return None
    info_hash = (info_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
        return None

    existing = _torrents.get(info_hash)
    if existing is not None:
        # Re-pick the file for what is being asked for *now*. Returning
        # the torrent untouched kept the previous request's file_index,
        # and for a season pack that made every later episode serve the
        # first one again: five queued Frieren episodes each found "their"
        # file already complete, reported done instantly, and copied
        # episode 1's file five times - the owner found one video where
        # five were promised. Same staleness served the wrong episode to
        # the player when switching within a held pack.
        existing.last_touched = time.time()
        if start_at is not None:
            existing.set_start_seconds(start_at)
        try:
            wanted = (file_index if file_index is not None
                      else _pick_file(existing.info, season, episode)
                      if (season and episode) else existing.file_index)
            if wanted is not None and wanted != existing.file_index:
                existing.file_index = wanted
                priorities = [0] * existing.info.files().num_files()
                priorities[wanted] = 7
                existing.handle.prioritize_files(priorities)
                # See _settle_file_priorities: without this the focus()
                # below is overwritten a second later by the file
                # priorities being applied.
                _settle_file_priorities(existing.handle,
                                        existing.piece_at(0),
                                        existing.piece_at(
                                            max(existing.file_size() - 1, 0)))
                existing.focus(0, HEAD_BYTES)
        except Exception:
            pass
        return info_hash

    os.makedirs(_CACHE_DIR, exist_ok=True)
    # A new torrent is about to write to disk; this is the moment to
    # check the scratch has not run away. Throttled and off-thread -
    # see maybe_trim for the full-disk measurement that put it here.
    maybe_trim()
    magnet = "magnet:?xt=urn:btih:" + info_hash
    for tracker in trackers or ():
        tracker = str(tracker)
        # Indexers hand these over prefixed the way Stremio's addon
        # protocol writes them.
        if tracker.startswith("tracker:"):
            tracker = tracker[len("tracker:"):]
        elif tracker.startswith("dht:"):
            continue
        magnet += "&tr=" + urllib.parse.quote(tracker, safe="")

    try:
        params = lt.parse_magnet_uri(magnet)
        params.save_path = _CACHE_DIR
        params.flags |= lt.torrent_flags.sequential_download
        # **Left auto-managed, and that was measured.** Clearing the
        # flag looked right - every torrent added here is one somebody
        # is waiting on, and the lifecycle is managed by prepare_fastest
        # and _reap rather than by libtorrent's queue - and it broke
        # playback outright: with it cleared, **none** of six test
        # episodes reached a playable url inside 60s, where five of six
        # did with it left alone (measured 22 August 2026). The queue
        # limits in _make_session are the half of this that works.
        handle = session().add_torrent(params)
    except Exception:
        return None

    torrent = _Torrent(handle, info_hash)
    _torrents[info_hash] = torrent

    # Metadata first - until it arrives there is no file list, so
    # nothing can be chosen or served.
    deadline = time.time() + metadata_timeout
    while time.time() < deadline:
        try:
            if handle.status().has_metadata:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        return None

    # Deliberately NOT sequential: `focus()` expresses the read position
    # as piece priorities, and sequential mode overrides them by pinning
    # the picker to the front of the file - which is what broke seeking.
    # The initial `focus(0, HEAD_BYTES)` below gives the same
    # start-from-the-beginning behaviour without that side effect.
    try:
        handle.set_sequential_download(False)
    except Exception:
        pass

    info = handle.torrent_file()
    torrent.file_index = (file_index if file_index is not None
                          else _pick_file(info, season, episode))
    # Before the first focus(), so the very first priority array already
    # carries the resume band rather than adding it a second later.
    if start_at is not None:
        torrent.set_start_seconds(start_at)

    # Everything except the chosen file is deprioritised to zero - a
    # season pack is a dozen episodes and fetching all of them to watch
    # one is the difference between starting now and starting later.
    try:
        priorities = [0] * info.files().num_files()
        priorities[torrent.file_index] = 7
        handle.prioritize_files(priorities)
        # **Wait for that to land before saying anything about pieces.**
        # It is applied about a second late and rewrites every piece of
        # the file to 7, which is the whole of focus() undone - see
        # _settle_file_priorities for the read-back that showed it.
        _settle_file_priorities(handle, torrent.piece_at(0),
                                torrent.piece_at(max(torrent.file_size() - 1, 0)))
    except Exception:
        pass

    torrent.focus(0, HEAD_BYTES)
    return info_hash


def _file_priorities(handle) -> list:
    """The handle's file priorities, whichever name this binding gives
    the getter."""
    for name in ("get_file_priorities", "file_priorities"):
        getter = getattr(handle, name, None)
        if callable(getter):
            return list(getter())
    return []


def _want_pieces(torrent, wanted_indexes):
    """Set the piece priorities to fetch exactly these files in full: 7
    across every wanted file's range, 0 everywhere else.

    Piece priorities are the layer that actually gates the picker - file
    priorities alone are a bulk way of writing them, and a later
    prioritize_pieces overwrites what they said. The old download path
    set *every* piece to 7 after zeroing the other files, which quietly
    re-wanted them all: downloading one episode out of a 28-episode pack
    fetched the entire pack, with the wanted file competing against 27
    others for the same swarm."""
    info = torrent.info
    files = info.files()
    length = info.piece_length()
    priorities = [0] * info.num_pieces()
    for index in wanted_indexes:
        offset = files.file_offset(index)
        size = files.file_size(index)
        if size <= 0:
            continue
        for piece in range(int(offset // length),
                           int((offset + size - 1) // length) + 1):
            priorities[piece] = 7
    torrent.handle.prioritize_pieces(priorities)


def download_whole(info_hash: str, *, all_files: bool = False) -> bool:
    """Switch a torrent from streaming to fetching the chosen file whole.

    Streaming deliberately fetches a narrow band around the read
    position and leaves the rest of the file at priority 1, which is
    right for watching and wrong for keeping: the file on disk is full
    of holes. This raises the chosen file - or every video file in the
    torrent, with all_files - to full priority so it completes, and
    wants nothing else (see _want_pieces for the bug the old
    every-piece-at-7 version had)."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return False
    # From here this torrent belongs to a download, not to the player -
    # see pin(). want_whole also stops focus() from rewriting the piece
    # priorities set below: the streaming window would zero every piece
    # the download is trying to fetch.
    pin(info_hash)
    torrent.want_whole = True
    try:
        info = torrent.info
        count = info.files().num_files()
        if all_files:
            wanted = [i for i in range(count)
                      if str(info.files().file_path(i)).lower()
                      .endswith(_VIDEO_SUFFIXES)]
        else:
            wanted = [torrent.file_index]
        priorities = [0] * count
        for index in wanted:
            priorities[index] = 7
        torrent.handle.prioritize_files(priorities)
        _want_pieces(torrent, wanted)
        return True
    except Exception:
        return False


def file_index_for(info_hash: str, season, episode):
    """The file in an already-held torrent whose name states this
    episode, or None - None also when the torrent isn't held or has no
    metadata. This is how the download queue asks "can the season pack I
    already have serve the next episode" without paying another source
    lookup; only a name match counts, because handing back the largest
    file here would recreate the very copied-the-wrong-episode bug this
    exists to avoid."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return None
    try:
        return _episode_file_index(torrent.info, season, episode)
    except Exception:
        return None


def raise_files(info_hash: str, indexes) -> bool:
    """Additionally want these files, keeping everything already wanted.
    The download queue calls this once per season job with every sibling
    episode's file, so the whole group rides one swarm together instead
    of each job re-warming it from zero - the pieces the later jobs need
    are arriving while the first one is still being tracked."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or not indexes:
        return False
    torrent.want_whole = True
    try:
        current = _file_priorities(torrent.handle)
        if not current:
            current = [0] * torrent.info.files().num_files()
        for index in indexes:
            if 0 <= int(index) < len(current):
                current[int(index)] = max(current[int(index)], 7)
        torrent.handle.prioritize_files(current)
        _want_pieces(torrent,
                     [i for i, p in enumerate(current) if p > 0])
        return True
    except Exception:
        return False


def file_progress(info_hash: str) -> dict:
    """How far along a download is: bytes, fraction, rate, and where the
    finished file will be."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.file_index is None:
        return {}
    try:
        status = torrent.handle.status()
        wanted = torrent.file_size()
        done = 0
        try:
            done = torrent.handle.file_progress()[torrent.file_index]
        except Exception:
            done = int(status.progress * wanted)
        return {"done": int(done), "total": int(wanted),
                "fraction": (done / wanted) if wanted else 0.0,
                "rate": int(status.download_rate),
                "peers": int(status.num_peers),
                "path": torrent.file_path(),
                "name": os.path.basename(torrent.file_path()),
                "finished": wanted > 0 and done >= wanted}
    except Exception:
        return {}


def peers(info_hash: str) -> int:
    torrent = _torrents.get(info_hash)
    if torrent is None:
        return 0
    try:
        return int(torrent.handle.status().num_peers)
    except Exception:
        return 0


def stats(info_hash: str) -> dict:
    torrent = _torrents.get(info_hash)
    if torrent is None:
        return {}
    try:
        status = torrent.handle.status()
        return {"peers": status.num_peers, "seeds": status.num_seeds,
                "download_rate": status.download_rate,
                "progress": status.progress, "name": status.name}
    except Exception:
        return {}


def has_data(info_hash: str, wait: float = 20.0) -> bool:
    """Whether the opening of the file is actually arriving.

    Peers alone are not proof - a swarm can connect and never unchoke -
    so this waits for the first piece of the chosen file, which is the
    thing playback genuinely needs."""
    torrent = _torrents.get(info_hash)
    if torrent is None or torrent.file_index is None:
        return False
    return torrent.wait_for(torrent.piece_at(0), timeout=wait)


# How long to spend fetching the container's seek index before the
# player is given a URL. Bounded, and small: this is time the user is
# already watching a loading frame for, and going over budget means
# handing mpv the URL anyway - the index band stays priority 7, so it
# keeps arriving, it just is not waited on any longer.
#
# **3.0, down from 6.0, measured 22 August 2026.** With the streaming
# window in place the index is no longer racing 164 other pieces: two
# runs that did not wait for it at all (INDEX_WAIT 0.5) still had it on
# disk by the time the demuxer asked - the tail read was served with a
# time-to-first-byte of 0.01s and 0.32s. So the wait is now a safety
# margin for a slow swarm rather than the normal path, and six seconds
# of it was 1.5-2.5s of pure loading screen. It is also no longer
# *added* to the wait for the first piece - see await_start.
INDEX_WAIT = 3.0

# The part of the tail the demuxer provably reads, and therefore the
# only part worth *waiting* for. Measured: mpv seeked to size-150KB and
# size-50KB on the two releases timed. The rest of TAIL_BYTES stays
# priority 7 and lands on its own.
INDEX_CRITICAL_BYTES = 1024 * 1024

# How long to keep trying to read the Cues for a resume point. Off the
# critical path - the player already has its url by then - and short
# enough that it cannot outlive the seek it exists to serve.
ARM_RETRY_S = 8.0

# **Resuming waits for the index rather than hoping for it.** The resume
# offset is read out of the Cues, so nothing can be primed until they
# are on disk; with the ordinary INDEX_WAIT they routinely arrived a
# quarter of a second before mpv drew its first frame and asked for the
# seek, which is no time at all to fetch anything. Measured on Frieren
# S01E02: armed at 7.49s, first picture at 7.74s, and the seek still
# cost 3.51s.
RESUME_INDEX_WAIT = 6.0

# ...and then waits for the band itself, briefly. This is the whole
# trade: a second or two more of the loading screen the viewer is
# already looking at, against the same seconds spent frozen on a frame
# they did not ask for after the "Resumed From" toast - which is what
# the owner actually reported.
RESUME_BAND_WAIT = 3.0


def set_start_seconds(info_hash: str, seconds) -> bool:
    """Tell a torrent already added where playback is going to start,
    for a caller that learns the resume point after add(). Resolving it
    to a byte happens in arm_start_band."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return False
    return torrent.set_start_seconds(seconds)


def arm_start_band(info_hash: str):
    """Resolve the resume time to a byte and start wanting it. Returns
    the offset, or None when it could not be resolved.

    Called once the player has its url, and not before, for two reasons:
    the Cues this reads are only on disk by then, and until the url is
    handed over the only things worth wanting are the first piece and
    the index - everything else wanted competes with them."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.start_seconds is None:
        return None

    def arm():
        offset = torrent.resolve_start(torrent.start_seconds)
        if offset is None:
            return False
        torrent.start_offset = int(offset)
        torrent.start_armed = True
        torrent.focus(0, HEAD_BYTES)    # rewrite the array with it in
        return True

    if arm():
        return torrent.start_offset
    # **Not resolvable yet, and that is normal.** await_start only
    # guarantees the last INDEX_CRITICAL_BYTES of the file, and the Cues
    # element routinely starts before that - measured on Frieren S01E02,
    # where it begins 165KB from the end but the piece holding its front
    # had not landed when the url was handed over. Retrying costs
    # nothing: it is off the critical path by construction (the player
    # already has its url) and the tail band is priority 7, so the bytes
    # are on their way.
    def keep_trying():
        deadline = time.time() + ARM_RETRY_S
        while time.time() < deadline:
            if _torrents.get(torrent.info_hash) is not torrent:
                return
            if arm():
                return
            time.sleep(0.25)
    threading.Thread(target=keep_trying, daemon=True,
                     name="atomic-torrent-arm").start()
    return None


def await_start_band(info_hash: str, wait: float = None) -> bool:
    """Wait, briefly, for the piece playback is going to start on.

    Only makes sense after arm_start_band has resolved an offset. Fails
    soft: over budget hands the url over anyway and the band keeps
    arriving at priority 7."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.start_seconds is None:
        return False
    if wait is None:
        wait = RESUME_BAND_WAIT
    deadline = time.time() + max(0.0, wait)
    # One budget covering both halves: arm_start_band retries in the
    # background when the Cues were not readable yet (measured taking
    # anywhere from 5.6s to over 6s on the same release), so waiting
    # only for the *band* would skip the case where the offset itself is
    # still a second away.
    while torrent.start_offset is None and time.time() < deadline:
        if _torrents.get(torrent.info_hash) is not torrent:
            return False
        time.sleep(0.05)
    if torrent.start_offset is None:
        return False
    piece = torrent.piece_at(torrent.start_offset)
    while time.time() < deadline:
        if torrent.have(piece):
            return True
        if _torrents.get(torrent.info_hash) is not torrent:
            return False
        time.sleep(0.05)
    return torrent.have(piece)


def await_start(info_hash: str, data_wait: float = 20.0,
                index_wait: float = None):
    """Wait for both things playback is blocked on, **at the same time**.

    Returns `(has_data, has_index)`.

    This used to be `has_data()` followed by a separate wait for the
    index, which is
    two waits in a row for two things that arrive in parallel - both
    bands are priority 7 from the moment the torrent is added, so
    waiting for one and then the other only adds up the *waiting*.
    Measured 22 August 2026 on Frieren S01E02 (EMBER): 3.14s for the
    first piece plus 6.01s for the index, 9.15s before the player was
    handed a url. The same two things, waited on together, cost the
    longer of them.

    `has_data` False means the swarm is not answering and the caller
    should try another release. `has_index` False is not a failure: the
    url is handed over anyway and the index band stays priority 7, which
    is exactly what happened before any of this existed."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.file_index is None:
        return False, False
    if index_wait is None:
        index_wait = INDEX_WAIT
    first_piece = torrent.piece_at(0)
    started = time.time()
    data_deadline = started + max(0.0, data_wait)
    index_deadline = started + max(0.0, index_wait)
    got_data = got_index = False
    while True:
        if not got_data:
            got_data = torrent.have(first_piece)
        if not got_index:
            got_index = not torrent._tail_pieces(INDEX_CRITICAL_BYTES)
        if got_data and got_index:
            break
        now = time.time()
        # No data and out of time: this release is dead, say so now
        # rather than spending the index budget on it as well.
        if not got_data and now >= data_deadline:
            break
        if got_data and now >= index_deadline:
            break
        # The player released this torrent (moved on, or the race picked
        # somebody else) - stop waiting on it at once.
        if _torrents.get(torrent.info_hash) is not torrent:
            break
        try:
            torrent.handle.set_piece_deadline(first_piece, 200)
        except Exception:
            pass
        time.sleep(0.05)
    return got_data, got_index


# ------------------------------------------------------------ http side

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # silence; this is not a web server
        pass

    def _torrent(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if not parts:
            return None
        return _torrents.get(parts[0].lower())

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only=False):
        torrent = self._torrent()
        if torrent is None or torrent.file_index is None:
            self.send_error(404)
            return
        size = torrent.file_size()
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header:
            match = re.match(r"bytes=(\d*)-(\d*)", header.strip())
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
        start = max(0, min(start, max(size - 1, 0)))
        if end < start:
            end = size - 1
        length = end - start + 1

        self.send_response(206 if header else 200)
        self.send_header("Content-Type", "video/x-matroska"
                         if torrent.file_path().lower().endswith(".mkv")
                         else "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        # A seek is just a Range that starts somewhere else; re-aim the
        # download at it rather than waiting for the gap to fill in.
        # Under this connection's own key, not the shared one: mpv reads
        # the header on one connection while another goes looking for
        # the Cues, and one window per reader is what stops them zeroing
        # each other's pieces.
        reader = id(self)
        torrent.focus(start, reader=reader)

        piece_length = torrent.piece_length()
        offset = start
        remaining = length
        path = torrent.file_path()
        # Where the window was last pointed. mpv opens one open-ended
        # Range and reads the rest of the file down it, so without this
        # the window only ever moved when a piece was *missing* - that
        # is, after playback had already caught up with the download and
        # stalled. Sliding it while there is still buffer in hand is the
        # difference between "the loading while playing is slow" and not
        # noticing there is a download at all.
        focused_at = start
        cached_piece, cached_bytes = None, None
        try:
            while remaining > 0:
                if offset - focused_at >= STREAM_REFOCUS_BYTES:
                    focused_at = offset
                    torrent.focus(offset, reader=reader)
                piece = torrent.piece_at(offset)
                if not torrent.have(piece):
                    focused_at = offset
                    torrent.focus(offset, reader=reader)
                    if not torrent.wait_for(piece):
                        return          # client sees a short read; mpv retries
                # Read no further than the end of the piece we know is
                # present, so a partially written next piece is never
                # handed over as real data.
                piece_end = ((torrent.file_offset() + offset) // piece_length
                             + 1) * piece_length - torrent.file_offset()
                chunk = int(min(remaining, max(piece_end - offset, 1)))
                # **Through libtorrent, not through the file.** A piece
                # reads back as zeros from the file for up to half a
                # second after have_piece() says it is there, and those
                # zeros were being served as video - see
                # _Torrent.read_piece for the measurement. Cached for
                # the life of this request because a 4MB piece is served
                # in several writes and asking for it once per write
                # would be several copies of the same read.
                if cached_piece != piece:
                    blob = torrent.read_piece(piece)
                    if blob is not None:
                        cached_piece, cached_bytes = piece, blob
                data = None
                if cached_piece == piece and cached_bytes is not None:
                    piece_start = piece * piece_length - torrent.file_offset()
                    begin = offset - piece_start
                    if 0 <= begin < len(cached_bytes):
                        data = cached_bytes[begin:begin + chunk]
                if data is None:
                    # libtorrent could not answer - fall back to the
                    # file, which is what always happened before.
                    try:
                        with open(path, "rb") as handle:
                            handle.seek(offset)
                            data = handle.read(chunk)
                    except (FileNotFoundError, OSError):
                        time.sleep(0.2)
                        continue
                if not data:
                    time.sleep(0.1)
                    continue
                self.wfile.write(data)
                offset += len(data)
                remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                OSError):
            # The player moved on - normal, and it happens on every
            # single seek. **ConnectionAbortedError is the one Windows
            # actually raises** (WinError 10053) and it was not caught:
            # measured 22 August 2026, every abandoned range request
            # printed a socketserver traceback to the console and killed
            # its handler thread. OSError covers the rest of the winsock
            # family for the same reason - nothing this handler can do
            # about a client that stopped listening is worth an
            # exception escaping into the server loop.
            return
        finally:
            # Give the window back. A dead reader's window would go on
            # holding 48MB of the file wanted for the rest of the
            # session, and every abandoned seek leaves one.
            try:
                torrent.end_read(reader)
            except Exception:
                pass


def start_server():
    """The local HTTP endpoint, started once, on a free port."""
    global _server, _server_port
    if _server is not None:
        return _server_port
    if lt is None:
        return None
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    except Exception:
        return None
    _server_port = _server.server_address[1]
    thread = threading.Thread(target=_server.serve_forever, daemon=True,
                              name="atomic-torrent-http")
    thread.start()
    return _server_port


def stream_url(info_hash: str):
    port = start_server()
    if port is None or info_hash not in _torrents:
        return None
    return f"http://127.0.0.1:{port}/{info_hash}/0"


# Torrents a *download* is using. The player releases what it has
# finished with (see player.close_player) and the stream race releases
# every loser, and both of those would otherwise be able to take a
# download's torrent out from under it - the session is shared, and
# add() hands back the existing torrent when the hash matches.
_pinned = set()


def pin(info_hash: str):
    """Mark a torrent as somebody's download, so an ordinary release
    leaves it alone."""
    _pinned.add((info_hash or "").lower())


def release(info_hash: str, delete_files: bool = False, force: bool = False):
    """Stop a torrent. Called when the player moves on, and by the
    download queue when a job ends (which passes force, since the pin
    is its own).

    **Releasing at all is new, on the player's side, and it is a fix.**
    Nothing ever removed the torrent that had just been watched: it kept
    downloading and announcing for the life of the process, so a session
    of six episodes ended with six swarms competing for the connection
    with whatever was playing. Measured 22 August 2026 - and with
    libtorrent's queue limit at its old 8, the ninth was simply paused,
    which is indistinguishable from a dead swarm (see _make_session)."""
    key = (info_hash or "").lower()
    if key in _pinned and not force:
        return
    _pinned.discard(key)
    torrent = _torrents.pop(key, None)
    if torrent is None or lt is None:
        return
    try:
        flags = lt.session.delete_files if delete_files else 0
        session().remove_torrent(torrent.handle, flags)
    except Exception:
        pass


def clear_cache():
    """Delete everything downloaded. The cache is scratch space - a
    watched episode is not a library."""
    for info_hash in list(_torrents):
        release(info_hash)
    shutil.rmtree(_CACHE_DIR, ignore_errors=True)


def cache_size_bytes() -> int:
    total = 0
    for root, _dirs, files in os.walk(_CACHE_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
