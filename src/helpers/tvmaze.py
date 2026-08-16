"""Minimal client for the public TVMaze API (https://www.tvmaze.com/api) -
just the "when does the next episode air" lookup, no API key needed.

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
from datetime import datetime

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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
