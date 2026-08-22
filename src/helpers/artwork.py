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
import re
import urllib.parse
import urllib.request

from . import app_settings, net, storage

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


def _cache_dir():
    path = storage.DATA_DIR / "logo_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bundled_token() -> str:
    """The token shipped inside the build, if there is one.

    Kept in a file next to the frozen app rather than written into
    source: this repository is public, and a token pushed to GitHub gets
    found by secret scanners and revoked - which would break the feature
    for every copy at once instead of none. packaging/Atomic.spec bundles
    it and .gitignore keeps it out of the repo.

    Its whole purpose is that nobody running Atomic has to obtain a key
    of their own."""
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
    # v4 bearer, not the v3 `api_key=` query parameter. The token this
    # ships with is a read access token, and v3-style auth answers 401
    # for it - which looks exactly like a bad key rather than the wrong
    # auth scheme, so it is worth naming here.
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
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline))


def _tmdb_id(imdb_id: str, timeout):
    """TMDB's own id and media type for an IMDb id.

    `find` rather than `search`: the tracker already stores an IMDb id
    for everything it resolved, and matching on an id cannot pick the
    wrong show the way a title search can - which this project has been
    bitten by before."""
    url = f"{API}/find/{urllib.parse.quote(imdb_id)}?external_source=imdb_id"
    body = _get_json(url, timeout)
    for kind, field in (("tv", "tv_results"), ("movie", "movie_results")):
        rows = (body or {}).get(field) or []
        if rows:
            return kind, rows[0].get("id")
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
        except Exception:
            continue
        for row in (found.get("results") or [])[:3]:
            try:
                images = _get_json(f"{API}/tv/{row['id']}/images", timeout)
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


def _cached_file(imdb_id, suffix):
    """(image path, "asked and there is none" marker) for one kind of
    artwork. Cached on disk by IMDb id: artwork does not change, and the
    player asks for it every time an episode starts."""
    return (_cache_dir() / f"{imdb_id}{suffix}",
            _cache_dir() / f"{imdb_id}{suffix}.none")


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
    if missing.exists():
        return None

    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout)
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
    except Exception:
        return None                 # fail soft; the player shows text

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
    if missing.exists():
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
    except Exception:
        return None                 # fail soft; the banner keeps its text
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
    if missing.exists():
        return None
    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout)
        if not tmdb_id:
            missing.touch()
            return None
        images = _get_json(f"{API}/{kind}/{tmdb_id}/images", timeout)
        path = _best_backdrop(images)
        if not path:
            missing.touch()
            return None
        data = _download(f"/w780{path}", timeout)
    except Exception:
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
    if missing.exists():
        return None

    try:
        kind, tmdb_id = _tmdb_id(imdb_id, timeout)
        if not tmdb_id:
            missing.touch()
            return None
        images = _get_json(f"{API}/{kind}/{tmdb_id}/images", timeout)
        path = _best_backdrop(images)
        if not path:
            missing.touch()
            return None
        data = _download(f"/{BACKDROP_SIZE}{path}", timeout)
    except Exception:
        return None

    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        return None
    return str(cached)


def clear_cache():
    for name in os.listdir(_cache_dir()):
        try:
            os.remove(_cache_dir() / name)
        except OSError:
            pass
