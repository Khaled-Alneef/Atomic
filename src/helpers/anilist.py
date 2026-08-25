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

# Everything airing in a window, rather than one title's next episode.
# `airingAt` is a unix timestamp and the sort is by it, so the first page
# is simply "what is out next" across the whole catalogue - which is what
# the Schedule tab needs to show more than the user's own saved shows.
_UPCOMING_QUERY = """
query ($from: Int, $to: Int, $perPage: Int) {
  Page(perPage: $perPage) {
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to,
                    sort: TIME) {
      episode
      airingAt
      media {
        title { romaji english native }
        coverImage { large }
        format
        isAdult
      }
    }
  }
}
"""


def fetch_upcoming_airing(hours: int = 168, limit: int = 40,
                          timeout: int = 8) -> list:
    """What airs next across AniList, soonest first.

    The Schedule tab's "everything else" - the rows that are not the
    owner's own saved shows. Each row is
    {title, episode, at (aware UTC datetime), cover_url}.

    Adult titles are dropped, and so are the formats nobody schedules a
    week around (music videos above all). Fails soft to [] and lets
    RateLimited through for the caller to say out loud, exactly as
    fetch_next_episode does - a schedule that quietly shows only saved
    rows because AniList said 403 is the failure this project has
    already shipped once."""
    now = int(time.time())
    try:
        data = _post(_UPCOMING_QUERY,
                     {"from": now, "to": now + int(hours) * 3600,
                      "perPage": max(1, min(int(limit), 50))}, timeout)
    except RateLimited:
        raise
    except Exception:
        return []
    page = ((data or {}).get("data") or {}).get("Page") or {}
    out = []
    for row in page.get("airingSchedules") or []:
        media = row.get("media") or {}
        if media.get("isAdult") or media.get("format") in ("MUSIC",):
            continue
        titles = media.get("title") or {}
        title = (titles.get("english") or titles.get("romaji")
                 or titles.get("native") or "").strip()
        if not title or not row.get("airingAt"):
            continue
        out.append({
            "title": title,
            "episode": int(row.get("episode") or 0),
            "at": datetime.fromtimestamp(int(row["airingAt"]), timezone.utc),
            "cover_url": (media.get("coverImage") or {}).get("large") or "",
            # What the Schedule tab files the row under. Stated rather
            # than assumed now that TVmaze's calendar is merged in
            # beside this one (tvmaze.fetch_upcoming_schedule) - the tab
            # used to type every catalogue row "Anime" because this was
            # the only source it had.
            "type": "Anime",
        })
    return out[:limit]


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


# Both media types in **one** document, because the manga side of
# AniList is where `bannerImage` is thinnest. Measured 22 August 2026
# over 43 real reading titles (the owner's tracker and history plus a
# live Discover catalogue): 29 carried a manga banner, 9 carried only a
# portrait cover, 5 matched nothing. Of the 14 with no manga banner, the
# same franchise's *anime* entry carried a real wide banner for 2
# (Tsukimichi, Chillin' in Another World) - and an alias in the same POST
# costs no extra request and no extra throttle wait, which a second
# lookup would. The anime side is asked for only when a banner is what
# is wanted; a poster tile must never be handed one (see fetch_manga_cover).
_MANGA_ART_QUERY = """
query ($search: String, $withAnime: Boolean!) {
  manga: Page(perPage: 8) {
    media(search: $search, type: MANGA, sort: SEARCH_MATCH) {
      title { romaji english native }
      synonyms
      bannerImage
      coverImage { extraLarge large }
    }
  }
  anime: Page(perPage: 6) @include(if: $withAnime) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      title { romaji english native }
      synonyms
      bannerImage
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
        with net.urlopen(req, timeout=timeout) as resp:
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


def _best_url(title: str, media_list, pick):
    """The URL `pick` yields for the best-matching entry in `media_list`,
    or None.

    Scored against the title the user actually has, never the stripped or
    quote-folded query: normalize() drops the tag and folds the quote on
    both sides, so the full string loses nothing and stays the thing being
    matched. Only an entry whose title genuinely matches is considered at
    all, because a wrong backdrop is still a page confidently dressed as a
    different series. Shortest romaji breaks a score tie, the same
    base-series rule as fetch_external_urls."""
    scored = []
    for media in media_list or []:
        url = pick(media)
        if not url:
            continue
        names = _candidate_names(media)
        score = title_match.best_similarity(title, names)
        if score < _MATCH_THRESHOLD:
            continue
        scored.append((-score, len(title_match.normalize(names[0] or "")), url))
    return min(scored)[2] if scored else None


def _cover_of(media):
    cover = media.get("coverImage") or {}
    return cover.get("extraLarge") or cover.get("large")


def manga_art(title: str, timeout: int = 8, banner_first: bool = True):
    """`(url, kind)` for the best AniList art for a reading title, where
    kind is "banner" (a real landscape image, 1900x400) or "cover" (the
    portrait one, ~460x650), or `(None, None)`.

    The caller needs to know *which*, because they are not
    interchangeable at a 1266x300 hero: a 460x650 cover scaled with
    KeepAspectRatioByExpanding into that box is 1266x1717, of which the
    banner shows 300 rows - the middle **17%** of the picture, upscaled
    2.75x. See helpers/hero_art.py for what is done with a cover instead.

    Preference order, and it matters: this title's manga banner, then the
    same franchise's *anime* banner (same POST, see _MANGA_ART_QUERY),
    then the manga cover. A cover is never preferred over a banner and an
    anime banner is never preferred over the work's own.

    `banner_first=False` keeps the portrait cover only, which is what a
    poster tile needs, and skips the anime alias entirely.

    The search is retried across title_match.search_variants - the quote
    fold first, then the raw string, then both with the reading site's
    group tag dropped. Measured: AniList answers "Kingdom (WAN)" with
    nothing and "Kingdom" with the banner, and answers "Swordmaster’S
    Youngest Son" with nothing and "Swordmaster'S Youngest Son" with the
    entry (that one is U+2019 against U+0027 and nothing else)."""
    title = (title or "").strip()
    if not title:
        return None, None
    for query in title_match.search_variants(title):
        try:
            body = _post(_MANGA_ART_QUERY,
                         {"search": query, "withAnime": bool(banner_first)}, timeout)
        except RateLimited:
            # The block is on the connection, not the query: a second
            # variant buys another 403 and another throttle wait.
            return None, None
        except Exception:
            # Anything else is this one request failing. Measured: the
            # first variant timed out at 8.4s and the stripped retry
            # never ran, so "Kingdom (WAN)" reported no artwork on a
            # title AniList answers for.
            continue

        data = body.get("data") or {}
        manga = ((data.get("manga") or {}).get("media")) or []
        if banner_first:
            found = _best_url(title, manga, lambda m: m.get("bannerImage"))
            if found:
                return found, "banner"
            anime = ((data.get("anime") or {}).get("media")) or []
            found = _best_url(title, anime, lambda m: m.get("bannerImage"))
            if found:
                return found, "banner"
        found = _best_url(title, manga, _cover_of)
        if found:
            return found, "cover"
    return None, None


def fetch_manga_artwork(title: str, timeout: int = 8):
    """A wide banner (or failing that, the large cover) URL for a manga
    title, or None - what the reading details page draws its ground from,
    since reading entries have no IMDb id and so no TMDB artwork.

    Kept for callers that only want a URL; anything drawing a *hero*
    wants manga_art, so it can tell a banner from a cover."""
    return manga_art(title, timeout, banner_first=True)[0]


def fetch_manga_cover(title: str, timeout: int = 8):
    """The portrait cover only - the last fallback for a card whose own
    reading site served no cover art (manga_sites._external_cover).
    Never the banner: it would be cropped to its middle in a poster
    box, which reads as a broken image rather than as a cover."""
    return manga_art(title, timeout, banner_first=False)[0]
