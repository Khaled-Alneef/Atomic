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
        "listen_interfaces": "0.0.0.0:0,[::]:0",
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
    threading.Thread(target=lambda: (session(), start_server()),
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


def _pick_file(info, season=None, episode=None) -> int:
    """Which file in the torrent to play - the episode if its name says
    so, else the largest video."""
    files = info.files()
    candidates = []
    for index in range(files.num_files()):
        name = files.file_path(index)
        candidates.append((index, name, files.file_size(index)))
    videos = [c for c in candidates
              if c[1].lower().endswith(_VIDEO_SUFFIXES)] or candidates
    if season and episode:
        for index, name, _size in videos:
            match = _EPISODE_RE.search(os.path.basename(name))
            if (match and int(match.group(1)) == int(season)
                    and int(match.group(2)) == int(episode)):
                return index
    return max(videos, key=lambda c: c[2])[0]


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
        existing.last_touched = time.time()
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
