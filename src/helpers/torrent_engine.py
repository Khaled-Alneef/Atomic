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

import bisect
import os
import re
import select
import shutil
import socket
import socketserver
import struct
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# **Imported here, on the main thread, and not left to the worker.** See
# _LocalHTTPServer for the crash this is the second half of: the codec
# registry resolves `encodings.idna` lazily, and the first thing that
# ever asked for it was a background thread during startup. Paying the
# import here means no thread can be the one to trigger it.
import encodings.idna        # noqa: F401  (imported for its side effect)

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


# **Fast-resume, so a replay does not re-hash what is already on disk.**
# The owner, 31 August 2026: "instant for the videos started watching
# already before". Measured on Attack on Titan S01E02, the same episode
# played twice in one session: sources came back in 0.17s (already
# cached) and the picture still took **9.06s**, of which **8.4s was
# prepare** - on a release whose pieces were sitting in the cache from
# the play four seconds earlier.
#
# Nothing was re-downloaded. libtorrent simply does not know the files
# are good: added without resume data it re-checks them piece by piece
# before `have_piece` answers True for anything, and until then
# await_start sees no data and waits. Saving what libtorrent already
# knows - which pieces verified - and handing it back on the next add
# skips that check entirely.
#
# Written beside the .torrent metadata cache and read the same way: a
# missing or unparseable file is a normal state that costs one re-check,
# never a failure to play.
_RESUME_DIR = os.path.join(_CACHE_DIR, "resume")


def _resume_path(info_hash: str) -> str:
    return os.path.join(_RESUME_DIR, info_hash + ".resume")


def _cached_resume(info_hash: str):
    """Add-params carrying a previous session's verified pieces, or
    None. Returns fully-formed params - resume data holds the save path
    and the torrent's own identity, so it replaces the magnet parse
    rather than decorating it."""
    try:
        path = _resume_path(info_hash)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            blob = handle.read()
        if not blob:
            return None
        return lt.read_resume_data(blob)
    except Exception:
        return None


def _save_resume(info_hash: str, params) -> None:
    """Write the resume params libtorrent handed back on a
    save_resume_data alert. Atomic, for the reason the metadata cache
    is: a truncated file that half-parses is worse than none."""
    try:
        os.makedirs(_RESUME_DIR, exist_ok=True)
        blob = lt.write_resume_data_buf(params)
        if not blob:
            return
        path = _resume_path((info_hash or "").lower())
        temporary = path + ".part"
        with open(temporary, "wb") as handle:
            handle.write(blob)
        os.replace(temporary, path)
    except Exception:
        pass


def _request_resume_save(handle) -> None:
    """Ask libtorrent to hand back this torrent's verified state. The
    answer arrives as an alert - see _alert_pump."""
    try:
        if handle is None or not handle.is_valid():
            return
        status = handle.status()
        if not status.has_metadata:
            return
        flags = 0
        try:
            flags = lt.save_resume_flags_t.save_info_dict
        except Exception:
            flags = 0
        handle.save_resume_data(flags)
    except Exception:
        pass


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
# **What the rest of the chosen file is set to while its first piece is
# still missing** (`_Torrent.warming`): nothing, so every peer works on
# the piece playback is blocked on.
#
# Measured 5 September 2026 on the owner's Jujutsu Kaisen S01E01, after
# "the connecting to source takes a while then the buffering then it
# goes to connecting to source again". The hand-picked `[Judas] ...
# (Season 1)` batch (374 seeders, 8MB pieces) was prepared solo: 112
# peers, 18MB/s, **153MB of the chosen file downloaded in 14 seconds, and
# the first piece of it still not complete** - so `await_start` gave a
# healthy swarm the `no-peers` verdict at its hard cap, and the player
# moved on ("connecting again") to whatever won the race behind it. With
# the rest of the file merely *wanted* (priority 1), the two head pieces
# at 7 are spread over a handful of peers and a hundred others fill the
# file from all over; libtorrent hands each block of a piece to one peer,
# so the head lands at the pace of the slowest of those few.
#
# Time to the first piece, cold, alternated on the same swarm:
#
#     rest at 1 (this used to be it):   12.6s   19.0s   21.1s   20.0s
#     rest at 0 while warming:           7.5s    7.5s    6.5s
#     ... and with redundant connections kept (see _make_session):
#                                        6.0s    5.0s
#
# Only while warming. After the first piece the rest goes back to 1, for
# the 25 August reason in _apply_windows (a peer holding nothing wanted
# is dropped), and that drop is why `close_redundant_connections` is off
# in _make_session: at 0 alone one run of three reached the first piece
# with **0 peers** and spent ten seconds rebuilding the swarm.
WARMING_FILL_PRIORITY = 0
# **...and after it as well.** The rest of the file outside the reader's
# window is not wanted either - this supersedes the 25 August 2026 "rest
# stays wanted at priority 1" in _apply_windows, whose reason (a peer
# holding nothing wanted is dropped, and a 701-seeder swarm collapsed to
# 2 peers) is now answered by close_redundant_connections being off.
#
# Measured 5 September 2026 on the owner's Obsession .mp4 (5.3GB, 4MB
# pieces), a ranged read at a fresh offset held for 20s, alternated on
# the same swarm - first the frozen build's own picture: 212 peers,
# 26.45MB/s downloading, and mpv "stream starved" at the seat with a
# 0.0s buffer on a 24.79Mbps film. Then the split:
#
#                        first byte    served to reader   downloaded
#     rest at 1:          9.8s 10.4s    2.59  2.76 MB/s   12.0 17.5 MB/s
#     rest at 0:          5.9s  3.6s    8.07 11.81 MB/s   ~14  15.8 MB/s
#
# With the rest at 1 the window's few pieces were requested from a few
# peers and a hundred others filled the file from all over, so the
# reader got a fifth of the bandwidth - under the film's own bitrate.
# At 0 every peer works on the window; peers held (43-171) because they
# are no longer dropped for being uninteresting, and once the window is
# on disk the swarm idles for a tick until the reader advances it.
STREAM_FILL_PRIORITY = 0
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

# How long a response may go without sending a byte before it gives up
# and lets the reader reopen. Longer than _Torrent.wait_for's own 45s
# cap on one piece, so a slow swarm that is still making progress is
# never cut off by this - it only catches the paths that cannot make
# progress at all (see _serve). A minute of frozen picture is already
# far too long; the point of the number is that it is finite.
STREAM_STALL_S = 60.0
# How long one read may wait for the piece it is blocked on before the
# response is ended and the client reopens - _Torrent.wait_for's own
# default, now waited in slices so the client hanging up is noticed.
PIECE_WAIT_S = 45.0

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

# How much of the head a *resume* primes, in pieces. Opening a file at a
# seat reads the container header and then jumps, so the rest of the head
# is bandwidth taken from the index and the seat itself - see
# _apply_windows for the run where fetching 12MB of unwatched head left
# the player with no picture at all. Two pieces is 4MB at the usual piece
# size, comfortably past any Matroska/MP4 header.
RESUME_HEADER_PIECES = 2

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
        # **A peer that holds nothing wanted right now is kept, not
        # dropped.** Streaming wants a few pieces at a time and moves
        # on; libtorrent's default closes every connection that is not
        # interesting *and* not interested, which for a narrow window is
        # most of the swarm - the 25 August collapse of a 701-seeder
        # release to 2 peers, and, measured 5 September 2026 with the
        # warming window at WARMING_FILL_PRIORITY 0, a first piece
        # reached with 0 peers and ten seconds of rebuilding. With the
        # setting off the same runs held 69 and 58 peers at that moment
        # and were at 16-20MB/s ten seconds later. connections_limit
        # above still bounds the total.
        "close_redundant_connections": False,
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
            # Verified-piece state coming back from a save_resume_data
            # request - see _cached_resume for why it is kept.
            if isinstance(alert, getattr(lt, "save_resume_data_alert", ())):
                try:
                    try:
                        resume_hash = str(
                            alert.handle.info_hashes().v1).lower()
                    except AttributeError:      # libtorrent 1.x
                        resume_hash = str(alert.handle.info_hash()).lower()
                    _save_resume(resume_hash, alert.params)
                except Exception:
                    pass
                continue
            if not isinstance(alert, lt.read_piece_alert):
                continue
            try:
                # The *v1* hash, never the deprecated singular
                # info_hash(): for a hybrid v1+v2 torrent that returns
                # the truncated v2 hash, while every key in _torrents
                # (and so in _reads) is the v1 hex the magnet named.
                # Measured 29 August 2026 on a hybrid: the alert arrived
                # with its buffer in under 0.1s and matched nothing, so
                # every read_piece sat out its full 10s timeout and the
                # local server streamed a fully-downloaded file at
                # 0.10 MB/s - one dead decade per piece. That is
                # "buffering finished but the video will not play".
                try:
                    info_hash = str(alert.handle.info_hashes().v1).lower()
                except AttributeError:          # libtorrent 1.x
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


_RESUME_SAVE_INTERVAL_S = 20.0
_resume_saved_at = {}


def _maybe_save_resume() -> None:
    """Ask each streaming torrent to hand back its verified state, at
    most every _RESUME_SAVE_INTERVAL_S. The write happens when the alert
    comes back (see _alert_pump); this only asks."""
    now = time.time()
    for key, torrent in list(_torrents.items()):
        try:
            if now - _resume_saved_at.get(key, 0.0) < _RESUME_SAVE_INTERVAL_S:
                continue
            _resume_saved_at[key] = now
            _request_resume_save(torrent.handle)
        except Exception:
            continue


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
        # And each live torrent's verified pieces, on the same ride -
        # see _cached_resume for the 8.4s replay this exists to remove.
        # Throttled per torrent rather than globally: they start at
        # different times, and the point is that whatever was watched is
        # recorded before the app is closed.
        _maybe_save_resume()
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
        # The container's seek index, once parsed: for an .mp4 the
        # first video track's sample tables (see _mp4_parse_moov), keyed
        # by where the moov was found so a re-added torrent cannot reuse
        # another file's. Parsing a 6.78MB moov is tens of milliseconds
        # and the seat poll asks every couple of seconds.
        self._index_tables = None
        self._index_key = None

    # -- geometry -----------------------------------------------------
    @property
    def info(self):
        return self.handle.torrent_file()

    def file_size(self, index=None) -> int:
        return self.info.files().file_size(
            self.file_index if index is None else index)

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

    def container_kind(self):
        """"mkv", "mp4", "other", or None while the file's first bytes
        are not on disk to tell. Off the first 16 bytes: an EBML header
        for Matroska/WebM, an ISO-BMFF atom type at bytes 4-8 for MP4
        (`ftyp` on every real file; the other top-level types are
        accepted so a file that skips ftyp is not misread as "other")."""
        try:
            head = self.read_present(0, 16)
        except Exception:
            head = None
        if not head or len(head) < 8:
            return None
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return "mkv"
        if head[4:8] in _MP4_TOP_ATOMS:
            return "mp4"
        return "other"

    def _moov_span(self):
        """(offset, length) of an .mp4's moov atom, header included; None
        while the bytes needed to locate it are not on disk; False when
        this file has no reachable moov (see _mp4_find_moov)."""
        try:
            found = _mp4_find_moov(self.read_present, self.file_size())
        except Exception:
            return None
        if not found:
            return found
        body_at, body_size = found
        header = 16 if body_at >= 16 else 8
        return body_at - header, body_size + header

    def _index_pieces(self, critical: bool = False):
        """The pieces of the container's seek index still missing.

        **Matroska's index is in the tail; an MP4's is wherever its moov
        is, and for the owner's film releases that is a 6.78MB moov at
        the very end of a 5.3GB file** (measured 5 September 2026 on his
        Obsession .mp4: `moov` at 5,314,433,909, 6,784,467 bytes). The
        tail band alone (TAIL_BYTES, 4MB) never covered it, so the
        demuxer's open blocked on pieces nothing had prioritised, and the
        seat could never be resolved because the tables it needs were not
        on disk. Once the head has landed and the moov is located, the
        whole of it is the index band - `critical` or not, because an
        MP4 demuxer reads all of it before frame one - and a moov at the
        head of a faststart file is inside the head band anyway.

        Until the head is on disk the tail is the best guess, exactly as
        before; `critical` then means the last INDEX_CRITICAL_BYTES, as
        await_start has always asked."""
        try:
            if self.container_kind() == "mp4":
                span = self._moov_span()
                if span:
                    offset, length = span
                    length = min(length, MOOV_INDEX_CAP)
                    first = self.piece_at(offset)
                    last = self.piece_at(offset + length - 1)
                    return [p for p in range(first, last + 1)
                            if not self.have(p)]
        except Exception:
            pass
        return self._tail_pieces(INDEX_CRITICAL_BYTES if critical else None)

    def _index_reads_forward(self) -> bool:
        """Which way the demuxer walks the index band: an MP4 reads its
        moov front to back, Matroska's Cues are asked for from the end
        of the file (see the deadline order in _apply_windows)."""
        try:
            return self.container_kind() == "mp4" and bool(self._moov_span())
        except Exception:
            return False

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
            if len(data) != length:
                return None
            # **A hole reads as zeros and passes every length check.**
            # This fallback runs when read_piece could not answer for a
            # piece have() says is there - and the usual reason for that
            # is the one the comment above records, a piece completed but
            # not yet flushed. Reading it then returns the sparse hole,
            # full length, and zeros decode to macroblock garbage rather
            # than failing: that is the picture in the owner's recording
            # of 31 August 2026, and what clicking a minute the swarm has
            # not reached produced. Refusing it makes the caller wait for
            # the real bytes. A genuine all-zero read of a video is not a
            # thing worth serving either.
            if not data.strip(b"\x00"):
                return None
            return data
        except Exception:
            return None

    def seat_resolvable(self):
        """Whether resolve_start can ever answer for this file: True for
        a Matroska file (the Cues map time to bytes), False for anything
        else, None while the head is not on disk to tell.

        **This is the owner's "when I stop at some point in the movie
        and come watch it again it starts from the beginning, not like
        the anime and series" (5 September 2026).** Anime and series
        releases are .mkv; his film sources were .mp4 (`ftyp` then a
        5.3GB `mdat`, the `moov` at the tail). resolve_start answers
        None for those, correctly - but the player's seat poll read
        None as "not yet" and waited its whole 180s for a band that
        was never going to be armed, while the film played from 0:00.
        Telling "never" from "not yet" is what lets the poll seek
        without a band instead (player_resume_latency_patch).

        **And True for an .mp4 whose moov can be located, from 5
        September 2026** - the "follow-up that would make it instant"
        the note above ended on. _mp4_find_moov walks the top-level
        atoms from the head (ftyp, then an mdat whose 64-bit size says
        where the tail moov starts) and answers None while the bytes to
        tell are not on disk, which is the same "not yet" the Cues have:
        the seat poll keeps asking and the band is armed when the moov
        lands. False only for a container with no index path at all - a
        fragmented MP4, an .avi, a .ts."""
        kind = self.container_kind()
        if kind is None:
            return None
        if kind == "mkv":
            return True
        if kind == "mp4":
            span = self._moov_span()
            if span is None:
                return None
            return bool(span)
        return False

    def resolve_start(self, seconds):
        """Turn a resume *time* into the byte the demuxer will read.

        Answers None unless it is certain: no index this code can read,
        or the bytes holding it not on disk yet, both mean "prime
        nothing", because a wrong offset costs bandwidth the first frame
        needs and buys nothing (see the measurement above
        _matroska_layout). Matroska through the Cues; MP4 through the
        moov's sample tables (_mp4_seat_for) - measured against ffprobe
        on a 217MB test file with the moov at either end, the byte for
        1s, 5s, 8.5s, 30s and 61.7s is the keyframe's own `pos` every
        time (499, 499, 499, 11075272, 27139413)."""
        if self.container_kind() == "mp4":
            return self._resolve_mp4_start(seconds)
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

    def _resolve_mp4_start(self, seconds):
        """The MP4 half of resolve_start: the byte of the last sync
        sample at or before `seconds` on the first video track, or None
        while the moov is not on disk or cannot be read."""
        span = self._moov_span()
        if not span:
            return None
        try:
            body_at, body_size = _mp4_find_moov(self.read_present,
                                                self.file_size())
        except Exception:
            return None
        key = (body_at, body_size)
        tables = self._index_tables if self._index_key == key else None
        if tables is None:
            moov = self.read_present(body_at, body_size)
            if not moov:
                return None
            tables = _mp4_parse_moov(moov)
            if not tables:
                return None
            self._index_tables, self._index_key = tables, key
        return _mp4_seat_for(tables, seconds)

    def seat_on_disk(self, seconds):
        """Whether the bytes a seek to `seconds` will read are already
        here: True/False when the index can say, None when it cannot
        (no index on disk yet, a container without one). Two pieces,
        not one - a demuxer needs a run past the offset, the same test
        player_resume_latency_patch applies before opening at a seat."""
        try:
            offset = self.resolve_start(seconds)
        except Exception:
            offset = None
        if offset is None:
            return None
        try:
            first = self.piece_at(int(offset))
            last = self.piece_at(min(int(offset) + self.piece_length(),
                                     max(self.file_size() - 1, 0)))
            return all(self.have(p) for p in range(first, last + 1))
        except Exception:
            return None

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
            # **A resume does not read the head, so the head must not be
            # allowed to spend the opening bandwidth.**
            #
            # Measured 30 August 2026, 217MB file, 2MB pieces, one seeder
            # throttled to 1.5MB/s, resuming at 4:00 - the shipped order
            # delivered head, head, head, then the index at 8.77s, by
            # which time `await_start`'s 6s index wait had already given
            # up and `await_start_band`'s 3s had expired with the seat's
            # own piece still missing. mpv was handed the url with
            # neither the index nor the data it was about to seek to, and
            # **no picture arrived at all inside 90 seconds** while the
            # startup gauge sat at 99% - the owner's "it does not play
            # the video until it reaches 100%". The 99% is literally the
            # head: 12MB nobody was going to watch, fetched in front of
            # the two things playback was blocked on.
            #
            # Opening a file at a seat reads the header, the index, and
            # the seat - and that is all. So while a resume is pending,
            # the head keeps only the couple of pieces the container's
            # header needs, and the seat band is ordered in front of the
            # rest of the window below rather than behind it.
            resuming = bool(self.start_seconds is not None
                            and self._start_pieces())
            if resuming:
                urgent_count = min(urgent_count, RESUME_HEADER_PIECES)

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
            # **Only the bytes the demuxer provably reads keep playback's
            # own priority; the rest of the tail waits its turn.**
            #
            # Measured 30 August 2026, a swarm delivering 1.5x realtime -
            # the owner's "it plays but stuck until it loads ~50%",
            # reproduced: playback ran to 5.9s and then froze for **19.7
            # seconds** while the torrent climbed 4.4% -> 11.5%. It was
            # never short of bandwidth; it was short of the *next* piece,
            # because the whole 4MB tail sat at priority 7 beside the
            # head's urgent band and libtorrent split the line between
            # them. `await_start` has already waited for
            # INDEX_CRITICAL_BYTES of that tail before the url was
            # handed over, so the remainder is readahead for a seek
            # nobody has asked for yet - it stays wanted (4, above the
            # ordinary fill) and stops competing with the picture.
            # **The index band is the container's, not always the
            # tail's** - see _index_pieces for the 6.78MB moov at the end
            # of his film releases that the 4MB tail never covered.
            critical = set(self._index_pieces(critical=True))
            tail = self._index_pieces()
            for piece in tail:
                if 0 <= piece < total_pieces:
                    priorities[piece] = (7 if piece in critical
                                         else max(priorities[piece], 4))

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

            # **The rest of this file stays wanted, at priority 1** - and
            # that is what `download_whole`'s docstring has always
            # claimed streaming does, while this array was built as
            # `[0] * total_pieces` and never raised.
            #
            # A piece at 0 is not "fetch later", it is "do not want",
            # and libtorrent drops a peer that holds nothing wanted
            # (close_redundant_connections). So the swarm was being cut
            # down to whoever happened to hold the ~24MB window.
            # Measured 25 August 2026 on Solo Leveling S02E05, a release
            # advertising **701 seeders**: the engine opened 22
            # connections and was down to **2 peers, 2 seeds, 2
            # connections** nine seconds later, and stayed there for the
            # rest of a 45s run at 0.3-0.5 MB/s - the owner's "10 peers
            # only gives me around 2 MB, while in stremio the 10 peers
            # gives around 10 MB".
            #
            # Anchoring the window at the first *missing* piece (see the
            # 22 August note above) fixed the total collapse to zero;
            # this fixes the standing case, where a peer is dropped for
            # not holding the few pieces being read right now.
            #
            # Priority 1 against the bands' 4 and 7, so ordering is
            # untouched: the head still arrives first, the deadlines
            # below still decide sequence, and this only fills what is
            # otherwise idle capacity. **Other files in a pack stay at
            # 0** - `_set_wanted_files` exists so one episode out of a
            # 28-episode pack does not drag the other 27 down with it,
            # and this must not undo that: the loop is bounded to this
            # file's own pieces.
            # **Except that it is 0 now, warming or not** - see
            # STREAM_FILL_PRIORITY for the seek measurement that ended the
            # priority-1 fill, and WARMING_FILL_PRIORITY for the first-piece
            # one. The paragraph above is kept as the record of why 1 was
            # tried. Measured 5 September 2026 (see
            # WARMING_FILL_PRIORITY): with the rest at 1 a 112-peer swarm
            # put 153MB of the file on disk in 14s around a first piece
            # that never completed. Until that piece exists, wanting the
            # rest of the file costs the one thing playback is blocked on.
            fill = WARMING_FILL_PRIORITY if self.warming else STREAM_FILL_PRIORITY
            for piece in range(max(0, file_first),
                               min(total_pieces - 1, file_last) + 1):
                if priorities[piece] == 0:
                    priorities[piece] = fill
            handle.prioritize_pieces(priorities)

            # **Clear the old deadlines before writing the new ones -
            # this is what made seeking take longer the more you
            # seeked.** `set_piece_deadline` is relative to now, and
            # nothing ever removed one, so every piece any earlier
            # window had asked for kept a deadline that is now in the
            # *past*. libtorrent orders its picker by deadline, so those
            # expired entries sort ahead of the fresh 200ms one belonging
            # to the place the user just seeked to: the seek waits behind
            # every position previously visited, in the order they were
            # visited. The priority array below already covers the union
            # of every live reader's window (see the docstring), and
            # `wait_for` re-asserts the deadline on the exact piece it is
            # blocked on every poll, so clearing loses nothing.
            #
            # Measured 25 August 2026, Attack on Titan S01E02, a 4.33GB
            # release with 8.39MB pieces: after the head landed, seeks to
            # 25%, 55% and 80% each got **no piece at all inside 45s**.
            # The floor here is one whole piece either way - 8.39MB is
            # ~11s at what that swarm gave - which is the honest reason
            # "Skipping to..." is not a one-second operation on a
            # torrent, whatever this fixes.
            try:
                handle.clear_piece_deadlines()
            except Exception:
                pass

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
            # **Back to front, and that is not a detail.** The tail is a
            # range of pieces and this used to deadline them ascending,
            # so the *last* piece of the file was fetched last of the
            # three - while the last piece is precisely what the index
            # actually needs: Matroska writes its Cues at the end of the
            # file, and `await_start` asks `_tail_pieces(
            # INDEX_CRITICAL_BYTES)`, i.e. the final 1MB alone.
            #
            # Measured 30 August 2026 (217MB file, 2MB pieces, one seeder
            # at 1.5MB/s, resuming at 4:00): ascending order delivered
            # tail piece 101 at 5.0s and had still not fetched piece 103
            # fifteen seconds in, so the index wait timed out, the Cues
            # were unreadable, `arm_start_band` could not turn 4:00 into
            # a byte at all (start_offset stayed None) and the seat band
            # was never armed. Reversed, the piece the check is waiting
            # for is the first one asked for.
            #
            # **`tail` was never bound here, from the day this was
            # written until 5 September 2026.** The loop below read a
            # name no line assigned, the NameError landed in the
            # function's outer `except Exception: pass`, and everything
            # after it - these index deadlines and the resume band's
            # below - was silently skipped on every focus(). Both bands
            # still arrived, on priority alone; what the measurement
            # above records as fixed had in fact never run. Found by
            # reading the function's names with `ast` while wiring the
            # MP4 index in; `tail` is now the index band computed above.
            # **Forward for an MP4**, whose demuxer walks the moov front
            # to back, and back to front for Matroska as measured.
            ordered = (list(tail) if self._index_reads_forward()
                       else list(reversed(tail)))
            for index, piece in enumerate(ordered):
                try:
                    handle.set_piece_deadline(piece, 300 + index * 120)
                except Exception:
                    pass
            # **The resume band goes in front of the head window, not
            # behind it.**
            #
            # It used to start at 1600 - behind every deadline the head
            # window owns - on the reasoning that "mpv reads roughly the
            # first 20MB of the file (header plus attached fonts),
            # *then* jumps". That is what mpv does when it opens a file
            # at the beginning and seeks afterwards. It is not what this
            # player does: `_load_into_mpv` opens the file *at* the seat
            # (`loadfile ... start=`), so those 20MB are never read, and
            # ordering the seat behind them meant the one thing playback
            # was blocked on came last. See the urgent-band note above
            # for the run where no picture arrived at all.
            #
            # 600 puts it after the file's first piece (200) and the
            # index (300-540) - both genuinely needed before the seat is
            # useful - and ahead of the rest of the head window.
            start_base = 600 if resuming else 1600
            for index, piece in enumerate(start_band):
                try:
                    handle.set_piece_deadline(piece, start_base + index * 200)
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

    def file_path(self, index=None) -> str:
        storage_path = self.handle.status().save_path
        return os.path.join(storage_path, self.info.files().file_path(
            self.file_index if index is None else index))


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


# ------------------------------------------------- reading the moov
#
# **The MP4 counterpart of the Cues, written 5 September 2026** - the
# "follow-up that would make it instant" from the day before, when the
# seat on his film releases was found to be seeked blind because
# resolve_start could only read Matroska. An MP4's index is its `moov`
# atom: per track, the sample tables that map a time to a sample
# (stts), a sample to its sync frame (stss), a sample to its chunk
# (stsc), a chunk to a byte (stco/co64) and a sample to its size (stsz).
# The demuxer lands on the last sync sample at or before the target,
# and that sample's byte is exactly what mpv's first read after the seek
# asks for - so it is the byte to prime, a fact rather than a guess, the
# same way the Cues are.
#
# Where the moov sits is the whole difficulty. A faststart file keeps it
# at the head; his releases keep it at the tail, behind a 5.3GB mdat -
# measured on his Obsession .mp4: `moov` at 5,314,433,909, 6,784,467
# bytes (one video, one E-AC3 and eight mov_text tracks; the video
# track's stsz/stco/stts are most of it). Walking the top-level atoms
# from byte 0 finds it either way: ftyp, then an mdat whose 64-bit
# "largesize" says where the next atom starts. That walk needs sixteen
# bytes at each atom boundary and nothing else, so it can be answered
# off the head alone, and `_index_pieces` then wants the whole moov at
# the index's priority.
#
# Validated against ffprobe's own packet positions on a 217MB test file
# in both layouts: the byte for 1s, 5s, 8.5s, 30s and 61.7s equals the
# keyframe's `pos` in every case (499, 499, 499, 11075272, 27139413 on
# the tail-moov copy; 266353 higher on the faststart copy, which is the
# moov now sitting in front of the mdat). Parsing that moov is 1.5ms;
# a film's is tens of milliseconds and is cached on the torrent.
#
# Everything here answers None (or False) rather than raising: a
# fragmented MP4 (moof), a broken size field, a track without the four
# tables - all of them mean "prime nothing", as the Cues do.

_MP4_TOP_ATOMS = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide",
                  b"uuid", b"meta", b"sidx", b"styp", b"pdin", b"junk",
                  b"pnot", b"moof")
# A moov past this is a broken size field, not a film's index: a 2h
# 1080p film with ten tracks measured 6.78MB. And the most of it that
# is put on the index band's priority - the rest of a pathological one
# would be fetched when the demuxer asks, like any other read.
MOOV_CAP = 64 * 1024 * 1024
MOOV_INDEX_CAP = 32 * 1024 * 1024
# Top-level atoms walked before giving up on finding a moov. A real file
# is a handful; a fragmented one is stopped at its first moof anyway.
_MP4_MAX_TOP_ATOMS = 32


def _mp4_atom_header(data, pos, file_size):
    """(size, type, header_len) of the atom whose header `data` holds
    (up to 16 bytes from `pos`). Raises ValueError on garbage."""
    if len(data) < 8:
        raise ValueError("short atom header")
    size, kind = struct.unpack_from(">I4s", data, 0)
    header = 8
    if size == 1:
        if len(data) < 16:
            raise ValueError("short largesize header")
        size = struct.unpack_from(">Q", data, 8)[0]
        header = 16
    elif size == 0:
        size = file_size - pos              # "to the end of the file"
    if size < header:
        raise ValueError("atom size below its header")
    return size, kind, header


def _mp4_find_moov(read, file_size):
    """(moov_body_offset, moov_body_size) - the atom's contents, header
    excluded, so `read(offset, size)` is exactly what _mp4_parse_moov
    wants. None when a header needed to tell is not on disk yet; False
    when the walk proves there is no moov to find (not an ISO-BMFF
    file, fragmented, or walked off the end).

    `read(offset, length)` is _Torrent.read_present: bytes when every
    piece under them is on disk, None otherwise - which is what makes
    the None/False split honest."""
    pos = 0
    for _ in range(_MP4_MAX_TOP_ATOMS):
        if pos >= file_size:
            return False
        head = read(pos, 16)
        if head is None:
            return None
        try:
            size, kind, header = _mp4_atom_header(head, pos, file_size)
        except ValueError:
            return False
        if pos == 0 and kind not in _MP4_TOP_ATOMS:
            return False                    # not an ISO-BMFF file at all
        if kind == b"moov":
            body = size - header
            if body <= 0 or body > MOOV_CAP:
                return False
            return pos + header, body
        if kind == b"moof":
            return False                    # fragmented: no moov index
        pos += size
    return False


def _mp4_children(data, pos, end):
    """Yield (type, body_start, body_end) for the atoms in [pos, end)."""
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        stop = min(pos + size, end)
        yield kind, pos + header, stop
        pos = stop


def _mp4_find(data, pos, end, kind):
    for found, start, stop in _mp4_children(data, pos, end):
        if found == kind:
            return start, stop
    return None


def _mp4_parse_track(moov, trak_start, trak_end, movie_timescale):
    """The seek tables of one trak, or None unless it is a video track
    with samples: timescale, edit_offset (media ticks to add to a
    presentation time), dts (per sample, cumulative), sync (sorted
    0-based sync samples, or None for "every sample"), sizes (a list, or
    one constant), stsc rows (first_chunk0, per_chunk), chunk_offsets."""
    mdia = _mp4_find(moov, trak_start, trak_end, b"mdia")
    if not mdia:
        return None
    hdlr = _mp4_find(moov, mdia[0], mdia[1], b"hdlr")
    if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
        return None
    mdhd = _mp4_find(moov, mdia[0], mdia[1], b"mdhd")
    if not mdhd:
        return None
    version = moov[mdhd[0]]
    timescale = struct.unpack_from(
        ">I", moov, mdhd[0] + (20 if version == 1 else 12))[0]
    if not timescale:
        return None
    minf = _mp4_find(moov, mdia[0], mdia[1], b"minf")
    stbl = minf and _mp4_find(moov, minf[0], minf[1], b"stbl")
    if not stbl:
        return None

    # The edit list: presentation 0 is media `media_time` of the first
    # non-empty edit, and a leading empty edit delays the whole track.
    edit_offset = 0
    edts = _mp4_find(moov, trak_start, trak_end, b"edts")
    elst = edts and _mp4_find(moov, edts[0], edts[1], b"elst")
    if elst:
        version = moov[elst[0]]
        count = struct.unpack_from(">I", moov, elst[0] + 4)[0]
        pos = elst[0] + 8
        delay_movie = 0
        for _ in range(count):
            if version == 1:
                seg_dur, media_time = struct.unpack_from(">Qq", moov, pos)
                pos += 20
            else:
                seg_dur, media_time = struct.unpack_from(">Ii", moov, pos)
                pos += 12
            if media_time == -1:
                delay_movie += seg_dur
                continue
            edit_offset = media_time - (
                delay_movie * timescale // max(movie_timescale, 1))
            break

    def table(kind):
        return _mp4_find(moov, stbl[0], stbl[1], kind)

    stts, stsc = table(b"stts"), table(b"stsc")
    stco = table(b"stco") or table(b"co64")
    stsz = table(b"stsz") or table(b"stz2")
    if not (stts and stsc and stco and stsz):
        return None

    # stts -> every sample's decode time, cumulative.
    count = struct.unpack_from(">I", moov, stts[0] + 4)[0]
    pairs = struct.unpack_from(">%dI" % (2 * count), moov, stts[0] + 8)
    dts = []
    tick = 0
    for i in range(count):
        run, delta = pairs[2 * i], pairs[2 * i + 1]
        if delta:
            dts.extend(range(tick, tick + run * delta, delta))
        else:
            dts.extend([tick] * run)
        tick += run * delta
    if not dts:
        return None

    # stss -> sorted 0-based sync samples; absent means every sample.
    sync = None
    stss = table(b"stss")
    if stss:
        count = struct.unpack_from(">I", moov, stss[0] + 4)[0]
        sync = [s - 1 for s in struct.unpack_from(">%dI" % count, moov,
                                                  stss[0] + 8)]

    # stsz / stz2 -> sizes. The atom's type is the four bytes before its
    # body (the size field is the four before those).
    if moov[stsz[0] - 4:stsz[0]] == b"stsz":
        default, count = struct.unpack_from(">II", moov, stsz[0] + 4)
        sizes = default if default else list(
            struct.unpack_from(">%dI" % count, moov, stsz[0] + 12))
    else:
        field, count = struct.unpack_from(">xxxBI", moov, stsz[0] + 4)
        raw = moov[stsz[0] + 12:stsz[0] + 12 + (count * field + 7) // 8]
        if field == 4:
            sizes = []
            for byte in raw:
                sizes.append(byte >> 4)
                sizes.append(byte & 15)
            sizes = sizes[:count]
        elif field == 8:
            sizes = list(raw[:count])
        else:
            sizes = list(struct.unpack_from(">%dH" % count, raw, 0))

    # stsc -> (first_chunk0, samples_per_chunk) runs.
    count = struct.unpack_from(">I", moov, stsc[0] + 4)[0]
    triples = struct.unpack_from(">%dI" % (3 * count), moov, stsc[0] + 8)
    stsc_rows = [(triples[3 * i] - 1, triples[3 * i + 1])
                 for i in range(count)]

    # stco / co64 -> chunk offsets, absolute in the file.
    count = struct.unpack_from(">I", moov, stco[0] + 4)[0]
    if moov[stco[0] - 4:stco[0]] == b"co64":
        offsets = list(struct.unpack_from(">%dQ" % count, moov, stco[0] + 8))
    else:
        offsets = list(struct.unpack_from(">%dI" % count, moov, stco[0] + 8))

    return {"timescale": timescale, "edit_offset": edit_offset, "dts": dts,
            "sync": sync, "sizes": sizes, "stsc": stsc_rows,
            "chunk_offsets": offsets}


def _mp4_parse_moov(moov):
    """The first video track's tables, or None."""
    try:
        mvhd = _mp4_find(moov, 0, len(moov), b"mvhd")
        movie_timescale = 1000
        if mvhd:
            version = moov[mvhd[0]]
            movie_timescale = struct.unpack_from(
                ">I", moov, mvhd[0] + (20 if version == 1 else 12))[0] or 1000
        for kind, start, stop in _mp4_children(moov, 0, len(moov)):
            if kind != b"trak":
                continue
            track = _mp4_parse_track(moov, start, stop, movie_timescale)
            if track:
                return track
    except Exception:
        return None
    return None


def _mp4_sample_offset(track, sample):
    """Absolute byte of 0-based `sample`: its chunk's offset plus the
    sizes of the samples before it in that chunk."""
    rows = track["stsc"]
    offsets = track["chunk_offsets"]
    sizes = track["sizes"]
    seen = 0
    for i, (first_chunk, per_chunk) in enumerate(rows):
        last_chunk = rows[i + 1][0] if i + 1 < len(rows) else len(offsets)
        run = (last_chunk - first_chunk) * max(per_chunk, 1)
        if sample < seen + run or i + 1 == len(rows):
            within = sample - seen
            chunk = first_chunk + within // max(per_chunk, 1)
            first_in_chunk = sample - within % max(per_chunk, 1)
            if chunk >= len(offsets):
                return None
            base = offsets[chunk]
            if isinstance(sizes, int):
                return base + sizes * (sample - first_in_chunk)
            return base + sum(sizes[first_in_chunk:sample])
        seen += run
    return None


def _mp4_seat_for(track, seconds):
    """The byte a seek to `seconds` reads first - the last sync sample at
    or before it, where the demuxer lands - or None."""
    try:
        media = int(float(seconds) * track["timescale"]) + track["edit_offset"]
        dts = track["dts"]
        index = bisect.bisect_right(dts, media) - 1
        if index < 0:
            index = 0
        sync = track["sync"]
        if sync:
            k = bisect.bisect_right(sync, index) - 1
            index = sync[max(k, 0)]
        return _mp4_sample_offset(track, index)
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
# "Part 2" is not episode 2 - it is how a film splits itself, and the
# owner's Attack on Titan S1E2 ask was served the franchise's *concert
# film* ("...Movie - Part 2 Sing, Songs That Become Us & Film Live")
# because a loose read took the 2 (30 August 2026, his own log). Same
# rule the season side has always had: anilist._SEASON_NUMBER_RE
# deliberately does not match "Part". Masked before any loose-number
# read; SxxExx statements are unaffected.
_PART_NUM_RE = re.compile(r"\b(?:part|cour)[\s._-]*\d{1,3}\b", re.I)
# A file that names itself a film, concert or compilation is not an
# episode, whoever's fileIdx points at it - the trust the addon pointer
# keeps for unnamed files does not extend to a file that positively
# names itself something else. Word-bounded and deliberately short:
# an episode *title* inside a file name ("Movie Night") is a real case,
# which is why this is consulted only when nothing identifies the
# episode and only to refuse, never to choose.
_NON_EPISODE_RE = re.compile(
    r"\b(movie|film|gekijou\s*ban|gekijouban|concert|compilation|"
    r"recap|ova|oad)\b", re.I)


def _blind_parts(stem: str) -> str:
    """`stem` with the Part/Cour numbers masked out - see _PART_NUM_RE."""
    return _PART_NUM_RE.sub(" ", str(stem or ""))


def _loose_numbers(stem: str) -> set:
    """Every plausible episode number a file name states on its own.

    The same reading `_folder_episode_index` counts with, pulled out so
    `_pick_file` can ask the one question that matters when an addon
    points somewhere the names disagree with: *does the file it chose
    claim to be a different episode?*"""
    out = set()
    for found in _LOOSE_NUM_RE.findall(_blind_parts(stem)):
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
        for found in _LOOSE_NUM_RE.findall(_blind_parts(stem)):
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
        # **An extras-named file is never strong enough to OVERRIDE the
        # addon's pointer.** The owner's Attack on Titan S1E2, 30 August
        # 2026, his own log: a 501-file Ultimate Collection names every
        # season-1 file "[ITA BD Menu NN] ...", so the extras filter
        # emptied `main`, the `or in_season` fallback put the menus
        # back, "Menu 02" read as episode 2 inside a Season 01 folder -
        # "folder" strength - and file 181, a BD menu loop, overrode the
        # addon's correct fileIdx 5. A row that only exists because the
        # extras filter had nothing left is weak evidence by
        # construction, so it keeps "loose": enough to serve when the
        # addon offers nothing, never enough to overrule it.
        strength = "loose" if _EXTRAS_RE.search(best[1]) else "folder"
        return best[0], strength
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
            # The show's name alone is enough only for a file that does
            # not name itself something else: a franchise's concert film
            # carries the title too, and it is not the episode
            # (_NON_EPISODE_RE - the owner's S1E2, 30 August 2026).
            if (title and indexers.is_same_title(str(title), name)
                    and not _NON_EPISODE_RE.search(name)):
                return True
        except Exception:
            return True
    return False




def _pick_file(info, season=None, episode=None, file_index=None, title=None):
    """`_pick_file_chosen` with one line of evidence written down.

    **Because the wrong episode keeps being reported and the log has
    never once said which file was served.** The owner, 27 August 2026:
    Silo S01E01 "plays the wrong ep from another season". The row list
    for that ask is clean - every top row states season 1, measured live
    - and `_pick_file` overrides even a wrong addon `fileIdx` on every
    pack shape that could be modelled here, so the release that actually
    misbehaved cannot be guessed at from this end.

    What settles it is what the pack held and which file was chosen, and
    that is one line per playback start rather than the temporary traces
    that were added and removed twice for this same class of bug."""
    chosen = _pick_file_chosen(info, season, episode, file_index, title)
    try:
        path = ""
        if chosen is not None:
            path = str(info.files().file_path(chosen))
        logs.info(f"pick_file {title or '?'} S{season}E{episode}: "
                  f"fileIdx={file_index} chose={chosen} "
                  f"of {info.num_files()} | {path[-90:] or '(refused)'}")
    except Exception:
        pass                # a diagnostic must never fail playback
    return chosen


def _pick_file_chosen(info, season=None, episode=None, file_index=None, title=None):
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
                # A loose candidate that names itself an extra (a BD
                # menu, a creditless opening) never overrides - it is
                # the demoted all-extras fallback from
                # _identify_episode_file, and serving it over the
                # addon's pointer is how a menu loop played for S1E2.
                try:
                    named_name = str(info.files().file_path(named))
                except Exception:
                    named_name = ""
                if (claimed and int(episode) not in claimed
                        and not _EXTRAS_RE.search(named_name)):
                    return named
            wrong_season = bool(seasons) and int(season or 0) not in seasons
            if wrong_season:
                return named    # None when there is nothing better
            # The pointer's trust covers files that say *nothing* - it
            # does not cover a file that positively names itself a film
            # or a concert when an episode was asked for. The owner's
            # S1E2 played "...Movie - Part 2 Sing, Songs That Become Us
            # & Film Live" on exactly this trust (30 August 2026, his
            # log): nothing in that 4-file pack identified the episode,
            # so the addon's fileIdx stood, and the title guard cannot
            # object to a release that really is the same franchise.
            if (season and episode and named is None
                    and _NON_EPISODE_RE.search(stem)):
                return None
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
        start_at=None, torrent_bytes=None, own=True):
    """Start streaming a torrent; returns its id, or None.

    **`own=False` means "warm this, but it is not yours".** A torrent
    already in `_torrents` is *being served* through it: the stream URL
    is `<hash>/0` and the file is resolved here from `file_index`, so
    moving that index repoints a live stream at a different file.

    The prewarm is the caller that must not do it. Measured 26 August
    2026 from the owner's own log, playing Attack on Titan S01E02 out of
    a Complete Collection pack:

        23:16:36  pick_file route=fresh  asked=S1E2  chose=28  (correct)
        23:16:38  loadfile  seat=151.38    duration 1451s
        23:16:44  pick_file route=reuse   asked=S1E3  chose=29  was=28
                  Thread-17 (_prewarm_worker)

    Six seconds after episode 2 started, the prewarm asked the same pack
    for episode 3 and this moved `file_index` 28 -> 29 underneath it, so
    the server began handing episode 3's bytes to a player showing
    episode 2 - a different episode *and* a different position, which is
    exactly what he reported. His `_playing_file_complete()` guard did
    not stop it because that file was already in the cache from an
    earlier session; and completeness only frees bandwidth, never
    identity.

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
            if not own and wanted is not None and wanted != existing.file_index:
                # Warm it without claiming it: raise that file's
                # priority so its pieces arrive, and leave `file_index`,
                # the other files' priorities and the read focus exactly
                # where the stream being served needs them.
                try:
                    priorities = list(
                        existing.handle.get_file_priorities()
                        or [1] * existing.info.files().num_files())
                    if 0 <= wanted < len(priorities):
                        priorities[wanted] = max(priorities[wanted], 4)
                        existing.handle.prioritize_files(priorities)
                except Exception:
                    pass
                return info_hash
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
        # Verified pieces from a previous play, when there are any -
        # see _cached_resume for what this is worth.
        resumed = _cached_resume(info_hash)
        if resumed is not None:
            try:
                for tracker in list(params.trackers or ()):
                    if tracker not in (resumed.trackers or ()):
                        resumed.trackers.append(tracker)
            except Exception:
                pass
            params = resumed
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
                # From the raw bytes, never via lt.bdecode: torrent_info
                # over a decoded dict re-bencodes it non-canonically and
                # computes a *different* info hash - measured 29 August
                # 2026, 238ec1... against a true 3865dc... for the same
                # file - so the guard below refused every .torrent the
                # caller ever fetched and this fast path never once ran;
                # every first play silently paid the full magnet
                # metadata wait it was written to skip.
                candidate = lt.torrent_info(torrent_bytes)
                try:
                    candidate_hash = str(candidate.info_hashes().v1)
                except AttributeError:      # libtorrent 1.x
                    candidate_hash = str(candidate.info_hash())
                if candidate_hash.lower() == info_hash:
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

    # A resume primes the header only; the head window would otherwise be
    # the first thing asked for and the seat the last - see
    # _apply_windows. `start_at` was applied above, so _start_pieces is
    # already answering by the time this window is built.
    torrent.focus(0, (RESUME_HEADER_PIECES * torrent.piece_length()
                      if start_at is not None else HEAD_BYTES))
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


def finished_file_path(info_hash: str, *, season=None, episode=None,
                       title=None, file_index=None):
    """The complete file this release already holds on disk, or None.

    **The instant replay path** - the owner, 31 August 2026: "instant for
    the videos started watching already before". Everything else here
    goes through the session: add the torrent, wait for metadata, wait
    for the head pieces. Measured on his Attack on Titan S01E02, played
    twice in one run, that was 8.4s of a 9.06s replay; fast-resume
    (see _cached_resume) took it to ~2s, and the rest is libtorrent
    checking the resume blob for six lanes at once, which is time spent
    proving something the file itself already answers.

    So: if the metadata is cached, and the file the episode maps to is
    on disk at exactly the length the torrent says it should be, it is
    the episode - complete, verified once already when it was written -
    and it can be handed straight to the player as an ordinary file. No
    session, no swarm, no wait.

    Deliberately strict. A short file is a partial download and falls
    through to the ordinary path; no metadata means no way to know what
    the length should be, so that falls through too. Being wrong here
    would play a truncated file, which is worse than waiting.
    """
    info = _cached_torrent_info((info_hash or "").lower())
    if info is None:
        return None
    try:
        index = file_index
        if index is None:
            index = _pick_file(info, season, episode, title=title)
        if index is None:
            return None
        files = info.files()
        expected = int(files.file_size(index))
        if expected <= 0:
            return None
        path = os.path.join(_CACHE_DIR, files.file_path(index))
        if not os.path.isfile(path):
            return None
        if int(os.path.getsize(path)) != expected:
            return None         # still downloading - not ours to shortcut
        if not _fully_written(path, expected):
            return None         # the right length, and full of holes
        return path
    except Exception:
        return None



def _fully_written(path: str, expected: int) -> bool:
    """Is every byte of this file actually on disk?

    **A length check is not a completeness check, and that cost three
    bugs.** libtorrent pre-allocates the whole file the moment a torrent
    starts, so a download that has fetched a tenth of the pieces still
    has a file of exactly the right size - it is *sparse*, with holes
    where the missing pieces go. finished_file_path tested only the
    length, so it handed the player a half-empty file as though it were
    a finished one.

    Measured 1 September 2026 on the owner's Reacher S01E02: 2,021,317,323
    bytes on disk, the exact torrent length, real data in the last 4KB and
    **4KB of zeros in the middle**. Played from that file mpv seeks into a
    hole and lands at the end of what it can index, which is his "when I
    click in random time ahead in the progress bar it takes me to the vid
    end"; playback stops on reaching the first hole, which is his "freezes
    after ~25 seconds"; and the buffering readout disagrees with the bytes
    that exist, which is his "99% buffering although it loaded ~16%".

    Asked two ways. NTFS records how much of a sparse file is really
    allocated, and GetCompressedFileSizeW reports it - that is exact and
    costs one call. Where that is unavailable the file is sampled
    instead: a hole reads as zeros, and a compressed video stream does
    not contain 4KB of aligned zeros by accident.

    Wrong in the safe direction on purpose: saying "no" only means the
    ordinary path is taken, which plays. Saying "yes" wrongly is what
    breaks playback.
    """
    try:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCompressedFileSizeW.restype = w.DWORD
            kernel.GetCompressedFileSizeW.argtypes = [w.LPCWSTR,
                                                      ctypes.POINTER(w.DWORD)]
            high = w.DWORD(0)
            low = kernel.GetCompressedFileSizeW(str(path), ctypes.byref(high))
            if low != 0xFFFFFFFF or ctypes.get_last_error() == 0:
                allocated = (int(high.value) << 32) | int(low)
                if allocated > 0:
                    # A hair under, because a compressed volume can store
                    # a complete file in fewer bytes than it holds.
                    return allocated >= expected * 0.98
    except Exception:
        pass
    try:
        holes = 0
        with open(path, "rb") as handle:
            for step in range(1, 12):
                handle.seek(expected * step // 13)
                block = handle.read(4096)
                if block and not any(block):
                    holes += 1
        return holes == 0
    except OSError:
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


def file_index_for(info_hash: str, season=None, episode=None,
                   title=None, file_index=None):
    """Which file in an added torrent holds this episode, **without
    moving anything**.

    A download needs to know its own file; `add()` used to be the only
    way to find out, and it answers by repointing `file_index` - which
    is the pointer the stream server resolves `<hash>/0` through. So a
    download starting while an episode from the same pack was playing
    moved the picture to a different episode. See `add` for the log that
    caught the prewarm doing exactly this.

    **There were two of these, and the second silently ate the first.**
    The owner, 4 September 2026, with a screenshot of a queue in which
    every job read `Failed - file_index_for() takes 1 positional argument
    but 3 were given`. This module defined `file_index_for` twice; the
    later definition won, and it was keyword-only, while two of the three
    call sites in helpers/downloads pass season and episode
    *positionally* - so every download that had to ask which file it
    wanted failed on the call itself. `season` and `episode` are ordinary
    parameters again and the shadowed twin is gone.

    Its body called `_episode_file_index`, which nothing reaches now.
    That function was already unreachable before this change (the
    shadowing is what made it so), so it is left where it is rather than
    removed in passing - see CLAUDE.md rule 11."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.info is None:
        return None
    try:
        return _pick_file(torrent.info, season=season, episode=episode,
                          file_index=file_index, title=title)
    except Exception:
        return None


def file_progress(info_hash: str, index=None) -> dict:
    """How far along a download is: bytes, fraction, rate, and where the
    finished file will be.

    `index` asks about a file other than the one being served - what a
    download needs when the torrent it shares is a season pack with an
    episode playing out of it."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return {}
    wanted_index = torrent.file_index if index is None else index
    if wanted_index is None:
        return {}
    try:
        status = torrent.handle.status()
        wanted = torrent.file_size(wanted_index)
        done = 0
        try:
            done = torrent.handle.file_progress()[wanted_index]
        except Exception:
            done = int(status.progress * wanted)
        path = torrent.file_path(wanted_index)
        return {"done": int(done), "total": int(wanted),
                "fraction": (done / wanted) if wanted else 0.0,
                "rate": int(status.download_rate),
                "peers": int(status.num_peers),
                "path": path,
                "name": os.path.basename(path),
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


def payload_rate(info_hash: str) -> int:
    """Bytes of *payload* a second this swarm is delivering right now -
    libtorrent's own smoothed figure, protocol chatter excluded. 0 for a
    torrent the engine does not hold. What streams.prepare_fastest judges
    a race lane by (see RACE_SETTLE_S there)."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return 0
    try:
        return int(torrent.handle.status().download_payload_rate)
    except Exception:
        return 0


def seat_on_disk(info_hash: str, seconds):
    """_Torrent.seat_on_disk for a hash: True/False when the container's
    index can say whether the bytes a seek to `seconds` reads are here,
    None when it cannot (or the engine does not hold the torrent). The
    player asks before a seek so a jump into pieces the swarm has not
    sent yet - backwards as well as forwards - is narrated and primed
    rather than left to tear."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return None
    return torrent.seat_on_disk(seconds)


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
# critical path - the player already has its url by then.
#
# **60s, not 8 (5 September 2026).** The owner: "the series are super
# slow to load and do not resume". Measured on his Reacher S1E3 resume
# (the Ghost BluRay pack, **16MB pieces**): the url was handed over at
# 13-19s and the piece holding the Cues landed 20-30s after it - and in
# one run of four the seat was never armed at all in 75s while the swarm
# ran at 24MB/s, because this retry had given up at 8s and nothing ever
# asked again. The player's seat poll then waited its whole 180s on a
# band that was never going to exist, which is "do not resume". The poll
# now re-asks as well (player_resume_latency_patch), and only one retry
# runs per torrent at a time (see arm_start_band).
ARM_RETRY_S = 60.0

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
# How long a lane that is *delivering* payload may be waited on before it
# is called dead anyway - past `data_wait_max`, which stays the bound for
# a lane that merely has peers connected. Measured 5 September 2026: the
# owner's hand-picked Jujutsu Kaisen release was receiving 18MB/s at its
# 12s cap and was declared `no-peers` (see WARMING_FILL_PRIORITY for why
# the head had not landed). Killing a lane that is receiving bytes only
# hands the wait to the next candidate, which starts from zero; the
# player is showing "Buffering the first seconds..." for exactly this
# lane, and switching away under that message is the report. 45s is the
# race's own RACE_TIMEOUT - at EXTEND_MIN_BYTES per window (256KB/s) it
# is enough for a 9MB head and index, and nothing slower is a stream.
DELIVERING_CAP_S = 45.0

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
        # The one line that says a seat became a byte, and through which
        # index - the MP4 path had nothing in the log to prove itself by.
        # Once per offset: the seat poll re-arms every two seconds until
        # the band lands, and each re-arm resolves the same byte again
        # (measured: a line every 2s for the whole wait).
        if torrent.start_offset != int(offset) or not torrent.start_armed:
            try:
                logs.info(f"seat band armed: {torrent.info_hash[:8]} "
                          f"{float(torrent.start_seconds):.1f}s -> byte "
                          f"{int(offset)} ({torrent.container_kind() or '?'})")
            except Exception:
                pass
        torrent.start_offset = int(offset)
        torrent.start_armed = True
        # Rewrite the priority array with the band in. **Through the
        # windows as they are, not a fresh head window**: this used to
        # call focus(0, HEAD_BYTES), which files a new PRIMARY window at
        # the head with the newest sequence number - and "newest read
        # first" then hands the head the best deadlines over whatever a
        # live reader is actually blocked on. Harmless inside the 8s the
        # retry used to last; with the retry now outliving playback's
        # first frames (ARM_RETRY_S), it would have re-aimed a playing
        # torrent at 12 pieces of its opening.
        with torrent.lock:
            live = [k for k in torrent.readers if k != PRIMARY_READER]
        if live:
            torrent.refresh_windows()
        else:
            torrent.focus(0, HEAD_BYTES)
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
    #
    # One retry per torrent at a time: the seat poll may call this
    # every couple of seconds now, and several retries racing one
    # another was the reason it used to be forbidden to.
    with torrent.lock:
        running = getattr(torrent, "_arm_thread", None)
        if running is not None and running.is_alive():
            return None

        def keep_trying():
            deadline = time.time() + ARM_RETRY_S
            while time.time() < deadline:
                if _torrents.get(torrent.info_hash) is not torrent:
                    return
                if arm():
                    return
                time.sleep(0.25)
        thread = threading.Thread(target=keep_trying, daemon=True,
                                  name="atomic-torrent-arm")
        torrent._arm_thread = thread
        thread.start()
    return None


def seat_resolvable(info_hash: str):
    """_Torrent.seat_resolvable for a hash, or None when the engine
    does not hold it."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return None
    return torrent.seat_resolvable()


def await_start_band(info_hash: str, wait: float = None) -> bool:
    """Wait, briefly, for the piece playback is going to start on.

    Only makes sense after arm_start_band has resolved an offset. Fails
    soft: over budget hands the url over anyway and the band keeps
    arriving at priority 7. Not waited at all for a file whose seat can
    never be resolved (see seat_resolvable) - that was RESUME_BAND_WAIT
    of loading screen bought for nothing on every mp4 resume."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None or torrent.start_seconds is None:
        return False
    if torrent.seat_resolvable() is False:
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


def _say_gave_up(info_hash, why, started, peers, payload_now, payload_base):
    """One log line per lane given up on - which rule ended it, when,
    and what the swarm had done by then. The owner's reports arrive as
    "it went back to connecting"; this is the line that says why."""
    try:
        from . import logs
        logs.info(f"await_start {str(info_hash)[:8]}: gave up ({why}) at "
                  f"{time.time() - started:.1f}s - peers {peers}, payload "
                  f"{max(0, int(payload_now) - int(payload_base or 0)) // 1024}KB")
    except Exception:
        pass


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
    peers_connected = 0
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
            got_index = not torrent._index_pieces(critical=True)
        if got_data and got_index:
            # Record the verified state now: this is the first moment it
            # is worth anything, and a viewer who watches two minutes
            # and leaves would otherwise never reach the ticker's
            # interval with anything saved.
            try:
                _request_resume_save(torrent.handle)
                _resume_saved_at[(info_hash or "").lower()] = time.time()
            except Exception:
                pass
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
                peers_connected = int(status.num_peers)
                if delivered or peers_connected > 0:
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
                _say_gave_up(info_hash, "dead", started, peers_connected,
                             payload_now, payload_base)
                break
            if now >= data_deadline:
                # Still flowing at the soft deadline? Keep the lane, up
                # to the hard cap. The trailing-window test is what
                # keeps a 288KB-in-six-seconds trickle from holding
                # one - see EXTEND_MIN_BYTES.
                #
                # **Connected counts too, not only delivering.** The
                # 4s solo window was priced when the metadata wait ran
                # first and the swarm connected during it; the .torrent
                # fast path made metadata instant, so a switch inside
                # the player gave a swarm 4s to handshake AND deliver -
                # and live sources started failing as "no peers" (the
                # owner, 30 August 2026). Peers on the wire at the soft
                # deadline are evidence the swarm is real; the hard cap
                # still bounds how long that faith lasts.
                delivering = bool(recent and payload_now - recent[0][1]
                                  >= EXTEND_MIN_BYTES)
                flowing = ((hard_deadline > now
                            and (delivering or peers_connected > 0))
                           # Past the cap, only bytes on the wire keep
                           # the lane - see DELIVERING_CAP_S.
                           or (delivering
                               and now - started < DELIVERING_CAP_S))
                if not flowing:
                    _say_gave_up(info_hash, ("capped" if delivering
                                             else "not delivering"),
                                 started, peers_connected, payload_now,
                                 payload_base)
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

    def _client_gone(self) -> bool:
        """Whether the reader on the other end has closed its side.

        A zero-length peek is the one honest question: a client that is
        merely waiting has nothing readable, one that has hung up reads
        as end-of-stream. Never raises - a socket that cannot be asked is
        treated as gone, which ends a wait nobody could be served from
        anyway."""
        try:
            sock = self.connection
            ready, _, _ = select.select([sock], [], [], 0)
            if not ready:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except Exception:
            return True

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
        # **This loop had no deadline, and two of its paths never send a
        # byte.** `if not data: time.sleep(0.1); continue` spins forever
        # when the file reads back empty and libtorrent will not answer,
        # and the reader on the other end waits with no timeout of its
        # own (player_seekable_stream_patch._EngineStream) - so mpv's
        # stream thread blocked, mpv's play loop blocked behind it, and
        # every property the UI thread asked for blocked behind that.
        # _Torrent.wait_for's docstring already spells out that chain
        # for its own 45s cap; this path simply had no cap at all. That
        # is the owner's "the video player freeze after playing for
        # sometime", 2 September 2026.
        #
        # A stall ends the response instead. The client sees a short
        # read, reopens from where it stopped, and the engine re-aims
        # its window at that offset - which is what it does after every
        # seek anyway, so the recovery path is one that already runs
        # thousands of times a session.
        stalled_since = time.monotonic()
        try:
            while remaining > 0:
                if time.monotonic() - stalled_since > STREAM_STALL_S:
                    logs.info(
                        f"stream stalled at {offset} of {size} after "
                        f"{STREAM_STALL_S:g}s with nothing to send - "
                        f"ending the response so the reader reopens")
                    return
                if offset - focused_at >= STREAM_REFOCUS_BYTES:
                    focused_at = offset
                    torrent.focus(offset, reader=reader)
                piece = torrent.piece_at(offset)
                if not torrent.have(piece):
                    focused_at = offset
                    torrent.focus(offset, reader=reader)
                    # **In slices, so a reader that has hung up is noticed.**
                    # mpv drops its connection the moment it seeks, but
                    # this handler used to sit in a 45s piece wait and
                    # never look - and mpv cannot issue the new Range
                    # until the read it is blocked in returns. Measured 5
                    # September 2026 on the owner's "super slow when I
                    # move forward on the seek bar": clicked 70% in at
                    # 04:54:30, the old read was waiting for piece 62 and
                    # got it at :39, and only then did the seek's own
                    # piece get asked for (3.9s more on that swarm) -
                    # nine of the thirteen seconds were the wait for a
                    # piece nobody wanted any more. A peek at the socket
                    # every half second is what tells "still reading"
                    # from "gone", and the window the dead reader held
                    # is given back with it (end_read, below).
                    waited_from = time.monotonic()
                    served_ok = gone = False
                    while True:
                        if torrent.wait_for(piece, timeout=0.5):
                            served_ok = True
                            break
                        if _torrents.get(torrent.info_hash) is not torrent:
                            break
                        if self._client_gone():
                            gone = True
                            break
                        if time.monotonic() - waited_from >= PIECE_WAIT_S:
                            break
                    waited = time.monotonic() - waited_from
                    if waited >= 1.0:
                        # The owner's "super slow when I move forward on
                        # the seek bar" (5 September 2026) arrived with
                        # no line for a seek anywhere in the log; this
                        # is the wait a seek actually costs, per piece.
                        try:
                            logs.info(f"serve {torrent.info_hash[:8]}: waited "
                                      f"{waited:.1f}s for piece {piece} at "
                                      f"{offset // (1024 * 1024)}MB "
                                      + ("(got it)" if served_ok else
                                         "(reader hung up)" if gone else
                                         "(gave up)"))
                        except Exception:
                            pass
                    if not served_ok:
                        return          # client sees a short read; mpv retries
                    stalled_since = time.monotonic()   # the piece arrived
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
                stalled_since = time.monotonic()   # progress, so start over
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


class _LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the hostname lookup in `server_bind`.

    **This is what was killing the app at launch**, and it took Windows'
    own crash records plus faulthandler to find, because an access
    violation leaves nothing in atomic.log. Reproduced 6 times out of 6
    on 28 August 2026 by launching the way the owner does - a
    double-click in Explorer - against 21 clean launches from a shell,
    which is why it looked random for hours.

    `HTTPServer.server_bind` sets `server_name` from `socket.getfqdn()`.
    That resolves the local hostname, which reaches the codec registry
    for `encodings.idna`, which imports `stringprep` - a **lazy import
    running on the torrent prewarm thread while the main thread is still
    importing the app**. Two threads inside the import machinery at once,
    with PyInstaller's own importer and this package's meta_path hooks in
    the middle of it, and the interpreter faulted: every capture ended

        Current thread ...: <invalid frame>

    with the fault at _PyEval_EvalFrameDefault+0x13eb - the bytecode loop
    dereferencing something already gone.

    `server_name` is only used to fill in Host headers, and this server
    is bound to 127.0.0.1 and only ever talked to by mpv on this machine,
    so the literal address is as correct as the FQDN would have been -
    and it costs no DNS round trip on the startup path either."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def start_server():
    """The local HTTP endpoint, started once, on a free port."""
    global _server, _server_port
    if _server is not None:
        return _server_port
    if lt is None:
        return None
    try:
        _server = _LocalHTTPServer(("127.0.0.1", 0), _Handler)
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


def sequential(info_hash: str, on: bool = True) -> bool:
    """Fetch this torrent's pieces in order. A browser saving the file
    reads it from the first byte and never seeks, and with every piece
    at one priority the head arrives late: measured 7 September 2026 on
    a 416MB episode with 28 peers, the swarm at 7.9MB/s inside ten
    seconds while the reader got 0.4MB/s in those ten, then 10.6MB/s
    over the whole file. The player's add() turns this off again for a
    hash it plays (a seek waits forever under sequential mode - see
    focus)."""
    torrent = _torrents.get((info_hash or "").lower())
    if torrent is None:
        return False
    try:
        torrent.handle.set_sequential_download(bool(on))
        return True
    except Exception:
        return False


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

