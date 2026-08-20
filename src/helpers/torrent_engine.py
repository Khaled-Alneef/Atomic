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
# Pieces just behind the urgent band, fetched at middling priority so
# playback does not stall the instant the urgent band is consumed.
SOON_PIECES = 6

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
        "alert_mask": lt.alert.category_t.error_notification
                      | lt.alert.category_t.status_notification,
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        # Announce to a decent number of peers quickly; the default is
        # conservative in a way that shows up as a slow start.
        "connections_limit": 500,
        "active_downloads": 8,
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


def session():
    global _session
    with _session_lock:
        if _session is None and lt is not None:
            _session = _make_session()
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


def prewarm():
    """Bring the session and DHT up before anything is played.

    Bootstrapping the DHT and binding the listen sockets costs several
    seconds, and paying it at the moment someone presses play makes the
    first torrent of a session look slower than every later one. Called
    from a background thread when the player page opens, so by the time
    a source is chosen the session is already routable.

    Cheap to call more than once - the session is built once."""
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

    # -- prioritising -------------------------------------------------
    def focus(self, offset_in_file: int, span_bytes: int = READAHEAD_BYTES):
        """Point the download at this offset.

        **Piece priorities, not just deadlines - this is what makes a
        seek work at all.** The first version set deadlines and left
        `sequential_download` on, and sequential mode pins libtorrent's
        picker to the front of the file: a seek to the middle then waits
        for pieces that are never chosen, the read times out, and mpv
        goes on showing what it already had. Measured exactly that -
        the player reported 74 minutes in and was still displaying the
        opening title card.

        So the window around the read position is raised to priority 7,
        the rest of the file is dropped to 1 (wanted, but only once the
        window is satisfied), and deadlines order the first few pieces
        inside the window. Sequential mode is turned off because these
        priorities *are* the ordering, and leaving it on fights them."""
        try:
            handle = self.handle
            info = self.info
            total_pieces = info.num_pieces()
            file_first = self.piece_at(0)
            file_last = self.piece_at(max(self.file_size() - 1, 0))
            first = max(file_first, self.piece_at(offset_in_file))
            last = min(file_last,
                       self.piece_at(min(offset_in_file + span_bytes,
                                         max(self.file_size() - 1, 0))))

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
            # couple of pieces are urgent, a short band behind them is
            # next, and the rest of the file is merely wanted.
            urgent_count = max(2, -(-URGENT_BYTES // self.piece_length()))
            urgent_last = min(last, first + urgent_count - 1)
            soon_last = min(last, urgent_last + SOON_PIECES)

            priorities = [0] * total_pieces
            for piece in range(file_first, file_last + 1):
                priorities[piece] = 1
            for piece in range(urgent_last + 1, soon_last + 1):
                priorities[piece] = 4
            for piece in range(first, urgent_last + 1):
                priorities[piece] = 7
            handle.prioritize_pieces(priorities)

            # Deadlines on the urgent band only, in order. Spreading
            # deadlines across the whole window is what made every piece
            # look equally due.
            for index, piece in enumerate(range(first, urgent_last + 1)):
                handle.set_piece_deadline(piece, 200 + index * 400)
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
            try:
                self.handle.set_piece_deadline(piece, 200)
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def file_path(self) -> str:
        storage_path = self.handle.status().save_path
        return os.path.join(storage_path, self.info.files().file_path(self.file_index))


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
        file_index=None, metadata_timeout: float = 45.0):
    """Start streaming a torrent; returns its id, or None.

    `trackers` matters more than it looks. Some indexers return a bare
    info hash with no announce list at all, and DHT alone can take
    minutes to find a swarm that trackers would surface in a second."""
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
        try:
            wanted = (file_index if file_index is not None
                      else _pick_file(existing.info, season, episode)
                      if (season and episode) else existing.file_index)
            if wanted is not None and wanted != existing.file_index:
                existing.file_index = wanted
                priorities = [0] * existing.info.files().num_files()
                priorities[wanted] = 7
                existing.handle.prioritize_files(priorities)
                existing.focus(0, HEAD_BYTES)
        except Exception:
            pass
        return info_hash

    os.makedirs(_CACHE_DIR, exist_ok=True)
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

    # Everything except the chosen file is deprioritised to zero - a
    # season pack is a dozen episodes and fetching all of them to watch
    # one is the difference between starting now and starting later.
    try:
        priorities = [0] * info.files().num_files()
        priorities[torrent.file_index] = 7
        handle.prioritize_files(priorities)
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
        torrent.focus(start)

        piece_length = torrent.piece_length()
        offset = start
        remaining = length
        path = torrent.file_path()
        try:
            while remaining > 0:
                piece = torrent.piece_at(offset)
                if not torrent.have(piece):
                    torrent.focus(offset)
                    if not torrent.wait_for(piece):
                        return          # client sees a short read; mpv retries
                # Read no further than the end of the piece we know is
                # present, so a partially written next piece is never
                # handed over as real data.
                piece_end = ((torrent.file_offset() + offset) // piece_length
                             + 1) * piece_length - torrent.file_offset()
                chunk = int(min(remaining, max(piece_end - offset, 1), 1 << 20))
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
        except (BrokenPipeError, ConnectionResetError):
            return                       # the player moved on; normal


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


def release(info_hash: str, delete_files: bool = False):
    """Stop a torrent. Called when the player moves on."""
    torrent = _torrents.pop((info_hash or "").lower(), None)
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
