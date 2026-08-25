"""Minimal client for the public TVMaze API (https://www.tvmaze.com/api) -
"when does the next episode air", and the catalogue-wide airing calendar
behind the Schedule tab's "Airing Soon". No API key needed.

Series entries already carry the IMDb id their Stremio/Cinemeta match was
saved with (see tracker._entry_imdb_id), and TVMaze can look a show up by
exactly that, so no fuzzy title matching is needed for the common case -
the title search is only a fallback for entries saved without one.

Every lookup fails soft (returns None) so a flaky connection or an
unlisted show never crashes the tracker UI - it just means no airing
schedule shows up on the card.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import net, title_match

BASE_URL = "https://api.tvmaze.com"

# TVMaze asks for roughly 20 calls per 10s; the tracker fires one lookup
# per entry in its own thread, so space them out rather than risking a
# 429 that would cost the whole page its schedules.
_MIN_REQUEST_GAP = 0.35
_throttle_lock = threading.Lock()
_last_request_at = 0.0

_MATCH_THRESHOLD = 0.8


def _get(url: str, timeout: int):
    global _last_request_at
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    with _throttle_lock:
        wait = _last_request_at + _MIN_REQUEST_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    # After the throttle sleep, not before it - see anilist._post.
    deadline = net.deadline_in(timeout)
    with net.urlopen(req, timeout=timeout) as resp:
        return json.loads(net.read_text(resp, deadline))


def _find_show(imdb_id: str, title: str, timeout: int):
    """The show record, by IMDb id where the entry has one (exact, and
    what Series entries are saved with) - falling back to a title search
    only when it doesn't, with a similarity check so a near-miss search
    hit can't pass itself off as the right show."""
    if imdb_id:
        try:
            show = _get(f"{BASE_URL}/lookup/shows?imdb={urllib.parse.quote(imdb_id)}", timeout)
        except Exception:
            show = None  # 404 = TVMaze doesn't carry this show; fall through to the title search
        if show:
            return show
    title = (title or "").strip()
    if not title:
        return None
    try:
        show = _get(f"{BASE_URL}/singlesearch/shows?q={urllib.parse.quote(title)}", timeout)
    except Exception:
        return None
    if not show:
        return None
    names = [show.get("name"), *(show.get("_embedded", {}).get("akas") or [])]
    if title_match.best_similarity(title, names) < _MATCH_THRESHOLD:
        return None
    return show


def fetch_next_episode(imdb_id: str = None, title: str = None, timeout: int = 8):
    """When the next episode of this show airs, from TVMaze's published
    schedule (not an estimate). Returns {"at": aware UTC datetime,
    "season": int, "episode": int} or None if the show isn't on TVMaze,
    has ended, or has no next episode scheduled yet.

    The show record links its own next episode when there is one, so an
    ended/between-seasons show costs a single request - the episode
    itself is only fetched once there's actually something to fetch."""
    show = _find_show(imdb_id, title, timeout)
    if not show:
        return None
    href = ((show.get("_links") or {}).get("nextepisode") or {}).get("href")
    if not href:
        return None
    try:
        episode = _get(href, timeout)
    except Exception:
        return None
    # airstamp is the full UTC-offset timestamp (airdate/airtime are the
    # show's own local ones, with no offset to resolve them against).
    stamp = (episode or {}).get("airstamp")
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {"at": when, "season": episode.get("season"), "episode": episode.get("number")}


# ---- The catalogue-wide calendar -----------------------------------------
# The owner's ask, 25 August 2026: *"in the airing soon make the series
# also show, not just the anime!"*. "Airing Soon" was AniList's calendar
# and nothing else, so it could only ever list anime - the comment on
# tracker.SCHEDULE_CATALOGUE said as much ("anime is the only medium
# with a published forward schedule"), and that was true of the sources
# then wired up rather than of television.
#
# TVmaze publishes one, and it is the same service this module already
# asks for a saved show's next episode - so this adds a second endpoint,
# not a second dependency.
#
# **Two endpoints, because they hold different things.** `/schedule/web`
# is the streaming calendar (Netflix, Prime, Apple, Hulu, Disney+) and
# `/schedule?country=US` is broadcast. A library of current streaming
# series sits almost entirely in the first; the second is what carries
# HBO and the networks. Neither takes a range, so a week is seven dates
# each - fourteen requests, which is why this is a background job with a
# 12h cache in front of it (tracker._fetch_upcoming_calendar) and never
# something a page waits on.
SCHEDULE_DAYS = 7
# TVmaze's own popularity score, 0-100. Unfiltered, a single US day is
# a hundred-odd episodes of daytime television, and "everything else
# airing this week" becomes a wall nobody reads. 90 keeps the shows a
# person plausibly follows; it is a threshold on *TVmaze's* metric, not
# a judgement encoded here, so it moves if their scale does.
SCHEDULE_MIN_WEIGHT = 90
# **Weight alone is not enough, and the first run proved it.** Measured
# live 25 August 2026 with only the weight threshold: 40 rows, and the
# top of them were NBC Nightly News, ABC World News Tonight, Jeopardy!,
# America's Got Talent and WWE NXT. TVmaze's weight is a popularity
# score, and a programme that airs *every weekday* scores highly on it
# precisely because it is always on - which is the opposite of what
# "airing soon" is asking. Nobody puts the evening news on a watchlist.
#
# So the show's own type is the filter that matters. Scripted and
# Animation are the two that carry series people follow; News, Talk
# Show, Game Show, Reality, Sports, Variety and Documentary are what
# flooded it.
SCHEDULE_TYPES = ("scripted", "animation")
# And a cap on how many days a week a show is on, for the same reason.
# Measured on the run after the type filter: EastEnders, Emmerdale,
# Hollyoaks and Days of Our Lives took four of the first thirteen rows.
# They are genuinely scripted drama - they are also on most weeknights,
# forever, which is what puts them at the top of a list sorted by
# "soonest" and keeps them there every day of the week.
#
# **Genre does not separate them, and that was the first attempt.**
# TVmaze tags none of those four as "Soap"; EastEnders and Hollyoaks
# both come back `type=Scripted, genres=['Drama']`, identical in shape
# to a weekly series. What does separate them is `schedule.days`, which
# TVmaze fills in from the show's own slot: EastEnders four days,
# Hollyoaks three, against one for Reacher or Ted Lasso. So the rule is
# "not a strip programme", stated as the number it actually is.
SCHEDULE_MAX_DAYS_PER_WEEK = 2


def _schedule_rows(payload, out, seen, now):
    """Fold one day's schedule response into `out`.

    The show hangs off a different key per endpoint - `_embedded.show`
    on the web schedule, `show` on the country one - which is the sort
    of difference that silently returns nothing rather than raising."""
    for episode in payload or []:
        if not isinstance(episode, dict):
            continue
        show = ((episode.get("_embedded") or {}).get("show")
                or episode.get("show") or {})
        show_id = show.get("id")
        if not show_id or show_id in seen:
            continue        # one row per show: the soonest episode of it
        if str(show.get("type") or "").strip().lower() not in SCHEDULE_TYPES:
            continue
        slots = (show.get("schedule") or {}).get("days") or []
        if len(slots) > SCHEDULE_MAX_DAYS_PER_WEEK:
            continue
        try:
            weight = int(show.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        if weight < SCHEDULE_MIN_WEIGHT:
            continue
        stamp = episode.get("airstamp")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            continue
        title = (show.get("name") or "").strip()
        if not title:
            continue
        seen.add(show_id)
        out.append({
            "title": title,
            "episode": int(episode.get("number") or 0),
            "season": int(episode.get("season") or 0),
            "at": when,
            "cover_url": ((show.get("image") or {}).get("medium") or ""),
            # What the Schedule tab files the row under. AniList's rows
            # carry "Anime"; without this every row on that page would
            # be typed as anime, which is the bug being fixed.
            "type": "Series",
        })


def fetch_upcoming_schedule(days: int = SCHEDULE_DAYS, limit: int = 40,
                            timeout: int = 8) -> list:
    """What airs next across TVmaze, soonest first.

    Rows are shaped exactly like `anilist.fetch_upcoming_airing`'s -
    {title, episode, at (aware UTC datetime), cover_url, type} - so the
    Schedule tab merges the two lists rather than growing a second code
    path for them.

    Fails soft to whatever it managed to collect, per day and per
    endpoint: a week with one bad request is six good days, not none.
    Bounded by one wall-clock deadline across the whole chain rather
    than per request, because fourteen requests at `timeout` each is not
    a `timeout` bound (.claude/rules/integrations.md)."""
    now = datetime.now(timezone.utc)
    today = now.date()
    out, seen = [], set()
    # 2x per-request, the same budget shape anime_sites.search_site uses,
    # widened for the request count: below this a slow first day would
    # take the whole week down with it.
    ends_at = time.monotonic() + max(timeout * 3, 20)
    for offset in range(max(1, int(days))):
        day = today + timedelta(days=offset)
        for url in (f"{BASE_URL}/schedule/web?date={day}",
                    f"{BASE_URL}/schedule?country=US&date={day}"):
            remaining = ends_at - time.monotonic()
            if remaining < 1.0:
                # Not worth opening a connection that cannot finish -
                # give back the days already collected instead.
                break
            try:
                _schedule_rows(_get(url, int(min(timeout, remaining))),
                               out, seen, now)
            except Exception:
                continue    # one bad day is not a bad week
        else:
            continue
        break
    out.sort(key=lambda row: row["at"])
    return out[:limit]
