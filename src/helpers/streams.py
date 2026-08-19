"""Finding something playable for a tracked entry, for the in-app player.

Three routes, in the order they're worth trying:

  1. **Stremio addons.** The addon protocol is a plain GET -
     `<addon>/stream/{type}/{id}.json` where id is `tt1234567` for a
     film or `tt1234567:1:5` for series S1E5 - answering with a list of
     streams that each carry either a direct `url` or a torrent
     `infoHash`. The addon list is not curated here: it is read from
     the user's *own* Stremio account (they already sign in - see
     app_settings.get_stremio_auth), so what plays in their Stremio is
     what plays here, and this file ships no opinion about which addons
     those should be. Anything extra can be added by URL.
  2. **The Stremio local server**, if Stremio Desktop is running on this
     machine. It turns an infoHash into an ordinary HTTP URL on
     127.0.0.1:11470 that any player can open. Without it, torrent
     streams are listed but marked unplayable and say why - this file
     deliberately contains no torrent client.
  3. **Any open site.** `resolve_page(url)` takes *any* watch/episode
     page and digs out the media URL. That is the path for the sites
     the user has configured in anime_sites, and for anything else they
     paste in.

Everything fails soft to an empty list, and the whole chain is bounded
by one deadline rather than a timeout per request (three dead hosts at
6s each is not a 6s bound - see .claude/rules/integrations.md).

DRM is not a failure to be retried. Netflix and Crunchyroll are
Widevine-protected and there is no CDM in this app; reading Crunchyroll
directly is a settled dead end here, tried and removed twice. Those come
back as a `"drm"` stream carrying the service name so the player can say
so and offer the browser, which is the honest answer rather than a
spinner that never resolves.
"""

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import app_settings, net, storage

SITES_FILE = "stream_addons.json"

# The local server Stremio Desktop runs. Fixed port, not configurable in
# Stremio itself.
LOCAL_SERVER = "http://127.0.0.1:11470"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Per-request ceiling. The deadline passed in bounds the whole chain;
# this only stops one request eating all of it.
DEFAULT_TIMEOUT = 6


def _headers(referer: str = None) -> dict:
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        parts = urllib.parse.urlsplit(referer)
        headers["Referer"] = referer
        headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    return headers


def _get(url: str, timeout: float, referer: str = None,
         max_bytes: int = net.MAX_RESPONSE_BYTES) -> str:
    request = urllib.request.Request(url, headers=_headers(referer))
    deadline = net.deadline_in(timeout)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return net.read_text(response, deadline, max_bytes)


def _get_json(url: str, timeout: float):
    return json.loads(_get(url, timeout))


# ---------------------------------------------------------------- addons

def _normalize_addon(url: str) -> str:
    """An addon's base URL, with the manifest filename trimmed.

    Stremio addons are shared *as* their manifest URL - that is what a
    user copies - but every other endpoint hangs off the directory, so
    keeping the filename produces `/manifest.json/stream/...` and a 404
    that looks like the addon being down."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("stremio://"):
        url = "https://" + url[len("stremio://"):]
    return url[:-len("/manifest.json")] if url.endswith("/manifest.json") else url.rstrip("/")


def _load() -> list:
    return storage.load(SITES_FILE, [])


def list_addons() -> list:
    return _load()


def add_addon(url: str, timeout: int = DEFAULT_TIMEOUT):
    """Add an addon by URL, validating it by reading its manifest.

    Validated rather than trusted: an addon that does not answer a
    manifest will not answer a stream either, and finding that out at
    playback time means an empty list with nothing saying why."""
    base = _normalize_addon(url)
    if not base:
        return None
    try:
        manifest = _get_json(base + "/manifest.json", timeout)
    except Exception:
        return None
    if not isinstance(manifest, dict) or "id" not in manifest:
        return None
    addons = _load()
    for existing in addons:
        if existing.get("base_url") == base:
            return existing
    addon = {
        "id": uuid.uuid4().hex[:12],
        "name": manifest.get("name") or base,
        "base_url": base,
        "types": manifest.get("types") or [],
        "from_account": False,
    }
    addons.append(addon)
    storage.save(SITES_FILE, addons)
    return addon


def remove_addon(addon_id: str):
    addons = [a for a in _load() if a.get("id") != addon_id]
    storage.save(SITES_FILE, addons)


def _supports_streams(manifest: dict) -> bool:
    """Whether this addon answers /stream at all.

    Most of a user's installed collection does not - Cinemeta is
    metadata, OpenSubtitles is subtitles - and asking those for streams
    is a guaranteed 404 per entry, per lookup. The manifest says so;
    read it instead of finding out the expensive way."""
    resources = manifest.get("resources") or []
    for resource in resources:
        if resource == "stream":
            return True
        if isinstance(resource, dict) and resource.get("name") == "stream":
            return True
    return False


# Public, keyless addons that answer /stream, each measured against a
# real anime episode (Bleach TYBW S1E1) before being put here - the same
# rule the subtitle sources follow. Nothing goes in this table that was
# not seen to return streams.
#
#   TorrentsDB        50 streams   (more than Torrentio, and the reason
#                                   this table exists at all)
#   Torrentio         33 streams
#   Torrentio Anime   23 streams   same addon, pinned to the anime
#                                  trackers - nyaa/tokyotosho/anidex -
#                                  so a title indexed there but not on
#                                  the general trackers still resolves
#
# Measured and deliberately left out: MediaFusion answered 0, Jackettio
# 1, Comet 403s without its own configuration, StremThru 404s, and
# AIOStreams/Anime Kitsu declare no stream resource at all (Kitsu is a
# catalogue, not a source).
DEFAULT_ADDONS = (
    ("TorrentsDB", "https://torrentsdb.com",
     ("movie", "series", "anime", "other")),
    ("Torrentio", "https://torrentio.strem.fun",
     ("movie", "series", "anime", "other")),
    ("Torrentio Anime", "https://torrentio.strem.fun/providers=nyaasi,tokyotosho,anidex",
     ("movie", "series", "anime", "other")),
)


# The standard public trackers, used only for a stream whose addon
# supplied none of its own (see prepare). These are the long-standing
# open announce URLs that every torrent client ships defaults from -
# nothing title-specific, just a way for the swarm to be discoverable at
# all when the addon did not say where to look.
DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
)

SEEDED_FILE = "stream_addons_seeded.json"


def seed_default_addons() -> list:
    """Add the measured defaults, once ever.

    Runs alongside importing the user's own Stremio collection rather
    than instead of it: their collection is what they chose, these are
    what makes a title resolve when their collection has nothing for it.

    "Once ever" is recorded separately from the addon list rather than
    inferred from it, because those are different questions. Inferring
    it - "this default isn't present, so add it" - means an addon the
    user deliberately removed comes back on the very next lookup, and
    there is no way to get rid of it. A version marker also lets a later
    release add a newly-measured default without re-adding the ones
    already dismissed."""
    seeded = storage.load(SEEDED_FILE, {})
    if not isinstance(seeded, dict):
        seeded = {}
    already = set(seeded.get("urls") or [])
    addons = _load()
    known = {a.get("base_url") for a in addons}
    added = []
    for name, base_url, types in DEFAULT_ADDONS:
        base = _normalize_addon(base_url)
        if base in known or base in already:
            continue
        addons.append({"id": uuid.uuid4().hex[:12], "name": name,
                       "base_url": base, "types": list(types),
                       "from_account": False, "built_in": True})
        added.append(name)
        known.add(base)
        already.add(base)
    if added:
        storage.save(SITES_FILE, addons)
        storage.save(SEEDED_FILE, {"urls": sorted(already)})
    return added


def import_account_addons(timeout: int = 8) -> list:
    """The user's own installed Stremio addons, via the auth key they
    already signed in with.

    This is why no default addon list is shipped: whatever they watch
    with in Stremio is what this plays, without this file taking a view
    on which addons anyone should have."""
    _, auth_key = app_settings.get_stremio_auth()
    if not auth_key:
        return []
    payload = json.dumps({"type": "AddonCollectionGet", "authKey": auth_key,
                          "update": True}).encode()
    request = urllib.request.Request(
        "https://api.strem.io/api/addonCollectionGet", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _UA})
    try:
        deadline = net.deadline_in(timeout)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(net.read_text(response, deadline))
    except Exception:
        return []
    collection = ((body or {}).get("result") or {}).get("addons") or []
    addons = _load()
    known = {a.get("base_url") for a in addons}
    added = []
    for item in collection:
        manifest = item.get("manifest") or {}
        base = _normalize_addon(item.get("transportUrl") or "")
        if not base or base in known or not _supports_streams(manifest):
            continue
        addon = {
            "id": uuid.uuid4().hex[:12],
            "name": manifest.get("name") or base,
            "base_url": base,
            "types": manifest.get("types") or [],
            "from_account": True,
        }
        addons.append(addon)
        added.append(addon)
        known.add(base)
    if added:
        storage.save(SITES_FILE, addons)
    return added


# ----------------------------------------------------------- local server

def probe_local_server(timeout: float = 1.0) -> bool:
    """Whether Stremio's local server is up.

    A TCP connect rather than an HTTP GET: this runs before every lookup
    that has torrent streams in it, and the answer is only "is anything
    listening" - a full request to a port with nothing on it costs the
    same but a request to a *slow* one costs the whole timeout."""
    try:
        with socket.create_connection(("127.0.0.1", 11470), timeout):
            return True
    except OSError:
        return False


# Where Stremio installs itself. The streaming server is not the Stremio
# app: it is `server.js` run by the Node build shipped beside it, and it
# serves happily with no window and no UI. That is what makes "playable
# only while Stremio is open" the wrong behaviour - the machine has
# everything needed, it just is not running yet.
_STREMIO_DIRS = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Stremio"),
    os.path.join(os.environ.get("ProgramFiles", ""), "Stremio"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Stremio"),
)

_server_process = None


def stremio_server_files():
    """(runtime exe, server.js) if Stremio is installed here, else None."""
    for directory in _STREMIO_DIRS:
        if not directory:
            continue
        runtime = os.path.join(directory, "stremio-runtime.exe")
        script = os.path.join(directory, "server.js")
        if os.path.isfile(runtime) and os.path.isfile(script):
            return runtime, script
    return None


def ensure_local_server(wait_seconds: float = 12.0) -> bool:
    """Make sure the streaming server is running, starting it if needed.

    Measured: started this way it logs `EngineFS server started at
    http://127.0.0.1:11470` and serves exactly as it does under the
    Stremio app - 32 peers and 560 KB/s on the first torrent tried.

    Returns False only when Stremio genuinely is not installed, which is
    the one case the UI has to explain rather than fix."""
    global _server_process
    if probe_local_server():
        return True
    found = stremio_server_files()
    if not found:
        return False
    runtime, script = found
    if _server_process is not None and _server_process.poll() is None:
        pass                                   # already starting; just wait
    else:
        # CREATE_NO_WINDOW alone, and **never DETACHED_PROCESS** - that
        # combination is what made a console flash open and shut every
        # time a video started.
        #
        # Measured with a window-event hook over a real open: no Qt or
        # mpv top-level window is ever created, but a
        # CASCADIA_HOSTING_WINDOW_CLASS + PseudoConsoleWindow pair
        # appears for ~129ms. DETACHED_PROCESS gives this server *no*
        # console at all and, per Microsoft's own documentation, makes
        # CREATE_NO_WINDOW a no-op - so every console helper the server
        # itself spawns (which is what opening a torrent does) has to
        # allocate a fresh console, and Windows 11 hands that to the
        # default terminal app. With CREATE_NO_WINDOW alone the server
        # owns an invisible console its children inherit and nothing is
        # allocated. A controlled A/B of the two flag sets is what
        # separated them.
        #
        # CREATE_NEW_PROCESS_GROUP keeps the original intent - the
        # server does not die with Atomic's console signals - without
        # taking the console away.
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            _server_process = subprocess.Popen(
                [runtime, script], cwd=os.path.dirname(script),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=flags)
        except Exception:
            return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if probe_local_server(0.5):
            return True
        time.sleep(0.4)
    return False


def _local_stream_url(info_hash: str, file_index) -> str:
    index = file_index if isinstance(file_index, int) else 0
    return f"{LOCAL_SERVER}/{info_hash.lower()}/{index}"


def prepare(stream: dict, *, season=None, episode=None, timeout: float = 12) -> dict:
    """Make a torrent stream actually playable, immediately before playing.

    **This is the step whose absence made every torrent stream time out.**
    A bare infoHash gives the server nothing to find peers with: DHT
    alone is slow enough to look like a hang, and the first attempt sat
    through twelve 20s reads without a byte. Handing it the addon's own
    `sources` - 27 trackers on a typical Torrentio result - turned the
    same stream into 262KB in 0.8s.

    Done here rather than in find_streams on purpose: a lookup returns
    30-plus streams and creating every one of them would announce to
    every tracker for torrents the user is never going to watch. One
    stream, at the moment it is chosen.

    Returns the stream with its url filled in, or with `reason` set to
    something the player can say out loud."""
    stream = dict(stream or {})
    if stream.get("kind") != "torrent":
        return stream
    info_hash = (stream.get("info_hash") or "").lower()
    if not info_hash:
        return stream

    # Atomic's own engine first. This is the whole point of having one:
    # playback should not require Stremio to be installed, and this path
    # needs nothing outside the app. Stremio's server stays below as a
    # fallback for a build without libtorrent.
    prepared = _prepare_with_own_engine(stream, info_hash, season, episode)
    if prepared is not None:
        return prepared

    if not ensure_local_server():
        stream["url"] = None
        stream["reason"] = ("stremio-not-installed" if not stremio_server_files()
                            else "stremio-server-failed")
        return stream

    # Trackers to find peers through. An addon that supplies its own is
    # trusted; one that supplies none gets the standard public list
    # rather than nothing.
    #
    # **This is the difference between playing and hanging forever.**
    # Measured: a Torrentio stream carries 27 trackers and served bytes
    # in 0.8s, while a TorrentsDB stream for the same kind of title
    # carries *zero* - and with an empty peerSearch the server falls
    # back to DHT alone and reported `peers=0` for 30 seconds straight,
    # never returning from /create at all. The swarm was fine (632
    # seeders); it simply had no way to be found.
    sources = list(stream.get("sources") or [])
    if not sources:
        sources = [f"tracker:{url}" for url in DEFAULT_TRACKERS]
        sources.append(f"dht:{info_hash}")
    payload = {"torrent": {"infoHash": info_hash},
               "peerSearch": {"sources": sources, "min": 40, "max": 150}}
    file_index = stream.get("file_index")
    if file_index is None and (season or episode):
        # The addon did not say which file; let the server match the
        # episode inside a season pack rather than guessing index 0,
        # which on a 13-file pack is a one-in-thirteen chance of the
        # right episode.
        payload["guessFileIdx"] = {"season": int(season or 1),
                                   "episode": int(episode or 1)}
    request = urllib.request.Request(
        f"{LOCAL_SERVER}/{info_hash}/create", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": _UA})
    try:
        deadline = net.deadline_in(timeout)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            created = json.loads(net.read_text(response, deadline))
    except Exception:
        created = {}
    if file_index is None:
        file_index = _pick_file(created, season, episode)
    stream["file_index"] = file_index
    stream["url"] = _local_stream_url(info_hash, file_index)
    stream["reason"] = ""

    # A claimed seeder count is not a live swarm. Measured on one real
    # film: the top result advertised 632 seeders, connected to **zero**
    # peers and never returned a byte, while the next one down (201
    # claimed) reached 6 peers and served 262KB in 0.9s. Waiting on the
    # dead one is exactly the "it takes so long" complaint, and it never
    # resolves - so it is rejected here and the caller moves on rather
    # than the user staring at a buffering message with no end.
    if not _has_peers(info_hash):
        stream["url"] = None
        stream["reason"] = "no-peers"
    return stream


def _prepare_with_own_engine(stream, info_hash, season, episode):
    """Stream through Atomic's own libtorrent engine.

    Returns the finished stream, or None to mean "this build has no
    engine, fall through to Stremio" - deliberately distinct from
    returning a stream with no url, which means "the engine tried and
    this release is dead".

    Trackers are supplied from `DEFAULT_TRACKERS` when the indexer gave
    none of its own; that is not optional, see the note in `prepare`.
    Measured on a real film: metadata in 15s, first piece at 33s, then
    the local bridge served 256KB instantly."""
    try:
        from . import torrent_engine
    except Exception:
        return None
    if not torrent_engine.available():
        return None

    trackers = list(stream.get("sources") or [])
    if not trackers:
        trackers = [f"tracker:{url}" for url in DEFAULT_TRACKERS]
    try:
        added = torrent_engine.add(
            info_hash, trackers=trackers, season=season, episode=episode,
            file_index=stream.get("file_index"))
    except Exception:
        added = None
    if not added:
        stream["url"] = None
        stream["reason"] = "no-metadata"
        return stream
    if not torrent_engine.has_data(info_hash, wait=OWN_ENGINE_DATA_WAIT):
        # Nothing arriving. Release it rather than leaving a dead
        # torrent announcing in the background while the player moves on
        # to the next source.
        try:
            torrent_engine.release(info_hash)
        except Exception:
            pass
        stream["url"] = None
        stream["reason"] = "no-peers"
        return stream
    stream["url"] = torrent_engine.stream_url(info_hash)
    stream["engine"] = "atomic"
    stream["reason"] = "" if stream["url"] else "engine-failed"
    return stream


# How long to wait for the first piece before calling a release dead and
# moving to the next. Longer than the Stremio path's peer check because
# this one is waiting for actual data, not just a peer count - but still
# short enough that walking several dead releases stays bearable.
OWN_ENGINE_DATA_WAIT = 25.0

# How many candidates to start at once in prepare_fastest.
RACE_WIDTH = 3


def prepare_fastest(candidates, *, season=None, episode=None,
                    width: int = RACE_WIDTH, timeout: float = 45.0):
    """Start several sources at once; play whichever delivers data first.

    **Trying them one at a time is most of the wait.** A release's
    advertised seeder count says nothing about whether its swarm will
    actually answer - measured, a film's top result claimed 2081 seeders
    and never completed a piece, and finding that out cost 45 seconds
    before the next one was even started. Started together, the healthy
    one answers while the dead one is still failing, and its failure
    costs nothing.

    Bounded at three, not "all of them": each one opens connections to a
    separate swarm, and past a few that is bandwidth taken away from the
    stream actually about to play. The losers are released as soon as
    there is a winner.

    Returns the prepared stream, or None if none of them started."""
    live = [c for c in (candidates or []) if c.get("info_hash")][:max(1, width)]
    if not live:
        return None
    if len(live) == 1:
        got = prepare(live[0], season=season, episode=episode)
        return got if got.get("url") else None

    import threading
    winner = {}
    done = threading.Event()
    lock = threading.Lock()

    def attempt(candidate):
        try:
            got = prepare(candidate, season=season, episode=episode)
        except Exception:
            return
        if not got.get("url"):
            return
        with lock:
            if winner:
                # Someone else already won; give the swarm back.
                _release_quietly(candidate.get("info_hash"))
                return
            winner.update(got)
        done.set()

    threads = [threading.Thread(target=attempt, args=(c,), daemon=True)
               for c in live]
    for thread in threads:
        thread.start()
    done.wait(timeout)
    with lock:
        result = dict(winner) if winner else None
    if result:
        for candidate in live:
            if candidate.get("info_hash") != result.get("info_hash"):
                _release_quietly(candidate.get("info_hash"))
    return result


def _release_quietly(info_hash):
    if not info_hash:
        return
    try:
        from . import torrent_engine
        torrent_engine.release(info_hash)
    except Exception:
        pass

PEER_WAIT_SECONDS = 6.0


def _has_peers(info_hash: str, wait: float = PEER_WAIT_SECONDS) -> bool:
    """Whether this torrent actually connected to anybody.

    Short on purpose: a live swarm shows peers within a couple of
    seconds, and anything slower is not worth a user's wait when there
    are thirty other releases of the same episode."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"{LOCAL_SERVER}/{info_hash}/stats.json", timeout=4) as response:
                stats = json.loads(response.read(200000))
            if int(stats.get("peers") or 0) > 0:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


_EPISODE_IN_NAME_RE = re.compile(r"s(\d{1,2})[\s._-]?e(\d{1,3})", re.I)


_VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts")


def _file_name(item) -> str:
    """A torrent file's name, whichever key this server used.

    EngineFS reports `path` (a list of segments, or a string); other
    shapes use `name`. Reading only one of them makes every file look
    unnamed, and then the episode match below can never fire."""
    path = item.get("path")
    if isinstance(path, list) and path:
        return str(path[-1])
    if isinstance(path, str) and path:
        return path.rsplit("/", 1)[-1]
    return str(item.get("name") or "")


def _pick_file(created: dict, season, episode):
    """Which file inside a torrent to play - **by position in the
    torrent's own file list**.

    The index is the position, not a field. An earlier version read
    `idx`, which EngineFS does not send: every lookup fell through to 0,
    and file 0 of a release is routinely a sample, an .nfo or the first
    episode of a pack rather than the thing asked for. That is why a
    film with 2081 seeders served no bytes in 45 seconds - the swarm was
    never the problem, the app was asking for the wrong file.

    Prefers a name stating this season and episode (a pack holds a
    dozen), else the largest video - right for a film and for a
    single-episode torrent alike."""
    files = (created or {}).get("files") or []
    if not files:
        return 0
    indexed = list(enumerate(files))
    videos = [(i, f) for i, f in indexed
              if _file_name(f).lower().endswith(_VIDEO_SUFFIXES)] or indexed
    if season and episode:
        for index, item in videos:
            match = _EPISODE_IN_NAME_RE.search(_file_name(item))
            if (match and int(match.group(1)) == int(season)
                    and int(match.group(2)) == int(episode)):
                return index
    return max(videos, key=lambda pair: pair[1].get("length") or 0)[0]


# ------------------------------------------------------------- streams

_QUALITY_RE = re.compile(r"\b(2160p|1440p|1080p|720p|480p|360p|4k)\b", re.I)


def _quality_of(text: str) -> str:
    match = _QUALITY_RE.search(text or "")
    return match.group(1).lower() if match else ""


def _quality_rank(quality: str) -> int:
    order = {"2160p": 5, "4k": 5, "1440p": 4, "1080p": 3, "720p": 2,
             "480p": 1, "360p": 0}
    return order.get((quality or "").lower(), -1)


# How many seeders count as "as good as it gets". Past this, more peers
# stop meaningfully changing how fast playback starts.
_SEEDER_CEILING = 200


def _playability(stream) -> float:
    """How good a *default* pick this stream is.

    Two things decide it, in this order:

    **The resolution the user asked for wins outright.** Settings carries
    a preferred resolution (1080p unless changed) and anything matching
    it is ranked above everything else, whatever its seeder count. A
    picker that quietly starts something other than the chosen
    resolution is not a preference, it is a suggestion.

    **Within a resolution, seeders decide.** Sorting on resolution alone
    once picked a 2160p release with 21 seeders over a 1080p one with
    236 - the largest pieces from the smallest swarm, which cold is
    minutes of black screen and is exactly what "it never plays" looked
    like. Even at 313 seeders the 4K release measured here served no
    bytes in 60s while 1080p started instantly.

    "best" as a preference restores plain highest-resolution-first. The
    full list is always in the picker; this only decides what starts."""
    quality = (stream.get("quality") or "").lower()
    if quality == "4k":
        quality = "2160p"
    rank = _quality_rank(quality)
    seeders = min(int(stream.get("seeders") or 0), _SEEDER_CEILING)
    try:
        from . import app_settings
        preferred = app_settings.get_preferred_resolution()
    except Exception:
        preferred = "1080p"
    if preferred != "best" and quality == preferred:
        # Above every other resolution, but still ordered among its own.
        return 100 + seeders / 40.0
    return rank + seeders / 40.0


def _stremio_id(entry, season=None, episode=None) -> str:
    imdb_id = (entry or {}).get("imdb_id") or ""
    if not imdb_id:
        return ""
    if season and episode:
        return f"{imdb_id}:{int(season)}:{int(episode)}"
    if episode:
        return f"{imdb_id}:1:{int(episode)}"
    return imdb_id


def _stream_from_addon(item: dict, addon_name: str, server_up: bool):
    """One entry of an addon's `streams[]`, as this app's shape."""
    title = (item.get("title") or item.get("name") or "").replace("\n", " ").strip()
    quality = _quality_of(title) or _quality_of(item.get("name") or "")
    url = item.get("url")
    if url:
        kind = "hls" if ".m3u8" in url.lower() else "direct"
        headers = {}
        # Addons pass proxy headers through behaviorHints; a stream that
        # needs a Referer and does not get one is a 403 at play time.
        hints = item.get("behaviorHints") or {}
        proxy = hints.get("proxyHeaders") or {}
        if isinstance(proxy.get("request"), dict):
            headers = {str(k): str(v) for k, v in proxy["request"].items()}
        return {"title": title or addon_name, "url": url, "kind": kind,
                "source": addon_name, "quality": quality, "reason": "",
                "headers": headers}
    info_hash = item.get("infoHash")
    if info_hash:
        # No url yet, and that is not a failure - `prepare()` fills it in
        # at the moment this stream is chosen, because the server has to
        # be given the trackers before it can serve a byte. `server_up`
        # is no longer a precondition either: the server gets started on
        # demand when Stremio is installed, so the only genuinely
        # unplayable case is Stremio not being on the machine at all.
        return {"title": title or addon_name, "url": None, "kind": "torrent",
                "source": addon_name, "quality": quality,
                "reason": "" if server_up else "needs-stremio-server",
                "headers": {},
                "info_hash": str(info_hash).lower(),
                "file_index": item.get("fileIdx"),
                # The trackers to find peers through. Without these the
                # stream resolves to a URL that never returns anything.
                "sources": item.get("sources") or [],
                "seeders": _seeders_of(title)}
    return None


_SEEDERS_RE = re.compile(r"(?:👤|seeders?[:\s]*)\s*(\d+)", re.I)


def _seeders_of(title: str) -> int:
    """Torrentio writes the seeder count into the stream title. It is the
    best available predictor of whether something will actually play, so
    it rides along and sorts."""
    match = _SEEDERS_RE.search(title or "")
    return int(match.group(1)) if match else 0


def _drm_stream(entry):
    """The honest answer for a service that cannot be decrypted here."""
    from . import anime_sites
    provider = anime_sites.streaming_provider(entry.get("site_id")) if entry.get("site_id") else None
    if not provider:
        return None
    return {"title": provider.title(), "url": entry.get("url") or "", "kind": "drm",
            "source": provider, "quality": "", "reason": provider, "headers": {}}


def episode_fallbacks(season, episode, entry=None):
    """Other ways to ask for the same episode, in order of confidence.

    The indexers do not agree with the tracker about season numbering,
    and asking for the one combination that does not exist returns an
    empty list rather than an error. Measured on Bleach: the entry sits
    at S04E04 so the player asked for **S04E05 and got 0 streams**,
    while S01E05 returned 35 and S02E01 returned 30 - the show is there,
    under different numbers. Reporting "no source" for that is wrong; it
    is a numbering mismatch, not a missing title.

    Absolute numbering is the usual reason: anime is very often indexed
    as one long season, so a later season's episode 5 is really episode
    (previous seasons + 5). We cannot know the previous seasons' lengths
    without another lookup, so the cheap, high-yield guesses are tried
    instead - and every one is only *offered*, never assumed: whatever
    actually returns streams is what plays."""
    if not episode:
        return []
    season = int(season or 1)
    episode = int(episode)
    tried, order = set(), []

    def add(s, e):
        if e < 1 or (s, e) in tried:
            return
        tried.add((s, e))
        order.append((s, e))

    add(season, episode)
    if season > 1:
        # Same episode number in season 1 - what an absolutely-numbered
        # index looks like for a continuing show.
        add(1, episode)
        # A season that simply ended: the number before this one.
        add(season, episode - 1)
    add(1, 1)
    return order


def find_streams(entry, *, season=None, episode=None, deadline=None) -> list:
    """Everything playable for this entry, best first.

    Never raises and never returns None - an empty list means nothing
    was found, which the player says out loud."""
    if deadline is None:
        deadline = net.deadline_in(20)
    results = []

    drm = _drm_stream(entry or {})
    if drm:
        results.append(drm)

    imdb_id = (entry or {}).get("imdb_id")
    if imdb_id:
        kind = "movie" if (entry or {}).get("type") == "Movie" else "series"
        server_up = None
        # A film has one id; an episode has several plausible ones (see
        # episode_fallbacks). Each is tried in turn and the first that
        # answers wins - so a numbering mismatch costs one extra request
        # rather than the whole feature.
        attempts = (episode_fallbacks(season, episode, entry)
                    if kind == "series" and episode else [None])
        # Seeding is tried on every lookup, not only when the list is
        # empty, and that is a fix rather than a nicety: gating it on
        # "no addons at all" meant a profile that had already imported
        # the user's Stremio collection never got the defaults, so the
        # lookup ran on Torrentio alone and a title only TorrentsDB
        # indexes came back empty. seed_default_addons() is itself
        # idempotent and records that it has run, so a default the user
        # deletes stays deleted.
        try:
            seed_default_addons()
        except Exception:
            pass
        addons = _load()
        if not addons:
            try:
                import_account_addons()
            except Exception:
                pass
            addons = _load()
        usable = [a for a in addons
                  if not (a.get("types") or []) or kind in (a.get("types") or [])]
        # Counted from here, not from an empty list. `results` may
        # already hold a DRM row - an entry pinned to Netflix or
        # Crunchyroll always gets one - and treating that as "a
        # numbering already answered" skipped the addon loop entirely,
        # so those entries found no torrents at all and reported only
        # "this service is DRM protected". Measured on a Crunchyroll-
        # pinned anime: 1 result in 0.02s, no request made.
        addon_results_before = len(results)
        for attempt in attempts:
            if len(results) > addon_results_before:
                # An earlier numbering already answered; a later guess
                # would return a *different episode* and quietly mix it
                # into the list.
                break
            if attempt is None:
                stream_id = imdb_id
                asked_season = asked_episode = None
            else:
                asked_season, asked_episode = attempt
                stream_id = f"{imdb_id}:{asked_season}:{asked_episode}"
            for addon in usable:
                timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
                if timeout is None:
                    break
                url = (f"{addon['base_url']}/stream/{kind}/"
                       f"{urllib.parse.quote(stream_id, safe='')}.json")
                try:
                    body = _get_json(url, timeout)
                except Exception:
                    continue
                items = (body or {}).get("streams") or []
                if items and server_up is None:
                    # Probed once, and only when something actually came
                    # back that might need it.
                    server_up = probe_local_server()
                for item in items:
                    stream = _stream_from_addon(item, addon.get("name") or "addon",
                                                bool(server_up))
                    if stream:
                        results.append(stream)

    # A saved page URL is worth digging into as well - it is the only
    # route for the sites the user configured themselves.
    page_url = (entry or {}).get("url") or ""
    if page_url.startswith("http") and not drm:
        results.extend(resolve_page(page_url, deadline=deadline))

    return _rank(results)


def _rank(streams: list) -> list:
    """Playable before unplayable, higher quality before lower.

    DRM rows sort last but are kept: they are the explanation, and
    dropping them is what turns "Netflix can't be played here" back into
    an empty list with no reason in it."""
    def key(stream):
        drm = 1 if stream.get("kind") == "drm" else 0
        # A torrent has no url until prepare() runs, so "has a url" would
        # sort every torrent to the bottom as if it were broken. What
        # makes one unplayable is having no way to resolve it at all.
        resolvable = 0 if (stream.get("url") or stream.get("info_hash")) else 1
        return (drm, resolvable, -_playability(stream))
    seen, unique = set(), []
    for stream in sorted(streams, key=key):
        marker = (stream.get("url") or stream.get("info_hash")
                  or (stream.get("kind"), stream.get("title")))
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(stream)
    return unique


def qualities(streams) -> list:
    """The distinct resolutions present, best first - for the player's
    resolution picker. "4k" and "2160p" are the same thing said two ways
    and must not appear as two entries."""
    canonical, seen = [], set()
    for stream in streams or []:
        quality = (stream.get("quality") or "").lower()
        if quality == "4k":
            quality = "2160p"
        if not quality or quality in seen:
            continue
        seen.add(quality)
        canonical.append(quality)
    return sorted(canonical, key=lambda q: -_quality_rank(q))


def matching_quality(streams, quality: str) -> list:
    """Every stream at one resolution, best-seeded first."""
    wanted = "2160p" if (quality or "").lower() == "4k" else (quality or "").lower()
    return [s for s in streams or []
            if ("2160p" if (s.get("quality") or "").lower() == "4k"
                else (s.get("quality") or "").lower()) == wanted]


# -------------------------------------------------------- any open site

# Media the player can actually open. Ordered longest-first so `.m3u8`
# wins over a bare `.ts` segment inside the same URL.
_MEDIA_RE = re.compile(
    r"""https?://[^\s'"<>\\]+?\.(?:m3u8|mpd|mp4|mkv|webm|m4v)(?:\?[^\s'"<>\\]*)?""",
    re.I)
_IFRAME_RE = re.compile(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_VIDEO_SRC_RE = re.compile(
    r"""<(?:video|source)[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
# JWPlayer and the dozen themes that copy it: sources:[{file:"..."}] and
# the bare file:"..." form. Both are everywhere on these sites.
_FILE_RE = re.compile(r"""["']?file["']?\s*:\s*["']([^"']+)["']""", re.I)
_LABEL_RE = re.compile(r"""["']?label["']?\s*:\s*["']([^"']+)["']""", re.I)

# Hosts that appear in an iframe on nearly every one of these pages and
# never hold the video - following them is pure latency.
_NOISE_HOSTS = ("google.com", "googletagmanager.com", "doubleclick.net",
                "facebook.com", "disqus.com", "youtube.com/embed/subscribe",
                "recaptcha", "cloudflareinsights", "histats", "adservice")

_PACKED_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\((.*?)\)\)", re.S)


def _unbase(value: str, base: int) -> int:
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for char in value:
        total = total * base + digits.index(char)
    return total


def _unpack_packed(body: str) -> str:
    """Undo the Dean Edwards packer these embed pages wrap their player
    config in.

    Worth the forty lines: the packed form is the single most common
    shape on open streaming sites, and without unpacking it the page
    contains no media URL at all - the resolver finds nothing and looks
    broken on exactly the sites it exists for. Failure returns "" and
    the caller carries on with the raw body."""
    out = []
    for match in _PACKED_RE.finditer(body):
        args = match.group(1)
        try:
            payload = re.match(r"""\s*['"](.*?)['"]\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*['"](.*?)['"]\.split\(['"]\|['"]\)""",
                               args, re.S)
            if not payload:
                continue
            script, radix, count, words = payload.groups()
            radix, count = int(radix), int(count)
            table = words.split("|")
            script = script.encode().decode("unicode_escape")

            def replace(token, _table=table, _radix=radix):
                index = _unbase(token.group(0), _radix)
                if index < len(_table) and _table[index]:
                    return _table[index]
                return token.group(0)

            out.append(re.sub(r"\b\w+\b", replace, script))
        except Exception:
            continue
    return "\n".join(out)


def _absolute(url: str, page_url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        url = urllib.parse.urlsplit(page_url).scheme + ":" + url
    return urllib.parse.urljoin(page_url, url)


def _media_in(body: str, page_url: str, source: str) -> list:
    """Every playable URL this page states, however it states it."""
    found, seen = [], set()

    def add(url, quality=""):
        url = _absolute(url, page_url)
        if not url.lower().startswith("http") or url in seen:
            return
        if not _MEDIA_RE.match(url) and ".m3u8" not in url.lower():
            return
        seen.add(url)
        lowered = url.lower()
        found.append({
            "title": f"{source} {quality}".strip(),
            "url": url,
            "kind": "hls" if (".m3u8" in lowered or ".mpd" in lowered) else "direct",
            "source": source,
            "quality": quality or _quality_of(url),
            "reason": "",
            # Referer/Origin of the page it was found on. Most of these
            # hosts 403 the media URL without it, which is why the same
            # link plays in a browser and not in a player.
            "headers": _headers(page_url),
        })

    for match in _VIDEO_SRC_RE.finditer(body):
        add(match.group(1))
    # file:"..." with the label that usually sits beside it
    for match in _FILE_RE.finditer(body):
        window = body[match.end():match.end() + 200]
        label = _LABEL_RE.search(window)
        add(match.group(1), label.group(1) if label else "")
    for match in _MEDIA_RE.finditer(body):
        add(match.group(0))
    return found


def resolve_page(page_url: str, *, deadline=None, _depth: int = 0) -> list:
    """Playable streams from any open watch page.

    Chases iframes, because nearly every one of these sites embeds a
    third-party player rather than serving the file itself - a resolver
    that reads only the first page finds nothing on the majority of
    them. Two levels deep and a handful of frames per level: past that
    it is following adverts, and the deadline is being spent on pages
    that have never held a video."""
    if deadline is None:
        deadline = net.deadline_in(15)
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None or not (page_url or "").startswith("http"):
        return []
    host = urllib.parse.urlsplit(page_url).netloc
    try:
        body = _get(page_url, timeout, referer=page_url if _depth else None)
    except Exception:
        return []

    unpacked = _unpack_packed(body)
    results = _media_in(body + ("\n" + unpacked if unpacked else ""),
                        page_url, host)
    if results or _depth >= 2:
        return _rank(results)

    for match in list(_IFRAME_RE.finditer(body))[:4]:
        frame = _absolute(match.group(1), page_url)
        lowered = frame.lower()
        if not lowered.startswith("http") or any(n in lowered for n in _NOISE_HOSTS):
            continue
        if net.step_timeout(deadline, DEFAULT_TIMEOUT) is None:
            break
        results.extend(resolve_page(frame, deadline=deadline, _depth=_depth + 1))
        if results:
            break
    return _rank(results)


def playable_check(stream: dict, timeout: float = 6) -> bool:
    """Whether a resolved URL actually serves media, by asking for the
    first bytes only.

    A Range request rather than a HEAD: these hosts routinely answer 405
    or 404 to HEAD while serving the same URL perfectly to a GET, so a
    HEAD-based check discards working streams."""
    url = stream.get("url") or ""
    if not url.startswith("http"):
        return False
    headers = dict(stream.get("headers") or {})
    headers.setdefault("User-Agent", _UA)
    headers["Range"] = "bytes=0-1023"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read(1024)
    except Exception:
        return False
    if any(marker in content_type for marker in
           ("video", "mpegurl", "octet-stream", "dash+xml", "mp4")):
        return True
    # An HLS playlist is text/plain on plenty of hosts; its first line is
    # not ambiguous.
    return body.lstrip().startswith(b"#EXTM3U")
