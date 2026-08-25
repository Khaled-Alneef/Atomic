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

# For reading an episode number out of a file name inside a pack - the
# same parser the source list uses, so the two cannot disagree about
# what "episode 2" means. Imported defensively for the same reason
# streams.py does it: a missing parser means the older, weaker filename
# rule, not an engine that will not start.
try:
    from . import indexers
except Exception:                                       # pragma: no cover
    indexers = None

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

# **Fetched metadata is kept on disk, keyed by info hash.** Fetching it
# from the swarm is the single most variable cost on the winner's
# critical path - measured 22 August 2026 across the owner's three test
# titles, the release that eventually won paid 0.7-5.8s for metadata
# alone, and the race retries the same top-ranked releases on every
# attempt (a re-pressed episode, the next episode of the same season
# pack, a retry after a stall). A .torrent file is a few hundred KB at
# most, reading it back is instant, and with it the episode check, the
# tiny-file check and the dead-swarm clock all start at t=0 instead of
# t=metadata. Written atomically (tmp + replace) so a killed process
# cannot leave a truncated file that parses as garbage; a file that
# fails to parse is ignored and the magnet path runs as before.
_METADATA_DIR = os.path.join(_CACHE_DIR, "metadata")


# **The DHT routing table is kept across runs.** Every launch used to
# bootstrap from zero - the prewarm() measurement below records 0 nodes
# at session birth and ~50 at 2.44s, and until that table exists the
# first magnet has nobody to ask for peers. Harbor persists its table
# for the same reason (dht.json, src-tauri/src/torrent_engine.rs tier 1).
# Measured 23 August 2026 on the owner's connection: restoring the state
# a 25-second-old session had saved put **71 nodes in the table at 1.0s
# against 9 cold** (72 vs 30 at 3.0s). The file is ~1.5KB, written
# atomically, and a file that fails to parse means a cold bootstrap -
# exactly what always happened before.
_SESSION_STATE_PATH = os.path.join(_CACHE_DIR, "session_state.bin")

# A table smaller than this is not worth writing over what is already
# on disk - saving the first seconds of a bootstrap would *replace* a
# mature table with a nearly-empty one and make the next launch colder.
_STATE_MIN_NODES = 40
_STATE_SAVE_INTERVAL_S = 300.0
_state_saved_at = 0.0


def _restored_session_params():
    """Last run's session state (DHT table included), or fresh params."""
    try:
        with open(_SESSION_STATE_PATH, "rb") as fh:
            return lt.read_session_params(fh.read())
    except Exception:
        return lt.session_params()


def _maybe_save_session_state():
    """Write the session state to disk, throttled, and only when the
    DHT table is worth keeping. Called from the window ticker, so a
    session that streams anything at all saves within a second of its
    table maturing, and every five minutes after."""
    global _state_saved_at
    now = time.time()
    if now - _state_saved_at < _STATE_SAVE_INTERVAL_S:
        return
    current = _session
    if current is None or lt is None:
        return
    try:
        # One synchronous status() per tick until the table matures,
        # none for the interval after each save. status() is a call
        # into the session's own thread, which is why this is not done
        # more often than the ticker already runs.
        if int(current.status().dht_nodes) < _STATE_MIN_NODES:
            return
        buf = lt.write_session_params_buf(current.session_state())
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _SESSION_STATE_PATH + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(buf)
        os.replace(tmp, _SESSION_STATE_PATH)
        _state_saved_at = now
    except Exception:
        # Throttle failures like successes - a session that cannot
        # answer once should not be asked again every second.
        _state_saved_at = now


def _metadata_path(info_hash: str) -> str:
    return os.path.join(_METADATA_DIR, info_hash + ".torrent")


def _cached_torrent_info(info_hash: str):
    """The torrent_info a previous session already paid the swarm for,
    or None."""
    try:
        path = _metadata_path(info_hash)
        if not os.path.isfile(path):
            return None
        return lt.torrent_info(path)
    except Exception:
        return None


def _save_torrent_info(info_hash: str, handle):
    try:
        info = handle.torrent_file()
        if info is None:
            return
        data = lt.bencode(lt.create_torrent(info).generate())
        os.makedirs(_METADATA_DIR, exist_ok=True)
        path = _metadata_path(info_hash)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        pass

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
# How far past the reader `_apply_windows` may scan for the first piece
# that is still missing, as a multiple of the window. The scan is what
# stops a satisfied window from asking the swarm for nothing (see
# _apply_windows); the bound is what stops a nearly-complete file from
# turning every re-aim into a walk of the whole piece array. 0 restores
# the old anchored-to-the-reader behaviour, which is how the A/B in
# _apply_windows' comment was run.
WINDOW_SKIP_FACTOR = 4

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
    # Built from last run's saved state so the DHT table starts warm -
    # see _SESSION_STATE_PATH for the 71-vs-9-nodes-at-1s measurement.
    # The settings above always win over whatever was serialized.
    params = _restored_session_params()
    params.settings = settings
    session = lt.session(params)
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


# How often every streaming torrent's window is re-applied. Short enough
# that a window cannot sit satisfied long enough for libtorrent to hang
# up on the swarm (measured taking about a second and a half to start
# dropping peers), long enough that rebuilding a 20,000-entry priority
# array is nothing - _serve already does it once per 8MB served.
WINDOW_TICK_S = 1.0


def _window_ticker():
    """Keep every streaming torrent's window pointed at something that
    is actually missing. See _Torrent.refresh_windows for what this is
    for and what it was measured costing when it did not exist."""
    while True:
        time.sleep(WINDOW_TICK_S)
        if session() is None:
            return
        # Rides the ticker rather than owning a thread: the throttle
        # inside means one status() probe per tick at most, and a save
        # every five minutes - see _maybe_save_session_state.
        _maybe_save_session_state()
        for torrent in list(_torrents.values()):
            # A download owns its own priorities (see download_whole);
            # a torrent with no chosen file has nothing to point at.
            if torrent.want_whole or torrent.file_index is None:
                continue
            try:
                torrent.refresh_windows()
            except Exception:
                pass


def session():
    global _session, _pump_started
    with _session_lock:
        if _session is None and lt is not None:
            _session = _make_session()
            if not _pump_started:
                _pump_started = True
                threading.Thread(target=_alert_pump, daemon=True,
                                 name="atomic-torrent-alerts").start()
                threading.Thread(target=_window_ticker, daemon=True,
                                 name="atomic-torrent-window").start()
        return _session


# How much watched-but-not-kept video to leave lying around. Streaming
# writes every episode to disk and nothing ever removed it: a single
# session of testing left 24 files and ~10GB behind, and it only ever
# grew. A download the user actually asked for is copied to their own
# folder, so everything in here is scratch by definition.
CACHE_LIMIT_BYTES = 6 * 1024 * 1024 * 1024


def _allocated_size(path, stat_result=None) -> int:
    """How much disk a file actually occupies, not how big it claims to
    be. Falls back to the apparent size when it cannot be asked.

    **Streaming writes sparse files, and measuring them by `st_size`
    emptied the cache every time it was looked at.** libtorrent creates
    the whole file up front and fills in the pieces that arrive, so a
    20GB pack holding 50MB of downloaded pieces *reports* 20GB. The old
    code knew `st_size` over-reported and called that "the safe
    direction - it only makes trimming more eager, never less"; eager is
    not safe when it is wrong by two orders of magnitude.

    Measured 23 August 2026 on the owner's own scratch (22 files):
    **10.13 GB apparent against 0.32 GB actually allocated** - one 2.21GB
    file occupied 0.000GB. Against a 6GB limit that reads as 169% full
    when the truth is 5%, so every `maybe_trim` deleted almost the whole
    cache, oldest-first. That is why on-disk pieces vanished between
    sessions and a re-watched episode lost its instant start (a Demon
    Slayer batch had been winning in ~6s off a 380MB recheck).

    `GetCompressedFileSizeW` is the Windows answer - it reports the
    allocated size for sparse *and* compressed files. On POSIX
    `st_blocks` already says it. INVALID_FILE_SIZE is a legitimate low
    word for a file over 4GB, so the error is read rather than the value
    guessed at."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCompressedFileSizeW.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
            high = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            low = kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
            if low != 0xFFFFFFFF or ctypes.get_last_error() == 0:
                return (high.value << 32) | low
        except Exception:
            pass                # fall through to the apparent size
    try:
        stat_result = stat_result or os.stat(path)
        blocks = getattr(stat_result, "st_blocks", None)
        if blocks is not None:
            return int(blocks) * 512
        return int(stat_result.st_size)
    except OSError:
        return 0


def trim_cache(limit: int = CACHE_LIMIT_BYTES) -> int:
    """Delete the least recently used scratch files until under `limit`.

    Returns the bytes freed. Oldest-touched first, and never a file
    belonging to a torrent this session still has open - that would be
    deleting the thing currently playing.

    Sizes are what the files actually *occupy* (see _allocated_size), not
    what they claim - the difference between the two is 10.13GB and
    0.32GB on the owner's real cache, and measuring the wrong one made
    this function delete nearly everything every time it ran."""
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
            # What it occupies, not what it claims - see _allocated_size
            # for the 10.13GB-vs-0.32GB measurement that made this the
            # difference between trimming sanely and wiping the cache.
            size = _allocated_size(path, stat)
            entries.append((stat.st_atime, size, path))
            total += size
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
        # The highest file offset ever handed to a reader. Bytes below it
        # have been served once already, which is the proof _serve needs
        # to read them straight from the file - see the note there.
        self.served_hwm = 0
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
        # **True until the first piece of the chosen file exists.** While
        # warming, _apply_windows narrows the want-window to the urgent
        # band alone, so the whole swarm lands on the piece playback is
        # blocked on instead of being spread across the full window.
        # Measured 22 August 2026 across the owner's three test titles:
        # the big-piece packs (a 16MB-piece batch is normal for a
        # multi-season BD release) pulled 1-74MB at 0.2-9MB/s and still
        # had no complete first piece when their lane's budget ran out,
        # in 2-7 lanes of nearly every run - a 12-piece window of 16MB
        # pieces is 192MB of equally-wanted data, and at a few MB/s
        # every piece fills evenly and none finishes. Cleared the moment
        # the first piece lands (await_start) or a real HTTP read starts
        # (_serve); the band anchors to the first *missing* piece, so a
        # narrowed window can never go empty and idle the swarm.
        self.warming = True

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

    def refresh_windows(self):
        """Re-apply the current windows without moving them.

        **A priority array goes stale the moment its window fills up**,
        and nothing was re-running it. `focus()` is called by a reader -
        when it starts, when it advances STREAM_REFOCUS_BYTES, and when
        it hits a missing piece - so a reader that is blocked, or one
        whose 48MB window arrived in full, leaves libtorrent holding an
        array in which every wanted piece is already on disk. It then has
        nothing to ask anyone for and drops the swarm.

        Measured 22 August 2026 on Demon Slayer S01E05, with the
        skip-what-is-present window already in place: peers climbed to 90
        while the head arrived, then went to **0 at 28.2s** and stayed
        there, and mpv's read of the file's opening - which had sent only
        4.5MB - did not complete until **42.8s**. The window fix alone
        cannot help there, because the only thing that recomputes the
        window is the reader that is stuck.

        So the window is re-applied on a timer as well (see
        _window_ticker). Same windows, same readers - it just re-asks the
        question "what is still missing", which is now what the window is
        measured from."""
        with self.lock:
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
            # **A full window used to go idle, and that was not safe** -
            # peers dropped to 0 and took ten to twenty seconds to come
            # back, which is longer than the reader can wait. The window
            # is now measured from the first piece still missing, so it
            # always holds something to ask for; see the scan below for
            # the measurement.
            window_pieces = max(STREAM_WINDOW_MIN_PIECES,
                                -(-STREAM_WINDOW_BYTES // self.piece_length()))
            # Until the first piece exists there is nothing to play, so
            # nothing beyond the urgent band is worth wanting - see
            # `warming` in __init__ for the 74MB-and-no-first-piece
            # measurement behind this.
            if self.warming:
                window_pieces = urgent_count

            priorities = [0] * total_pieces
            urgent_bands = []
            for offset, _span, _seq in windows:
                start = max(file_first, self.piece_at(offset))
                # **Skip what is already on disk, and this is the single
                # biggest thing measured on 22 August 2026.**
                #
                # The window was anchored to the reader's byte offset,
                # so once its ~48MB had all arrived it contained nothing
                # missing - and with every other piece of the file at
                # priority 0, libtorrent then had nothing to ask anyone
                # for and **disconnected the entire swarm**. Measured on
                # Frieren S01E02 (EMBER, 4.19MB pieces): peers climbed
                # 20 -> 78 while the head was arriving, and 0.3s after
                # the head read finished went to **0 peers, 0 seeds, and
                # stayed there**. The demuxer's next read was the last
                # piece of the file, which nothing was fetching any
                # more, so it blocked for **20.0s** waiting for a swarm
                # that had been thrown away - open cost 35.2s on a
                # release whose head had arrived in seven. House of the
                # Dragon S01E05 was worse: still 0 peers at 88s, no
                # picture.
                #
                # The comment above used to record "the window goes idle
                # once it is full (peers drop to 0 - measured, and
                # expected)" and treat that as safe. It is not safe: the
                # peers do not come back for ten to twenty seconds, and
                # the reader routinely needs a piece before then.
                #
                # A window of *missing* pieces is what was meant all
                # along - bounded exactly as before, just measured from
                # the first piece the reader still needs rather than
                # from where it happens to be sitting. The scan is
                # bounded so a nearly-complete file cannot turn this
                # into a walk of the whole piece array.
                scanned, limit = 0, window_pieces * WINDOW_SKIP_FACTOR
                while start < file_last and scanned < limit and self.have(start):
                    start += 1
                    scanned += 1
                # The urgent band is `urgent_count` pieces from the first
                # one still missing, and is deliberately no longer
                # clamped to the reader's own `span`. That clamp never
                # bound anything - the two spans callers pass are
                # READAHEAD_BYTES (24MB) and HEAD_BYTES (12MB), both
                # larger than URGENT_BYTES - and once the scan above can
                # move `start` past the end of the span it would clamp
                # the band down to a *single* piece, which is the one
                # shape this whole scheme exists to avoid.
                band_last = min(file_last, start + urgent_count - 1)
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


def _stem(name: str) -> str:
    """A file's own name, without directory or extension. The extension
    has to go before the name is parsed: `... - 02.mkv` has no boundary
    after the `02` while the `.mkv` is attached, so the fansub numbering
    below reads as nothing at all."""
    return os.path.splitext(os.path.basename(name or ""))[0]


def _path_seasons(name: str) -> set:
    """Every season this file's own **directories** name.

    **The season of a file in a big pack is written on the folder, not
    on the file, and this app was throwing that away.** Measured 25
    August 2026 on `[Anime Time] Attack On Titan (Complete Collection)
    (S01-S04+OVA+Movies+Junior High)`, 131 videos, asked for S01E01:

        30 files  Attack on Titan Season 4
        25 files  Attack on Titan Season 1        <- where S01E01 lives
        22 files  Attack on Titan Season 3
        12 files  Attack On Titan Junior High     <- a different show
        12 files  Attack on Titan Season 2
         8 files  Attack On Titan OAD

    and **not one file stem states a season at all** - they are numbered
    absolutely (`Attack on Titan - 01`, `- 26`, `- 38`, `- 60`), so a
    bare "01" appears in Season 1, in Junior High and in the OADs.
    `_stem` reduced every path to its basename before judging it, which
    deleted the only evidence there was. That is the owner's "when I
    played aot s1 ep 1 then ep 2 both played episodes from s4".

    The torrent's own root folder is deliberately **not** read: it is the
    release title and names every season the pack holds, so counting it
    would make every file claim all of them."""
    parts = str(name or "").replace("/", "\\").split("\\")
    if len(parts) < 3:
        return set()            # no per-file directory to learn from
    if indexers is None:
        return set()
    found = set()
    for directory in parts[1:-1]:
        try:
            found |= indexers.stated_seasons(directory)
        except Exception:
            continue
    return found


def _names_show(stem: str, title) -> bool:
    """Whether this file's own name carries the show being asked for.

    A **tiebreak only, never a filter**: a fansub file is called
    "[Erai-raws] Shingeki no Kyojin - 01" and would fail this against
    "Attack on Titan", which is a legitimate file for that request. It
    exists to separate two files that both loosely match inside one
    directory - the same Complete Collection has
    "Attack on Titan - 01.mkv" and "Creditless Ending 1.mkv" in its
    Season 1 folder, and both read as episode 1."""
    if not title or indexers is None:
        return False
    try:
        return bool(indexers.is_same_title(str(title), str(stem)))
    except Exception:
        return False


def _names_episode(stem: str, season, episode) -> bool:
    """Whether this file name says it is the episode asked for.

    **`SxxExx` was the only form recognised, and half the packs on these
    indexers do not use it.** Measured 22 August 2026 on Frieren S01E02:
    `[SubsPlease] Sousou no Frieren (01-28) [Batch]` names its files
    `... - 02v2 (1080p) [00DB7386].mkv`, so `_episode_file_index`
    answered None, `_pick_file` fell back to the largest video, and the
    engine served **episode 04**. Both SubsPlease batches in the list did
    it; the packs that name files `S01E02-...` were fine.

    helpers/indexers.py already parses release names for exactly this and
    is the stricter reader of the two (it knows that a season stated in
    words outranks a loose number), so it is asked first and _EPISODE_RE
    is the fallback for a build where that import failed."""
    if indexers is not None:
        try:
            return indexers.episode_match(stem, season, episode) == "exact"
        except Exception:
            pass
    match = _EPISODE_RE.search(stem)
    return bool(match and int(match.group(1)) == int(season)
                and int(match.group(2)) == int(episode))


_EXTRAS_RE = re.compile(r"\b(extras?|specials?|creditless|nc(?:op|ed)|"
                        r"bonus|sample|preview|pv|menu)\b", re.I)
_LOOSE_NUM_RE = re.compile(r"(?:^|[\s\-_\[(])(\d{1,3})(?:v\d)?(?=[\s\-_\])]|$)")


def _loose_numbers(stem: str) -> set:
    """Every plausible episode number a file name states on its own.

    The same reading `_folder_episode_index` counts with, pulled out so
    `_pick_file` can ask the one question that matters when an addon
    points somewhere the names disagree with: *does the file it chose
    claim to be a different episode?*"""
    out = set()
    for found in _LOOSE_NUM_RE.findall(str(stem or "")):
        value = int(found)
        if 0 < value <= 999:
            out.add(value)
    return out


def _folder_episode_index(rows, episode):
    """The file for `episode` inside one season's folder, read from the
    folder's **own numbering**, or None.

    **Season folders in these packs are numbered absolutely, and that is
    the second half of the wrong-episode bug.** Measured 25 August 2026
    on `[Anime Time] Attack On Titan (Complete Collection)`:

        Attack on Titan Season 1    - 01 .. - 25
        Attack on Titan Season 2    - 26 .. - 37
        Attack on Titan Season 3    - 38 .. - 59
        Attack on Titan Season 4    - 60 ..   plus "Season 4 - Finale 1..3"

    so season 2 episode 1 is the file called `- 26`, and asking for a
    file named "01" inside that folder finds nothing at all. Fixing only
    the folder-matching would therefore have left every season past the
    first still falling through to the addon's fileIdx, which is exactly
    the pointer that was wrong.

    The base is found as the **longest contiguous run** of numbers the
    folder's files carry, not as the smallest number in it: the Season 4
    folder also holds "Finale 1", "Finale 2" and "Finale 3", and the
    smallest-number reading would make its episode 1 the finale. The run
    60..86 is twenty-seven long and the run 1..4 is four, so the real
    numbering wins by counting."""
    numbers = {}
    for index, _name, stem, _seasons in rows:
        for found in _LOOSE_NUM_RE.findall(stem):
            value = int(found)
            if 0 < value <= 999:
                numbers.setdefault(value, index)
    if len(numbers) < 2:
        return None, None
    best_start = best_length = 0
    for value in sorted(numbers):
        if value - 1 in numbers:
            continue                    # not the start of a run
        length = 0
        while value + length in numbers:
            length += 1
        if length > best_length:
            best_start, best_length = value, length
    if best_length < 2:
        return None, None
    wanted = best_start + int(episode) - 1
    if wanted >= best_start + best_length:
        return None, best_start         # the folder does not go that far
    return numbers.get(wanted), best_start


def _identify_episode_file(info, season, episode, title=None):
    """`(index, strength)` for the file that holds this episode.

    `strength` is how much the answer can be trusted, and it is what
    lets `_pick_file` decide whether to override an addon's own
    `fileIdx`:

      * "exact"  - the file's name states `SxxExx` for this episode;
      * "folder" - the file sits in a folder naming this season and its
        name names this episode. That is what a Complete Collection
        looks like (see `_path_seasons` for the 131-file measurement);
      * "loose"  - a bare episode number, from a pack whose folders say
        nothing about seasons at all. The weakest reading, and the one
        that used to be applied to everything.

    `(None, None)` means the pack cannot be shown to hold it - which for
    a pack that *does* sort itself into seasons is a real answer, not a
    failure: the race takes the next release rather than serving the
    nearest thing."""
    if not (season and episode):
        return None, None
    season = int(season)
    rows = [(index, name, _stem(name), _path_seasons(name))
            for index, name, _size in _video_files(info)]

    for index, _name, stem, _seasons in rows:
        match = _EPISODE_RE.search(stem)
        if (match and int(match.group(1)) == season
                and int(match.group(2)) == int(episode)):
            return index, "exact"

    # The folder says the season, the file says the episode. Preferring a
    # file that also carries the show's name settles "Attack on Titan -
    # 01.mkv" against "Creditless Ending 1.mkv", which sit in the same
    # folder and both read as episode 1.
    in_season = [row for row in rows if season in row[3]]
    # Extras live under the season they belong to ("Season 1\\Extras"),
    # so they inherit its number and would otherwise compete for the
    # episode - "Creditless Ending 1.mkv" reads as episode 1 exactly as
    # well as "Attack on Titan - 01.mkv" does.
    main = [row for row in in_season if not _EXTRAS_RE.search(row[1])] or in_season
    named = [row for row in main
             if _names_episode(row[2], season, episode)]
    by_folder, base = _folder_episode_index(main, episode) if in_season         else (None, None)
    # **An absolutely-numbered folder outranks a file that merely reads
    # as "1".** Season 4 of the pack above runs 60..86 and *also* holds
    # "Attack on Titan Season 4 - Finale 1", which states season 4 and a
    # loose 1, so the plain name match served the finale for S04E01
    # (measured). Where the folder's own run starts above 1 that run is
    # the numbering, and a file called 1 inside it is something else.
    if by_folder is not None and base and base > 1:
        return by_folder, "folder"
    if named:
        best = next((row for row in named if _names_show(row[2], title)),
                    named[0])
        return best[0], "folder"
    if by_folder is not None:
        return by_folder, "folder"

    # If the pack sorts itself into seasons at all and the asked-for one
    # holds nothing this app can identify, the pack does not hold the
    # episode. Answered rather than guessed: the race takes the next
    # release, which is this codebase's standing trade.
    if any(row[3] for row in rows):
        return None, "absent"

    # No folder anywhere says anything about a season, so a loose number
    # is the only evidence there is.
    for index, _name, stem, _seasons in rows:
        if _names_episode(stem, season, episode):
            return index, "loose"
    return None, None


def _episode_file_index(info, season, episode, title=None):
    """The file that holds this episode, or None - see
    `_identify_episode_file`, which is this plus how sure it is.

    Deliberately no largest-file fallback: callers asking "does this
    pack hold episode N" must get an honest no, not the biggest file
    wearing N's number (see file_index_for)."""
    return _identify_episode_file(info, season, episode, title)[0]


def movie_file_index(info, title):
    """The index of the film `title` inside a multi-video release, or
    None. Delegates to `debrid.movie_file`, which carries the
    measurement and the reasoning - one rule, two call sites, because a
    pack served by the engine is exactly as wrong as one served by
    debrid."""
    videos = _video_files(info)
    if len(videos) <= 1:
        return videos[0][0] if videos else None
    try:
        from . import debrid
    except Exception:
        return None
    chosen = debrid.movie_file(videos, title,
                              name_of=lambda c: c[1], size_of=lambda c: c[2])
    return None if chosen is None else chosen[0]


def _plausible_episode(names, season, episode, title) -> bool:
    """Whether any of `names` can be believed to be this episode of this
    show - see the note in _pick_file for what this exists to stop.

    Deliberately generous about *how* it is satisfied, because both
    halves are common and legitimate: a fansub release states the
    episode and often not the English title ("Shingeki no Kyojin - 01"),
    while a single-episode file may name the show and no number at all
    ("Attack on Titan.mkv"). Either is proof enough; neither is the
    failure case."""
    try:
        from . import indexers
    except Exception:
        return True         # no way to judge: behave as before
    for name in names:
        if not name:
            continue
        try:
            if indexers.episode_match(name, season, episode) is not None:
                return True
            if title and indexers.is_same_title(str(title), name):
                return True
        except Exception:
            return True
    return False


def _pick_file(info, season=None, episode=None, file_index=None, title=None):
    """Which file in the torrent to play, or **None when that cannot be
    answered**.

    Three rules, in this order:

    **A file whose own name states a different episode is not served,
    whoever pointed at it.** `file_index` is the addon's `fileIdx`, which
    is its claim about its own mapping - measured correct on six of six
    real packs, so it is trusted where nothing contradicts it, and
    overridden where the file's name does.

    **The name beats the size.** See _names_episode for the pack that
    served episode 4 for a request for episode 2.

    **And when neither can identify it, refuse.** Returning the largest
    video was a guess dressed as an answer: in a 28-episode batch it is
    whichever episode happened to encode biggest. A release that cannot
    be shown to hold the right episode is dropped and the race takes the
    next one, which is the standing trade in this codebase - a shorter
    list beats a wrong episode that plays."""
    named, strength = _identify_episode_file(info, season, episode, title)
    if file_index is not None:
        try:
            index = int(file_index)
            path = info.files().file_path(index)
            stem = _stem(path)
            seasons = _path_seasons(path)
        except Exception:
            index, stem, seasons = None, "", set()
        if index is not None:
            # **What we can see in the file tree beats what the addon
            # says about it.** Measured 25 August 2026: for tt2560140:1:1
            # the addon pointed at file 1 of the Complete Collection,
            # which is `Attack On Titan Junior High\...Junior High -
            # 01.mkv` - a different show. The old rule only overrode a
            # fileIdx whose file *stated another SxxExx*, and no file in
            # that 131-video pack states a season at all, so nothing
            # contradicted it and Junior High played for every episode of
            # every season asked for. That is the owner's "when I played
            # aot s1 ep 1 then ep 2 both played episodes from s4".
            #
            # So a *positive* identification now wins outright, and only
            # a positive one: "exact" reads SxxExx off the file and
            # "folder" reads the season off the directory it is in, both
            # of which are evidence the addon does not have. A "loose"
            # read is a bare number and stays the weaker claim, so the
            # fileIdx keeps its long-measured trust wherever this app
            # cannot do better.
            if (named is not None and named != index
                    and strength in ("exact", "folder")):
                return named
            # "absent" is a positive finding too: the pack sorts itself
            # into seasons and this episode is in none of them, so the
            # fileIdx is pointing at something else whatever it says.
            if strength == "absent":
                return None
            # **A loose reading wins too, but only against a file that
            # contradicts itself.** The owner, 25 August 2026: *"the
            # same source in Silo series ep 1 s1 sometimes plays a diff
            # ep"* - *sometimes* because the fileIdx is the addon's
            # claim, and two addons offering the same release can
            # disagree about it, so which episode played depended on
            # which row was clicked.
            #
            # The test is deliberately two-sided: some other file reads
            # as the episode asked for, *and* the file this index points
            # at states a number that is not it. Both halves are
            # positive evidence, so this cannot fire on a pack whose
            # names say nothing - which is the case the fileIdx was
            # measured right for, six of six, and still owns.
            if named is not None and named != index and strength == "loose":
                claimed = _loose_numbers(stem)
                if claimed and int(episode) not in claimed:
                    return named
            wrong_season = bool(seasons) and int(season or 0) not in seasons
            if wrong_season:
                return named    # None when there is nothing better
            return index
    if named is not None:
        return named
    videos = _video_files(info)
    if season and episode and len(videos) > 1:
        return None
    if season and episode and len(videos) == 1:
        # **A single-video release still has to be shown to be the right
        # show - the owner, 24 August 2026: "when I play aot ep 1 season
        # 1 the auto sourcing played another whole anime!!".**
        #
        # Measured that day on his own entry: `find_streams` for Attack
        # on Titan S01E01 (tt2560140) came back with 67 rows, four of
        # which are unrelated Chinese BD raws that **Torrentio** returns
        # for that IMDb id - "[DBD-Raws][Kimi no Iro]", two Mahou Shoujo
        # Lyrical Nanoha films, a BanG Dream film. The indexers were
        # clean; the addon's own data is wrong, and nothing in this app
        # can make it right.
        #
        # Every other route was already guarded: a multi-video pack that
        # cannot be shown to hold the episode returns None just above,
        # and a film pack goes through movie_file_index. A release with
        # exactly *one* video fell through to "play it" on the addon's
        # word alone - and a single-file BDRip of a different series is
        # precisely that case. So it now has to earn it: the name must
        # either state the episode being asked for, or name the show.
        # Failing both, return None and let prepare_fastest take the
        # next candidate - being wrong costs one more attempt, which is
        # this codebase's standing trade.
        names = []
        try:
            names.append(str(info.name() or ""))
        except Exception:
            pass
        names.append(_stem(videos[0][1]))
        if not _plausible_episode(names, season, episode, title):
            logs.info(f"rejected single-file release for S{season}E{episode}: "
                      f"{names[0][:90]!r} names neither the episode nor the show")
            return None
    # **A film in a pack is identified by name too, not by size.** The
    # rule above has always covered episodes; movies fell through to
    # "largest video", which in a 263-film pack is simply the biggest
    # film in it - measured serving Monty Python's Life of Brian for a
    # request for The Dark Knight. See debrid.movie_file for the walk.
    if not (season and episode) and len(videos) > 1:
        return movie_file_index(info, title)
    return max(videos, key=lambda c: c[2])[0]


def add(info_hash: str, *, trackers=(), season=None, episode=None,
        title=None, file_index=None, metadata_timeout: float = 45.0,
        start_at=None, torrent_bytes=None):
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
            wanted = (_pick_file(existing.info, season, episode,
                                 file_index=file_index, title=title)
                      if (season and episode) or file_index is not None
                      or title else existing.file_index)
            if wanted is not None and wanted != existing.file_index:
                existing.file_index = wanted
                # A different file in the same pack starts cold: its
                # first piece is what playback is now blocked on, so
                # the narrow warming window applies again until it
                # lands (await_start clears it).
                existing.warming = not existing.have(existing.piece_at(0))
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
        # Metadata a previous attempt already fetched skips the whole
        # metadata wait - see _METADATA_DIR. The magnet's trackers stay
        # in params.trackers and are merged either way.
        cached_info = _cached_torrent_info(info_hash)
        if cached_info is None and torrent_bytes:
            # The release's own .torrent file, fetched over HTTP by the
            # caller (streams._torrent_file_bytes) - the same skip of
            # the metadata wait the disk cache buys, available on the
            # *first* play. Checked against the hash that was asked
            # for, never trusted: a mirror serving the wrong file must
            # fall through to the magnet, not hijack the add.
            try:
                candidate = lt.torrent_info(lt.bdecode(torrent_bytes))
                if str(candidate.info_hash()).lower() == info_hash:
                    cached_info = candidate
            except Exception:
                cached_info = None
        if cached_info is not None:
            params.ti = cached_info
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
    # Paid the swarm for it - keep it, so the next attempt at this
    # release (a retry, the next episode of the same pack) starts with
    # the file list instead of the wait.
    if cached_info is None:
        _save_torrent_info(info_hash, handle)

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
    torrent.file_index = _pick_file(info, season, episode,
                                    file_index=file_index, title=title)
    if torrent.file_index is None:
        # **Nothing in this torrent can be shown to be the episode asked
        # for.** Give it straight back rather than streaming a guess -
        # see _pick_file. The caller reads `episode_file(info_hash)` and
        # says "wrong-episode" out loud, and the race takes the next
        # candidate immediately instead of spending a data wait on it.
        return info_hash
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


def episode_file(info_hash: str):
    """The name of the file this torrent is going to serve, or None when
    none could be identified as the episode asked for (see _pick_file).

    None is how `streams.prepare` tells "the swarm is dead" apart from
    "this release does not hold that episode" - two failures that want
    two different messages and, more importantly, two different waits:
    the second one is knowable the moment the metadata lands."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.file_index is None:
        return None
    try:
        return os.path.basename(torrent.info.files().file_path(
            torrent.file_index))
    except Exception:
        return None


def chosen_file_size(info_hash: str) -> int:
    """The size in bytes of the file this torrent is going to serve, or
    0 when that is not knowable. Known the moment the metadata lands,
    which is what lets streams.prepare drop a release whose "movie" is
    a 20MB sample before spending a data wait on it."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.file_index is None:
        return 0
    try:
        return int(torrent.file_size())
    except Exception:
        return 0


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

# **How a dead swarm is recognised before its budget runs out.** The
# early-out proposed in streams.py's phase comment - "zero peers and
# zero connect candidates a couple of seconds after metadata" - was
# replayed against 12 recorded runs of the owner's three test titles on
# 22 August 2026 and **never fires**: a dead release keeps 27-378
# connect candidates the whole time it is dead, because the trackers
# and DHT keep supplying addresses that then give nothing. What actually
# separates dead from live, in every one of those runs, is what happens
# right after the metadata lands: a dead release's peers collapse to
# zero within half a second of has_metadata (4-18 peers connect, serve
# the metadata, and every one of them is gone by the next sample) and
# no payload byte ever arrives, while a live release keeps its peers
# and starts delivering within a couple of seconds. Replayed with these
# thresholds - no verdict before DEAD_GRACE_S of the wait, zero peers
# and zero payload sustained for DEAD_QUIET_S, and any release that
# ever delivered a payload block exempt for the rest of its wait - the
# rule fired 16 times across those runs with zero false positives. The
# exemption is not decoration: without it the one release that
# delivered 1MB, went quiet for two seconds and then won (Top Gun run
# 3) is killed mid-comeback.
DEAD_GRACE_S = 2.0
DEAD_QUIET_S = 1.5
# One block of real payload is proof the swarm answers; below this the
# counter movement is protocol noise.
DELIVERED_MIN_BYTES = 16 * 1024

# **A lane that is actively receiving payload is not given up on at its
# soft deadline.** The other failure mode those 12 runs recorded, and
# the dominant one for the series packs: a live swarm delivering
# 0.5-1.5MB/s whose *piece size* is too big for the first piece to
# complete inside QUEUED_DATA_WAIT - the lane was killed with 1-6MB
# already fetched (once 74MB), and the next candidate was usually
# another such pack starting again from zero. House of the Dragon
# S01E05 walked 23 live-but-slow releases that way, 33.0s to a url.
# So a lane at its soft deadline keeps waiting - up to the caller's
# hard cap - while the trailing EXTEND_WINDOW_S of payload is at least
# EXTEND_MIN_BYTES (~256KB/s, enough to finish a typical 4-8MB first
# piece within the extension). A trickle does not qualify: 288KB in six
# seconds is a swarm that answers and cannot feed a player, and it
# should lose its lane.
EXTEND_WINDOW_S = 2.0
EXTEND_MIN_BYTES = 512 * 1024

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
                index_wait: float = None, data_wait_max: float = None):
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

    `data_wait` is a *soft* deadline in two directions, both set by
    replaying 12 recorded runs of the owner's titles (see DEAD_GRACE_S
    and EXTEND_WINDOW_S above for the numbers): a swarm whose peers
    vanished at metadata and never sent a payload byte is given up on
    after DEAD_GRACE_S + DEAD_QUIET_S rather than at the deadline, and
    a swarm actively delivering payload at the deadline keeps its
    lane - up to `data_wait_max`, when the caller grants one - because
    killing a live lane over a large first piece only hands the wait to
    the next candidate, which starts again from zero. `data_wait_max`
    None means the deadline is hard, which is the old behaviour.

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
    hard_deadline = (started + max(data_wait, data_wait_max)
                     if data_wait_max else data_deadline)
    index_deadline = started + max(0.0, index_wait)
    got_data = got_index = False
    # The dead/alive bookkeeping. Payload is measured against a baseline
    # taken on the first sample rather than against zero: by the time
    # this runs the torrent has already spent seconds trading metadata,
    # and total_payload_download is not guaranteed to start clean.
    payload_base = None
    payload_now = 0
    delivered = False
    last_alive = started
    recent = []                 # (t, payload) over the trailing window
    next_status = 0.0
    while True:
        if not got_data:
            got_data = torrent.have(first_piece)
            if got_data:
                # The piece playback was blocked on exists - widen the
                # window back out so the swarm builds real readahead.
                torrent.warming = False
        if not got_index:
            got_index = not torrent._tail_pieces(INDEX_CRITICAL_BYTES)
        if got_data and got_index:
            break
        now = time.time()
        if not got_data and now >= next_status:
            # Status at 4Hz, not on every 0.05s tick - it is a
            # synchronous call into the session, and six race lanes
            # each polling at 20Hz is load the session does not owe.
            next_status = now + 0.25
            try:
                status = torrent.handle.status()
                payload_now = int(status.total_payload_download)
                if payload_base is None:
                    payload_base = payload_now
                if payload_now - payload_base >= DELIVERED_MIN_BYTES:
                    delivered = True
                if delivered or int(status.num_peers) > 0:
                    last_alive = now
                recent.append((now, payload_now))
                while recent and now - recent[0][0] > EXTEND_WINDOW_S:
                    recent.pop(0)
            except Exception:
                last_alive = now        # cannot tell, so do not condemn
        if not got_data:
            # Dead: the metadata came and the swarm behind it
            # evaporated - zero peers and not one payload block,
            # sustained. Never inside the grace, and never for a
            # release that has delivered anything (the one that
            # delivered 1MB, went quiet and then *won* is why - see
            # DEAD_GRACE_S above).
            if (not delivered and now - started >= DEAD_GRACE_S
                    and now - last_alive >= DEAD_QUIET_S):
                break
            if now >= data_deadline:
                # Still flowing at the soft deadline? Keep the lane, up
                # to the hard cap. The trailing-window test is what
                # keeps a 288KB-in-six-seconds trickle from holding
                # one - see EXTEND_MIN_BYTES.
                flowing = (hard_deadline > now and recent
                           and payload_now - recent[0][1] >= EXTEND_MIN_BYTES)
                if not flowing:
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
        # A real read means playback has begun; if the warming narrow
        # window is somehow still on (a path that skipped await_start),
        # it must not strangle the readahead.
        torrent.warming = False
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
                data = None
                # **A re-read of bytes already served comes from the file,
                # not from libtorrent.** read_piece exists because a piece
                # can read back as zeros for ~0.5s after have_piece() turns
                # true (its docstring has the measurement), so a *first*
                # read must go through the alert round trip. A byte that
                # was handed to a reader before is past that window by
                # definition, and reading it again through read_piece costs
                # one alert round trip per piece for nothing.
                #
                # This is the owner's "when I change the embedded subtitles
                # it freezes for ~5-10 sec" (23 August 2026). Measured by
                # the Map agent that day: selecting an embedded sub track
                # makes mpv drop its Range and reopen one from the file's
                # first cluster, then re-read forward through everything
                # it had already demuxed - 1.3MB at 12s in, 16.8MB at 60s
                # in - and the picture stays frozen until that re-read
                # catches up. Unthrottled (OS cache) it is a 10ms blip; at
                # 1MB/s it was 5.5-8.2s. The re-read was paying the alert
                # path for every piece it had already been served.
                if offset + chunk <= torrent.served_hwm:
                    try:
                        with open(path, "rb") as handle:
                            handle.seek(offset)
                            data = handle.read(chunk) or None
                    except (FileNotFoundError, OSError):
                        data = None
                if data is None and cached_piece != piece:
                    blob = torrent.read_piece(piece)
                    if blob is not None:
                        cached_piece, cached_bytes = piece, blob
                if data is None and cached_piece == piece and cached_bytes is not None:
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
                if offset > torrent.served_hwm:
                    torrent.served_hwm = offset
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


def prefetch_metadata(info_hash: str, *, trackers=(),
                      metadata_timeout: float = 45.0) -> bool:
    """Fetch a release's .torrent metadata into the disk cache and then
    let the swarm go again. Returns whether the metadata is now cached.

    **This is the half of "warm the next episode" that is safe to do
    while something is playing.** Measured 23 August 2026, a cold Next
    cost 35.0s, and the fixed part of that - the part that is the same
    however good the swarm is - is the metadata round trip over DHT and
    the trackers. The *data* half cannot be paid early without a second
    swarm competing with the episode on screen for one connection, which
    is exactly the starvation `_maybe_prewarm_next` guards against.

    Metadata is a few hundred KB and no file payload is requested at all:
    the torrent is added, its info dictionary saved by `_save_torrent_info`
    (the same cache `add` consults through `_cached_torrent_info`), and
    then removed from the session. A later `add` for the same release
    finds `params.ti` already populated and skips the metadata wait
    outright.

    Never raises, and never disturbs a torrent that is already live - an
    info hash currently held (the thing playing, or one the race is using)
    is left exactly alone."""
    key = (info_hash or "").strip().lower()
    if lt is None or not re.fullmatch(r"[0-9a-f]{40}", key):
        return False
    if _cached_torrent_info(key) is not None:
        return True                     # already paid for, nothing to do
    if key in _torrents:
        # Live already: adding a second handle for it would be a no-op at
        # best and a fight over the same files at worst.
        return False
    magnet = "magnet:?xt=urn:btih:" + key
    for tracker in trackers or ():
        tracker = str(tracker)
        if tracker.startswith("tracker:"):
            tracker = tracker[len("tracker:"):]
        elif tracker.startswith("dht:"):
            continue
        magnet += "&tr=" + urllib.parse.quote(tracker, safe="")
    handle = None
    try:
        params = lt.parse_magnet_uri(magnet)
        params.save_path = _CACHE_DIR
        os.makedirs(_CACHE_DIR, exist_ok=True)
        # Upload mode: libtorrent will take the metadata and ask for no
        # piece data, which is the whole point of this function.
        try:
            params.flags |= lt.torrent_flags.upload_mode
        except Exception:
            pass
        handle = session().add_torrent(params)
        deadline = time.time() + metadata_timeout
        while time.time() < deadline:
            try:
                if handle.status().has_metadata:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            return False
        _save_torrent_info(key, handle)
        return _cached_torrent_info(key) is not None
    except Exception:
        return False
    finally:
        # Hand the swarm back either way - this must not leave a torrent
        # announcing behind the episode being watched.
        if handle is not None:
            try:
                session().remove_torrent(handle)
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
