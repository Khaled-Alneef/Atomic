"""Minimal client for Stremio's Cinemeta addon (https://v3-cinemeta.strem.io)
and a launcher for the Stremio desktop app. Search + a full-metadata
lookup for the latest aired episode, no API key needed. Every lookup
fails soft (returns []/None) so a flaky connection never crashes the
tracker UI - it just means no suggestions/covers/progress show up.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

from . import net

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(net.read_text(resp, deadline))
    except Exception:
        return None
    meta = (body or {}).get("meta")
    return meta if isinstance(meta, dict) else None


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
