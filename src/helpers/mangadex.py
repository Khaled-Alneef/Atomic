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

from . import title_match

BASE_URL = "https://api.mangadex.org"

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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
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


def fetch_next_chapter(title: str, known_latest_chapter=None, timeout: int = 10):
    """Best estimate of when this title's next chapter lands, from its
    MangaDex release history. Returns {"at": aware UTC datetime,
    "chapter": int, "estimated": True} or None when there's no confident
    answer - no title match, nothing recent enough to extrapolate from,
    or a release pattern too irregular to call.

    `known_latest_chapter` is the newest chapter the *user's own* reading
    site has (tracked on the entry already): scanlation sites regularly
    run ahead of MangaDex, and the next chapter number should be the one
    they're actually waiting for, not the one MangaDex happens to be up
    to. It only ever raises the number, never lowers it."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        candidates = _search(title, timeout)
    except Exception:
        return None

    now = datetime.now(timezone.utc)
    for manga in candidates:
        try:
            by_language = _chapter_feed(manga["id"], timeout)
        except Exception:
            continue
        releases = _leading_feed(by_language)
        if not releases:
            continue
        prediction = _predict(releases, now)
        if prediction:
            try:
                known = float(known_latest_chapter or 0)
            except (TypeError, ValueError):
                known = 0.0
            prediction["chapter"] = max(prediction["chapter"], math.floor(known) + 1)
            return prediction
    return None
