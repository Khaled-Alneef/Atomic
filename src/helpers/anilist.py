"""Minimal client for the public AniList GraphQL API (https://anilist.co) -
a title search, a public list-progress lookup, the airing schedule for
the next episode, and a title's Crunchyroll page, no login/API key
needed.

Unlike Crunchyroll (whose equivalent data lives behind an OAuth-gated,
Cloudflare-bot-protected API this app won't try to bypass - see the
Crunchyroll paragraph in anime_sites.py's module docstring), AniList's
is fully open for public profiles: a username is enough to read
someone's list, no password or token flow. Since a lot of people track
their watching on AniList regardless of which app they actually watch
in, this gives real progress for Crunchyroll-provider Anime entries
too, not just Stremio's - and `fetch_crunchyroll_urls` is what lets
those entries resolve to a real Crunchyroll page at all, since
Crunchyroll itself cannot be asked.

Every lookup fails soft (returns None) so a flaky connection, a private
list, or no title match never crashes the tracker UI - it just means no
progress shows up.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import net, title_match

API_URL = "https://graphql.anilist.co"

# AniList rate-limits per minute and answers a burst with 429s. The
# tracker fires one lookup per entry in its own thread on page load, so
# space them out here rather than letting a big library get itself
# throttled out of its own schedule data.
_MIN_REQUEST_GAP = 0.7
_throttle_lock = threading.Lock()
_last_request_at = 0.0

# How close a catalog title has to be to the tracker's own before its
# schedule is trusted (see title_match.similarity).
_MATCH_THRESHOLD = 0.8

_SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
  }
}
"""

# Deliberately a paged search, not the single-Media one above: a title
# like "Bleach: Thousand-Year Blood War" resolves to the *first* season's
# finished entry, while the currently-airing cour is a separate entry
# ("... - The Calamity") further down the results. Only the paged form
# can see it, so the airing one can be preferred over the finished one.
_AIRING_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      title { romaji english native }
      synonyms
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""

_PROGRESS_QUERY = """
query ($userName: String, $mediaId: Int) {
  MediaList(userName: $userName, mediaId: $mediaId, type: ANIME) {
    progress
  }
}
"""

# Every entry a franchise has, because AniList files each season as its
# own work while Crunchyroll (and this app's cards) put them all under
# one name. Searching "One-Punch Man" and reading the first hit gives
# season 1 forever - which is exactly how a card sat at E12 while its
# owner was part-way through season 2.
#
# startDate is what orders them: AniList has no "season number" field,
# and release order is what "season 2" actually means.
_SEASONS_QUERY = """
query ($search: String) {
  Page(perPage: 25) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      format
      episodes
      startDate { year month day }
      title { romaji english native }
      synonyms
    }
  }
}
"""

# One request for the user's progress on every season found, rather than
# one per season - a franchise with four entries would otherwise be four
# round trips on a lookup that already runs per tracked entry.
_SEASON_PROGRESS_QUERY = """
query ($userName: String, $ids: [Int]) {
  Page(perPage: 50) {
    mediaList(userName: $userName, mediaId_in: $ids, type: ANIME) {
      mediaId
      progress
      status
    }
  }
}
"""

# Only formats that are *seasons*. A franchise's OVAs, specials and
# films would otherwise take slots in the ordering and shift every real
# season's number - "S03E02" for something that is actually season 2.
_SEASON_FORMATS = ("TV", "TV_SHORT", "ONA")

# Paged for the same reason _AIRING_QUERY is: the single-Media search
# picks one entry, and for a franchise it is routinely the wrong one.
# Searching "Frieren: Beyond Journey's End" ranks the spin-off "Sousou no
# Frieren: ●● no Mahou" above the series itself, and only the series
# carries a Crunchyroll link - so the whole page has to be scored, not
# just its first row.
_EXTERNAL_LINKS_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      title { romaji english native }
      synonyms
      externalLinks { site url }
    }
  }
}
"""


class RateLimited(Exception):
    """AniList is refusing this connection outright, which is a different
    fact from "this title isn't on the list".

    Sustained querying gets a **403 on every POST** - not the 429 the API
    documents - and it lasts: measured at over an hour. Every lookup here
    fails soft, so until this had its own type a block was indis-
    tinguishable from a genuine no-match, and the user was left looking
    at an app that appeared to have quietly stopped working."""


# 429 is what the documentation promises and 403 is what actually
# arrives; both mean the same thing to a caller.
_RATE_LIMIT_CODES = (403, 429)


def _post(query: str, variables: dict, timeout: int):
    global _last_request_at
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    with _throttle_lock:
        wait = _last_request_at + _MIN_REQUEST_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    # After the throttle sleep, not before it: the budget is for the
    # request, and the gap above can be most of a second on its own.
    deadline = net.deadline_in(timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(net.read_text(resp, deadline))
    except urllib.error.HTTPError as exc:
        if exc.code in _RATE_LIMIT_CODES:
            raise RateLimited(f"AniList answered {exc.code}") from exc
        raise


def _main_names(media: dict) -> list:
    """The entry's own titles - deliberately *not* its synonyms.

    Synonyms are where a spin-off picks up the parent's name: AniList
    lists the 1-episode short "Go! Saitama" with a One-Punch Man synonym,
    and counting it as a season pushed the real season 3 to number 4
    (measured against the live API). An actual sequel carries the
    franchise name in its own title."""
    titles = media.get("title") or {}
    return [titles.get("romaji"), titles.get("english"), titles.get("native")]


def _same_franchise(title: str, media: dict) -> bool:
    """Whether this AniList entry is a season of `title`.

    Containment rather than similarity, because a sequel is named after
    its predecessor plus a suffix ("One Punch Man" -> "One Punch Man 2nd
    Season") - which scores far below the similarity threshold while
    being unmistakably the same show."""
    wanted = title_match.normalize(title)
    if not wanted:
        return False
    for name in _main_names(media):
        if not name:
            continue
        candidate = title_match.normalize(name)
        if not candidate:
            continue
        if wanted in candidate or candidate in wanted:
            return True
        if title_match.similarity(title, name) >= _MATCH_THRESHOLD:
            return True
    return False


# "2nd Season", "Season 3", "3rd Season", "Part 2" is deliberately absent
# - a cour split ("Season 3 Part 2") is still season 3, and matching
# "Part" would turn it into its own season.
_SEASON_NUMBER_RE = re.compile(
    r'\b(?:season\s*(\d{1,2})|(\d{1,2})\s*(?:st|nd|rd|th)\s*season)\b',
    re.IGNORECASE)


def _stated_season(media: dict):
    """The season number written in the entry's own title, or None.

    Preferred over release order because release order counts things
    that are not seasons and splits things that are: AniList files
    "One Punch Man Season 3 Part 2" as its own work, and it is still
    season 3."""
    for name in _main_names(media):
        found = _SEASON_NUMBER_RE.search(name or "")
        if found:
            return int(found.group(1) or found.group(2))
    return None


def _release_key(media: dict):
    start = media.get("startDate") or {}
    # id last, so entries with no date at all still order stably rather
    # than swapping places between lookups.
    return (start.get("year") or 9999, start.get("month") or 99,
            start.get("day") or 99, media.get("id") or 0)


def fetch_season_progress(title: str, username: str, timeout: int = 6):
    """How far through a franchise `username` actually is, as (season,
    episode), or None.

    AniList keeps each season as a separate work and counts episodes
    from 1 within it; Crunchyroll and this app's cards use one title
    with a season number. Asking AniList for "One-Punch Man" therefore
    answers about season 1 forever, which is the whole reason this
    exists: a card stuck at E12 while its owner was on season 2.

    So: find the franchise's seasons, order them by release date, ask
    for progress on all of them in one request, and report the furthest
    one actually started. Season 1 finished plus season 2 at episode 5
    is "S02E05" - the answer a person would give."""
    title = (title or "").strip()
    username = (username or "").strip()
    if not title or not username:
        return None

    body = _post(_SEASONS_QUERY, {"search": title}, timeout)
    media = ((body.get("data") or {}).get("Page") or {}).get("media") or []
    seasons = [m for m in media
               if m.get("format") in _SEASON_FORMATS and _same_franchise(title, m)]
    if not seasons:
        return None
    seasons.sort(key=_release_key)

    ids = [m["id"] for m in seasons if m.get("id")]
    listed = _post(_SEASON_PROGRESS_QUERY, {"userName": username, "ids": ids}, timeout)
    rows = ((listed.get("data") or {}).get("Page") or {}).get("mediaList") or []
    progress_by_id = {row.get("mediaId"): (row.get("progress") or 0)
                      for row in rows if row.get("mediaId")}

    # Numbers written in the titles win over release order wherever they
    # exist - see _stated_season. Release order is the fallback for
    # franchises that name their sequels rather than numbering them.
    stated = {id(entry): _stated_season(entry) for entry in seasons}
    numbered = any(value for value in stated.values())

    # The furthest season actually started, not the newest one on the
    # list: adding season 3 to your list without watching it must not
    # move progress forwards, and rewatching season 1 must not move it
    # back.
    furthest = None
    for index, entry in enumerate(seasons, start=1):
        progress = progress_by_id.get(entry.get("id")) or 0
        if progress <= 0:
            continue
        # An entry with no number among numbered siblings is the first
        # season - that is exactly how AniList names them ("One-Punch
        # Man", then "One-Punch Man Season 2").
        season = (stated[id(entry)] or 1) if numbered else index
        if furthest is None or season >= furthest[0]:
            furthest = (season, progress)
    if furthest is None:
        return None
    # One season and nothing else means there is no season number to
    # claim - keep the flat "E12" shape the rest of the app already
    # stores for those.
    season, episode = furthest
    return (season if len(seasons) > 1 else 0), episode


def fetch_watch_progress(title: str, username: str, timeout: int = 6):
    """Your real AniList progress for the anime matching `title`, from
    `username`'s list - AniList's profile privacy has to allow public
    list viewing for this to work; there's no login here, just their
    username. Returns (season, episode), or None if there's no title
    match, the list/entry is private, or the lookup fails.

    Tries the season-aware path first (see fetch_season_progress) and
    only falls back to the single-entry lookup when that finds nothing,
    so a franchise answers about the season you are on rather than
    about its first one."""
    title = (title or "").strip()
    username = (username or "").strip()
    if not title or not username:
        return None
    try:
        seasonal = fetch_season_progress(title, username, timeout)
        if seasonal:
            return seasonal
        search_body = _post(_SEARCH_QUERY, {"search": title}, timeout)
        media_id = ((search_body.get("data") or {}).get("Media") or {}).get("id")
        if not media_id:
            return None
        body = _post(_PROGRESS_QUERY, {"userName": username, "mediaId": media_id}, timeout)
        entry = (body.get("data") or {}).get("MediaList")
        progress = entry.get("progress") if entry else None
        if progress is None:
            return None
        return 0, int(progress)
    except RateLimited:
        # The one lookup here that does not fail soft. This is the answer
        # the user reads as "the app is broken", so the caller has to be
        # able to say which of the two happened; the schedule lookups
        # below stay soft, because a missing countdown on a card has
        # nowhere to say it and no one waiting on it.
        raise
    except Exception:
        return None


def _candidate_names(media: dict):
    titles = media.get("title") or {}
    return [titles.get("romaji"), titles.get("english"), titles.get("native"),
            *(media.get("synonyms") or [])]


def fetch_next_episode(title: str, timeout: int = 8):
    """When the next episode of `title` airs, straight from AniList's own
    airing schedule (no estimating involved - this is the broadcast time
    the industry publishes). Returns {"at": aware UTC datetime,
    "episode": int} or None if the title doesn't match anything on
    AniList, has finished airing, or has no scheduled next episode yet.

    Among the search hits, the one that's actually airing wins: a
    long-running title has one AniList entry per season/cour, and the
    search's own best match is usually the *first* one (finished years
    ago) rather than the cour currently going out - but only entries
    whose title genuinely matches are considered at all, so a loose
    search can't hand back some unrelated show's schedule."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        body = _post(_AIRING_QUERY, {"search": title}, timeout)
    except Exception:
        return None

    media_list = (((body.get("data") or {}).get("Page") or {}).get("media")) or []
    soonest = None
    for media in media_list:
        airing = media.get("nextAiringEpisode")
        if not airing or not airing.get("airingAt"):
            continue
        if title_match.best_similarity(title, _candidate_names(media)) < _MATCH_THRESHOLD:
            continue
        if soonest is None or airing["airingAt"] < soonest["airingAt"]:
            soonest = airing
    if soonest is None:
        return None
    return {
        "at": datetime.fromtimestamp(soonest["airingAt"], timezone.utc),
        "episode": soonest.get("episode"),
    }


def fetch_external_urls(title: str, site_keyword: str, timeout: int = 8) -> list:
    """Every link AniList holds for `title` on the streaming site named
    by `site_keyword` ("crunchyroll", "netflix", ...), matched against
    AniList's own `site` label, in AniList's order, or [] if nothing
    matches. Returns the URLs *as stored* - what shape each site's URL
    should end up in is that site's business, not this module's, so the
    caller (anime_sites) normalizes them.

    This exists for the sites that cannot be asked directly: their
    search pages render client-side, and their content APIs want a
    session the app has no business holding (the full reasoning is in
    anime_sites.py's docstring). AniList publishes these links as
    ordinary public data on the key-less endpoint everything else here
    already uses, which makes it the only unauthenticated route to a
    real title page on any of them.

    Same matching rule as fetch_next_episode, for the same reason: only
    entries whose title genuinely matches are considered at all, so a
    loose search can't hand back some other show's page - the worst
    possible outcome, since the entry then silently points at the wrong
    series forever. Links come back only for the single best-matching
    entry rather than pooled across hits, because a franchise's
    spin-offs each carry their own and pooling them would mix a
    sequel's page in with the base series'."""
    title = (title or "").strip()
    site_keyword = (site_keyword or "").strip().lower()
    if not title or not site_keyword:
        return []
    try:
        body = _post(_EXTERNAL_LINKS_QUERY, {"search": title}, timeout)
    except Exception:
        return []

    media_list = (((body.get("data") or {}).get("Page") or {}).get("media")) or []
    scored = []
    for media in media_list:
        urls = [link["url"] for link in (media.get("externalLinks") or [])
                 if link.get("url") and site_keyword in (link.get("site") or "").lower()]
        if not urls:
            continue
        names = _candidate_names(media)
        score = title_match.best_similarity(title, names)
        if score < _MATCH_THRESHOLD:
            continue
        # Shortest romaji breaks a score tie, the same rule anime_sites
        # uses on its own results: every cour of a franchise scores
        # alike, and the shortest name is the base series rather than a
        # sequel ("Kaijuu 8-gou" over "Kaijuu 8-gou 2nd Season").
        scored.append((-score, len(title_match.normalize(names[0] or "")), urls))
    if not scored:
        return []
    return min(scored)[2]


def fetch_crunchyroll_urls(title: str, timeout: int = 8) -> list:
    """Crunchyroll's own links for `title` - see fetch_external_urls,
    which this is the original and most-used case of."""
    return fetch_external_urls(title, "crunchyroll", timeout)
