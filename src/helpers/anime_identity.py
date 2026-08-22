"""What season a torrent release actually belongs to, for anime whose
seasons are named by *arc* rather than numbered.

**The owner's report, 22 August 2026:** "when I want to play Demon Slayer
E1S1 it shows me sources from diff seasons Ep1 ... especially in anime."
Measured on his own Demon Slayer entry (tt9335498) asked for S01E01,
`streams.find_streams` returned 77 rows of which **24 were later arcs** -
the Hashira Training arc at 327 seeders, the Entertainment District arc
at 290, the Mugen Train arc at 235 - every one sorting near the top,
which is where both the eye and the default pick land.

`streams._drop_wrong_season` already existed and already caught the
*numbered* case (Re:Zero's "S04", "3rd Season"). It could not touch
Demon Slayer, because a Demon Slayer release states **no season number
at all** - it is called "Kimetsu no Yaiba - Hashira Geiko Hen - 01", and
"Hashira Geiko" is season 5 only to someone who knows the arc order.
`indexers.stated_seasons` reads that name as stating no season, so the
row passed unchecked; the indexer rows bypassed the addon-only filter
besides.

**So the arc order is looked up, exactly as the owner suggested** -
"a strict anime episode resolver that maps AniList/TMDB IDs to the
correct season + episode". Two sources, each supplying the half the
other lacks, measured 22 August 2026:

  * **TMDB is the authority on the numbering**, because it *is* the
    numbering the app uses - the entry's `latest_available` "S05E08"
    is TMDB's, and `/tv/{id}` lists the seasons the same way the card
    does. For tt9335498 it returns S01 "Unwavering Resolve Arc" /
    S02 "Mugen Train Arc" / S03 "Entertainment District Arc" /
    S04 "Swordsmith Village Arc" / S05 "Hashira Training Arc", air dates
    and all. No other source's ordering can be trusted to match the
    card's, which is the whole risk in this - AniList files each cour as
    its own work and does not number them the way TMDB splits them.
  * **AniList supplies the romaji** the releases actually use. TMDB says
    "Mugen Train Arc"; the release says "Mugen Ressha-hen". "Mugen" and
    "train" are shared, so TMDB's English alone catches that one, but
    "Entertainment District" never appears as "Yuukaku" and "Swordsmith
    Village" never as "Katanakaji", so the romaji is needed for two of
    the five. AniList's entry for each cour is matched to its TMDB
    season **by air date** (within two months), never by title order,
    so a re-cut movie filed a year off cannot take a TV season's slot.

The result is a `{season -> {tokens}}` map. A token is kept only if it
belongs to **exactly one** season across the whole franchise - the
franchise name itself ("kimetsu", "demon", "slayer") appears in every
season's title and would otherwise match every release, so the
one-season-only rule is what turns the franchise words back into noise
and leaves only the arc words. Measured on the 77 real rows: 24 dropped,
every one a genuinely later arc, none of the 53 season-1 rows touched;
asked for S05 instead, the Hashira rows and the all-seasons batch are
kept and S1-4 dropped. Both directions, no false drop.

**Nothing here is on the play path's critical timing.** The map is a
franchise fact, not an episode one, so it is cached on disk per IMDb id
for thirty days (`identity_cache/`, the shape ratings.py uses) and
resolved on a background thread. `streams.find_streams` reads the cache
if it is warm - a dict lookup - and filters nothing at all when it is
cold, populating for next time instead. So a title's very first play in
a month is unfiltered and every play after it is filtered, and the
owner's Demon Slayer - played over and over - is filtered from the
second press on. This is deliberate: filtering only from the cache means
a partial batch and the final list are filtered identically, so no row
ever appears and then vanishes under the pointer (the standing rule in
streams.find_streams), and the "rows on screen inside a second" budget
is never spent waiting on TMDB.

Fails soft to an empty map at every step - a missing TMDB key, a title
neither source knows, AniList's 403, a movie with no seasons - all give
`{}`, which filters nothing, which is the honest answer: a shorter list
beats a wrong episode, but an *empty* list beats neither.
"""

import json
import os
import re
import threading
import time

from . import artwork, logs, net, storage

# Franchise facts change about never; thirty days is already generous,
# and a title re-asked sooner than that is served from memory anyway.
_TTL = 30 * 24 * 3600.0
# A map built without AniList carries only TMDB's English arc names, which
# miss the two Demon Slayer arcs whose romaji shares no word with the
# English ("Yuukaku" for Entertainment District, "Katanakaji" for
# Swordsmith Village). That is a *partial* answer and must not sit for a
# month: it is re-resolved in six hours so a one-off AniList timeout
# (measured 8.4s once, then 1.4s on the next try) heals itself rather
# than baking in.
_PARTIAL_TTL = 6 * 3600.0
_CACHE_VERSION = 1
_TIMEOUT = 8

_memory = {}
_memory_lock = threading.Lock()
_inflight = set()          # imdb ids currently being resolved in the background
_inflight_lock = threading.Lock()

_ANILIST_URL = "https://graphql.anilist.co"
# Only the franchise's seasons and their air dates and names - the same
# search the schedule lookups use, one page, nothing per-episode.
_ANILIST_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 30) {
    media(search: $search, type: ANIME, sort: [SEARCH_MATCH]) {
      format
      startDate { year month }
      title { romaji english }
    }
  }
}
"""

# Words that are structure, not identity: every anime title is full of
# them and none tells one arc from another.
_STOP = frozenset(
    "hen arc season seasons part cour final the tv movie ova ona special "
    "specials kai saga chapter no wo to ga wa ni de of a an and version "
    "st nd rd th".split())

# A token has to be a real word to match a real word - four letters up.
# Measured against the arcs that matter: sato(4) mugen(5) geiko(5)
# train(5) yuukaku(7) village(7) district(8) katanakaji(10) all clear it,
# and nothing shorter carried arc meaning in the franchises checked.
_MIN_TOKEN = 4


def _cache_dir():
    path = storage.DATA_DIR / "identity_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _words(text: str) -> set:
    """The meaningful lowercase words of a title - punctuation gone,
    structure words gone, digits gone, anything too short gone."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return {w for w in cleaned.split()
            if len(w) >= _MIN_TOKEN and not w.isdigit() and w not in _STOP}


def _fresh(record) -> bool:
    try:
        ttl = _PARTIAL_TTL if record.get("partial") else _TTL
        return (time.time() - float(record.get("checked_at") or 0)) < ttl
    except (TypeError, ValueError, AttributeError):
        return False


def _load_disk(imdb_id):
    path = _cache_dir() / f"{imdb_id}.json"
    try:
        if path.exists():
            # utf-8-sig for storage.py's reason: an invisible BOM has
            # already cost this project one file that silently stopped
            # parsing.
            with open(path, encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and loaded.get("v") == _CACHE_VERSION:
                return loaded
    except (OSError, ValueError):
        pass                            # a corrupt cache is just a miss
    return None


def _save_disk(imdb_id, record):
    record["v"] = _CACHE_VERSION
    path = _cache_dir() / f"{imdb_id}.json"
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(temp, path)
    except OSError:
        pass                            # cache only; the answer still stands


def _anilist_media(title: str, attempts: int = 2):
    """The franchise's AniList entries (TV/ONA only, with an air date),
    or [] on any failure - a 403 block included, which is why this never
    lets an exception out.

    Retried once, because the failure that mattered was transient: the
    romaji-carrying call for Demon Slayer hit the 8s timeout once and
    answered in 1.4s on the very next try. Cheap to retry - this whole
    resolve is paid once per title per month."""
    import urllib.request
    payload = json.dumps({"query": _ANILIST_QUERY,
                          "variables": {"search": title}}).encode("utf-8")
    for attempt in range(max(1, attempts)):
        request = urllib.request.Request(_ANILIST_URL, data=payload, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 PC-App/1.0",
        })
        try:
            deadline = net.deadline_in(_TIMEOUT)
            with net.urlopen(request, timeout=_TIMEOUT) as response:
                body = json.loads(net.read_text(response, deadline))
            media = (((body.get("data") or {}).get("Page") or {}).get("media")) or []
            usable = [m for m in media
                      if m.get("format") in ("TV", "TV_SHORT", "ONA")
                      and (m.get("startDate") or {}).get("year")]
            if usable:
                return usable
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return []


def _months(year, month) -> int:
    return int(year) * 12 + int(month or 1)


def _build(entry):
    """Resolve the `{season -> [tokens]}` map for one entry, live.

    TMDB gives the season split and its English arc names; AniList, matched
    to each season by air date, gives the romaji. A token surviving into
    the map is one that names exactly one of those seasons.

    Returns `(arcs, anilist_ok)` - the second flag is False when AniList
    gave nothing, so the caller can re-check a TMDB-English-only map soon
    rather than trusting it for a month (see _resolve_and_store)."""
    imdb_id = (entry or {}).get("imdb_id")
    title = (entry or {}).get("title") or ""
    if not imdb_id or not artwork.available():
        return {}, True
    try:
        media_type, tmdb_id = artwork.tmdb_id(imdb_id, _TIMEOUT)
    except Exception:
        return {}, True
    if media_type != "tv" or not tmdb_id:
        return {}, True                 # a film has no seasons to confuse
    try:
        show = artwork.get_json(f"{artwork.API}/tv/{tmdb_id}", _TIMEOUT)
    except Exception:
        return {}, True
    seasons = [s for s in (show or {}).get("seasons", [])
               if (s.get("season_number") or 0) >= 1 and s.get("air_date")]
    if not seasons:
        return {}, True

    media = _anilist_media(title)

    # Candidate words per season: TMDB's own arc name (unless it is the
    # generic "Season N", which says nothing), plus the romaji and English
    # of whichever AniList cour aired closest to that season.
    raw = {}
    for season in seasons:
        number = season["season_number"]
        name = season.get("name") or ""
        tokens = set() if re.search(r"season\s*\d", name, re.I) else _words(name)
        try:
            air = _months(int(season["air_date"][:4]), int(season["air_date"][5:7]))
        except (ValueError, IndexError):
            air = None
        if air is not None and media:
            best, best_gap = None, 99
            for candidate in media:
                start = candidate["startDate"]
                gap = abs(air - _months(start["year"], start.get("month")))
                if gap < best_gap:
                    best_gap, best = gap, candidate
            if best is not None and best_gap <= 2:
                tokens |= _words(best["title"].get("romaji"))
                tokens |= _words(best["title"].get("english"))
        raw[number] = tokens

    # The franchise name is in every season's title; the one-season-only
    # rule is what removes it and leaves the arc words behind.
    seen = {}
    for tokens in raw.values():
        for word in tokens:
            seen[word] = seen.get(word, 0) + 1
    arcs = {number: sorted(word for word in tokens if seen[word] == 1)
            for number, tokens in raw.items()}
    return arcs, bool(media)


def _resolve_and_store(entry):
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id:
        return {}
    partial = False
    try:
        arcs, anilist_ok = _build(entry)
        # Only anime with more than one season can have an arc a release
        # names; a one-season franchise (or a TMDB-English-only map) that
        # still lacks its romaji is a partial answer worth re-checking.
        partial = (not anilist_ok) and len(arcs) > 1
    except Exception:
        logs.exception("anime_identity: resolve failed")
        arcs = {}
    record = {"checked_at": time.time(), "partial": partial,
              "arcs": {str(k): v for k, v in arcs.items()}}
    with _memory_lock:
        _memory[imdb_id] = record
    _save_disk(imdb_id, record)
    return record


def _spawn_resolve(entry):
    """Populate the cache in the background, at most one thread per id."""
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id:
        return
    with _inflight_lock:
        if imdb_id in _inflight:
            return
        _inflight.add(imdb_id)

    def worker():
        try:
            _resolve_and_store(entry)
        finally:
            with _inflight_lock:
                _inflight.discard(imdb_id)

    threading.Thread(target=worker, daemon=True,
                     name="atomic-anime-identity").start()


def _arcs_of(record) -> dict:
    """The `{int season -> set(tokens)}` view of a stored record."""
    out = {}
    for key, tokens in (record.get("arcs") or {}).items():
        try:
            out[int(key)] = {t for t in tokens if t}
        except (TypeError, ValueError):
            continue
    return out


def arc_map(entry) -> dict:
    """The franchise's `{season -> {tokens}}` map **from cache only**, or
    `{}` when it is not warm yet.

    Never blocks on the network: a warm entry is a dict, a cold one is
    `{}` and a background resolve kicked off for next time. Anime with an
    IMDb id only - everything else has nothing to resolve and gets `{}`.

    `{}` is a real answer, not a failure: it means "filter nothing", which
    is what a cold or unknowable title should do."""
    if (entry or {}).get("type") != "Anime":
        return {}
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id:
        return {}
    with _memory_lock:
        record = _memory.get(imdb_id)
    if record is None:
        record = _load_disk(imdb_id)
        if record is not None:
            with _memory_lock:
                _memory[imdb_id] = record
    if record is not None and _fresh(record):
        return _arcs_of(record)
    _spawn_resolve(entry)               # cold or stale: warm it for next time
    return {}


def prewarm(entry):
    """Resolve the map in the background now, so a later play is filtered
    on its first press rather than its second. Cheap to over-call - a
    warm id returns immediately, an in-flight one is deduplicated."""
    if (entry or {}).get("type") != "Anime" or not (entry or {}).get("imdb_id"):
        return
    if arc_map(entry):                  # already warm; nothing to do
        return


def seasons_named(release_title: str, arcs: dict) -> set:
    """Which seasons this release name points to by arc token, as a set.

    Empty means the name carries no arc word this franchise knows - which
    is not the same as "season one": a plain "Kimetsu no Yaiba - 01" and
    an all-seasons batch both say nothing here and are left for the
    numeric reader (indexers.stated_seasons) and the caller's bound
    logic to judge."""
    if not arcs:
        return set()
    words = _words(release_title)
    if not words:
        return set()
    return {season for season, tokens in arcs.items() if tokens & words}


def clear_cache():
    """Drop everything - the memory layer and the disk copies. For tests
    and a manual reset; nothing in normal use needs it."""
    with _memory_lock:
        _memory.clear()
    try:
        for path in _cache_dir().glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass
