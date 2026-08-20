"""Minimal client for the public AniList GraphQL API (https://anilist.co) -
the airing schedule for the next episode, and a title's page on a
streaming service AniList records a link for. No login or API key.

**Not watch progress.** AniList used to answer that too, and it was
removed: it only ever knew what some other tracker had written to it, so
a card could state an episode its owner had never reached, confidently
and with nothing to say where the number came from. Progress now comes
from the user's Stremio account or from nowhere - see
tracker._fetch_real_progress. Don't reintroduce a second source here
without a way to tell which one is right.

`fetch_external_urls` is what lets a Crunchyroll or Netflix entry
resolve to a real title page at all, since neither service can be
searched (see anime_sites.py's module docstring).

Every lookup fails soft (returns None/[]), so a flaky connection or no
title match never crashes the tracker UI - it just means no schedule or
no link.
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


_MANGA_ART_QUERY = """
query ($search: String) {
  Page(perPage: 8) {
    media(search: $search, type: MANGA, sort: SEARCH_MATCH) {
      title { romaji english native }
      synonyms
      bannerImage
      coverImage { extraLarge large }
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


def fetch_manga_artwork(title: str, timeout: int = 8):
    """A wide banner (or failing that, the large cover) URL for a manga
    title, or None - what the reading details page draws its ground from,
    since reading entries have no IMDb id and so no TMDB artwork.

    Same matching rule as everything else here: only an entry whose title
    genuinely matches is considered, because a wrong *backdrop* is still
    a page confidently dressed as a different series. The banner is
    preferred outright - it is landscape art cut for exactly this use -
    and the portrait cover is only the fallback for titles AniList has
    no banner for."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        body = _post(_MANGA_ART_QUERY, {"search": title}, timeout)
    except Exception:
        return None

    media_list = (((body.get("data") or {}).get("Page") or {}).get("media")) or []
    scored = []
    for media in media_list:
        cover = media.get("coverImage") or {}
        url = (media.get("bannerImage")
               or cover.get("extraLarge") or cover.get("large"))
        if not url:
            continue
        names = _candidate_names(media)
        score = title_match.best_similarity(title, names)
        if score < _MATCH_THRESHOLD:
            continue
        # Banner beats cover at equal match; shortest romaji breaks the
        # remaining tie for the same base-series reason as
        # fetch_external_urls.
        scored.append((-score, 0 if media.get("bannerImage") else 1,
                       len(title_match.normalize(names[0] or "")), url))
    if not scored:
        return None
    return min(scored)[3]
