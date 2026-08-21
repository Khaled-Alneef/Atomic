"""Per-episode ratings from TMDB, because Cinemeta's are mostly "0".

Cinemeta carries a `rating` on every video row, and for this library it
is almost always the string "0" - its way of saying "unrated". Measured
21 August 2026 over the owner's own tracked titles plus the three named
in the bug report:

    Attack on Titan     0 rated / 89 episodes
    Demon Slayer        0 / 63        House of the Dragon  0 / 26
    Frieren             0 / 38        Re:Zero              0 / 85
    Jujutsu Kaisen      0 / 59
    Breaking Bad       67 / 67        Bleach (2004)      373 / 373

So Cinemeta answers for western live action and for one older anime, and
answers nothing at all for everything currently being watched here. TMDB
rates the same episodes - 25/25 on Attack on Titan season 1, all five
Demon Slayer seasons, all three of House of the Dragon.

**The number this returns is TMDB's own user rating, not IMDb's.** They
are close but not equal (Breaking Bad's pilot: TMDB 8.5, Cinemeta 8.2 in
the same fetch), so a row must not be labelled "IMDb" once it is coming
from here. One list should also print one source rather than mixing two
scales down the same column.

## Why a season number is not enough to ask with

TMDB and Cinemeta disagree about what a season *is*, and for anime they
disagree more often than not. Measured over the same nine titles: TMDB
files Frieren (38 episodes), Re:Zero (85), Jujutsu Kaisen (59) and
Bleach (366) as a single absolutely-numbered season, where Cinemeta
splits each into the seasons the tracker shows. Asking TMDB for
"season 3" of those is a 404 - harmless - but Bleach shows the harmful
shape: **TMDB's Bleach season 2 is Thousand-Year Blood War (2022), while
Cinemeta's season 2 is the 2005 Soul Society arc.** The episode numbers
overlap perfectly and the air dates are 6425 to 6611 days apart, so
matching on the number alone would have printed 2022 ratings onto 2005
episodes and looked entirely convincing.

What this module matches on instead is the **air date**, which both
sources agree on to within a day (measured: delta 0 or 1 across every
title, tolerance here is 2). Every rating returned has been confirmed
against the date of the row it will be printed on; anything unconfirmed
is dropped. That is why `videos` is a required argument rather than a
convenience - it is the evidence, not decoration.

The TMDB season to look in is chosen the same way: by which season's
air-date window covers the rows being asked about, not by its number.

## Cost

One `/find` (IMDb id to TMDB id), one `/tv/{id}` (the season list) and
normally one `/tv/{id}/season/{n}` - then nothing, for a week. The whole
index is cached per series on disk, so flipping between seasons on the
details page fires no requests at all after the first, and a restart
does not pay for it again. Never one request per row.

Everything fails soft to `{}`: no key, no match, a 404, a dead network,
a season TMDB has never heard of. A missing rating is a row without a
number on it, which is what the page shows today anyway.
"""

import datetime
import json
import os
import threading
import time

from . import artwork, logs, net, storage, title_match

DEFAULT_TIMEOUT = 8

# The whole chain - find, series, seasons - under one budget, 2x a single
# request the way anime_sites bounds its engines. Five requests at eight
# seconds each is not an eight second wait.
CHAIN_BUDGET = 2

# Votes accumulate for years; nothing here changes hour to hour. A week
# is short enough that a season airing now picks up its first ratings
# while the owner is still watching it.
TTL_SECONDS = 7 * 24 * 3600
# A title TMDB answered nothing useful for is re-asked sooner than that:
# the usual reason is that the episodes are too new to carry votes yet.
MISS_TTL_SECONDS = 12 * 3600

# Cinemeta's air dates run one day behind TMDB's for US-scheduled shows
# (a timezone, measured as exactly 1 across Breaking Bad, House of the
# Dragon and Attack on Titan) and identical for anime. Two days of slack
# covers that without ever reaching a neighbouring weekly episode.
DATE_TOLERANCE_DAYS = 2

# A vote_average with no votes behind it is TMDB's 0.0, and printing that
# as a confident rating is exactly the bug this module exists to fix.
# Measured over the titles above, a rated episode carries 70-540 votes
# and an unrated one carries 0 - there is no populated middle to tune
# against, so this only has to be above zero.
MIN_VOTES = 1

# Fetching every season of a long-running show to answer one question is
# how a helper turns into a stall. The window search normally picks one.
MAX_SEASON_FETCHES = 3

_CACHE_VERSION = 1

_memory = {}
_memory_lock = threading.Lock()
_series_locks = {}


def _cache_dir():
    path = storage.DATA_DIR / "rating_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _series_lock(imdb_id):
    """One lock per series, so the details page flipping quickly between
    two seasons of the same show does not fetch the same index twice.
    Not one global lock: that would make an unrelated title wait out a
    slow one on a shared worker."""
    with _memory_lock:
        return _series_locks.setdefault(imdb_id, threading.Lock())


def _fresh(stamp, ttl):
    try:
        return (time.time() - float(stamp or 0)) < ttl
    except (TypeError, ValueError):
        return False


def _load(imdb_id):
    with _memory_lock:
        record = _memory.get(imdb_id)
    if record is not None:
        return record
    path = _cache_dir() / f"{imdb_id}.json"
    record = {}
    try:
        if path.exists():
            # utf-8-sig for the same reason storage.py uses it: a BOM is
            # invisible and has already cost this project one settings
            # file that silently stopped parsing.
            with open(path, encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and loaded.get("v") == _CACHE_VERSION:
                record = loaded
    except (OSError, ValueError):
        record = {}                     # a corrupt cache is just a miss
    with _memory_lock:
        _memory[imdb_id] = record
    return record


def _save(imdb_id, record):
    record["v"] = _CACHE_VERSION
    with _memory_lock:
        _memory[imdb_id] = record
    path = _cache_dir() / f"{imdb_id}.json"
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(temp, path)
    except OSError:
        pass                            # cache only; the answer still stands


def _day(value):
    """The date out of an ISO stamp, or None. Cinemeta writes
    "2013-04-06T00:00:00.000Z" and TMDB writes "2013-04-07"; both start
    with the ten characters that matter."""
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _season_rows(videos, season):
    """(number, date, name) for one Cinemeta season, newest data as-is.

    Rows with no usable episode number are dropped rather than numbered
    zero - a rating has to land on a row the page can key it back to."""
    out = []
    for video in (videos or []):
        # AttributeError too: one row that is not a dict must cost that
        # row, not the whole season's ratings.
        try:
            if int(video.get("season") or 0) != int(season):
                continue
            number = int(video.get("number") or video.get("episode") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if number <= 0:
            continue
        out.append((number,
                    _day(video.get("firstAired") or video.get("released")),
                    str(video.get("name") or video.get("title") or "")))
    return out


def _fetch_series(imdb_id, deadline, timeout):
    """TMDB's id for an IMDb id, plus its season list with air dates."""
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return None
    kind, tmdb_id = artwork.tmdb_id(imdb_id, step)
    if kind != "tv" or not tmdb_id:
        return None                     # a film has no episode list
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return None
    show = artwork.get_json(f"{artwork.API}/tv/{tmdb_id}", step)
    seasons = []
    for row in ((show or {}).get("seasons") or []):
        try:
            number = int(row.get("season_number"))
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue                    # season 0 is specials, never asked for
        seasons.append({"n": number, "air": str(row.get("air_date") or "")[:10]})
    seasons.sort(key=lambda row: row["n"])
    return {"tmdb_id": tmdb_id, "seasons": seasons, "checked": time.time()}


def _fetch_season(tmdb_id, number, deadline, timeout):
    """One TMDB season as [[episode number, air date, rating, name], ...].

    Unrated episodes are kept with a null rating on purpose: they are
    still needed to pair up a date that carries two episodes."""
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return None
    body = artwork.get_json(
        f"{artwork.API}/tv/{tmdb_id}/season/{int(number)}", step)
    rows = []
    for episode in ((body or {}).get("episodes") or []):
        try:
            index = int(episode.get("episode_number"))
        except (TypeError, ValueError):
            continue
        votes = episode.get("vote_count") or 0
        score = episode.get("vote_average") or 0
        rating = None
        try:
            if int(votes) >= MIN_VOTES and float(score) > 0:
                rating = round(float(score), 1)
        except (TypeError, ValueError):
            rating = None
        rows.append([index, str(episode.get("air_date") or "")[:10], rating,
                     str(episode.get("name") or "")])
    return {"rows": rows, "checked": time.time()}


def _candidate_seasons(seasons, first, last, asked):
    """Which TMDB seasons could hold episodes that aired between `first`
    and `last`.

    By window rather than by number, because the numbers disagree: TMDB
    keeps Frieren, Re:Zero, Jujutsu Kaisen and Bleach as one long season
    each, so every Cinemeta season of those lives inside TMDB's season 1.
    A season's window runs from its own air date to the next dated
    season's - seasons TMDB has not dated yet cannot be placed, so they
    are only tried when nothing else fits, and the number actually asked
    for is the last resort. Trying a season is cheap to be wrong about:
    every episode in it still has to match a row's air date before its
    rating is used, so the worst a bad guess costs is one request and an
    empty answer."""
    if not seasons or not first or not last:
        return ([row["n"] for row in (seasons or [])]
                or [int(asked)])[:MAX_SEASON_FETCHES]
    slack = datetime.timedelta(days=DATE_TOLERANCE_DAYS)
    dated = [row for row in seasons if _day(row.get("air"))]
    undated = [row["n"] for row in seasons if not _day(row.get("air"))]
    hits = []
    for position, row in enumerate(dated):
        start = _day(row["air"])
        end = None
        for later in dated[position + 1:]:
            end = _day(later["air"])
            break
        if start - slack > last:
            continue
        if end is not None and end <= first - slack:
            continue
        hits.append(row["n"])
    return (hits or undated or [int(asked)])[:MAX_SEASON_FETCHES]


def _index(record, numbers):
    """{air date: [row, ...]} across the given cached TMDB seasons."""
    out = {}
    for number in numbers:
        season = (record.get("episodes") or {}).get(str(number)) or {}
        for row in (season.get("rows") or []):
            stamp = _day(row[1])
            if stamp:
                out.setdefault(stamp, []).append(row)
    for rows in out.values():
        rows.sort(key=lambda row: row[0])
    return out


def _group_key(index, stamp):
    """The index date this row's date belongs to, within tolerance.

    Exact first, then a day either side: Cinemeta runs a day behind for
    US-scheduled shows and level with anime, and nothing airs weekly
    closely enough for two days of slack to reach the wrong episode."""
    if not stamp:
        return None
    offsets = [0]
    for step in range(1, DATE_TOLERANCE_DAYS + 1):
        offsets += [step, -step]
    for offset in offsets:
        key = stamp + datetime.timedelta(days=offset)
        if key in index:
            return key
    return None


def _pair(wanted, candidates):
    """Pair Cinemeta rows against TMDB episodes that share an air date.

    Usually one against one. Two arrive together when a show aired a
    double episode - measured on Bleach (8 such dates), Frieren, Jujutsu
    Kaisen and Attack on Titan - and on a whole season dropped at once.
    In that case the name settles it, or the number where the two sources
    number alike, or, failing both, ascending order when each side holds
    the same count. Anything still ambiguous gets no rating: a swapped
    pair is a wrong number printed confidently."""
    if len(wanted) == 1 and len(candidates) == 1:
        return [(wanted[0], candidates[0])]
    taken, pairs = set(), []
    left = []
    for row in wanted:
        number, name = row[0], row[2]
        best, score = None, 0
        for position, episode in enumerate(candidates):
            if position in taken:
                continue
            if name and title_match.normalize(name) == title_match.normalize(
                    episode[3]):
                best, score = position, 3
                break
            if episode[0] == number and score < 2:
                best, score = position, 2
            elif score < 1 and name and episode[3] and title_match.similarity(
                    name, episode[3]) >= 0.85:
                best, score = position, 1
        if best is None:
            left.append(row)
        else:
            taken.add(best)
            pairs.append((row, candidates[best]))
    spare = [episode for position, episode in enumerate(candidates)
             if position not in taken]
    if left and len(left) == len(spare):
        # Same count on both sides and nothing to tell them apart by
        # name: they are the same episodes in the same order. This is
        # what recovers Bleach's double airings, where the two sources
        # transliterate the titles differently ("Friido" / "Fried").
        for row, episode in zip(sorted(left, key=lambda row: row[0]),
                                sorted(spare, key=lambda episode: episode[0])):
            pairs.append((row, episode))
    return pairs


def _match(index, rows):
    """{episode number: rating} for the Cinemeta rows that TMDB confirms."""
    groups = {}
    for row in rows:
        key = _group_key(index, row[1])
        if key is not None:
            groups.setdefault(key, []).append(row)
    out = {}
    for key, wanted in groups.items():
        for row, episode in _pair(wanted, index.get(key) or []):
            if episode[2]:
                out[row[0]] = episode[2]
    return out


def _answer(record, videos, season):
    """What the cached index can answer right now, with no network."""
    rows = _season_rows(videos, season)
    dates = [row[1] for row in rows if row[1]]
    if not dates:
        return {}
    numbers = _candidate_seasons(record.get("seasons") or [],
                                 min(dates), max(dates), season)
    return _match(_index(record, numbers), rows)


def cached_episode_ratings(imdb_id: str, season, videos) -> dict:
    """Whatever is already cached for this season, without a request.

    Safe to call from the UI thread - it never opens a socket, so a page
    can draw with it immediately and ask `episode_ratings` on a worker
    for the rest. Empty until something has fetched."""
    try:
        if not imdb_id:
            return {}
        return _answer(_load(str(imdb_id)), videos, season)
    except Exception:
        logs.exception("cached episode ratings failed")
        return {}


def episode_ratings(imdb_id: str, season, videos,
                    timeout: int = DEFAULT_TIMEOUT) -> dict:
    """TMDB's rating for each episode of one season: `{number: 8.4}`.

    `imdb_id` is the entry's own id, `season` the season the page is
    showing, and `videos` Cinemeta's `videos` list from the same meta
    record the rows were built from - the whole list is fine, it filters
    by season itself. Those rows carry the air dates that every rating
    returned here is confirmed against; without them nothing can be
    matched safely (see the module docstring - TMDB's Bleach season 2 is
    a different decade from Cinemeta's).

    Keyed by the episode number the *page* prints, so a row reads its own
    rating straight out: `ratings.get(number)`. An episode nobody has
    voted on is absent rather than 0.0, and a missing key means "no
    rating", never "zero".

    Blocking - it makes up to five HTTP requests on a cold series and
    none at all on a warm one - so call it from a worker (lookup_pool)
    and carry the answer back over a signal, never from the UI thread.
    Returns {} for anything that goes wrong."""
    try:
        imdb_id = str(imdb_id or "").strip()
        rows = _season_rows(videos, season)
        dates = [row[1] for row in rows if row[1]]
        if not imdb_id or not dates or not artwork.token():
            return {}

        deadline = net.deadline_in(timeout * CHAIN_BUDGET)
        with _series_lock(imdb_id):
            record = dict(_load(imdb_id))
            dirty = False
            if not record.get("tmdb_id") or not _fresh(
                    record.get("checked"),
                    TTL_SECONDS if record.get("seasons") else MISS_TTL_SECONDS):
                series = _fetch_series(imdb_id, deadline, timeout)
                if series:
                    record.update(series)
                    dirty = True
                elif not record.get("tmdb_id"):
                    # Remember the miss so a title TMDB does not carry is
                    # not looked up again on every visit.
                    _save(imdb_id, {"checked": time.time(), "seasons": []})
                    return {}

            numbers = _candidate_seasons(record.get("seasons") or [],
                                         min(dates), max(dates), season)
            episodes = dict(record.get("episodes") or {})
            for number in numbers:
                cached = episodes.get(str(number)) or {}
                ttl = TTL_SECONDS if cached.get("rows") else MISS_TTL_SECONDS
                if _fresh(cached.get("checked"), ttl):
                    continue
                season_rows = _fetch_season(record["tmdb_id"], number,
                                            deadline, timeout)
                if season_rows is not None:
                    episodes[str(number)] = season_rows
                    dirty = True
            record["episodes"] = episodes
            if dirty:
                _save(imdb_id, record)

        return _match(_index(record, numbers), rows)
    except Exception:
        # Fail soft, but not silently - a rating that stops appearing
        # should be findable in the log rather than guessed at.
        logs.exception("episode ratings lookup failed")
        return {}


def clear_cache():
    """Forget every cached index - Settings' cache reset, and the way a
    measurement starts from cold."""
    with _memory_lock:
        _memory.clear()
    try:
        for name in os.listdir(_cache_dir()):
            try:
                os.remove(_cache_dir() / name)
            except OSError:
                pass
    except OSError:
        pass
