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
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import title_match

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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_watch_progress(title: str, username: str, timeout: int = 6):
    """Your real AniList progress for the anime matching `title`, from
    `username`'s list - AniList's profile privacy has to allow public
    list viewing for this to work; there's no login here, just their
    username. Returns (season, episode) - season is always 0 since
    AniList tracks a flat episode count, not per-season numbering like
    Stremio - or None if there's no title match, the list/entry is
    private, or the lookup fails."""
    title = (title or "").strip()
    username = (username or "").strip()
    if not title or not username:
        return None
    try:
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


def fetch_crunchyroll_urls(title: str, timeout: int = 8) -> list:
    """Every Crunchyroll link AniList holds for `title`, in AniList's own
    order, or [] if nothing matches. Returns the URLs *as stored* - what
    shape a Crunchyroll URL should end up in is Crunchyroll's business,
    not this module's, so the caller (anime_sites) normalizes them.

    This exists because Crunchyroll cannot be asked directly: its search
    page renders client-side and its content API answers 401 without an
    OAuth bearer (the full reasoning is in anime_sites.py's docstring).
    AniList publishes the same link as ordinary public data on the
    key-less endpoint everything else here already uses, which makes it
    the only unauthenticated route to a real Crunchyroll title page.

    Same matching rule as fetch_next_episode, for the same reason: only
    entries whose title genuinely matches are considered at all, so a
    loose search can't hand back some other show's Crunchyroll page -
    the worst possible outcome, since the entry then silently points at
    the wrong series forever. Links come back only for the single
    best-matching entry rather than pooled across hits, because a
    franchise's spin-offs each carry their own and pooling them would
    mix a sequel's page in with the base series'."""
    title = (title or "").strip()
    if not title:
        return []
    try:
        body = _post(_EXTERNAL_LINKS_QUERY, {"search": title}, timeout)
    except Exception:
        return []

    media_list = (((body.get("data") or {}).get("Page") or {}).get("media")) or []
    scored = []
    for media in media_list:
        urls = [link["url"] for link in (media.get("externalLinks") or [])
                 if link.get("url") and "crunchyroll" in (link.get("site") or "").lower()]
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
