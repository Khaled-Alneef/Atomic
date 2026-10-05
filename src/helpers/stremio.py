"""Minimal client for Stremio's Cinemeta addon (https://v3-cinemeta.strem.io)
and a launcher for the Stremio desktop app. Search + a full-metadata
lookup for the latest aired episode, no API key needed. Every lookup
fails soft (returns []/None) so a flaky connection never crashes the
tracker UI - it just means no suggestions/covers/progress show up.
"""

import time
import re
import json
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

from . import net, storage

BASE_URL = "https://v3-cinemeta.strem.io"
API_URL = "https://api.strem.io/api"


class AuthFailed(Exception):
    """The stored authKey is what's wrong - revoked, expired, or belonging
    to an account that no longer exists.

    Its own type for the same reason anilist.RateLimited has one, only
    with more riding on it: Stremio is the *only* watch-progress source
    (.claude/rules/integrations.md, "settled"), so a dead session and "you
    haven't watched this yet" both arriving as None means every entry
    quietly stops syncing, forever, with nothing on screen saying why.
    Callers let this one propagate; every other failure here still fails
    soft to None."""


# Two shapes, because the account API answers with the second one.
# HTTP status: covered in case that ever changes, and because the AniList
# fix taught that the documented code and the arriving code differ.
_AUTH_HTTP_CODES = (401, 403)
# Body: api.strem.io returns **200 with {"error": {"message", "code"}}**
# rather than a status code - Stremio's own client checks `resp.status
# !== 200` and then `body.error` separately (stremio-api-client's
# apiClient.js), and stremio-core models the whole response as
# APIResult::Ok{result} | APIResult::Err{error}. So the status-code path
# alone would never fire on a real expired key.
#
# Matched on the message text, not the numeric code: Stremio publishes no
# error-code list, and neither stremio-core nor their own JS client
# branches on one, so a guessed number would be a silent wrong answer.
# "user not found" counts here because on datastoreGet the *only* thing
# identifying the user is the authKey - unlike login, where it means the
# typed email.
_AUTH_ERROR_MARKERS = (
    "authkey", "auth key", "not logged in", "not signed in", "session",
    "unauthorized", "unauthorised", "user not found", "invalid user",
)


def _raise_if_auth_error(body):
    """Turn an auth-shaped error body into AuthFailed; leave anything else
    alone.

    Deliberately narrow. An unrecognised error still falls through and
    fails soft exactly as before - telling someone their sign-in is broken
    when it isn't would be the same class of bug this exists to fix, just
    pointing the other way."""
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return
    message = str(error.get("message") or "")
    if any(marker in message.lower() for marker in _AUTH_ERROR_MARKERS):
        raise AuthFailed(message or "Stremio rejected the saved session")


def search(query_text: str, content_type: str = "series", timeout: int = 6):
    """content_type: 'series' or 'movie'.

    Returns a list of dicts: {id, title, format, cover_url, stremio_url}.
    """
    query_text = (query_text or "").strip()
    if not query_text:
        return []

    encoded = urllib.parse.quote(query_text)
    url = f"{BASE_URL}/catalog/{content_type}/top/search={encoded}.json"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(req, timeout=timeout) as resp:
            body = json.loads(net.read_text(resp, deadline))
    except Exception:
        return []

    metas = body.get("metas") or []
    results = []
    for m in metas:
        imdb_id = m.get("id") or ""
        if not imdb_id.startswith("tt"):
            continue
        results.append({
            "id": imdb_id,
            "title": m.get("name") or "Untitled",
            "format": m.get("year") or "",
            "cover_url": m.get("poster"),
            # Note the 3 slashes: "stremio://detail/..." makes Stremio's
            # protocol handler treat "detail" as an addon-manifest host
            # (it tries to fetch https://detail/... as a manifest, which
            # is the "Failed to get addon manifest" error). The extra
            # slash keeps the path in the path, not the host.
            #
            # No trailing videoId segment on purpose: appending one (even
            # the meta id itself) makes Stremio try to resolve a specific
            # episode/stream, landing on some arbitrary video instead of
            # the show's own overview page.
            "stremio_url": f"stremio:///detail/{content_type}/{imdb_id}",
        })
    return results


def _parse_aired(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# **An undated episode is usually an announcement, not an episode.**
# His report, 2 October 2026: Witch Hat Atelier and Family Guy each ended
# on a season of one episode that had not aired. Cinemeta files an
# announced season as one row with no date at all (Witch Hat S2E1
# "Episode 1", Family Guy S25E1), and every reader here took "no date"
# for "aired" - so the details page offered it, and the player stepped
# from S1E13 into it and played "[NanakoRaws] Tongari Boushi no Atelier
# - 14", marking 2:1 watched. Measured over the 912 Cinemeta records on
# his disk: 700 undated episodes; 252 sit *before* a dated episode that
# aired (Doraemon, Bullseye - real episodes missing a date), and of the
# 448 after the last aired one, 44 titles end on exactly this one-row
# placeholder season - Bocchi the Rock! last aired 2022, Yona of the
# Dawn 2015 - while old finished shows trail long undated runs that did
# air (Maya 165 rows after 1975, Doraemon 84 after 2009, Cardcaptor
# Sakura S3E25 after 2000). So an undated episode counts as unaired only
# past the last aired one, and only when the show aired recently or the
# tail is a short new season.
UNDATED_RECENT_DAYS = 730
UNDATED_PLACEHOLDER_MAX = 3


def unaired_episodes(videos, now=None) -> set:
    """{(season, number)} of the episodes in Cinemeta's `videos` that have
    not aired: dated after now, or undated and judged an announcement (see
    UNDATED_RECENT_DAYS). Specials (season or number 0) are left out of
    the judgement - they are never counted as aired by anyone. Never
    raises."""
    try:
        now = now or datetime.now(timezone.utc)
        rows = []
        for video in videos or []:
            if not isinstance(video, dict):
                continue
            season = int(video.get("season") or 0)
            number = int(video.get("number") or video.get("episode") or 0)
            if season < 1 or number < 1:
                continue
            when = _parse_aired(str(video.get("firstAired")
                                    or video.get("released") or ""))
            rows.append((season, number, when))
        rows.sort(key=lambda row: (row[0], row[1]))
        unaired = {(s, n) for s, n, when in rows if when is not None and when > now}
        aired_at = [i for i, (_s, _n, when) in enumerate(rows)
                    if when is not None and when <= now]
        if not any(when is not None for _s, _n, when in rows):
            return unaired          # no dates at all: nothing to judge by
        last = aired_at[-1] if aired_at else -1
        tail = [(s, n) for s, n, when in rows[last + 1:] if when is None]
        if not tail:
            return unaired
        if last < 0:
            return unaired | set(tail)      # nothing has aired yet
        last_season, _n, last_when = rows[last]
        recent = (now - last_when).days <= UNDATED_RECENT_DAYS
        placeholder = (len(tail) <= UNDATED_PLACEHOLDER_MAX
                       and all(s > last_season for s, _n in tail))
        if recent or placeholder:
            unaired |= set(tail)
        return unaired
    except Exception:
        return set()


def unannounced_seasons(videos) -> set:
    """Seasons whose first episode has no date and has not aired - an
    announcement, not a season yet. The owner, 2 October 2026: "if there
    is no announced date for the new season 1st ep do not show the season
    at all". A season whose first episode carries a date, even a future
    one, stays and draws as UPCOMING. Never raises."""
    try:
        unaired = unaired_episodes(videos)
        first = {}
        for video in videos or []:
            if not isinstance(video, dict):
                continue
            season = int(video.get("season") or 0)
            number = int(video.get("number") or video.get("episode") or 0)
            if season < 1 or number < 1:
                continue
            dated = _parse_aired(str(video.get("firstAired")
                                     or video.get("released") or "")) is not None
            if season not in first or number < first[season][0]:
                first[season] = (number, dated)
        return {season for season, (number, dated) in first.items()
                if not dated and (season, number) in unaired}
    except Exception:
        return set()


def fetch_meta(imdb_id: str, content_type: str = "series", timeout: int = 8):
    """Cinemeta's whole meta record for one title, or None.

    One keyless request carrying everything the details page draws:
    name, description, genres, cast, runtime, releaseInfo, imdbRating,
    and `videos[]` - every episode with its season, number, name,
    firstAired and thumbnail. Measured on Bleach TYBW: 50 episodes,
    all fields present."""
    url = f"{BASE_URL}/meta/{content_type}/{imdb_id}.json"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    try:
        deadline = net.deadline_in(timeout)
        with net.urlopen(req, timeout=timeout) as resp:
            body = json.loads(net.read_text(resp, deadline))
    except Exception:
        return None
    meta = (body or {}).get("meta")
    return meta if isinstance(meta, dict) else None


# **Cinemeta lags a new season; TMDB fills the gap, only where the two
# number the show alike.** His report, 5 October 2026: "why is Black
# Clover new ep is not there yet". Measured that day: AniList had Black
# Clover 2nd Season RELEASING since 3 October, TMDB had Season 2 (26
# episodes, E1 aired 2026-10-03, E2 dated 10-10) - and Cinemeta, fetched
# live that evening, still answered "Ended 2017-2021", Season 1 only,
# 170 episodes. Every list here is built from Cinemeta's `videos`, so
# the episode simply did not exist in the app.
#
# Then his follow-up: "make sure that all appear when they have new or
# upcoming seasons or ep". Censused over the 921 series lists on his
# disk the same evening, TMDB against Cinemeta *live*: 74 looked behind
# on disk, 32 of those were only stale files (Cinemeta had caught up),
# and the 42 left were four different things, each handled below:
#
#   - **A new season or episode TMDB has and Cinemeta does not** (Black
#     Clover S2, Koori no Jyouheki E15-28, The Gilded Age S4) - rows
#     appended past the end of Cinemeta's list.
#   - **An upcoming season Cinemeta files as one undated row** (High
#     Potential S3, Delicious in Dungeon S2, Dark Winds S5), which
#     unannounced_seasons rightly hides - TMDB had dated all three
#     (2027-01-06, 2027-10-01, 2027-02-07), so the date is copied onto
#     Cinemeta's own row and the season shows as UPCOMING.
#   - **An old season that disagrees on its count** (Tyler Perry's
#     Assisted Living: 2024's S5 is 22 on Cinemeta, 25 on TMDB) used to
#     refuse the whole fill and hid a new S7 of six aired episodes. Only
#     the *newest* seasons have to agree - they are what the new rows
#     continue - see _aligned.
#   - **A show TMDB numbers absolutely** (Doraemon: one 1,464-episode
#     TMDB season against Cinemeta's yearly seasons, ten weeks behind)
#     is lined up by air date instead of by number - see _by_date.
#
# What must never happen is a row on the wrong numbering - TMDB files
# Jujutsu Kaisen as one 59-episode season where Cinemeta has 24/23/12
# (integrations.md, "A franchise's season split is the entry's, not
# TMDB's"), and Bleach's TMDB season 2 is TYBW, which Cinemeta files as
# a separate title. Both are refused: neither has a TMDB season matching
# Cinemeta's newest one, and by date the episode after Bleach's last is
# in no season at all.
#
# And "behind" means recently. The census also filled Twin Peaks with
# 2017's The Return and The Kingdom with 2022's Exodus - revivals IMDb
# files as titles of their own, not a lag. Nothing older than
# TMDB_RECENT_DAYS is added or dated.
TMDB_DATE_TOLERANCE_DAYS = 2
TMDB_RECENT_DAYS = 365
TMDB_FILL_TTL_S = 6 * 3600.0
TMDB_FILL_MAX_NEW_SEASONS = 2
# A weekly show misses a week now and then; a gap longer than this is a
# cour break, after which an absolutely-numbered TMDB list may be what
# Cinemeta will file as a new season - so the date-led continuation
# stops there rather than guess.
TMDB_RUN_GAP_DAYS = 28
_TMDB_FILL_CACHE = {}           # imdb id -> (checked at, signature, answer)


def _day_of(value):
    """The calendar day of an ISO stamp or a bare YYYY-MM-DD, or None."""
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _video_key(video):
    try:
        return (int(video.get("season") or 0),
                int(video.get("number") or video.get("episode") or 0))
    except (TypeError, ValueError):
        return (0, 0)


def _tmdb_episodes(tmdb_id, season, deadline, timeout):
    """[(number, day, episode dict)] of one TMDB season, or None."""
    from . import artwork
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return None
    body = artwork.get_json(f"{artwork.API}/tv/{tmdb_id}/season/{int(season)}", step)
    if not isinstance(body, dict):
        return None
    out = []
    for episode in body.get("episodes") or []:
        try:
            number = int(episode.get("episode_number"))
        except (TypeError, ValueError):
            continue
        out.append((number, _day_of(episode.get("air_date")), episode))
    out.sort(key=lambda row: row[0])
    return out


def _tmdb_row(imdb_id, season, number, episode, when):
    stamp = f"{when.isoformat()}T00:00:00.000Z"
    still = str(episode.get("still_path") or "")
    overview = str(episode.get("overview") or "")
    # TMDB's placeholder titles carry *its* number: Doraemon's row placed
    # at S22E29 is "Episode 925" there, while the releases that answer
    # S22E29 are 924 (measured 5 October 2026 - the row is right, the
    # label is TMDB's own count). A generic title says this row's number.
    name = str(episode.get("name") or "").strip()
    if not name or re.fullmatch(r"(?i)episode\s*\d+", name):
        name = f"Episode {number}"
    return {
        "id": f"{imdb_id}:{season}:{number}",
        "name": name,
        "season": season, "number": number, "episode": number,
        "firstAired": stamp, "released": stamp,
        "overview": overview, "description": overview,
        "thumbnail": f"https://image.tmdb.org/t/p/w780{still}" if still else "",
        "source": "tmdb",
    }


def _seasons_of(videos):
    """{season: {count, max, first (E1's day), undated {numbers},
    last_dated (number, day) of its highest dated episode}}."""
    seasons = {}
    for video in videos:
        season, number = _video_key(video)
        if season < 1 or number < 1:
            continue
        row = seasons.setdefault(season, {"count": 0, "max": 0, "first": None,
                                          "undated": set(), "last_dated": None})
        row["count"] += 1
        row["max"] = max(row["max"], number)
        when = _day_of(video.get("firstAired") or video.get("released"))
        if number == 1:
            row["first"] = when
        if when is None:
            row["undated"].add(number)
        elif row["last_dated"] is None or number > row["last_dated"][0]:
            row["last_dated"] = (number, when)
    return seasons


def _aligned(seasons, theirs):
    """True when TMDB's newest seasons are Cinemeta's: the last season,
    and the last one whose E1 is dated if that is an earlier one. Same
    number, E1 within TMDB_DATE_TOLERANCE_DAYS, and at least as many
    episodes (exactly as many, for a season that is not the last)."""
    last = max(seasons)
    dated = [s for s in seasons if seasons[s]["first"]]
    anchors = {last} | ({max(dated)} if dated else set())
    for season in anchors:
        ours, row = seasons[season], theirs.get(season)
        if not row:
            return False
        count = int(row.get("episode_count") or 0)
        if count < ours["count"] or (season < last and count != ours["count"]):
            return False
        their_first = _day_of(row.get("air_date"))
        if ours["first"] and (their_first is None or abs(
                (their_first - ours["first"]).days) > TMDB_DATE_TOLERANCE_DAYS):
            return False
    return True


def _by_season(imdb_id, tmdb_id, seasons, theirs, deadline, timeout, recent):
    """Rows and dates when the numbering is aligned (see _aligned)."""
    last = max(seasons)
    wanted = []
    if (int(theirs[last].get("episode_count") or 0) > seasons[last]["max"]
            or seasons[last]["undated"]):
        wanted.append(last)
    later = sorted(n for n in theirs
                   if n > last and _day_of(theirs[n].get("air_date")))
    wanted += later[:TMDB_FILL_MAX_NEW_SEASONS]
    # Nothing new can predate what Cinemeta has already shown airing.
    floor = max((row["last_dated"][1] for row in seasons.values()
                 if row["last_dated"]), default=None)
    rows, dates = [], {}
    for season in wanted:
        episodes = _tmdb_episodes(tmdb_id, season, deadline, timeout)
        if episodes is None:
            return None
        ours = seasons.get(season)
        # **The same season can still be counted differently inside.**
        # Arafta, measured 5 October 2026: S2 aligned on number, E1 and
        # count, yet TMDB's E27 aired 31 August and Cinemeta's E26 on 16
        # October - a daily show split differently - so TMDB's E27 would
        # have been the wrong episode. Cinemeta's last dated episode must
        # be TMDB's episode of the same number, on the same day.
        if ours and ours["last_dated"]:
            number, when = ours["last_dated"]
            theirs_day = next((d for n, d, _e in episodes if n == number), None)
            if theirs_day is None or abs(
                    (theirs_day - when).days) > TMDB_DATE_TOLERANCE_DAYS:
                return [], {}
        for number, when, episode in episodes:
            if floor and when and when < floor - timedelta(
                    days=TMDB_DATE_TOLERANCE_DAYS):
                continue
            if when is None or when < recent:
                continue
            if ours and number in ours["undated"]:
                dates[(season, number)] = when
            elif not ours or number > ours["max"]:
                rows.append(_tmdb_row(imdb_id, season, number, episode, when))
    return rows, dates


def _by_date(imdb_id, tmdb_id, videos, seasons, theirs, deadline, timeout,
             recent, today):
    """Rows and dates for a show TMDB numbers differently, lined up on
    the air dates of Cinemeta's last two aired episodes - both must sit
    on two consecutive TMDB episodes, or nothing is said. What follows
    in that TMDB season continues Cinemeta's last aired season, one
    number at a time, until a gap of TMDB_RUN_GAP_DAYS."""
    aired = sorted(
        (when, _video_key(video)) for video in videos
        for when in [_day_of(video.get("firstAired") or video.get("released"))]
        if when and when <= today and min(_video_key(video)) > 0)
    if len(aired) < 2 or aired[-1][0] < recent:
        return [], {}
    (before_day, _k), (last_day, (season, number)) = aired[-2], aired[-1]
    # The TMDB season the last aired day falls in: the newest that had
    # started by then.
    started = [n for n, row in theirs.items()
               if n > 0 and _day_of(row.get("air_date"))
               and _day_of(row.get("air_date")) <= last_day]
    if not started:
        return [], {}
    episodes = _tmdb_episodes(tmdb_id, max(started), deadline, timeout)
    if episodes is None:
        return None
    near = lambda a, b: a and b and abs((a - b).days) <= 1
    hits = [i for i in range(1, len(episodes))
            if near(episodes[i][1], last_day) and near(episodes[i - 1][1], before_day)]
    if len(hits) != 1:
        return [], {}
    ours = seasons.get(season, {"max": number, "undated": set()})
    rows, dates = [], {}
    previous = last_day
    for offset, (_n, when, episode) in enumerate(episodes[hits[0] + 1:], start=1):
        # A list out of date order ends the run too: Doraemon's TMDB
        # season goes from October 2026 straight back to July 2018, and
        # the run followed it for 85 rows (measured 5 October 2026).
        if (when is None or when < previous
                or (when - previous).days > TMDB_RUN_GAP_DAYS):
            break
        previous = when
        target = number + offset
        if target in ours["undated"]:
            dates[(season, target)] = when
        elif target > ours["max"]:
            rows.append(_tmdb_row(imdb_id, season, target, episode, when))
    return rows, dates


def _tmdb_fill(imdb_id, videos, timeout):
    """(rows to append, {(season, number): day} for Cinemeta rows with no
    date) from TMDB, ([], {}) when it has nothing that may be added, and
    None when TMDB could not be asked - so a caller can keep what it
    had. Network; call off the UI thread. Never raises."""
    from . import artwork          # artwork imports nothing of ours back
    try:
        seasons = _seasons_of(videos)
        if not seasons:
            return [], {}
        deadline = net.deadline_in(timeout)
        step = net.step_timeout(deadline, timeout)
        if step is None:
            return None
        kind, tmdb_id = artwork.tmdb_id(imdb_id, step)
        if kind != "tv" or not tmdb_id:
            return [], {}
        step = net.step_timeout(deadline, timeout)
        show = artwork.get_json(f"{artwork.API}/tv/{tmdb_id}", step) if step else None
        if not isinstance(show, dict):
            return None
        theirs = {}
        for row in show.get("seasons") or []:
            try:
                theirs[int(row.get("season_number"))] = row
            except (TypeError, ValueError):
                continue
        today = datetime.now(timezone.utc).date()
        recent = today - timedelta(days=TMDB_RECENT_DAYS)
        if _aligned(seasons, theirs):
            return _by_season(imdb_id, tmdb_id, seasons, theirs, deadline,
                              timeout, recent)
        return _by_date(imdb_id, tmdb_id, videos, seasons, theirs, deadline,
                        timeout, recent, today)
    except Exception:
        return None


def _strip_tmdb(videos):
    """Cinemeta's own rows, with any TMDB date taken back off."""
    out = []
    for video in videos or []:
        if not isinstance(video, dict) or video.get("source") == "tmdb":
            continue
        if video.get("dated_by") == "tmdb":
            video = {k: v for k, v in video.items()
                     if k not in ("dated_by", "firstAired", "released")}
        out.append(video)
    return out


def _apply_tmdb(videos, rows, dates):
    out = []
    for video in videos:
        when = dates.get(_video_key(video))
        if when is not None:
            stamp = f"{when.isoformat()}T00:00:00.000Z"
            video = dict(video, firstAired=stamp, released=stamp, dated_by="tmdb")
        out.append(video)
    have = {_video_key(video) for video in out}
    return out + [row for row in rows if _video_key(row) not in have]


def fill_from_tmdb(imdb_id: str, meta, timeout: int = 8, fresh: bool = False):
    """`meta` with what TMDB knows past the end of Cinemeta's list: new
    episodes appended, and dates put on Cinemeta's undated upcoming rows
    - see TMDB_RECENT_DAYS for when either is allowed. Returns `meta`
    itself, changed in place. TMDB's answer is kept TMDB_FILL_TTL_S per
    title and per shape of Cinemeta's list; when TMDB cannot be asked, a
    fill the record already carries is kept as it was. A row Cinemeta
    has always wins. `fresh` skips that cache and asks TMDB now - for
    the episode list the user has just opened (details._meta_worker).
    Network; call off the UI thread. Never raises."""
    try:
        if not isinstance(meta, dict) or not imdb_id:
            return meta
        if str(meta.get("type") or "series") != "series":
            return meta
        videos = _strip_tmdb(meta.get("videos"))
        signature = (len(videos), max((_video_key(v) for v in videos), default=None))
        key = str(imdb_id)
        cached = _TMDB_FILL_CACHE.get(key)
        if (cached and not fresh and cached[1] == signature
                and time.time() - cached[0] < TMDB_FILL_TTL_S):
            answer = cached[2]
        else:
            answer = _tmdb_fill(imdb_id, videos, timeout)
            if answer is not None:
                _TMDB_FILL_CACHE[key] = (time.time(), signature, answer)
        if answer is None:
            return meta            # TMDB unreachable: keep any fill on it
        rows, dates = answer
        if rows or dates:
            try:
                from . import logs
                first = min([_video_key(r) for r in rows] + list(dates))
                logs.info(f"cinemeta behind for {imdb_id}: TMDB added "
                          f"{len(rows)} episode(s) and dated {len(dates)}, "
                          f"from S{first[0]:02d}E{first[1]:02d}")
            except Exception:
                pass
        meta["videos"] = _apply_tmdb(videos, rows, dates)
    except Exception:
        pass
    return meta


def keep_tmdb_fill(name, meta):
    """Put the TMDB fill the file on disk carries onto a fresh Cinemeta
    `meta` that was fetched without one, so a writer that does not ask
    TMDB (the search classifier, the genre fill) never takes a filled
    list back to Cinemeta's. Only rows Cinemeta still lacks, and dates
    for rows it still has undated. Never raises."""
    try:
        stored = storage.load(name, None)
        old = ((stored or {}).get("meta") or {}).get("videos") or []
        rows = [v for v in old if isinstance(v, dict) and v.get("source") == "tmdb"]
        dates = {}
        for video in old:
            if isinstance(video, dict) and video.get("dated_by") == "tmdb":
                when = _day_of(video.get("firstAired"))
                if when:
                    dates[_video_key(video)] = when
        if not rows and not dates:
            return meta
        fresh = _strip_tmdb(meta.get("videos"))
        undated = {_video_key(v) for v in fresh
                   if not _day_of(v.get("firstAired") or v.get("released"))}
        dates = {k: v for k, v in dates.items() if k in undated}
        meta["videos"] = _apply_tmdb(fresh, rows, dates)
    except Exception:
        pass
    return meta


# Cinemeta meta, kept on disk per title - the same files and shape the
# details page writes (windows.details._meta_worker), so one fetch serves
# the episode list, the search classifier and the player's audio hint.
META_CACHE_TTL_S = 24 * 3600.0


def _meta_cache_name(imdb_id, content_type) -> str:
    safe = re.sub(r"[^a-z0-9]", "", str(imdb_id or "").lower())
    return f"meta-{content_type}-{safe}.json"


def fetch_meta_cached(imdb_id: str, content_type: str = "series",
                      timeout: int = 8, tmdb: bool = False):
    """fetch_meta, answered from disk when the title was seen inside
    META_CACHE_TTL_S. Never raises; None when neither has it.

    `tmdb` fills a lagging episode list from TMDB before it is written
    (fill_from_tmdb) - for the readers that walk episodes (the player),
    and fills a fresh disk copy too, since another reader may have
    written it. Off by default: the search classifier reads genres off a
    dozen metas inside a 5s wait and has no use for three more requests
    a row - and what it writes keeps the fill already on disk
    (keep_tmdb_fill)."""
    name = _meta_cache_name(imdb_id, content_type)
    try:
        stored = storage.load(name, None)
        if (isinstance(stored, dict) and isinstance(stored.get("meta"), dict)
                and time.time() - float(stored.get("ts") or 0) < META_CACHE_TTL_S):
            meta = stored["meta"]
            if tmdb and content_type == "series":
                before = list(meta.get("videos") or [])
                meta = fill_from_tmdb(imdb_id, meta, timeout)
                if meta.get("videos") != before:
                    storage.save(name, {"ts": stored.get("ts"), "meta": meta})
            return meta
    except Exception:
        pass
    meta = fetch_meta(imdb_id, content_type, timeout)
    if meta and content_type == "series":
        meta = keep_tmdb_fill(name, meta)
        if tmdb:
            meta = fill_from_tmdb(imdb_id, meta, timeout)
    if meta:
        try:
            storage.save(name, {"ts": time.time(), "meta": meta})
        except Exception:
            pass
    return meta


# **A library's episode lists keep themselves current.** The owner, 5
# October 2026, after the TMDB fill landed: "I do not want you update it
# manually, make the app updates auto from APIs". The fill already came
# from the APIs at run time, but only when a title's page or the player
# fetched its list - a saved show's Home card read the file on disk,
# which nothing refreshed until he opened the title. refresh_meta is what
# Home's draw asks for in the background (server._refresh_episode_lists),
# once a list is older than this.
EPISODE_LIST_REFRESH_S = 6 * 3600.0


def meta_age_s(imdb_id: str, content_type: str = "series"):
    """Seconds since the list on disk was fetched, or None with no file."""
    try:
        stored = storage.load(_meta_cache_name(imdb_id, content_type), None)
        return time.time() - float((stored or {}).get("ts") or 0)
    except Exception:
        return None


def refresh_meta(imdb_id: str, content_type: str = "series", timeout: int = 8) -> bool:
    """Fetch a title's list from Cinemeta and fill it from TMDB, live,
    and write it if it differs from the file on disk. True when the
    episode list changed. A failed fetch leaves the file alone. Network;
    call off the UI thread. Never raises."""
    name = _meta_cache_name(imdb_id, content_type)
    try:
        stored = storage.load(name, None)
        before = (((stored or {}).get("meta") or {}).get("videos")
                  if isinstance(stored, dict) else None)
        meta = fetch_meta(imdb_id, content_type, timeout)
        if not meta:
            return False
        if content_type == "series":
            meta = keep_tmdb_fill(name, meta)
            meta = fill_from_tmdb(imdb_id, meta, timeout)
        storage.save(name, {"ts": time.time(), "meta": meta})
        return meta.get("videos") != before
    except Exception:
        return False


def cached_meta(imdb_id: str, content_type: str = "series"):
    """The Cinemeta meta on disk for a title, at any age, or None.
    Never fetches - for a reader that wants the title's own season split
    (anime_identity) and must not spend a network round trip on it."""
    name = _meta_cache_name(imdb_id, content_type)
    try:
        stored = storage.load(name, None)
        if isinstance(stored, dict) and isinstance(stored.get("meta"), dict):
            return stored["meta"]
    except Exception:
        pass
    return None


def looks_anime(meta) -> bool:
    """Whether a Cinemeta record describes anime: the Animation genre
    *and* Japan among its countries.

    Both, because either alone is wrong in a way the owner would see.
    Measured 24 August 2026: Demon Slayer and its Infinity Castle film
    carry `genres: [Animation, ...]` with `country: Japan` (the film
    says "Japan, United States"); House of the Dragon carries neither;
    and a Cinemeta search for "Infinity Castle" also returns Castle in
    the Sky - Ghibli, Japan, correctly anime - beside American animated
    films that are not. The search's own result rows carry no genres at
    all (measured: no `genres`, no genre `links`), which is why the
    classifier needs the full meta and the cache above."""
    if not isinstance(meta, dict):
        return False
    genres = {str(g).strip().lower() for g in (meta.get("genres") or meta.get("genre") or [])}
    if "animation" not in genres and "anime" not in genres:
        return False
    country = str(meta.get("country") or "").lower()
    return "japan" in country


def is_anime_entry(entry) -> bool:
    """Whether a tracked entry is anime - its own type when it says so,
    else Cinemeta's verdict from the genres and country the details page
    carries on it.

    Here rather than at the two call sites because both of them are
    asking the same question about the same entry: the download dialog
    and the player's download panel decide whether to offer an audio
    choice at all (the owner, 28 August 2026: "only show the Audio
    selection option while download page while in Anime, and completely
    remove this button selection from series and movies").

    Never raises and never asks the network."""
    try:
        data = entry if isinstance(entry, dict) else {}
        if str(data.get("type") or "").strip().lower() == "anime":
            return True
        return bool(looks_anime(data))
    except Exception:
        return False


def fetch_latest_episode(imdb_id: str, content_type: str = "series", timeout: int = 6):
    """Season/episode of the most recently aired episode, from Cinemeta's
    full episode list for this title (the catalog search used elsewhere
    doesn't include this - it's title/poster/year only). Used to prefill
    a new entry's progress with "here's the newest episode out" rather
    than leaving it blank. Specials (season 0) are skipped in favor of
    the latest numbered-season episode when both exist. Returns
    (season, episode) ints, or None if it can't be determined."""
    body = fetch_meta(imdb_id, content_type, timeout)
    if body is None:
        return None
    if content_type == "series":
        body = fill_from_tmdb(imdb_id, body, timeout)

    videos = body.get("videos") or []
    now = datetime.now(timezone.utc)
    aired = []
    for v in videos:
        when = _parse_aired(v.get("firstAired"))
        if when and when <= now:
            aired.append((when, v))
    if not aired:
        return None

    numbered = [item for item in aired if (item[1].get("season") or 0) > 0]
    aired = numbered or aired
    aired.sort(key=lambda item: item[0])
    latest = aired[-1][1]
    return latest.get("season") or 0, latest.get("number") or latest.get("episode") or 0


def _api_post(path: str, payload: dict, timeout: int):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{API_URL}/{path}", data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    deadline = net.deadline_in(timeout)
    try:
        with net.urlopen(req, timeout=timeout) as resp:
            return json.loads(net.read_text(resp, deadline))
    except urllib.error.HTTPError as exc:
        if exc.code in _AUTH_HTTP_CODES:
            raise AuthFailed(f"Stremio answered {exc.code}") from exc
        raise


def login(email: str, password: str, timeout: int = 10) -> str:
    """Sign into a Stremio account via Stremio's own account API
    (api.strem.io - the same one their apps use) and return its authKey
    (a session token). Raises RuntimeError with a human-readable message
    on failure. The password is only ever used for this one request -
    it's never stored; only the returned authKey is (in Settings)."""
    body = _api_post("login", {"email": email, "password": password}, timeout)
    error = body.get("error")
    if error:
        raise RuntimeError(error.get("message") or "Login failed")
    auth_key = ((body.get("result") or {}).get("authKey"))
    if not auth_key:
        raise RuntimeError("Stremio didn't return a session key")
    return auth_key


def fetch_watch_progress(imdb_id: str, auth_key: str, timeout: int = 6):
    """Your actual watch progress for this title, from the signed-in
    account's synced Stremio library - unlike fetch_latest_episode
    (which only knows how far the show currently goes, not what you've
    watched), this is the real "what episode am I on". Returns (season,
    episode) ints, or None if you're not signed in, the title isn't in
    your library yet, or the lookup fails.

    Raises AuthFailed - and only AuthFailed - when the saved key itself is
    the problem, so the caller can say so instead of showing the same
    "nothing to sync" as a title that genuinely isn't in the library."""
    if not auth_key:
        return None
    try:
        body = _api_post("datastoreGet", {
            "authKey": auth_key, "collection": "libraryItem",
            "ids": [imdb_id], "all": False,
        }, timeout)
    except AuthFailed:
        raise
    except Exception:
        return None
    # Checked before the empty-result test below, not after: a rejected
    # key comes back 200 with an error body and no "result" key at all,
    # which that test would read as "your library doesn't have this one".
    _raise_if_auth_error(body)
    items = body.get("result") or []
    if not items:
        return None
    state = items[0].get("state") or {}
    # Stremio's library state keys the last-touched video as
    # "<imdb_id>:<season>:<episode>" - but that's only set once you've
    # actually resumed/pressed play on a specific episode. A title whose
    # episodes were instead marked watched via checkmarks (never resumed
    # in the player) leaves video_id as the bare id with no season/
    # episode, even though Stremio clearly knows your progress - that
    # progress lives in "watched" instead, which encodes as
    # "<imdb_id>:<season>:<episode>:<bitmask length>:<bitmask>" (the
    # highest episode you've marked watched, followed by a compressed
    # per-episode watched bitmask we don't need here).
    for field in ("video_id", "watched"):
        parts = (state.get(field) or "").split(":")
        if len(parts) >= 3:
            try:
                return int(parts[1]), int(parts[2])
            except ValueError:
                continue
    return None


def launch(url: str):
    """Open a stremio:// deep link via the OS's registered protocol
    handler (Stremio's installer registers this automatically)."""
    webbrowser.open(url)
