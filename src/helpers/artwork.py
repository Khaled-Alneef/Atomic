"""Title logos from TMDB, for the player's loading indicator.

TMDB is the only source that publishes proper *logos* - the title
treatment as a transparent PNG, not a poster with the name printed on
it. That transparency is the whole point here: the player draws the logo
ghosted while a source is still buffering and fills it with its own
colour as the buffer grows, so the thing you are waiting for is the
thing telling you how long is left.

Two halves, and only one of them needs a key:

  * **Finding the logo** goes through api.themoviedb.org, which answers
    401 without a key. The owner pastes one in Settings (see
    `.claude/rules/integrations.md` - keys are allowed, installed apps
    are not), and until then this whole module simply returns None and
    the player falls back to its text status.
  * **Fetching the image** is `image.tmdb.org`, which is a plain CDN and
    needs no key at all. Measured: 200 and real image bytes with no
    credentials.

Everything fails soft. A missing key, a title TMDB has never heard of,
or a series with no logo uploaded are all the same answer - None - and
none of them is worth interrupting playback over.
"""

import hashlib
import json
import os
import threading
import time
import re
import urllib.error
import urllib.parse
import urllib.request

from . import app_settings, logs, net, storage

API = "https://api.themoviedb.org/3"
CDN = "https://image.tmdb.org/t/p"
# w500 rather than original: this is drawn at a few hundred pixels wide
# behind a loading message, and original logos run to several thousand.
LOGO_SIZE = "w500"
# The backdrop fills the whole player frame and the details page's
# entire ground. This was w1280 and the owner called the result blurry -
# a 1280px still stretched across a 2560px window is soft by
# construction. `original` costs a few MB once per title (it is cached
# on disk forever), and the pages that draw it now cache their scaled
# copy per window size, so the decode is not paid per paint.
BACKDROP_SIZE = "original"

DEFAULT_TIMEOUT = 8

_UA = "Atomic/1.0"


class Unreachable(Exception):
    """TMDB could not be *asked* - a refused host, a reset handshake, a
    timeout, a body that never arrived.

    Deliberately distinct from "TMDB answered, and has no artwork for
    this title", and that distinction is the whole point of this class:
    only the second one may write a `.none` marker. Both were the same
    answer - None - until 4 September 2026, when the owner's second
    machine showed no logo on any watchable title and the blurred cover
    where the backdrop belongs. helpers/net's own note records that
    machine's network resetting api.themoviedb.org at the TLS handshake,
    so every lookup there fails in transport; each failure was then
    filed as "there is no logo for this title", which stands for six
    hours (NEGATIVE_TTL_S) without asking again. One bad moment cost a
    session; a blocked host cost every title, permanently.

    A server that *answered* - 401, 404, 429 - is not this. It is
    reachable and it said something, so `_get_json` re-raises the
    HTTPError untouched and callers can still read `.code`."""


# A blocked host fails on every title of every page, so the line naming
# the cause is written once a minute per host - not forty times a page.
_SAY_EVERY_S = 60.0
_said = {}
_said_lock = threading.Lock()


def _say_once(key: str, message: str):
    now = time.monotonic()
    with _said_lock:
        when = _said.get(key)
        if when is not None and now - when < _SAY_EVERY_S:
            return
        _said[key] = now
    logs.warning(message)


def _safe_host(url) -> str:
    """The host, and never the URL.

    **A v3 key travels in the query string** (see `_get_json`), so a
    logged URL would write the owner's key into the file he pastes into
    a bug report. The host is the whole diagnostic anyway - what the log
    has to answer is "did this machine reach TMDB at all"."""
    return net._url_host(url) or "api.themoviedb.org"


def _cache_dir():
    path = storage.DATA_DIR / "logo_cache"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Every caller reaches this *before* its own try block, so an
        # unwritable data directory used to leave the loading screen
        # blank with nothing anywhere saying why - the same silence this
        # whole pass exists to end.
        _say_once("cache-dir", f"logo cache unusable at {path}: "
                               f"{type(exc).__name__}")
        raise
    return path


def _bundled_token() -> str:
    """The token shipped inside the build, if there is one.

    Kept in a file next to the frozen app rather than written into
    source: this repository is public, and a token pushed to GitHub gets
    found by secret scanners and revoked - which would break the feature
    for every copy at once instead of none. packaging/Atomic.spec bundles
    it and .gitignore keeps it out of the repo.

    Its whole purpose is that nobody running Atomic has to obtain a key
    of their own.

    **Verified in the shipped artefact, 4 September 2026**, after the
    owner reported from a second machine that his key was "not correctly
    embedded". It is: `Atomic.zip` -> `Atomic.exe` -> the PyInstaller
    CArchive carries `tmdb_token.txt` at the bundle root, 32 bytes, and
    those bytes are byte-identical to packaging/tmdb_token.txt, which
    answers 200 live. So a machine showing no artwork is not a build
    that lost the key - look for the `TMDB unreachable` line _get_json
    now writes, which is what that machine's atomic.log had no way to
    say before."""
    import sys
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.abspath(os.path.join(here, "..", "..", "packaging")))
    for root in roots:
        path = os.path.join(root, "tmdb_token.txt")
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    token = handle.read().strip()
                if token:
                    return token
        except OSError:
            continue
    return ""


def token() -> str:
    """A key the owner pasted wins over the bundled one, so a revoked or
    rate-limited shipped token can always be replaced without a new
    build."""
    return app_settings.get_tmdb_key() or _bundled_token()


def available() -> bool:
    return bool(token())


def _is_bearer(credential: str) -> bool:
    """A v4 read token is a JWT - three dot-separated parts. A v3 key is
    a bare 32-character hex string."""
    return credential.count(".") == 2 and len(credential) > 60


def _get_json(url, timeout):
    # **Both credential shapes, decided per token, because the shipped
    # one has changed shape since this was written.** The comment here
    # used to say the bundled token was a v4 read token for which v3
    # auth answers 401. Measured 4 September 2026, against the file
    # actually committed at packaging/tmdb_token.txt: it is 32
    # characters, no dots - a **v3 key** - and the three ways of asking
    # come back
    #
    #     v3 key as ?api_key=      200, real JSON
    #     v3 key as Bearer         401 "Invalid API key"
    #     no credential            401
    #
    # so the branch below is not a nicety, it is the only one of the two
    # that works for the token this build ships. `_is_bearer` routes it,
    # and the same code keeps working if the owner ever pastes a v4 one.
    credential = token()
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if _is_bearer(credential):
        headers["Authorization"] = f"Bearer {credential}"
    else:
        # v3 key: a query parameter, not a header. Accepting both means
        # either of the two things TMDB's site calls "your key" works,
        # instead of one of them silently 401ing.
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}api_key={urllib.parse.quote(credential)}"
    request = urllib.request.Request(url, headers=headers)
    # **Skip a host that has already refused twice**, rather than paying
    # its timeout again per card. On the owner's work network TMDB's
    # handshake is reset, and this path was measured making 187 attempts
    # in one session - each one a cover or a rating waiting on a
    # connection that was never going to open. See net.host_refusing.
    if net.host_refusing(url):
        raise Unreachable(f"{_safe_host(url)} is refusing connections")
    deadline = net.deadline_in(timeout)
    try:
        with net.urlopen(request, timeout=timeout) as response:
            answer = json.loads(net.read_text(response, deadline))
    except urllib.error.HTTPError as exc:
        # **A status is not a refusal, and this used to count it as one.**
        # `note_host_failure` on any exception meant two 404s - a title
        # TMDB has unlinked is enough - wrote the whole host off for ten
        # minutes (net.REFUSED_HOST_TTL_S), so every logo, backdrop,
        # poster and episode rating in the app then failed without a
        # request. A server that answered is alive; only transport
        # counts. Re-raised untouched so callers keep `.code`.
        net.note_host_success(url)
        if exc.code in (401, 403):
            # The one failure the owner has to be *told* about rather
            # than shown as missing art: a revoked or rate-limited key
            # looks exactly like a title with no logo, and the answer to
            # it is a key pasted in Settings, not a rebuild.
            _say_once(f"{_safe_host(url)}-auth",
                      f"TMDB refused the key ({exc.code}) - artwork will be "
                      f"missing until a working key is pasted in Settings")
        else:
            _say_once(f"{_safe_host(url)}-{exc.code}",
                      f"TMDB answered {exc.code}")
        raise
    except Exception as exc:
        net.note_host_failure(url)
        _say_once(f"{_safe_host(url)}-down",
                  f"TMDB unreachable at {_safe_host(url)}: "
                  f"{net.why(exc)} - no logos or backdrops "
                  f"while this lasts")
        raise Unreachable(f"{_safe_host(url)}: {net.why(exc)}") from exc
    net.note_host_success(url)
    return answer


# TMDB's genre id for Animation, on both the tv and the movie lists.
_ANIMATION_GENRE_ID = 16
# Atomic's own kinds, lowered, and which TMDB rows may answer a *title*
# search for each: the lists to ask in order, and whether the row must
# be animated (True), must not be (False), or may be either (None).
_KIND_SEARCH = {
    "anime": (("tv", "movie"), True),
    "manga": (("tv", "movie"), True),
    "manhwa": (("tv", "movie"), True),
    "manhua": (("tv", "movie"), True),
    "reading": (("tv", "movie"), True),
    "series": (("tv",), False),
    "movie": (("movie",), None),
    "movies": (("movie",), None),
}
_TITLE_YEAR_RE = re.compile(r"\(\s*((?:19|20)\d\d)\s*\)\s*$")


def _row_agrees(row, animated, year) -> bool:
    """Whether a search row can be the kind (and year) the caller
    already knows the title to be. `animated` None accepts either."""
    if animated is not None:
        genres = row.get("genre_ids") or []
        if (_ANIMATION_GENRE_ID in genres) != animated:
            return False
    if year:
        stamp = str(row.get("first_air_date") or row.get("release_date") or "")
        if stamp[:4].isdigit() and abs(int(stamp[:4]) - year) > 1:
            return False
    return True


def _tmdb_id(imdb_id: str, timeout, title: str = "", kind: str = ""):
    """TMDB's own id and media type for an IMDb id.

    **`kind` makes the title fallback agree with what the caller already
    knows.** Measured 2 September 2026 on "Kingdom", which is a 2012
    anime, a 2014 US drama and a 2019 Korean series on TMDB's tv list:
    the fallback took the first exact name (today the anime, because
    TMDB ranks it first - a popularity order this app does not control)
    for *every* kind, so a Series called Kingdom with no id would have
    carried the anime's poster, and the day TMDB reorders, the reverse.
    An Anime or reading kind therefore needs an animated row (genre 16,
    and Japan is preferred among those), a Series a non-animated one, a
    Movie the movie list only; a "(2019)" on the asked title must agree
    within a year. The id path is untouched - an id cannot pick the
    wrong show, and it answers tt2404499 as 46437 correctly.

    `find` rather than `search`: the tracker already stores an IMDb id
    for everything it resolved, and matching on an id cannot pick the
    wrong show the way a title search can - which this project has been
    bitten by before.

    **`title` is the fallback for an id TMDB has unlinked, and it is a
    real case, measured 24 August 2026:** `find/tt14986406` (Bleach:
    Thousand-Year Blood War) answers zero rows in every list - TMDB
    files TYBW as seasons of Bleach and no longer maps that IMDb id -
    so the loading screen that had shown its logo all week lost it the
    moment the cache was evicted (the owner's "why do the bleach tybw
    logo do not show while loading!!"). A guarded title search rescues
    exactly this: the answer must match the asked title at 0.85, **or
    be a strict prefix of it** - "Bleach" for "Bleach: Thousand-Year
    Blood War" is the franchise parent, and franchise art on a loading
    screen is right where no art is not. Anything looser inherits
    another show's artwork, which this project has been bitten by
    before too."""
    # An empty id skips straight to the title search. `find/` with no id
    # is a 404, which used to raise out of here before the fallback
    # below could run - and a title with no IMDb id at all (every
    # reading row, and any catalogue row Cinemeta filed without one) is
    # exactly the case `poster_url` needs answered.
    if imdb_id:
        url = f"{API}/find/{urllib.parse.quote(imdb_id)}?external_source=imdb_id"
        body = _get_json(url, timeout)
        for kind, field in (("tv", "tv_results"), ("movie", "movie_results")):
            rows = (body or {}).get(field) or []
            if rows:
                return kind, rows[0].get("id")
    title = (title or "").strip()
    if not title:
        return None, None
    from . import title_match
    lists, animated = _KIND_SEARCH.get((kind or "").strip().lower(),
                                       (("tv", "movie"), None))
    stamped = _TITLE_YEAR_RE.search(title)
    year = int(stamped.group(1)) if stamped else 0
    if stamped:
        title = title[:stamped.start()].strip()
    asked = title.lower()
    for media in lists:
        try:
            found = _get_json(
                f"{API}/search/{media}?query={urllib.parse.quote(title)}",
                timeout)
        except Unreachable:
            # Never swallowed: returning (None, None) here reads to the
            # caller as "TMDB has never heard of this title" and earns a
            # six-hour `.none` marker for a title whose logo is fine.
            raise
        except Exception:
            continue
        # Every acceptable row on this list, then the best of them: an
        # exact name over a franchise prefix, and among animated rows a
        # Japanese one over the rest - TMDB's "Kingdom" tv list carries
        # the anime and, under the same name, nothing else animated, but
        # a title shared by a Western cartoon would otherwise be decided
        # by TMDB's popularity order alone.
        best, best_rank = None, None
        for row in (found or {}).get("results") or []:
            name = str(row.get("name") or row.get("title") or "").strip()
            if not name or row.get("id") is None:
                continue
            lowered = name.lower()
            prefix = (asked.startswith(lowered)
                      and len(lowered) >= 4
                      and (len(asked) == len(lowered)
                           or not asked[len(lowered)].isalnum()))
            exact = title_match.similarity(title, name) >= 0.85
            if not (prefix or exact):
                continue
            if not _row_agrees(row, animated, year):
                continue
            rank = (0 if exact else 1,
                    0 if (animated and "JP" in (row.get("origin_country") or []))
                    else 1)
            if best_rank is None or rank < best_rank:
                best, best_rank = row, rank
        if best is not None:
            return media, best.get("id")
    return None, None


def get_json(url, timeout: int = DEFAULT_TIMEOUT):
    """A TMDB endpoint as parsed JSON, with this module's auth on it.

    Public so a second TMDB caller (ratings.py) does not have to reach
    for the underscore - or, far worse, copy the v3/v4 credential
    handling and the bounded `net.read_text` into itself, which is
    exactly how five modules once missed the bounded read entirely."""
    return _get_json(url, timeout)


def tmdb_id(imdb_id: str, timeout: int = DEFAULT_TIMEOUT):
    """(media type, TMDB id) for an IMDb id - see `_tmdb_id`. Public for
    the same reason as `get_json` above; the id lookup is the one request
    every TMDB feature starts with."""
    return _tmdb_id(imdb_id, timeout)


# The poster size the tracker's tiles are cut from. w500 rather than
# `original`: POSTER_SIZE is 160x216 logical, so even at DPR 2 a 500px
# wide poster is more than the tile needs, and `original` runs to 2000px
# for a picture nothing here draws that large.
POSTER_SIZE_PATH = "w500"


def poster_url(imdb_id: str = "", title: str = "",
               timeout: int = DEFAULT_TIMEOUT, kind: str = ""):
    """A TMDB poster URL for a title, or None.

    **The second art source for video rows, and the reason it exists is
    a report rather than a theory** (24 August 2026): a friend's fresh
    install showed no art anywhere on Watch or Read while the Watch
    schedule - whose covers come from s4.anilist.co - filled normally.
    Every Watch row's art comes from one host, `images.metahub.space`
    (measured: 35 of 36 Discover rows across anime/series/movie), so a
    machine that cannot reach that one host loses every cover on the
    page while a different host's art on the next tab is fine. There was
    no second source to fall back to. Now there is, and it is the one
    TMDB key a user is already asked for in Settings.

    Matched on the IMDb id where there is one, which cannot pick the
    wrong show; a title is passed through `_tmdb_id`'s guarded search
    (0.85, or a strict prefix for a franchise parent) rather than a bare
    best match, for the reason this project keeps re-learning - a
    confidently wrong cover is worse than a blank tile.

    Returns a CDN URL, not a file: the caller already owns a download
    cache keyed by URL, and routing this through it means the fallback
    is cached, shrunk and trimmed exactly like the primary."""
    if not token():
        return None
    imdb_id = (imdb_id or "").strip()
    title = (title or "").strip()
    if not imdb_id and not title:
        return None
    try:
        media, ident = _tmdb_id(imdb_id, timeout, title, kind)
    except Exception:
        return None
    if not ident:
        return None
    try:
        body = _get_json(f"{API}/{media}/{ident}", timeout) or {}
    except Exception:
        return None
    path = body.get("poster_path")
    return f"{CDN}/{POSTER_SIZE_PATH}{path}" if path else None


def _search_logo(title: str, timeout):
    """A logo for the franchise when the exact entry has none.

    Bleach: Thousand-Year Blood War is filed on TMDB as its own series
    (id 332002) which carries **zero** logos in any language, while the
    parent Bleach series has them. A title treatment is franchise
    artwork, so the parent's is the right picture for the child.

    Searching by name is normally forbidden in this codebase - it is how
    the wrong series gets matched - and the reason it is allowed here is
    that the failure mode is different in kind. A wrong *logo* is a
    cosmetic mistake on a loading screen; a wrong *episode* or a wrong
    release schedule is a silent lie about the user's data. This is only
    ever reached after the id lookup, which stays authoritative, and only
    for artwork.

    The subtitle is dropped first ("Bleach: Thousand-Year Blood War" ->
    "Bleach") because that is exactly the parent this is looking for."""
    head = re.split(r"[:–-]", title or "", 1)[0].strip()
    if not head:
        return None
    for query in ([head] if head.lower() == (title or "").strip().lower()
                  else [head, title]):
        try:
            found = _get_json(
                f"{API}/search/tv?query={urllib.parse.quote(query)}", timeout)
        except Unreachable:
            raise               # see the same clause in _tmdb_id
        except Exception:
            continue
        for row in (found.get("results") or [])[:3]:
            try:
                images = _get_json(f"{API}/tv/{row['id']}/images", timeout)
            except Unreachable:
                raise
            except Exception:
                continue
            path = _best_logo(images)
            if path:
                return path
    return None


def _best_logo(images: dict):
    """The logo worth drawing.

    English first, then a language-neutral one (`iso_639_1` null - these
    are usually the original title treatment and look right on any
    locale), then whatever exists. PNG is preferred over SVG because Qt
    renders PNG without an extra dependency, and over JPG because a JPG
    logo has no transparency and the fill effect needs it."""
    logos = (images or {}).get("logos") or []
    if not logos:
        return None

    def rank(logo):
        language = logo.get("iso_639_1")
        path = str(logo.get("file_path") or "")
        return (0 if language == "en" else 1 if language is None else 2,
                0 if path.lower().endswith(".png") else 1,
                -(logo.get("vote_average") or 0))

    best = sorted(logos, key=rank)[0]
    return best.get("file_path")


def _best_backdrop(images: dict):
    """The still worth putting behind a loading message.

    Language-neutral first, and that is the opposite of the logo's
    preference for a reason: a backdrop carrying `iso_639_1` is one with
    a title *printed on it*, which would sit under the real logo and
    read as the title twice. The clean art is the one filed with no
    language."""
    backdrops = (images or {}).get("backdrops") or []
    if not backdrops:
        return None

    def rank(row):
        return (0 if row.get("iso_639_1") is None else 1,
                -(row.get("vote_average") or 0),
                -(row.get("width") or 0))

    return sorted(backdrops, key=rank)[0].get("file_path")


# How long an "asked and there is none" marker stands before the art is
# asked about again. It used to stand forever, and forever is wrong in
# both directions: art gets *added* to TMDB over time, and - the case
# that actually happened, 24 August 2026 - a marker written during one
# bad moment blocked a logo that demonstrably existed. Bleach TYBW's
# logo and backdrop were on disk in the morning, the cache trim evicted
# them (logo_cache sat in trim_cache's roots and the reader's new
# full-resolution pages pushed the total over the cap), the refetch hit
# a bad answer, and two 0-byte `.none` files then made the loading
# screen permanently logoless - the owner's "why do the bleach tybw
# logo do not show while loading!!". Six hours keeps request spam
# bounded and heals the same evening.
NEGATIVE_TTL_S = 6 * 3600


def _cached_file(imdb_id, suffix):
    """(image path, "asked and there is none" marker) for one kind of
    artwork. Cached on disk by IMDb id: artwork does not change, and the
    player asks for it every time an episode starts."""
    return (_cache_dir() / f"{imdb_id}{suffix}",
            _cache_dir() / f"{imdb_id}{suffix}.none")


def _still_missing(marker) -> bool:
    """Whether this miss marker still speaks - see NEGATIVE_TTL_S. An
    expired one is removed so the next check is a clean ask."""
    try:
        if not marker.exists():
            return False
        if time.time() - marker.stat().st_mtime < NEGATIVE_TTL_S:
            return True
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _download(path, timeout):
    request = urllib.request.Request(f"{CDN}{path}",
                                     headers={"User-Agent": _UA})
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return net.read_bytes(response, deadline)


def logo_path(entry, timeout: int = DEFAULT_TIMEOUT):
    """A local PNG of this title's logo, or None."""
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id or not token():
        return None

    cached, missing = _cached_file(imdb_id, ".png")
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)
    # A title TMDB has no logo for must not be looked up on every single
    # episode; an empty marker file records "asked, nothing there".
    if _still_missing(missing):
        return None

    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout,
                                 (entry or {}).get("title") or "",
                                 (entry or {}).get("type") or "")
        if not tmdb_id:
            missing.touch()
            return None
        images = _get_json(
            f"{API}/{kind}/{tmdb_id}/images?include_image_language=en,null",
            timeout)
        path = _best_logo(images)
        if not path:
            # Bleach TYBW has zero logos under en,null but does carry
            # them in other languages - a title treatment is artwork, so
            # a Japanese one is still the right shape and colour. Asking
            # again without the filter is one extra request only for the
            # titles that would otherwise show nothing.
            images = _get_json(f"{API}/{kind}/{tmdb_id}/images", timeout)
            path = _best_logo(images)
        if not path:
            # Nothing filed under this exact entry - try the franchise by
            # name (see _search_logo for why that is allowed here).
            path = _search_logo((entry or {}).get("title") or "", timeout)
        if not path:
            missing.touch()
            return None
        data = _download(f"/{LOGO_SIZE}{path}", timeout)
    except (Unreachable, urllib.error.HTTPError):
        # Fail soft, and crucially *without* a marker: the player shows
        # text and the next episode asks again. Both are already named
        # in the log once per host by _get_json, so no traceback here -
        # forty heroes against a refused key would otherwise bury that
        # one useful line under forty stacks.
        return None
    except Exception:
        logs.exception(f"tmdb logo lookup failed for {imdb_id}")
        return None

    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        return None
    return str(cached)


# TMDB's genre id for Animation. A reading title borrows the *anime's*
# title treatment, so the show carrying the logo has to be the animated
# one - the safeguard that keeps "Kingdom" off "Animal Kingdom".
_ANIMATION_GENRE = 16
# How close the show's name must be to the reading title before its logo
# is trusted. 0.9, measured 22 August 2026: the anime "Kingdom" scores
# 1.00 and "Animal Kingdom" 0.67, "The Last Kingdom" 0.61 - so 0.9 keeps
# the first and drops the other two, and the live-action "One Piece"
# (not animated) is already gone on the genre test before similarity is
# even consulted.
_LOGO_TITLE_THRESHOLD = 0.9


def _search_anime_logo(title, timeout):
    """The logo of the *animated* TV show whose name matches `title`, or
    None. Strict on purpose - this is a name search, which is how the
    wrong show gets picked, and a banner drew **"Animal Kingdom"** for a
    reading entry called "Kingdom" until this required both animation and
    a near-exact name (measured 22 August 2026, screenshotted).

    Among the search hits it keeps only the animated ones whose name is a
    0.9+ match, then takes the most popular of those - which is the main
    series rather than a one-off spin-off. Everything else returns None,
    and None means the banner keeps its typed title, which is the honest
    answer to "there is no anime by this name"."""
    from . import title_match
    try:
        found = _get_json(
            f"{API}/search/tv?query={urllib.parse.quote(title)}", timeout)
    except Unreachable:
        raise                   # see the same clause in _tmdb_id
    except Exception:
        return None
    best, best_pop = None, -1.0
    for row in (found.get("results") or [])[:10]:
        if _ANIMATION_GENRE not in (row.get("genre_ids") or []):
            continue
        name = row.get("name") or ""
        if title_match.similarity(title, name) < _LOGO_TITLE_THRESHOLD:
            continue
        popularity = float(row.get("popularity") or 0)
        if popularity > best_pop:
            best_pop, best = popularity, row
    if best is None:
        return None
    try:
        images = _get_json(f"{API}/tv/{best['id']}/images", timeout)
    except Unreachable:
        raise
    except Exception:
        return None
    return _best_logo(images)


def logo_path_by_title(title, timeout: int = DEFAULT_TIMEOUT):
    """A local PNG logo found by **title** on TMDB, or None.

    For reading entries, which have no IMDb id but may exist as an anime
    whose title treatment is exactly the right picture - the owner's ask,
    22 August 2026: "Kingdom also can use the same logo as the anime
    Kingdom, also Hunter x Hunter and One Piece from TMDB". Measured that
    day, all three resolve to the correct anime logo.

    The match is deliberately strict (see _search_anime_logo): a name
    search is how the wrong show gets picked, and the first version of
    this drew "Animal Kingdom" for the entry "Kingdom". A wrong logo is
    only cosmetic, but a *recognisably* wrong one is worse than none, so
    this requires the animated show and a near-exact name and otherwise
    returns None - the banner then keeps its typed title.

    The scanlation-team tag a reading title carries ("Kingdom (WAN)") is
    stripped by trying `title_match.search_variants`, the same cleaner
    anilist.manga_art uses - measured turning "Kingdom (WAN)" into the
    "Kingdom" that TMDB answers for.

    Cached on disk under a hash of the title, beside the id-keyed logos,
    with the same "asked, nothing there" marker so a title TMDB has no
    logo for is not re-searched on every hero build."""
    from . import title_match
    title = (title or "").strip()
    if not title or not token():
        return None
    key = hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:16]
    cached, missing = _cached_file(key, ".title.png")
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)
    if _still_missing(missing):
        return None
    try:
        path = None
        for variant in title_match.search_variants(title):
            path = _search_anime_logo(variant, timeout)
            if path:
                break
        if not path:
            missing.touch()
            return None
        data = _download(f"/{LOGO_SIZE}{path}", timeout)
    except (Unreachable, urllib.error.HTTPError):
        return None                 # fail soft; the banner keeps its text
    except Exception:
        logs.exception(f"tmdb logo-by-title lookup failed for {title!r}")
        return None
    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        return None
    return str(cached)


def backdrop_fast_path(entry, timeout: int = DEFAULT_TIMEOUT):
    """A small (w780) copy of the backdrop, for showing *now*.

    The full-resolution original is several MB and takes seconds on the
    first visit, which the owner reported as the details page's ground
    "taking a while". This one is a few hundred KB: the pages draw it the
    moment it lands and swap the original in over it when that arrives.
    Cached separately (.bgq) so both sizes are one download each,
    ever."""
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id or not token():
        return None
    cached, missing = _cached_file(imdb_id, ".bgq.jpg")
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)
    if _still_missing(missing):
        return None
    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout,
                                 (entry or {}).get("title") or "",
                                 (entry or {}).get("type") or "")
        if not tmdb_id:
            missing.touch()
            return None
        images = _get_json(f"{API}/{kind}/{tmdb_id}/images", timeout)
        path = _best_backdrop(images)
        if not path:
            missing.touch()
            return None
        data = _download(f"/w780{path}", timeout)
    except (Unreachable, urllib.error.HTTPError):
        return None
    except Exception:
        logs.exception(f"tmdb quick backdrop lookup failed for {imdb_id}")
        return None
    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        return None
    return str(cached)


def backdrop_path(entry, timeout: int = DEFAULT_TIMEOUT):
    """A local still from this title, for the player's loading screen.

    Same shape as logo_path and cached the same way, deliberately: the
    two are asked for together at the start of every episode and neither
    is worth a request per episode. Fails soft to None, which the player
    reads as "keep the flat background".

    The two calls are kept apart rather than folded into one round trip
    because they are wanted at different moments - the backdrop the
    instant the player opens, the logo only once there is something to
    draw progress into - and one of them failing must not take the
    other with it."""
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id or not token():
        return None

    # ".bg2" rather than the old ".bg": every install already holds a
    # w1280 file under the old name, and it would satisfy the cache
    # check forever - the whole point of moving to `original` is that
    # those get re-fetched sharp, once.
    cached, missing = _cached_file(imdb_id, ".bg2.jpg")
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)
    if _still_missing(missing):
        return None

    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout,
                                 (entry or {}).get("title") or "",
                                 (entry or {}).get("type") or "")
        if not tmdb_id:
            missing.touch()
            return None
        images = _get_json(f"{API}/{kind}/{tmdb_id}/images", timeout)
        path = _best_backdrop(images)
        if not path:
            missing.touch()
            return None
        data = _download(f"/{BACKDROP_SIZE}{path}", timeout)
    except (Unreachable, urllib.error.HTTPError):
        return None
    except Exception:
        logs.exception(f"tmdb backdrop lookup failed for {imdb_id}")
        return None

    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        return None
    return str(cached)


def deliver(entry, on_backdrop=None, on_logo=None, timeout=DEFAULT_TIMEOUT):
    """A title's artwork, handed over piece by piece as it lands.

    **One implementation, because there were five and the logo was last
    in every one of them.** The owner, 4 September 2026: "in the ep list
    the logo does not show also the bg image is blurred in all watchable
    ... the loading in the vid player does not show any logo!", then
    "fix also in all pages including the home and the discover banners
    ... ALL pages". Every caller - windows/details, windows/player,
    windows/home's hero, windows/tracker's discover banner and
    web/backend's featured route - fetched the same three things in the
    same serial order, ending with the logo. The step in front of it is
    `backdrop_path`, whose download is BACKDROP_SIZE "original": 2.5MB
    on Attack on Titan. Measured cold, twice each:

        serial (as shipped)   w780 0.77-0.84s   original 1.52-1.62s
                              LOGO 1.75-1.88s
        logo on its own       w780 0.39-1.01s   original 1.09-1.85s
                              LOGO 0.34-1.00s

    So the logo was 1.1-1.4s outside the CLAUDE.md rule 7 second, and
    on a loading screen that is often over inside that gap it simply
    never appeared. It is invisible on a machine that has been running
    for weeks - logo_cache answers every ask with a `stat()` - which is
    why it took a fresh install on his second device to surface, and why
    reproducing it here meant deleting logo_cache first.

    `on_backdrop(path)` may be called **twice**, the w780 copy and then
    the full-resolution original, and that order is load-bearing: every
    caller draws whatever arrives last, so a small copy landing after
    the original would swap a sharp ground back for a soft one. That is
    the whole reason the backdrops stay sequential while only the logo
    moves.

    `on_logo(path)` is called exactly once, with "" when there is none -
    callers need the negative to decide whether to keep the typed title.
    Callbacks run on worker threads. Never raises."""
    def _logo():
        found = None
        try:
            found = logo_path(entry, timeout)
        except Exception:
            logs.exception("logo fetch failed")
        if on_logo is None:
            return
        try:
            on_logo(str(found or ""))
        except Exception:
            logs.exception("logo callback failed")

    thread = threading.Thread(target=_logo, daemon=True)
    thread.start()
    for fetch in (backdrop_fast_path, backdrop_path):
        try:
            found = fetch(entry, timeout)
        except Exception:
            logs.exception("backdrop fetch failed")
            continue
        if found and on_backdrop is not None:
            try:
                on_backdrop(str(found))
            except Exception:
                logs.exception("backdrop callback failed")
    # Bounded: the fetches carry their own timeouts, and a caller that
    # blocks on this (web/backend's route) must not be held by a thread
    # that somehow outlives them.
    thread.join(timeout * 3)


def clear_cache():
    for name in os.listdir(_cache_dir()):
        try:
            os.remove(_cache_dir() / name)
        except OSError:
            pass
