"""Minimal client for the public MangaDex API
(https://api.mangadex.org/docs/) - used to work out when a manga's next
chapter is likely to drop. No API key needed.

Unlike anime/series (AniList and TVMaze both publish an actual airing
schedule), MangaDex has no "next chapter" field to read: nobody
announces a scanlation release date. So this *estimates* one from the
title's own release history - which is why everything it returns is
flagged estimated and shown as "Expected", never stated as fact.

The estimate is deliberately conservative, because a confidently wrong
countdown is worse than none at all:

  * The title has to actually match (see title_match) - MangaDex's search
    happily returns "The Beginning After the End" for "The World After
    The End", and inheriting a completely different series' schedule is
    the worst failure mode here.
  * Chapters are read across *all* languages, not just English. Most of
    what people follow on scanlation sites is licensed in English, so its
    English feed here is a near-empty stub while the same title's other
    feeds are current - filtering to English throws away the schedule for
    exactly the titles most likely to be tracked.
  * Only one language's feed is used for the actual cadence, the one
    furthest along. Mixing them measures translators racing to catch up
    with each other rather than the series' own release rhythm.
  * A release rhythm has to look like one at all: a plausible interval, a
    feed that's still current, and enough chapters to measure. Anything
    outside that returns None and the card simply shows no estimate.
"""

import json
import math
import statistics
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import net, title_match

BASE_URL = "https://api.mangadex.org"
COVERS_URL = "https://uploads.mangadex.org/covers"

# MangaDex allows ~5 requests/second globally and resets connections on a
# burst; the tracker fires one lookup per entry in its own thread, so
# these are spaced out and retried once rather than dropped.
_MIN_REQUEST_GAP = 0.35
_throttle_lock = threading.Lock()
_last_request_at = 0.0

# Every rating included on purpose: filtering by rating would silently
# lose the schedule for anything MangaDex tagged suggestive or above,
# which is unrelated to what the user is asking (when's the next one).
_RATINGS = "".join(f"&contentRating[]={r}"
                   for r in ("safe", "suggestive", "erotica", "pornographic"))

_MATCH_THRESHOLD = 0.85   # stricter than anime/series: no id to fall back on
_MAX_CANDIDATES = 3       # near-identical titles are common; try a few
_HEAD_CHAPTERS = 10       # recent chapters the cadence is measured over
_MIN_INTERVALS = 4        # fewer than this isn't a rhythm, it's a coincidence

# A serialized manga runs somewhere between a few days and a month apart.
# Anything faster is a translator dumping a backlog in one sitting (which
# would predict "next chapter tomorrow" forever); anything slower is a
# hiatus, where a countdown would be pure invention either way.
_MIN_INTERVAL = timedelta(days=2)
_MAX_INTERVAL = timedelta(days=30)

# How stale the newest chapter may be before the series counts as dormant
# rather than ongoing - a few missed slots is a break, months of silence
# means there's nothing to count down to.
_MAX_SILENCE = timedelta(days=21)

# Share of recent releases that must land on the same weekday before the
# schedule is treated as a fixed weekly slot rather than a rolling gap.
_WEEKLY_CONFIDENCE = 0.6


def _get(url: str, timeout: int, retries: int = 1):
    global _last_request_at
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Atomic-Tracker/1.0",
    })
    for attempt in range(retries + 1):
        with _throttle_lock:
            wait = _last_request_at + _MIN_REQUEST_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()
        try:
            # Per attempt, not per call: a retry gets its own full budget,
            # the same way it gets its own socket timeout.
            deadline = net.deadline_in(timeout)
            with net.urlopen(req, timeout=timeout) as resp:
                return json.loads(net.read_text(resp, deadline))
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.0)


def _titles_of(manga: dict) -> list:
    attributes = manga.get("attributes") or {}
    names = list((attributes.get("title") or {}).values())
    for alt in attributes.get("altTitles") or []:
        names.extend(alt.values())
    return names


def _search(title: str, timeout: int) -> list:
    """Candidate manga for a title, best title match first. Searched
    without the reading site's own tags ("Kingdom (WAN)"), which match
    nothing on MangaDex - but still *scored* against the full title the
    user has, since that's the one they recognize."""
    url = (f"{BASE_URL}/manga?limit=10&order[relevance]=desc"
           f"&title={urllib.parse.quote(title_match.search_query(title))}{_RATINGS}")
    body = _get(url, timeout)
    scored = []
    for manga in body.get("data") or []:
        score = title_match.best_similarity(title, _titles_of(manga))
        if score >= _MATCH_THRESHOLD:
            scored.append((score, manga))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [manga for _, manga in scored[:_MAX_CANDIDATES]]


def fetch_cover_url(title: str, timeout: int = 8):
    """MangaDex's own cover art for a title, or None.

    The first fallback for a manga whose reading site served no cover
    (see manga_sites._external_cover). Measured 21 August 2026 on the
    title the owner reported missing one, "I Want to Stop Killing":
    MangaDex matches it at 1.00 and carries the art, while its only
    configured site (Mangalek) 403s every series page.

    The same 0.85 bar as the schedule lookups, and scored against the
    user's own title while the *query* is retried tag-stripped
    (title_match.search_variants) - a cover from the wrong series is
    worse than a letter avatar, so a near-miss returns None.

    `.512.jpg` is MangaDex's own thumbnailer, not a crop: measured 200
    at 59.1KB against 47.9KB for that title's original, and it is the
    size a poster tile actually draws."""
    title = (title or "").strip()
    if not title:
        return None
    for query in title_match.search_variants(title):
        url = (f"{BASE_URL}/manga?limit=5&includes[]=cover_art"
               f"&order[relevance]=desc"
               f"&title={urllib.parse.quote(query)}{_RATINGS}")
        try:
            body = _get(url, timeout)
        except Exception:
            continue    # this one request failed; the retry is still worth it
        best, best_score = None, 0.0
        for manga in body.get("data") or []:
            score = title_match.best_similarity(title, _titles_of(manga))
            if score < _MATCH_THRESHOLD or score <= best_score:
                continue
            file_name = None
            for relationship in manga.get("relationships") or []:
                if relationship.get("type") == "cover_art":
                    file_name = (relationship.get("attributes") or {}).get("fileName")
                    break
            # fileName only exists because of includes[]=cover_art -
            # without it the relationship is an id and a type.
            if file_name and manga.get("id"):
                best = f"{COVERS_URL}/{manga['id']}/{file_name}.512.jpg"
                best_score = score
        if best:
            return best
    return None


def _chapter_feed(manga_id: str, timeout: int) -> dict:
    """{language: {chapter number: earliest release time}} for one title.

    Keyed by earliest release because the same chapter gets re-uploaded
    by several groups - the first upload is when it actually dropped."""
    url = (f"{BASE_URL}/manga/{manga_id}/feed?limit=100&order[readableAt]=desc"
           f"{_RATINGS}")
    body = _get(url, timeout)
    by_language = {}
    for chapter in body.get("data") or []:
        attributes = chapter.get("attributes") or {}
        number, readable_at = attributes.get("chapter"), attributes.get("readableAt")
        if not number or not readable_at:
            continue
        try:
            number = float(number)
            when = datetime.fromisoformat(readable_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        releases = by_language.setdefault(attributes.get("translatedLanguage") or "", {})
        if number not in releases or when < releases[number]:
            releases[number] = when
    return by_language


def _leading_feed(by_language: dict):
    """The one language feed to measure, the one furthest into the series
    (ties broken by whichever released most recently). Every other feed is
    some translation catching up from behind at its own unrelated pace."""
    usable = {lang: releases for lang, releases in by_language.items()
              if len(releases) > _MIN_INTERVALS}
    if not usable:
        return None
    return max(usable.values(),
               key=lambda releases: (max(releases), max(releases.values())))


def _schedule_from(releases: dict):
    """(anchor release, interval) for a feed, or None if its releases
    don't form a usable rhythm. A feed whose recent releases mostly land
    on one weekday is treated as a fixed weekly slot anchored to that
    weekday; anything else just repeats its typical gap."""
    recent = sorted(releases.items())[-_HEAD_CHAPTERS:]
    times = [when for _, when in recent]
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    gaps = [gap for gap in gaps if gap > 3600]  # same-sitting uploads aren't a gap
    if len(gaps) < _MIN_INTERVALS:
        return None

    weekdays = Counter(when.astimezone().weekday() for when in times)
    weekday, hits = weekdays.most_common(1)[0]
    if hits / len(times) >= _WEEKLY_CONFIDENCE:
        anchor = max(when for when in times if when.astimezone().weekday() == weekday)
        return anchor, timedelta(days=7)
    return max(times), timedelta(seconds=statistics.median(gaps))


def _predict(releases: dict, now: datetime):
    schedule = _schedule_from(releases)
    if not schedule:
        return None
    anchor, interval = schedule
    if not _MIN_INTERVAL <= interval <= _MAX_INTERVAL:
        return None
    # Measured against the newest chapter in any position, not the anchor:
    # a weekly anchor can legitimately sit a few days back.
    if now - max(releases.values()) > max(_MAX_SILENCE, 3 * interval):
        return None

    when = anchor
    while when <= now:
        when += interval
    return {"at": when, "chapter": math.floor(max(releases)) + 1, "estimated": True}


def fetch_next_chapter(title: str, known_latest_chapter=None, manga_id=None,
                       timeout: int = 10):
    """Best estimate of when this title's next chapter lands, from its
    MangaDex release history.

    Returns (prediction, manga id). The prediction is {"at": aware UTC
    datetime, "chapter": int, "estimated": True}, or None when there's no
    confident answer - no title match, nothing recent enough to
    extrapolate from, or a release pattern too irregular to call. The id
    is what this title resolved to on MangaDex, for the caller to store
    and hand back as `manga_id` next time, or None when nothing worth
    reusing was resolved. The two are independent: a title that resolves
    cleanly but has no countable rhythm still returns an id.

    `known_latest_chapter` is the newest chapter the *user's own* reading
    site has (tracked on the entry already): scanlation sites regularly
    run ahead of MangaDex, and the next chapter number should be the one
    they're actually waiting for, not the one MangaDex happens to be up
    to. It only ever raises the number, never lowers it.

    `manga_id` skips the search entirely. A title's id on MangaDex never
    changes, but _search was being re-run for every entry on every single
    refresh: on the real Reading page that was 8 of 17 requests, and since
    every request waits _MIN_REQUEST_GAP behind the last one, the wall
    clock of a refresh is very nearly just the number of requests in it.
    With ids cached that page drops to 8 requests (measured, one per
    entry) - the feed still has to be re-read every time, since that's
    where a new chapter actually shows up."""
    title = (title or "").strip()
    if not title and not manga_id:
        return None, None
    if manga_id:
        # Deliberately no re-search fallback when a cached id turns up
        # nothing usable: that's the answer for a series gone quiet, and
        # falling back would put the search straight back for exactly the
        # dormant titles that never stop being dormant. A wrong id can
        # only come from a title that has since been edited, and that
        # clears the cached id at the point of the edit.
        candidates = [{"id": manga_id}]
    else:
        try:
            candidates = _search(title, timeout)
        except Exception:
            return None, None

    now = datetime.now(timezone.utc)
    # A search that clears the threshold exactly once has no ambiguity to
    # resolve - nothing else would ever be tried for it - so its id is
    # safe to cache even with no prediction to show for it. Only if that
    # one has a real feed, though: an id with nothing readable behind it
    # is the shape of a stub listing, and caching that would lock the
    # entry onto it and stop it ever finding the real one.
    sole_match = None
    for manga in candidates:
        try:
            by_language = _chapter_feed(manga["id"], timeout)
        except Exception:
            continue
        releases = _leading_feed(by_language)
        if not releases:
            continue
        if len(candidates) == 1:
            sole_match = manga["id"]
        prediction = _predict(releases, now)
        if prediction:
            try:
                known = float(known_latest_chapter or 0)
            except (TypeError, ValueError):
                known = 0.0
            prediction["chapter"] = max(prediction["chapter"], math.floor(known) + 1)
            # The candidate that actually answered, which is not always the
            # best-scoring title match: MangaDex carries dead duplicate
            # listings, and on a real entry here ("The Beginning After The
            # End") the live one was the second candidate, behind a listing
            # with no usable feed. Caching the answering id keeps that
            # resolution instead of re-deciding it every refresh.
            return prediction, manga["id"]
    return None, manga_id or sole_match
