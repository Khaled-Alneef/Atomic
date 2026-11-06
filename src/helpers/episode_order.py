"""Which episode of a whole series a Cinemeta (season, episode) is.

**The owner, 7 November 2026**: *"the one piece anime has an issue I think
because I have it here as seasons and Opensubtitles has it as one very long
season"*. Measured that day: Cinemeta files One Piece (tt0388629) as 23
seasons (8, 22, 17, 13, ... 194, 70, 25 episodes), and opensubtitles.org
files every one of its subtitles under Season 1 with the broadcast number -
asking it for season 2 answers **0 rows**, asking for season 1 episode 9
answers 11 Arabic rows for "Captain Usopp", which is Cinemeta's S02E01. The
Stremio OpenSubtitles addon is the same: `tt0388629:2:1` empty,
`tt0388629:1:9` five Arabic.

**Counting Cinemeta's own seasons is not the answer for this show.**
`streams.absolute_episode` does that, and on One Piece it is wrong for
**590 of 1,179 episodes**: Cinemeta's list has holes and doubles, so its
S21E01 (2019-07-07, "The Land of Wano!") counts out as 891 where the
broadcast number is 892, and S22E30 ("Kuzan") as 1114 against 1115 - a
subtitle for the episode next door, which is worse than none.

TVmaze lists the show's episodes in broadcast order, and matched to Cinemeta
by air date its position is the broadcast number at every point checked
against opensubtitles.org's own titles (9, 63, 144, 431, 892, 1085, 1115,
1156). The air dates need two guards, because about twenty of Cinemeta's are
wrong (One Piece S07E10 dated onto episode 105's day, Bleach TYBW S02E10
onto episode 14's): a match is kept only where it runs in order with the
matches around it, and an episode left without one is filled in only where
the matched episodes either side of it are exactly the right distance apart.
Anything else answers None, and the caller asks nothing extra.

Fails soft everywhere: no IMDb id, no TVmaze record, no network - None."""

import datetime
import threading
import time
from bisect import bisect_left

from . import logs, storage, title_match

try:
    from . import stremio, tvmaze
except Exception:  # pragma: no cover - a source module that cannot import
    stremio = tvmaze = None

# TVmaze's list on disk per title. A new episode airs weekly, so a day; an
# episode the cached list does not reach is asked again sooner (REFETCH_S),
# which is what the newest episode of an airing show meets.
TVMAZE_TTL_S = 24 * 3600.0
REFETCH_S = 600.0

# Two TVmaze rows on one day (a double episode) are told apart by title; a
# score this close to the best counts as a tie, and a tie goes to the
# earliest row after the previous match.
TIE_MARGIN = 0.02

_lock = threading.Lock()
_maps = {}          # imdb_id -> (built_at, list ts, {(season, episode): number})


def _day(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _cache_name(imdb_id) -> str:
    return "tvmaze-episodes-" + "".join(ch for ch in str(imdb_id).lower() if ch.isalnum()) + ".json"


def _tvmaze_list(imdb_id, timeout, fresh=False):
    """(fetched_at, rows) from disk or TVmaze; (0, None) when neither has it."""
    name = _cache_name(imdb_id)
    stored = None
    try:
        stored = storage.load(name, None)
    except Exception:
        stored = None
    if isinstance(stored, dict) and isinstance(stored.get("episodes"), list):
        age = time.time() - float(stored.get("ts") or 0)
        if age < (REFETCH_S if fresh else TVMAZE_TTL_S):
            return float(stored.get("ts") or 0), stored["episodes"]
    rows = tvmaze.fetch_episodes(imdb_id, timeout) if tvmaze is not None else None
    if rows:
        now = time.time()
        try:
            storage.save(name, {"ts": now, "episodes": rows})
        except Exception:
            pass
        return now, rows
    if isinstance(stored, dict) and isinstance(stored.get("episodes"), list):
        return float(stored.get("ts") or 0), stored["episodes"]   # stale beats nothing
    return 0, None


def _longest_increasing(values):
    """Indices into `values` (pairs of (position, number)) forming the
    longest run whose numbers strictly increase."""
    tails, tail_at, parent = [], [], [None] * len(values)
    for index, (_, number) in enumerate(values):
        slot = bisect_left(tails, number)
        if slot == len(tails):
            tails.append(number)
            tail_at.append(index)
        else:
            tails[slot] = number
            tail_at[slot] = index
        parent[index] = tail_at[slot - 1] if slot else None
    chain, at = [], (tail_at[-1] if tail_at else None)
    while at is not None:
        chain.append(at)
        at = parent[at]
    return chain[::-1]


def build_map(videos, episodes) -> dict:
    """{(season, episode): overall number} for every Cinemeta video that
    can be placed in TVmaze's broadcast order. Pure - no network."""
    order = sorted({(int(v.get("season") or 0), int(v.get("episode") or 0)): v
                    for v in videos or [] if isinstance(v, dict)
                    and int(v.get("season") or 0) >= 1
                    and int(v.get("episode") or 0) >= 1}.items())
    by_day = {}
    for number, row in enumerate(episodes or [], 1):
        when = _day(row.get("airdate"))
        if when:
            by_day.setdefault(when, []).append((number, row.get("name") or ""))

    # 1. Each video's own match: TVmaze rows within a day of its air date
    #    (Cinemeta stamps UTC, TVmaze the broadcast's local day).
    raw, last = [], 0
    for _key, video in order:
        when = _day(video.get("released") or video.get("firstAired"))
        candidates = []
        if when:
            for shift in (-1, 0, 1):
                candidates += by_day.get(when + datetime.timedelta(days=shift), [])
        if not candidates:
            raw.append(None)
            continue
        name = video.get("name") or video.get("title") or ""
        scored = [(title_match.similarity(name, tv_name) if name and tv_name else 0.0, number)
                  for number, tv_name in candidates]
        top = max(score for score, _ in scored)
        near = sorted(number for score, number in scored if score >= top - TIE_MARGIN)
        after = [number for number in near if number > last]
        pick = after[0] if after else near[0]
        raw.append((pick, top))
        last = max(last, pick)

    # 2. Two videos claiming one episode: the better title keeps it.
    best = {}
    for position, hit in enumerate(raw):
        if hit and (hit[0] not in best or hit[1] > raw[best[hit[0]]][1]):
            best[hit[0]] = position
    pairs = [(position, hit[0]) for position, hit in enumerate(raw)
             if hit and best.get(hit[0]) == position]

    # 3. Matches that run in order, and agree with a neighbour. The offset
    #    (number - position) only changes where Cinemeta has a hole or a
    #    double, so a match whose offset neither neighbour shares, while
    #    those two agree with each other, is a wrong air date.
    anchors = [pairs[i] for i in _longest_increasing(pairs)]
    kept = []
    for i, (position, number) in enumerate(anchors):
        offset = number - position
        before = anchors[i - 1][1] - anchors[i - 1][0] if i else None
        after = anchors[i + 1][1] - anchors[i + 1][0] if i + 1 < len(anchors) else None
        if before is not None and after is not None and before == after != offset:
            continue
        kept.append((position, number))

    # 4. Fill a gap only where it is exactly as long on both sides; from
    #    the start only where the first match lines up with episode 1.
    found = dict(kept)
    bounds = [(-1, 0)] + kept
    for (p, a), (q, b) in zip(bounds, bounds[1:]):
        if q - p > 1 and b - a == q - p:
            for position in range(p + 1, q):
                found[position] = a + (position - p)
    return {order[position][0]: number for position, number in found.items()}


def overall_number(imdb_id, season, episode, timeout=8):
    """The broadcast number of `season`x`episode` in Cinemeta's split, or
    None. Network on a cold title (TVmaze, two requests); call off the UI
    thread. Never raises."""
    try:
        season, episode = int(season or 0), int(episode or 0)
        if not imdb_id or season < 1 or episode < 1 or stremio is None:
            return None
        key = str(imdb_id)
        with _lock:
            for attempt in (0, 1):
                held = _maps.get(key)
                if held is None or attempt:
                    meta = stremio.fetch_meta_cached(key, "series", timeout)
                    videos = (meta or {}).get("videos") or []
                    listed_at, rows = _tvmaze_list(key, timeout, fresh=bool(attempt))
                    if not videos or not rows:
                        return None
                    held = (time.time(), listed_at, build_map(videos, rows))
                    _maps[key] = held
                number = held[2].get((season, episode))
                if number is not None:
                    return number
                # Not placed: a list fetched before this episode aired is
                # the one case another fetch can change. Once, and not
                # again inside REFETCH_S.
                if attempt or time.time() - held[1] < REFETCH_S:
                    return None
        return None
    except Exception:
        logs.exception("episode_order: could not number an episode")
        return None
