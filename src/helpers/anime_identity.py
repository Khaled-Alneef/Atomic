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

  * **The entry's own Cinemeta meta is the authority on the numbering**
    (since 5 September 2026 - see _entry_seasons), because it *is* the
    numbering the sources are asked in: `tt12343534:1:1`. TMDB was
    taken to be the same numbering and for Demon Slayer it is - for
    tt9335498 it returns S01 "Unwavering Resolve Arc" / S02 "Mugen Train
    Arc" / S03 "Entertainment District Arc" / S04 "Swordsmith Village
    Arc" / S05 "Hashira Training Arc", air dates and all - but for
    Jujutsu Kaisen TMDB has one 59-episode season where Cinemeta has
    four, and a map built on TMDB's split had nowhere to put "Shimetsu
    Kaiyuu". TMDB is now the fallback split (no meta on disk) and the
    source of the English arc names, attached to a season by air date.
    No source's ordering can be trusted to match the card's by number,
    which is the whole risk in this - AniList files each cour as its
    own work and does not number them the way either splits them.
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
import urllib.error

from . import artwork, logs, net, storage, stremio

# Franchise facts change about never; thirty days is already generous,
# and a title re-asked sooner than that is served from memory anyway.
_TTL = 30 * 24 * 3600.0
# A map built without AniList carries only TMDB's English arc names, which
# miss the two Demon Slayer arcs whose romaji shares no word with the
# English ("Yuukaku" for Entertainment District, "Katanakaji" for
# Swordsmith Village). That is a *partial* answer and must not be cached
# like a complete one.
#
# **Measured 23 August 2026 and worth knowing: AniList was answering 403
# to this whole network at the time** (the documented rate-limit trap in
# .claude/rules/integrations.md - not a 429, and measured lasting over an
# hour). So the map for Demon Slayer came back TMDB-English-only, which
# still catches "Hashira Geiko" (via "Hashira Training Arc") and "Mugen
# Ressha" (via "Mugen Train Arc") but *not* "Yuukaku Hen" or "Katanakaji
# no Sato Hen", whose romaji shares no word with TMDB's English. That is
# a smaller filter, never a wrong one - it drops fewer rows, and still
# drops none it should keep.
#
# 45 minutes, not six hours: the block lifts on its own, and the whole
# point of marking a map partial is to pick the romaji up shortly after
# it does rather than running degraded for the rest of the day.
_PARTIAL_TTL = 45 * 60.0
# 2 since 5 September 2026: version 1 records were built on TMDB's season
# split alone, and for a title TMDB does not split (Jujutsu Kaisen, one
# 59-episode season there against Cinemeta's four) they hold no arc at
# all - see _entry_seasons. A stored v1 map is a miss, not a stale hit.
_CACHE_VERSION = 2
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
        except urllib.error.HTTPError as exc:
            # **A 403/429 is the connection being refused, not this query
            # failing** - .claude/rules/integrations.md records the same
            # thing about search variants: "a second variant buys another
            # 403 and another throttle wait". Retrying is pure delay, so
            # give up now and let the partial map's short TTL re-ask later.
            if exc.code in (403, 429):
                return []
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return []


def _months(year, month) -> int:
    return int(year) * 12 + int(month or 1)


def _entry_seasons(entry):
    """The entry's own season split - `([(number, [air_month, ...]), ...],
    name)`, read from the Cinemeta meta the details page keeps on disk
    (`stremio.cached_meta`) - or `([], "")` when there is none.

    **This is the numbering the app actually asks the sources in, and
    TMDB's is not always the same numbering.** The module docstring took
    TMDB to be the app's own; measured 5 September 2026 on the owner's
    Jujutsu Kaisen (tt12343534), TMDB files the whole show as one
    59-episode "Season 1" while Cinemeta - whose ids the addons are
    asked with, `tt12343534:1:1` - has S1 (24 episodes, 2020-10), S2
    (23, 2023-07) and S3 (12, 2026-01). AniList knows all three TV
    seasons with exactly those start dates, so with TMDB's split there
    was nowhere to hang seasons 2 and 3 and the map came out as
    `{1: [jujutsu, kaisen]}`: not one arc word, and the franchise's own
    name filed as season 1's. "[Erai-raws] Jujutsu Kaisen: Shimetsu
    Kaiyuu - Zenpen - 01" - season 3, 687 seeders, no season number in
    its name - then headed the S01E01 list and was what played.

    Every episode's month, not only the first: a cour split airs months
    after its season's first episode and AniList files it as its own
    work, so it is matched to a season by whichever episode it aired
    beside (_season_beside)."""
    imdb_id = (entry or {}).get("imdb_id")
    if not imdb_id:
        return [], ""
    try:
        meta = stremio.cached_meta(imdb_id, "series") or {}
    except Exception:
        meta = {}
    by_season = {}
    for video in meta.get("videos") or []:
        try:
            number = int(video.get("season") or 0)
            released = str(video.get("released") or video.get("firstAired")
                           or "")
            if number < 1 or len(released) < 7:
                continue
            by_season.setdefault(number, set()).add(
                _months(int(released[:4]), int(released[5:7])))
        except (TypeError, ValueError):
            continue
    seasons = sorted((number, sorted(months))
                     for number, months in by_season.items())
    return seasons, str(meta.get("name") or "")


def _season_beside(begun, seasons, tolerance: int = 2):
    """Which of `seasons` ([(number, [months])]) something that started
    in month `begun` aired beside - the one with an episode within
    `tolerance` months of it, closest first - or None."""
    best, best_gap = None, tolerance + 1
    for number, months in seasons:
        for month in months:
            gap = abs(begun - month)
            if gap < best_gap:
                best_gap, best = gap, number
    return best


def _build(entry):
    """Resolve the `{season -> [tokens]}` map for one entry, live.

    The season split is the entry's own (Cinemeta's, see
    _entry_seasons), with TMDB's as the fallback when no meta is on disk.
    TMDB supplies the English arc names and AniList the romaji, each
    attached to a season by air date - never by number, because the two
    numberings are not always the same. A token surviving into the map is
    one that names exactly one of those seasons and is not a word of the
    franchise's own title.

    Returns `(arcs, anilist_ok)` - the second flag is False when AniList
    gave nothing, so the caller can re-check a TMDB-English-only map soon
    rather than trusting it for a month (see _resolve_and_store)."""
    imdb_id = (entry or {}).get("imdb_id")
    title = (entry or {}).get("title") or ""
    if not imdb_id:
        return {}, True
    seasons, meta_name = _entry_seasons(entry)
    source = "meta"

    # TMDB's seasons, `[(number, air_month, name)]`: the fallback split,
    # and the English arc names either way.
    tmdb_seasons = []
    if artwork.available():
        try:
            media_type, tmdb_id = artwork.tmdb_id(imdb_id, _TIMEOUT)
            if media_type == "tv" and tmdb_id:
                show = artwork.get_json(f"{artwork.API}/tv/{tmdb_id}",
                                        _TIMEOUT)
                for season in (show or {}).get("seasons", []):
                    number = int(season.get("season_number") or 0)
                    air = str(season.get("air_date") or "")
                    if number >= 1 and len(air) >= 7:
                        tmdb_seasons.append(
                            (number, _months(int(air[:4]), int(air[5:7])),
                             season.get("name") or ""))
        except Exception:
            tmdb_seasons = []
    if not seasons:
        if not tmdb_seasons:
            return {}, True             # a film, or a title nobody splits
        seasons = [(number, [air]) for number, air, _name in tmdb_seasons]
        source = "tmdb"

    media = _anilist_media(title or meta_name)

    raw = {number: set() for number, _months_ in seasons}
    # TMDB's own arc name - "Swordsmith Village Arc" - goes to the season
    # it aired beside, unless it is the generic "Season N", which says
    # nothing.
    for _number, air, name in tmdb_seasons:
        if re.search(r"season\s*\d", name, re.I):
            continue
        beside = _season_beside(air, seasons)
        if beside is not None:
            raw[beside] |= _words(name)
    # Each AniList work - a season, a cour, a recap ONA - goes to the
    # season it aired beside, and brings its romaji and English.
    for candidate in media:
        start = candidate.get("startDate") or {}
        try:
            begun = _months(start["year"], start.get("month"))
        except (KeyError, TypeError, ValueError):
            continue
        beside = _season_beside(begun, seasons)
        if beside is None:
            continue
        titles = candidate.get("title") or {}
        raw[beside] |= _words(titles.get("romaji"))
        raw[beside] |= _words(titles.get("english"))

    # The franchise name is in every season's title; the one-season-only
    # rule is what removes it and leaves the arc words behind. **And the
    # entry's own title words are removed outright**, because the rule
    # cannot fire when only one season carries any tokens at all - which
    # is how "rotten" (The Angel Next Door Spoils Me Rotten, 27 August
    # 2026) and "jujutsu"/"kaisen" (above) became season-1 arc words that
    # every release of every season matched.
    franchise = _words(title) | _words(meta_name)
    seen = {}
    for tokens in raw.values():
        for word in tokens:
            seen[word] = seen.get(word, 0) + 1
    arcs = {number: sorted(word for word in tokens
                           if seen[word] == 1 and word not in franchise)
            for number, tokens in raw.items()}
    try:
        logs.info(f"anime_identity: {title or meta_name}: "
                  f"{len(seasons)} seasons from {source}, anilist "
                  f"{len(media)} -> "
                  + ", ".join(f"S{n}:{'/'.join(t) or '-'}"
                              for n, t in sorted(arcs.items())))
    except Exception:
        pass
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


def _cached_arcs(entry):
    """The stored map for this entry without starting a resolve - the
    poll half of `arc_map_soon`, which must not re-spawn on every tick."""
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
    return {}


def arc_map_soon(entry, timeout: float = 0.0) -> dict:
    """The arc map, waiting up to `timeout` seconds for a resolve that is
    already in flight, or `{}`.

    `arc_map` deliberately never blocks, which is right while a lookup is
    being *drawn* - but wrong at the one moment the answer decides what
    gets played. Measured 23 August 2026: with a cold cache, "Kimetsu no
    Yaiba - Yuukaku Hen" (season 3) won a race for S01E01, because the
    map answered `{}` and the filter had nothing to judge with. A cold
    first play was therefore still able to play the wrong season - the
    exact complaint this module exists to fix.

    So the *final* list waits a moment where a partial does not. By the
    time this is called the fan-out has already run (0.4-2.4s measured),
    which is time the background resolve has been running too, so the
    usual cost here is zero; the timeout is only for the case where TMDB
    is slower than the sources were."""
    found = arc_map(entry)          # warm hit, or start the resolve
    if found or timeout <= 0:
        return found
    # **Wait only for a resolve that is actually running.** `arc_map`
    # answers `{}` without spawning anything for a non-anime entry or one
    # with no IMDb id, so the poll below had nothing to poll for and slept
    # out the whole timeout for an answer that was never coming.
    #
    # Measured 23 August 2026 inside find_streams, cold, on the owner's
    # own entries: House of the Dragon (type "Series") paid **2.52s** here
    # and Bleach: Thousand-Year Blood War (also filed "Series") **2.55s**,
    # both returning 0 seasons - against fan-outs of 1.95s and 0.16s. So
    # the flat ~2.5s tail on every cold press *was* this loop, and on the
    # two titles measured it bought precisely nothing. find_streams for
    # Bleach: 2.71s -> 0.16s.
    imdb_id = (entry or {}).get("imdb_id")
    with _inflight_lock:
        pending = bool(imdb_id) and imdb_id in _inflight
    if not pending:
        return {}
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        time.sleep(0.1)
        found = _cached_arcs(entry)
        if found:
            return found
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
