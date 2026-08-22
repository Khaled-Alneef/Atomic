"""High-resolution icon art for the Apps page, from Apple's iTunes
Search API.

The Apps grid draws poster-size tiles now (the owner's ask - the same
card the Movies & Series page uses), and nothing on disk can fill one:
the images the entries carry are extracted .exe icons and favicons,
32-128px files that upscale to mush at 160px. The iTunes Search API is
the one keyless place that publishes big, clean art for the apps people
actually pin here - Discord, Spotify, Netflix, WhatsApp, Telegram all
ship an iOS build under the same branding, and every search row carries
``artworkUrl512``, a 512x512 original.

One request, no account, no token:

    https://itunes.apple.com/search?media=software&limit=8&term=discord

Matching follows helpers/game_art's measured lesson rather than trusting
``results[0]``: a wrong icon is worse than none, so a candidate must be
squash-equal to the query (bare alphanumerics, no spaces - "WhatsApp" ==
"whatsapp"), start with the query at a word boundary within iTunes' top
rows, or score at least MATCH_THRESHOLD on title_match.similarity docked
0.12 per significant word the candidate carries that the query does not.

The prefix rule exists because similarity alone missed 4 of 5 real apps
(measured live 20 August 2026): iTunes files the canonical apps under
tagline names - "Discord - Talk, Play, Hang Out", "Spotify: Music and
Podcasts", "WhatsApp Messenger", "Steam Mobile" - every one of which
similarity-with-docking rejects. All four sat in the *first* result row,
which is what makes the rule safe to bound: only the first
_PREFIX_ROW_LIMIT rows may prefix-match, so a fan app like "Discord
Server Widgets" (ranked below the real one when the real one exists at
all) cannot claim the icon, and it scores below squash-equality so an
exact name still wins outright.

Caching is game_art's too, for the same reasons written there: the
resolved URL is cached on disk by name, an authoritative "iTunes has
nothing" is cached with a TTL so a miss is not re-asked on every page
build, and a *failed request* is never cached as anything - one flaky
minute must not blank a tile for a week.

Everything fails soft to None. Nothing here raises at a caller.
"""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

from . import images, net, storage, title_match

SEARCH_URL = ("https://itunes.apple.com/search?media=software&limit=8"
              "&term={term}")
DEFAULT_TIMEOUT = 8.0
MATCH_THRESHOLD = 0.90
# How deep into the results the prefix rule reaches (see the module
# docstring). Every canonical tagline-named app measured sat at row 0.
_PREFIX_ROW_LIMIT = 4
_PREFIX_SCORE = 0.95
NEGATIVE_TTL = 7 * 24 * 3600
_UA = "Atomic/1.0"

# Words too common to count as evidence that two app names differ.
_STOPWORDS = {"the", "a", "an", "of", "and", "app", "for"}


def _squash(text: str) -> str:
    return title_match.normalize(text or "").replace(" ", "")


def _is_prefix(query: str, candidate: str) -> bool:
    """Whether the candidate is the query plus a tagline: it starts with
    the query's normalized words at a word boundary ("Spotify: Music and
    Podcasts", "WhatsApp Messenger")."""
    query_words = title_match.normalize(query).split()
    candidate_words = title_match.normalize(candidate).split()
    return (bool(query_words)
            and len(candidate_words) > len(query_words)
            and candidate_words[:len(query_words)] == query_words)


def _match_score(query: str, candidate: str, row: int = 99) -> float:
    """1.0 only for squash-identity; _PREFIX_SCORE for a tagline-suffixed
    name in iTunes' leading rows; otherwise similarity docked per extra
    significant word - see game_art.match_score, whose measured
    separations this borrows."""
    squashed = _squash(query)
    if not squashed:
        return 0.0
    if squashed == _squash(candidate):
        return 1.0
    if row < _PREFIX_ROW_LIMIT and _is_prefix(query, candidate):
        return _PREFIX_SCORE
    query_words = set(title_match.normalize(query).split())
    extra = [word for word in title_match.normalize(candidate).split()
             if word not in query_words and word not in _STOPWORDS]
    return title_match.similarity(query, candidate) - 0.12 * len(extra)


# --------------------------------------------------------------------
# cache - game_art's shape

def _cache_dir():
    path = storage.DATA_DIR / "app_art"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_files(name: str):
    digest = hashlib.sha1(
        _squash(name).encode("utf-8", "replace")).hexdigest()
    return _cache_dir() / f"{digest}.url", _cache_dir() / f"{digest}.none"


def _read_cached(name: str):
    """The cached URL, "" for a still-valid cached miss, or None for
    "not asked yet"."""
    resolved, missing = _cache_files(name)
    try:
        if resolved.is_file():
            url = resolved.read_text(encoding="utf-8").strip()
            if url:
                return url
        if missing.is_file():
            if time.time() - missing.stat().st_mtime < NEGATIVE_TTL:
                return ""
            missing.unlink()
    except OSError:
        pass
    return None


def _write_cached(name: str, url):
    """`url` of None means iTunes answered and has nothing - never call
    it for a request that failed."""
    resolved, missing = _cache_files(name)
    try:
        if url:
            temporary = resolved.with_suffix(".url.tmp")
            temporary.write_text(url, encoding="utf-8")
            os.replace(temporary, resolved)
        else:
            missing.touch()
    except OSError:
        pass


# --------------------------------------------------------------------
# public

def fetch_art_url(name: str, timeout: float = DEFAULT_TIMEOUT):
    """The 512px artwork URL for an app called `name`, or None.

    Never raises. A network or parse failure returns None *without*
    caching; only an actual iTunes answer is remembered, hit or miss."""
    try:
        term = " ".join(title_match.search_query(name or "").split())
        if not term:
            return None
        cached = _read_cached(name)
        if cached is not None:
            return cached or None

        request = urllib.request.Request(
            SEARCH_URL.format(term=urllib.parse.quote(term)),
            headers={"User-Agent": _UA, "Accept": "application/json"})
        deadline = net.deadline_in(timeout)
        with net.urlopen(request, timeout=timeout) as response:
            body = json.loads(net.read_text(response, deadline))

        best_url, best_score = None, 0.0
        for index, row in enumerate((body or {}).get("results") or []):
            candidate = str(row.get("trackName") or "")
            url = row.get("artworkUrl512") or row.get("artworkUrl100")
            if not candidate or not url:
                continue
            # Strictly greater, so ties keep the earliest row - iTunes'
            # relevance puts the canonical app above its fan apps.
            score = _match_score(term, candidate, row=index)
            if score > best_score:
                best_url, best_score = str(url), score

        if best_url is None or best_score < MATCH_THRESHOLD:
            _write_cached(name, None)
            return None
        _write_cached(name, best_url)
        return best_url
    except Exception:
        return None            # request failed - cache nothing


def fetch_art(name: str):
    """A local file holding that artwork, or None. images.download does
    the fetching and the on-disk caching. Never raises - safe straight
    from a worker thread, which is where it is called from."""
    try:
        url = fetch_art_url(name)
        if not url:
            return None
        path = images.download(url, timeout=int(DEFAULT_TIMEOUT))
        return str(path) if path else None
    except Exception:
        return None
