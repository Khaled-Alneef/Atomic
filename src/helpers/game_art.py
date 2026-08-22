"""Portrait cover art for the Games page, from Steam's own store.

The Games page is moving from extracted .exe icons - a 32px shell icon
stretched across a tile - to movie-poster tiles at 160x216 logical, which
is 320x432 on a 2x display. Nothing on disk can supply that; the art has
to come from somewhere that publishes it.

Steam does, and does it keylessly end to end. Two requests, no account,
no token:

  1. ``store.steampowered.com/api/storesearch`` turns a name into an
     appid. Measured over 27 names: 200 every time, **0.44-0.74s, median
     0.52s**.
  2. ``cdn.cloudflare.steamstatic.com/steam/apps/{appid}/...`` serves the
     art. No credentials, and it **404s honestly for an appid that does
     not exist** (probed 999999999, 1, 2, 5, 12345678, 1245621 - all
     404). That matters: Crunchyroll answers 200 to a bogus id, so a
     probe there proves only that the site is up. Here a probe is real
     evidence, which is what lets the ladder below fall through.

**The "_2x" file is 600x900, not 1200x1800.** Measured on 12 appids: the
name is Steam's, not a description. The plain ``library_600x900.jpg``
next to it is **300x450**, which is narrower than the 320px a 160-wide
tile needs at 2x - so the 2x file is the only one that is actually sharp
at the size these tiles are drawn, and the 1x is a fallback that is
already a downgrade. The ladder is therefore:

    library_600x900_2x.jpg   600x900   91-240KB   the one we want
    library_600x900.jpg      300x450   29-77KB    soft at 2x, still portrait
    header.jpg               460x215   15-62KB    landscape, last resort

All 12 appids answered 200 on all three, back to Counter-Strike 1.6
(appid 10, released 2000), so library art is not a modern-titles-only
thing. Timings for the image itself: **0.12-1.66s, mostly under 0.9s**,
on the legacy ``cdn.cloudflare`` host. The newer
``shared.cloudflare.../store_item_assets/`` path returns byte-identical
files and was consistently the slowest of the six host/path shapes tried
(up to 2.1s), so the legacy path is what this uses.

## Matching, and why the threshold is where it is

A wrong cover is the failure that matters, so the score has to separate
a game from its own DLC, soundtrack and sequel - all of which Steam
returns for the same search, often ahead of the game. Measured, asking
for "Hades" puts **Hades II in first place** and the real Hades second;
asking for "Elden Ring" returns NIGHTREIGN and Shadow of the Erdtree
alongside it. Taking ``items[0]`` would be wrong about a third of the
time.

Three things do the separating:

  * **Squash equality.** Both sides stripped to bare alphanumerics with
    no spaces at all. Launcher folder names drop the punctuation the
    store keeps, and this makes that free: "Batman Arkham Knight" ==
    "Batman(tm): Arkham Knight", "NieRAutomata" == "NieR:Automata(tm)",
    "Baldurs Gate 3" == "Baldur's Gate 3", and an exe-derived
    "cyberpunk2077" == "Cyberpunk 2077".
  * **An extra-word penalty**, 0.12 per significant word the candidate
    has and the query does not. This is what demotes "Hades II" (0.649),
    "ELDEN RING NIGHTREIGN" (0.830) and "Palworld: Palfarm" (0.830)
    below the real answer. Without it ``title_match.similarity`` scores
    all three **0.95**, because its prefix-containment boost fires on
    exactly the shape a sequel has.
  * **A companion-product filter** that drops soundtrack/season pass/
    demo/REDmod/starter pack rows outright rather than scoring them.

Measured over 27 names - the owner's 10 real saved games plus 17 chosen
to be awkward - **every single correct answer scored exactly 1.000, and
the highest-scoring wrong candidate anywhere was 0.830.** There is
nothing in between. MATCH_THRESHOLD is **0.90**, in the middle of that
gap: 0.85 would give identical results today but sits 0.02 from a known
wrong answer, and the standing rule here is that a silently wrong result
is worse than none.

Hit rate at 0.90: **22/27**. The five that did not resolve:

  * *VALORANT* and a deliberately fake name - correctly None, neither
    is on Steam.
  * *Rocket League* and *Counter-Strike Global Offensive* - the store
    search returns **zero rows**. The first is delisted, the second was
    renamed to Counter-Strike 2. No amount of matching finds a row that
    is not there.
  * *"The Witcher 3"* (the shortened folder name) - the real row scores
    0.710 and is refused. Dropping the threshold to catch it would also
    admit "ELDEN RING Shadow of the Erdtree" and "Batman: Arkham Knight
    - GCPD Lockdown", which score 0.710 too. Not worth it.

## The exact route, when the caller has an install path

The three genuine misses share one shape - the folder name is not the
store name - and Steam already knows the answer. Every installed game
has ``steamapps/appmanifest_<appid>.acf`` naming its appid and its
``installdir``, so matching the game's own folder against that is an
**exact** identification with no searching and no network at all.

Verified against the owner's library: it resolves *rocketleague* to
252950 and CS:GO to 730, both of which do have 600x900 art on the CDN
despite being unfindable by name. It also catches something a name
search cannot: a folder called **"Ride"** in that library is really
*RV There Yet?* (appid 3949040). Steam has no plain "RIDE" today so the
name route returns None rather than the wrong game, but that is luck,
not safety - the manifest is right by construction.

So ``install_path`` is optional but always better. The public two-
argument contract is unchanged; passing the game's saved ``path`` just
skips the guessing.

## Caching

Cover art does not change, and a page rebuilt on every visit must not
re-ask. The resolved URL is cached on disk by name, and so is an
authoritative "Steam does not have this" - but only when Steam actually
answered. A failed request is never cached as a miss, or one flaky
minute would blank a tile permanently. The negative carries a 7-day TTL
so a delisted-then-relisted title recovers on its own.

The same rule is why the art ladder descends **only on a 404** - see
`_art_url`, where a transient failure once cached Elden Ring's 300x450
file although its 600x900 was there all along. A cache that remembers a
guess is worse than one that remembers nothing.

Everything fails soft to None. Nothing here raises at a caller.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import images, net, storage, title_match

SEARCH_URL = "https://store.steampowered.com/api/storesearch/?term={term}&cc=us&l=en"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/{shape}"

# Best first. See the module docstring for the measured sizes - the _2x
# file is the only one that is genuinely sharp on a 2x display, and
# header.jpg is landscape, so it is here to be better than a letter
# avatar rather than because it fits the tile.
ART_SHAPES = ("library_600x900_2x.jpg", "library_600x900.jpg", "header.jpg")

DEFAULT_TIMEOUT = 8.0
# 2x the per-request timeout, the same budget rule anime_sites uses: a
# lookup is a search plus up to three probes, and one dead host ahead of
# a live one must not turn a real hit into a saved miss.
DEFAULT_BUDGET = 16.0

# Measured: every correct match scored 1.000, the best wrong one 0.830.
# This sits in the middle of that gap rather than just above the wrong
# answer. Do not lower it to raise the hit rate - the three names it
# refuses are refused for the right reason.
MATCH_THRESHOLD = 0.90

# How long an authoritative "Steam has nothing for this name" stands
# before it is asked again. Only ever written when Steam answered.
NEGATIVE_TTL = 7 * 24 * 3600

_UA = "Atomic/1.0"

# Trademark marks sit inside store titles ("NieR:Automata(tm)") and
# sometimes inside folder names too, where they also break the search
# outright - measured, "ELDEN RING(TM)" returns **zero rows** while
# "ELDEN RING" returns five.
_TM_RE = re.compile(r"[\u2122\u00ae\u00a9]")

# Rows that carry a game's name but are not the game. Dropped rather
# than scored: dropping a candidate can only ever cost a match, never
# cause a wrong one, so this is the safe direction to be wrong in.
_NOT_A_GAME_RE = re.compile(
    r"\b(soundtrack|ost|original score|demo|dlc|artbook|art book|season pass|"
    r"upgrade|bonus content|dedicated server|sdk|playtest|beta|trailer|"
    r"wallpaper|skin pack|expansion pass|redmod|redkit|starter pack|bundle)\b",
    re.I)

# A trailing edition phrase, so "Divinity: Original Sin 2 - Definitive
# Edition" can be recognised as the game the folder "Divinity Original
# Sin 2" holds - which it is, and which is the only row Steam has for it.
#
# What this produces is used for an **exact** comparison only, never a
# fuzzy one, and that restriction is load-bearing: "Hogwarts Legacy"
# trimmed of "Legacy" is "Hogwarts", and similarity() scores a title
# against its own prefix 0.95 on the containment boost. Allowed to match
# fuzzily, this regex would hand Hogwarts Legacy the cover of any game
# called "Hogwarts". Allowed to match only exactly, it cannot.
_EDITION_RE = re.compile(
    r"[\s:\-\u2013]*\b((game of the year|goty|definitive|complete|deluxe|"
    r"ultimate|enhanced|anniversary|legendary|gold|premium|standard|"
    r"remastered?|director'?s cut|legacy)\b[\s\-]*)+(edition)?\s*$", re.I)

# Words too common to count as evidence that two titles differ.
_STOPWORDS = {"the", "a", "an", "of", "and"}

_APPID_RE = re.compile(r'"appid"\s*"(\d+)"')
_INSTALLDIR_RE = re.compile(r'"installdir"\s*"([^"]*)"')


# --------------------------------------------------------------------
# name shaping


def _search_term(name: str) -> str:
    """What to actually send to Steam's search.

    `title_match.search_query` already drops bracketed tags, which is
    what turns "ELDEN RING(TM)" - a search that measured **zero rows** -
    into "ELDEN RING", which returns the game first."""
    return " ".join(_TM_RE.sub(" ", title_match.search_query(name)).split())


def _squash(text: str) -> str:
    """A title reduced to bare alphanumerics, no spaces.

    Launcher folders drop the punctuation a store title keeps, and an
    exe-derived name drops the spaces too, so this is the comparison
    that makes "cyberpunk2077", "Cyberpunk 2077" and "Baldurs Gate 3" /
    "Baldur's Gate 3" land on each other without any fuzziness at all."""
    return title_match.normalize(_TM_RE.sub(" ", text or "")).replace(" ", "")


def match_score(query: str, candidate: str) -> float:
    """How much this row looks like the game that was asked for.

    1.0 only for an exact identity after normalization - including
    across a trailing edition phrase. Everything else is
    `title_match.similarity` docked 0.12 for each significant word the
    candidate carries that the query does not, which is what separates a
    game from its sequel and its DLC: similarity alone scores "Hades II"
    against "Hades" at 0.95, because its prefix-containment boost fires
    on precisely that shape."""
    squashed = _squash(query)
    if not squashed:
        return 0.0
    trimmed = _EDITION_RE.sub("", candidate or "").strip()
    # Exact only - see _EDITION_RE for why the trimmed form must never
    # reach the fuzzy comparison below.
    if squashed in (_squash(candidate), _squash(trimmed)):
        return 1.0

    query_words = set(title_match.normalize(query).split())
    extra = [word for word in title_match.normalize(candidate).split()
             if word not in query_words and word not in _STOPWORDS]
    return title_match.similarity(query, candidate) - 0.12 * len(extra)


# --------------------------------------------------------------------
# the exact route: Steam's own install manifest


def appid_from_install_path(install_path) -> int:
    """The Steam appid of the game installed at `install_path`, read out
    of Steam's own manifest, or None.

    Exact and offline - no search, no network, nothing to get wrong. The
    game's folder under steamapps/common is matched against the
    `installdir` each appmanifest_<appid>.acf declares.

    This is the only route that answers for a game whose folder name is
    not its store name, which is every one of the misses the name search
    has: measured on the owner's library, *rocketleague* -> 252950 and
    CS:GO -> 730, both delisted or renamed and both unfindable by name
    while their art is still on the CDN.

    Parsed with two regexes rather than a real VDF parser: an .acf is a
    handful of flat quoted pairs, only two of them are wanted, and a
    malformed one has to read as "no answer" regardless."""
    try:
        parts = Path(install_path).parts
        lowered = [part.lower() for part in parts]
        # steamapps/common/<the game's folder>/... - anything else is
        # not a Steam library layout and there is no manifest to read.
        if "steamapps" not in lowered:
            return None
        index = lowered.index("steamapps")
        if lowered[index + 1:index + 2] != ["common"] or len(parts) <= index + 2:
            return None
        steamapps = Path(*parts[:index + 1])
        wanted = parts[index + 2].casefold()

        for manifest in steamapps.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            installdir = _INSTALLDIR_RE.search(text)
            if not installdir or installdir.group(1).casefold() != wanted:
                continue
            appid = _APPID_RE.search(text)
            if appid:
                return int(appid.group(1))
    except Exception:
        return None
    return None


# --------------------------------------------------------------------
# the search route


def _get_json(url: str, timeout: float):
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/json"})
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline))


def search_appid(name: str, deadline=None):
    """The Steam appid for a game called `name`, or None.

    Returns None for three different things on purpose - no rows, no row
    good enough, and the request failing - because a caller can do
    nothing different about any of them. What the *cache* needs to tell
    apart is handled by `_resolve_appid`, which only records a miss when
    Steam actually answered."""
    term = _search_term(name)
    if not term:
        return None
    timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
    if timeout is None:
        return None
    body = _get_json(SEARCH_URL.format(term=urllib.parse.quote(term)), timeout)

    best_id, best_score = None, 0.0
    for row in (body or {}).get("items") or []:
        # "sub" and "bundle" rows are store packages, not games, and
        # carry no library art of their own.
        if row.get("type") != "app":
            continue
        title = row.get("name") or ""
        if _NOT_A_GAME_RE.search(title):
            continue
        score = match_score(term, title)
        if score > best_score:
            best_id, best_score = row.get("id"), score

    if best_id is None or best_score < MATCH_THRESHOLD:
        return None
    try:
        return int(best_id)
    except (TypeError, ValueError):
        return None


def _probe(url: str, deadline):
    """True/False for whether `url` exists, or None for "could not tell".

    HEAD, because the answer wanted is existence, not 240KB of JPEG.
    Retried once on anything that is not a definite verdict, for the same
    reason chapter_source retries its pages: one failed request out of
    six silently produced a result that looked complete and was not."""
    for attempt in range(2):
        timeout = net.step_timeout(deadline, DEFAULT_TIMEOUT)
        if timeout is None:
            return None
        request = urllib.request.Request(
            url, headers={"User-Agent": _UA}, method="HEAD")
        try:
            with net.urlopen(request, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False        # authoritative: no such file
            if error.code < 500 and attempt:
                return None
        except Exception:
            pass
    return None


def _art_url(appid: int, deadline=None):
    """(best art URL that exists for `appid` or None, whether that is a
    real answer).

    Probed rather than assumed. A bogus appid 404s here - measured on
    999999999, 1, 2, 5, 12345678 and 1245621 - so a 200 is genuine
    evidence the file is there. That is what makes descending the ladder
    meaningful rather than decorative, and it is also why descending is
    allowed **only on a 404**.

    That restriction is not theoretical. The first run of the end-to-end
    test resolved Elden Ring to the 300x450 file although its 600x900
    exists and answers 200 - one transient failure on the 2x probe, and
    the ladder quietly handed back the worse image, which the cache then
    kept. It could not be reproduced afterwards (25 HEADs and 25 GETs on
    that exact URL, all 200; a burst of 87 HEADs across 29 appids, 84x200
    and 3x404 with no other failure), which is precisely what makes it
    dangerous: rare enough never to be noticed, permanent once cached.

    A timeout or a reset is not evidence a file is absent, so it aborts
    the whole ladder with `False` for "this is not a real answer" - the
    caller then caches nothing and the next call tries again.

    A title with no art at all is a real state, not a bug: *RV There
    Yet?* (3949040) 404s on all three shapes."""
    for shape in ART_SHAPES:
        url = CDN.format(appid=appid, shape=shape)
        exists = _probe(url, deadline)
        if exists is None:
            return None, False      # could not tell - do not settle for less
        if exists:
            return url, True
    return None, True               # asked properly; Steam has no art


# --------------------------------------------------------------------
# cache


def _cache_dir() -> Path:
    path = storage.DATA_DIR / "game_art"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_files(name: str):
    """(resolved url, "asked and there is none") for one game name.

    Keyed on the normalized name rather than the raw one so a rename
    that changes only punctuation reuses what was already resolved."""
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
    """Record what was resolved. `url` of None means Steam answered and
    has nothing - never call it for a request that failed, or one flaky
    minute blanks the tile for a week."""
    resolved, missing = _cache_files(name)
    try:
        if url:
            # Written to a temp file and moved into place: several tiles
            # resolve at once on a page build, and a half-written file
            # read by the next one would be a bad URL cached as good.
            temporary = resolved.with_suffix(".url.tmp")
            temporary.write_text(url, encoding="utf-8")
            os.replace(temporary, resolved)
        else:
            missing.touch()
    except OSError:
        pass


def clear_cache():
    """Forget every resolved URL and miss. For Settings, and for a test
    that needs a cold run."""
    try:
        for name in os.listdir(_cache_dir()):
            try:
                os.remove(_cache_dir() / name)
            except OSError:
                pass
    except OSError:
        pass


# --------------------------------------------------------------------
# public


def _resolve_appid(name: str, install_path, deadline):
    """(appid or None, whether Steam actually gave that answer).

    The second half is what the cache needs: "Steam has no such game" is
    worth remembering, "the request failed" is not."""
    if install_path:
        appid = appid_from_install_path(install_path)
        if appid:
            return appid, True
    try:
        return search_appid(name, deadline), True
    except Exception:
        return None, False       # network/parse failure - do not cache


def fetch_cover_url(name: str, deadline=None, install_path=None):
    """The best portrait cover-art URL for a game called `name`, or None.

    `install_path` is optional and always better when there is one - the
    game's saved .exe path lets the appid be read straight out of Steam's
    install manifest instead of guessed from the name (see
    `appid_from_install_path`).

    `deadline` is a `net.deadline_in` value shared across a chain. Left
    out, one is made here at DEFAULT_BUDGET - there is no caller for whom
    an unbounded cover lookup is the right thing, and a page drawing
    tiles must not have one of them hang on the sum of four requests.

    Never raises."""
    try:
        if not (name or "").strip():
            return None
        cached = _read_cached(name)
        if cached is not None:
            return cached or None

        if deadline is None:
            deadline = net.deadline_in(DEFAULT_BUDGET)
        appid, answered = _resolve_appid(name, install_path, deadline)
        if not appid:
            if answered:
                _write_cached(name, None)
            return None

        url, definite = _art_url(appid, deadline)
        # Only a real verdict is worth remembering. A probe that could
        # not tell (see _art_url) must leave no trace, or one flaky
        # moment pins a game to the wrong resolution - or to nothing -
        # for as long as the cache stands.
        if definite:
            _write_cached(name, url)
        return url
    except Exception:
        return None


def fetch_cover(name: str, deadline=None, install_path=None):
    """A local file holding that cover, or None.

    `images.download` does the fetching and the on-disk caching, so a
    cover already pulled for another page costs no request at all.

    Never raises - safe to call straight from a worker thread, which is
    where it will be called from."""
    try:
        url = fetch_cover_url(name, deadline, install_path)
        if not url:
            return None
        path = images.download(url, timeout=int(DEFAULT_TIMEOUT))
        return str(path) if path else None
    except Exception:
        return None
