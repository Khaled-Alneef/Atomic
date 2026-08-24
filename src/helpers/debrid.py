"""A debrid service, asked to serve a torrent's file over plain HTTPS.

Why this exists: how fast pressing an episode turns into a picture is
bounded below by the swarm when the bytes come from peers - measured 23
August 2026 over 29 runs on the owner's connection, 4 of 29 came in
under 4.5s, per-title medians ran 3.3-16.7s, and the same title spanned
3.2-13.3s inside one hour. A debrid service already holds the popular
releases on its own storage, so a *cached* release is three or four
HTTPS round trips to a CDN URL mpv opens like any other direct stream -
the only route to a consistent 2-4s start. Harbor resolves in the same
order (direct addon URL, then debrid, then its local engine) for the
same reason.

Real-Debrid is the provider wired up. The key comes from two places, in
this order:

  1. Settings > API Keys (the "debrid" row of app_settings.API_KEYS,
     stored in settings.json beside the Stremio authKey) - a key the
     user pastes always wins, so a dead shared token can be overridden
     without a new build;
  2. the token bundled inside the build (`rd_token.txt`, read the same
     way artwork reads `tmdb_token.txt`) - the owner's own paid
     account, shipped at his explicit decision of 23 August 2026 so
     nobody downloading Atomic needs a key of their own. He was told
     Real-Debrid's terms forbid sharing and that multi-IP use gets
     accounts locked, and confirmed he wants it anyway. Which is why
     the cooldown below matters more than it looks: the day that token
     is refused, every copy must degrade to plain torrents silently,
     not break.

Never hardcoded in source, and no account is ever created from here
(.claude/rules/integrations.md: a key is fine, a second installed
application is not). With neither key present every function here
answers None/empty and the player behaves exactly as it did before
debrid existed.

**Everything fails soft.** No key, an expired key answering 401, a rate
limit, a hash the service has not cached, a timeout - every one of
these returns None or an empty set, and the caller falls through to the
torrent race. A debrid outage must never make Atomic worse than the
build that had no debrid at all.

**None of this file has run against the real API.** It was written and
tested against stubbed HTTP on 23 August 2026, before the owner's key
existed; the endpoint shapes follow Real-Debrid's published REST API
(api.real-debrid.com/rest/1.0). The places where the live service
could disagree are marked "unverified without a key" where they occur.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import app_settings, net

# The strict episode reader. Imported the same defensive way streams.py
# imports it: a build where this import fails falls back to the SxxEyy
# regex below, not to a client that will not load.
try:
    from . import indexers
except Exception:  # pragma: no cover
    indexers = None

_API = "https://api.real-debrid.com/rest/1.0"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Per-request ceiling; the caller's deadline bounds the whole exchange
# (net.step_timeout gives each request min(this, what remains)).
REQUEST_TIMEOUT = 6.0

# The whole add -> select -> unrestrict exchange when the caller gave no
# deadline of its own. A cached release answers well inside this; an
# uncached one is known to be uncached at the first status read after
# selection and never spends the rest.
DEFAULT_BUDGET_S = 10.0

# How often /torrents/info is re-read while Real-Debrid converts the
# magnet. A hash it knows converts in well under a second; the sleep is
# so an unknown one does not turn the wait into a request loop.
POLL_S = 0.35

# How many status reads after selectFiles may come back neither
# "downloaded" nor clearly downloading before the release is treated as
# uncached. Two, not one, in case the service reports a transitional
# status for a beat after selection - **unverified without a key**; the
# cost of guessing wrong here is a fall-through to the torrent race,
# never a wrong answer.
CACHE_VERDICT_POLLS = 2

# An expired, revoked or *locked* key answers 401/403 to everything,
# and a traffic-limited one answers 429/509. Re-paying those round
# trips on every episode press would slow the player down for nothing,
# so after one refusal the whole client goes dark for a while -
# available() answers False and not one request is made. This is the
# line that carries the shipped shared token: the day Real-Debrid locks
# it, every copy of Atomic falls back to plain torrents at the cost of
# one failed request per ten minutes, with no dialog, no provider name,
# and no retry loop (the same shape as anilist.RateLimited being told
# apart from "no result"). Settings still shows a pasted key as Set;
# the cooldown is in-memory, per session.
AUTH_COOLDOWN_S = 600.0
# **12s, down from 60.** Measured 23 August 2026 against the owner's own
# account: a 429 darkened the client for a full minute, and the trace
# showed it arriving *mid-lookup* - so one busy press took debrid out of
# the next several presses entirely, which is precisely the owner's
# "sometimes it's instant, mostly it's slow". A minute was never the
# service's own recovery time: re-probed at 0.00/0.35/0.75/1.10s spacing,
# forty consecutive addMagnet calls drew no 429 at all. What actually
# earns one is *volume* of full add->select->poll->delete cycles, and
# REFUSED_FILE below removes most of that volume at the root. Twelve
# seconds still stops a retry storm without costing the next episode.
RATE_COOLDOWN_S = 12.0

# Releases Real-Debrid refuses outright, remembered so they are asked for
# **once ever** rather than once per press.
#
# Measured 23 August 2026, live: /torrents/addMagnet answers
# `HTTP 451 {"error": "infringing_file", "error_code": 35}` for a release
# it will not carry - eleven of sixteen calls in one trace, and *all five*
# top-ranked releases for both House of the Dragon S01E05 and Bleach TYBW
# S01E01. That verdict is about the release, is permanent, and was being
# re-learned on every single episode press: five wasted round trips per
# press, and the request volume that then earned the 429 above.
#
# Persisted rather than in-memory because the cost it removes is paid on
# the *first* press of every session, which is the press the owner
# actually notices.
REFUSED_FILE = "debrid_refused.json"
# No TTL sweep: "we do not carry this" does not expire in any useful
# window, and a file of 40-hex strings is a few KB. Trimmed only if it
# somehow grows past this, oldest first.
REFUSED_MAX = 4000

# The library listing (which hashes the account already holds) is read
# at most this often - one lookup fires one cache check, and a tracker
# page can fire several lookups in a minute.
LIBRARY_TTL_S = 60.0
LIBRARY_PAGE_LIMIT = 500

# /torrents/instantAvailability batching: 40 hashes is ~1.7KB of URL,
# comfortably inside every limit involved.
INSTANT_BATCH = 40

# **Real-Debrid removed /torrents/instantAvailability in November
# 2024** (it answers an error, not an empty map). It is still asked -
# the endpoint may return, and a second provider behind this interface
# would have a working equivalent - but one failure marks it down for
# the session so every lookup after the first costs nothing. What keeps
# cache *detection* working without it is playable_url's add-and-check
# (a cached hash reports "downloaded" immediately after selectFiles),
# plus the account library read in cached_hashes.
INSTANT_DOWN_TTL_S = 3600.0

_HASH_RE = re.compile(r"[0-9a-f]{40}")

# Same suffixes as torrent_engine._VIDEO_SUFFIXES - what the player can
# actually open.
_VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".ts")

# torrent_engine._EPISODE_RE, the fallback reader for a build where
# helpers/indexers failed to import.
_EPISODE_RE = re.compile(r"s(\d{1,2})[\s._-]?e(\d{1,3})", re.I)

# The same floors streams.py draws (_MIN_REAL_SIZE / _MIN_MOVIE_SIZE,
# and the tiny-file check in _prepare_with_own_engine): below these an
# "episode" is a sample and a "movie" is minutes of video wearing the
# title's name - a 62MB "FULL IMAX 1080p" feature film won two races
# before that check existed. Values duplicated rather than imported
# because streams imports this module.
_MIN_EPISODE_BYTES = 30 * 1024 * 1024
_MIN_MOVIE_BYTES = 200 * 1024 * 1024

# Statuses /torrents/info can report. Busy means the service is
# fetching the torrent itself - i.e. the hash was NOT cached; gone
# means the torrent is dead on their side too.
_BUSY_STATUSES = ("queued", "downloading", "compressing", "uploading")
_GONE_STATUSES = ("error", "magnet_error", "virus", "dead")

_cooldown_until = 0.0
_instant_down_until = 0.0
_library_lock = threading.Lock()
_library = (0.0, frozenset())

# The HTTP status of the most recent _api call on *this* thread, so a
# caller can tell a refusal (451) from a timeout without _api having to
# raise. Thread-local because the race runs several lanes at once and a
# shared global would hand one lane another lane's verdict.
_last_status = threading.local()

_refused_lock = threading.Lock()
_refused = None                 # set of hashes, loaded once per session


# Read once and kept: inside a frozen run the bundled file cannot
# change, and _key() runs on every API request.
_bundled = None


def _bundled_token() -> str:
    """The debrid token shipped inside the build, if the owner placed
    one - packaging/rd_token.txt, bundled by Atomic.spec when it exists
    and read here exactly the way artwork._bundled_token reads the TMDB
    one: sys._MEIPASS in the frozen app, packaging/ for a source run.

    Unlike the TMDB token this file is gitignored, never committed: it
    is a paid account credential and this repository is public. A tree
    without the file is a normal state - "" here plus no pasted key
    means debrid stays dark and torrents carry everything, which is the
    whole app before this module existed."""
    global _bundled
    if _bundled is not None:
        return _bundled
    import sys
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.abspath(os.path.join(here, "..", "..", "packaging")))
    for root in roots:
        path = os.path.join(root, "rd_token.txt")
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    token = handle.read().strip()
                if token:
                    _bundled = token
                    return _bundled
        except OSError:
            continue
    _bundled = ""
    return _bundled


def _key() -> str:
    """A key the user pasted wins over the bundled one - same precedence
    as artwork.token(), and for the same reason: a locked or
    rate-limited shipped token can be replaced from Settings without a
    new build."""
    try:
        pasted = app_settings.get_api_key("debrid")
    except Exception:
        pasted = ""
    return pasted or _bundled_token()


def available() -> bool:
    """Whether debrid resolution is worth attempting at all: some key
    exists (pasted or bundled) and it has not just been refused. This is
    the gate every caller checks first, and it is what keeps a keyless
    build at exactly zero extra requests - and a locked shared token at
    one failed request per cooldown."""
    if time.monotonic() < _cooldown_until:
        return False
    return bool(_key())


def _note_http_error(code, auth_matters=True):
    """Turn an HTTP refusal into the right amount of silence. 509 is
    Real-Debrid's "bandwidth limit exceeded" - the shape a shared token
    under too many IPs is most likely to answer with - and it earns the
    short cooldown, not the long one: traffic limits reset."""
    global _cooldown_until
    if code in (401, 403) and auth_matters:
        _cooldown_until = time.monotonic() + AUTH_COOLDOWN_S
    elif code in (429, 509):
        _cooldown_until = time.monotonic() + RATE_COOLDOWN_S


def _api(path, *, deadline, data=None, method=None, auth_matters=True):
    """One API call, parsed. None on any failure - never raises.

    `data` (a dict) makes it a form-encoded POST, which is the only body
    shape this API takes. Bodies are read through net.read_bytes under
    the caller's deadline, never resp.read() - the standing rule, and
    the reason a host dribbling one byte a second cannot hold a player
    thread (helpers/net.py's docstring has the measurement)."""
    _last_status.value = None
    key = _key()
    if not key:
        return None
    timeout = net.step_timeout(deadline, REQUEST_TIMEOUT)
    if timeout is None:
        return None
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    request = urllib.request.Request(
        _API + path, data=body, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": _UA, "Accept": "application/json"})
    try:
        with net.urlopen(request, timeout=timeout) as resp:
            raw = net.read_bytes(resp, net.deadline_in(timeout))
    except urllib.error.HTTPError as exc:
        _last_status.value = exc.code
        _note_http_error(exc.code, auth_matters)
        return None
    except Exception:
        return None
    if not raw:
        return {}        # 204 - the DELETE answers with nothing
    try:
        return json.loads(raw)
    except Exception:
        return None


def refused_hashes() -> set:
    """Every release this service has answered 451 for, from disk.

    Read once per session and kept in memory; a lookup consults it on
    every candidate, so re-reading the file per row would put disk IO
    inside the race."""
    global _refused
    with _refused_lock:
        if _refused is not None:
            return _refused
    try:
        from . import storage
        stored = storage.load(REFUSED_FILE, [])
        loaded = {str(h).strip().lower() for h in stored
                  if isinstance(h, str) and _HASH_RE.fullmatch(str(h).strip().lower())}
    except Exception:
        loaded = set()
    with _refused_lock:
        if _refused is None:
            _refused = loaded
        return _refused


def _remember_refused(info_hash):
    """Record a 451 so this release is never asked for again.

    Failing to write is not a failure of the lookup - the set stays
    correct in memory for this session either way."""
    info_hash = str(info_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(info_hash):
        return
    known = refused_hashes()
    with _refused_lock:
        if info_hash in known:
            return
        known.add(info_hash)
        snapshot = sorted(known)[-REFUSED_MAX:]
    try:
        from . import storage
        storage.save(REFUSED_FILE, snapshot)
    except Exception:
        pass


def _delete_quietly(torrent_id):
    """Remove a torrent this module added and could not use. On its own
    fresh budget, not the caller's - by the time cleanup matters the
    caller's deadline is often spent, and skipping the delete leaves a
    junk "downloading" entry in the owner's account eating one of the
    service's active-torrent slots."""
    if not torrent_id:
        return
    _api(f"/torrents/delete/{torrent_id}", method="DELETE",
         deadline=net.deadline_in(REQUEST_TIMEOUT))


# ----------------------------------------------------- which file it is

def _stem(path: str) -> str:
    """torrent_engine._stem: the file's own name, no directory, no
    extension - the extension has to go or `... - 02.mkv` parses as
    nothing (see that function's comment)."""
    return os.path.splitext(os.path.basename(path or ""))[0]


def _names_episode(stem: str, season, episode) -> bool:
    """torrent_engine._names_episode, over a listing row: does this file
    name say it is the episode asked for. helpers/indexers is the
    stricter reader (it knows a season stated in words outranks a loose
    number) so it is asked first; _EPISODE_RE is the fallback for a
    build where that import failed."""
    if indexers is not None:
        try:
            return indexers.episode_match(stem, season, episode) == "exact"
        except Exception:
            pass
    match = _EPISODE_RE.search(stem)
    return bool(match and int(match.group(1)) == int(season)
                and int(match.group(2)) == int(episode))


def movie_file(videos, title, name_of, size_of):
    """The file in a multi-video release that is the *film* asked for, or
    None. Shared by this module and torrent_engine through
    `torrent_engine.movie_file`, which delegates here.

    **This is the "99% of the sources bring me the wrong movie" bug**
    (the owner, 23 August 2026), and it was not a near miss - it was the
    largest file, every time. `pick_file` answered a movie request with
    `max(videos, key=size)`, which in a pack is simply the biggest film
    in it. Measured live on The Dark Knight (tt0468569), the three
    top-ranked releases:

      263-movie pack   -> served *Monty Python's Life of Brian* (7.12GB)
      86-movie pack    -> served *Beau Is Afraid* (5.41GB)
      1474-file pack   -> served *Blade Runner* (8.85GB)

    all three of which do contain The Dark Knight, several gigabytes
    further down the list. Episodes were never exposed to this: they have
    had "identify it by name or refuse" since a SubsPlease batch served
    the wrong episode. Movies had no such rule because there was no
    episode number to match on - so the rule here is the title.

    `indexers.is_same_title` decides membership (60% of the title's
    significant words must appear - the same measured threshold that
    keeps a title search honest), and `title_match.similarity` picks
    between the survivors, which is what separates "The Dark Knight" from
    "The Dark Knight Rises" sitting in the same pack. Size only breaks a
    tie.

    Refusing is a real answer: the race takes the next release, which is
    the standing trade here - a shorter list beats the wrong film."""
    wanted = str(title or "").strip()
    if not wanted or len(videos) <= 1:
        return videos[0] if len(videos) == 1 else None
    scored = []
    for row in videos:
        stem = _stem(str(name_of(row) or ""))
        if not stem:
            continue
        try:
            if indexers is not None and not indexers.is_same_title(wanted, stem):
                continue
        except Exception:
            pass
        try:
            from . import title_match
            score = title_match.similarity(wanted, stem)
        except Exception:
            score = 0.0
        scored.append((score, int(size_of(row) or 0), row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def pick_file(files, season=None, episode=None, title=None):
    """Which of a debrid torrent's files is the one asked for, or **None
    when that cannot be answered** - the same three rules as
    torrent_engine._pick_file, because a debrid link that plays the
    wrong episode is the exact bug class this codebase keeps paying for
    (a SubsPlease batch served episode 4 for a request for episode 2
    before the engine grew this check).

    `files` is the API's own listing: dicts carrying "id", "path" and
    "bytes". Returns the chosen dict.

    **Deliberately no addon-fileIdx input.** The engine trusts the
    addon's fileIdx because both number the same thing - the torrent's
    file list. The debrid API numbers files with ids of its own, and
    mapping one numbering onto the other is a guess across namespaces,
    which is how wrong episodes ship. Names only; a pack whose names
    identify nothing is refused and the torrent race (which does hold
    the real file list) gets its turn."""
    rows = []
    for entry in files or []:
        if not isinstance(entry, dict):
            continue
        try:
            rows.append((entry, str(entry.get("path") or ""),
                         int(entry.get("bytes") or 0)))
        except Exception:
            continue
    if not rows:
        return None
    videos = [r for r in rows
              if r[1].lower().endswith(_VIDEO_SUFFIXES)] or rows

    if season and episode:
        chosen = _episode_row(videos, season, episode)
        if chosen is None:
            # A multi-file listing where no name states the episode is a
            # refusal, not a guess - the largest file of a 28-episode
            # batch is whichever episode encoded biggest.
            if len(videos) > 1:
                return None
            chosen = videos[0]
        return _big_enough(chosen, _MIN_EPISODE_BYTES)

    # A film. One video is itself; several means a pack, and the largest
    # of a pack is simply the biggest film in it - see movie_file.
    if len(videos) > 1:
        chosen = movie_file(videos, title,
                            name_of=lambda r: r[1], size_of=lambda r: r[2])
        if chosen is None:
            return None
        return _big_enough(chosen, _MIN_MOVIE_BYTES)
    return _big_enough(max(videos, key=lambda r: r[2]), _MIN_MOVIE_BYTES)


def _episode_row(videos, season, episode):
    """The row whose *name* states this episode, or None. An SxxEyy name
    wins over a bare number when both are present - it states the season
    too, so it is the stronger claim (torrent_engine._episode_file_index,
    same order)."""
    loose = None
    for row in videos:
        stem = _stem(row[1])
        match = _EPISODE_RE.search(stem)
        if (match and int(match.group(1)) == int(season)
                and int(match.group(2)) == int(episode)):
            return row
        if loose is None and _names_episode(stem, season, episode):
            loose = row
    return loose


def _big_enough(row, floor):
    """The file dict, unless its stated size proves it is a sample.
    Unknown (zero) sizes pass - the same shape as the engine's
    `0 < served_size < floor` check."""
    entry, _path, size = row
    return entry if size <= 0 or size >= floor else None


# ------------------------------------------------------- cache checking

def cached_hashes(hashes, deadline=None) -> set:
    """Which of these info hashes the service can serve instantly.

    Two sources, cheapest first: the account's own library (anything
    previously resolved through playable_url stays there "downloaded",
    so re-watches and the next episode of the same pack are known
    instantly - and this endpoint *works today*), then the batch
    instantAvailability endpoint, which Real-Debrid removed in November
    2024 and which is asked anyway in case it returns - see
    INSTANT_DOWN_TTL_S. An empty set is the honest answer for "could
    not find out", and costs the caller nothing but an unmarked row."""
    wanted = {str(h or "").strip().lower() for h in (hashes or ())}
    wanted = {h for h in wanted if _HASH_RE.fullmatch(h)}
    if not wanted or not available():
        return set()
    if deadline is None:
        deadline = net.deadline_in(3.0)
    found = _library_hashes(deadline) & wanted
    rest = wanted - found
    if rest:
        found |= _instant_availability(rest, deadline)
    return found


def _library_hashes(deadline) -> set:
    """Every hash the account holds complete, read at most once per
    LIBRARY_TTL_S. A failed read keeps the previous answer rather than
    flapping the flags off and on between lookups."""
    global _library
    with _library_lock:
        stamp, known = _library
        if time.monotonic() - stamp < LIBRARY_TTL_S:
            return set(known)
    rows = _api(f"/torrents?limit={LIBRARY_PAGE_LIMIT}", deadline=deadline)
    if not isinstance(rows, list):
        return set(known)
    fresh = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "downloaded":
            continue
        info_hash = str(row.get("hash") or "").strip().lower()
        if _HASH_RE.fullmatch(info_hash):
            fresh.add(info_hash)
    with _library_lock:
        _library = (time.monotonic(), frozenset(fresh))
    return fresh


def _instant_availability(hashes, deadline) -> set:
    """The batch cache-check endpoint, remembered as down for the
    session after one failure (see INSTANT_DOWN_TTL_S). Its errors are
    never read as an auth problem - a removed endpoint must not dark
    the whole client."""
    global _instant_down_until
    if time.monotonic() < _instant_down_until:
        return set()
    found = set()
    batch = sorted(hashes)
    for start in range(0, len(batch), INSTANT_BATCH):
        chunk = batch[start:start + INSTANT_BATCH]
        body = _api("/torrents/instantAvailability/" + "/".join(chunk),
                    deadline=deadline, auth_matters=False)
        if not isinstance(body, dict):
            _instant_down_until = time.monotonic() + INSTANT_DOWN_TTL_S
            return found
        for info_hash, verdict in body.items():
            variants = (verdict.get("rd")
                        if isinstance(verdict, dict) else None)
            if variants:
                found.add(str(info_hash).lower())
    return found


# ---------------------------------------------------------- resolution

def playable_url(info_hash, season=None, episode=None, deadline=None,
                 title=None):
    """A direct HTTPS URL for this release's right file, or None.

    The Harbor shape: addMagnet -> selectFiles -> unrestrict/link. What
    tells a cached hash from an uncached one is the status immediately
    after selection - "downloaded" means the service already holds the
    bytes and the link is a CDN read; anything it has to fetch first
    reports queued/downloading, in which case the torrent is deleted
    again and None falls the caller through to the race. This
    add-and-check is also the only cache detection that works with
    instantAvailability gone (see cached_hashes).

    Returns {"url", "file_name", "size"} so the caller can carry the
    served file's name the way the engine path does. A torrent that
    resolved successfully is left in the account on purpose: it is what
    makes the next episode of the same pack, and any re-watch, answer
    from the library check instantly."""
    info_hash = str(info_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(info_hash) or not available():
        return None
    # Already refused once - see REFUSED_FILE. Costs nothing and, more to
    # the point, spends none of the per-minute budget that a 429 comes out
    # of, so the lane moves straight on to a release that might answer.
    if info_hash in refused_hashes():
        return None
    if deadline is None:
        deadline = net.deadline_in(DEFAULT_BUDGET_S)

    added = _api("/torrents/addMagnet", deadline=deadline,
                 data={"magnet": f"magnet:?xt=urn:btih:{info_hash}"})
    torrent_id = str((added or {}).get("id") or "")
    if not torrent_id:
        if getattr(_last_status, "value", None) == 451:
            # "infringing_file" - a verdict about this release, not about
            # the account or the connection, and it does not change.
            _remember_refused(info_hash)
        return None

    result, status = None, ""
    try:
        result, status = _resolve(torrent_id, season, episode, deadline,
                                  title)
    except Exception:
        result, status = None, ""
    if result is None and status != "downloaded":
        # Never delete a torrent seen complete: addMagnet may have
        # landed on something already in the owner's library
        # (**unverified without a key** whether the service dedupes),
        # and deleting a finished library entry to tidy up a failed
        # lookup would be destroying their data. Everything else - our
        # own half-added junk - goes.
        _delete_quietly(torrent_id)
    return result


def _resolve(torrent_id, season, episode, deadline, title=None):
    """(result dict or None, last status seen). The status ride-along is
    what playable_url's cleanup decides on."""
    info = _await_files(torrent_id, deadline)
    files = (info or {}).get("files") or []
    status = str((info or {}).get("status") or "")
    if not files or status in _GONE_STATUSES:
        return None, status

    picked = pick_file(files, season, episode, title)
    if picked is None:
        return None, status

    if status == "waiting_files_selection":
        selected = _api(f"/torrents/selectFiles/{torrent_id}",
                        deadline=deadline,
                        data={"files": str(picked.get("id"))})
        if selected is None:
            return None, status
        # The cache verdict: downloaded means cached, busy means the
        # service would have to fetch it first - which is exactly what
        # the local race does better (it starts now; the service's own
        # fetch has no deadline the player can watch).
        for attempt in range(CACHE_VERDICT_POLLS + 1):
            info = _api(f"/torrents/info/{torrent_id}", deadline=deadline)
            status = str((info or {}).get("status") or "")
            if status == "downloaded" or status in _BUSY_STATUSES \
                    or status in _GONE_STATUSES:
                break
            if attempt < CACHE_VERDICT_POLLS:
                if net.step_timeout(deadline, POLL_S) is None:
                    break
                time.sleep(POLL_S)

    if status != "downloaded" or not isinstance(info, dict):
        return None, status

    link = _link_for(info.get("files") or files,
                     info.get("links") or [], picked)
    if not link:
        return None, status
    got = _api("/unrestrict/link", deadline=deadline, data={"link": link})
    url = str((got or {}).get("download") or "")
    if not url.startswith("http"):
        return None, status
    return {"url": url,
            "file_name": (str((got or {}).get("filename") or "")
                          or os.path.basename(str(picked.get("path") or ""))),
            "size": int((got or {}).get("filesize") or 0) or
                    int(picked.get("bytes") or 0)}, status


def _await_files(torrent_id, deadline):
    """The torrent's info once its file list exists. addMagnet answers
    before the magnet is converted; a hash the service knows converts in
    well under a second, and one it does not is given up on at the
    deadline rather than waited out."""
    while True:
        info = _api(f"/torrents/info/{torrent_id}", deadline=deadline)
        if not isinstance(info, dict):
            return None
        status = str(info.get("status") or "")
        if info.get("files") or status in _GONE_STATUSES:
            return info
        if net.step_timeout(deadline, POLL_S) is None:
            return info
        time.sleep(POLL_S)


def _link_for(files, links, picked):
    """The restricted link belonging to the picked file, or None.

    `links` aligns with the *selected* files in listing order - that is
    the API's contract for a torrent this module just selected one file
    of (one selected file, one link). The mapping is still walked
    explicitly rather than assumed, because a torrent that arrived
    already "downloaded" carries whatever selection it was created
    with - and if the picked file is not among its selected ones there
    is no link for it and the honest answer is None (fall through to
    the race), never links[0], which is *some other file*."""
    wanted = (picked or {}).get("id")
    selected = [f for f in files or []
                if isinstance(f, dict) and int(f.get("selected") or 0)]
    if selected and len(links or []) == len(selected):
        for entry, link in zip(selected, links):
            if entry.get("id") == wanted:
                return link
    if len(links or []) == 1 and not selected:
        # Selected flags missing entirely (a shape difference, not a
        # different selection) but exactly one link exists - the one
        # file this module selected.
        return links[0]
    return None
