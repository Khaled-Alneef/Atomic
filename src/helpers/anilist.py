"""Minimal client for the public AniList GraphQL API (https://anilist.co) -
a title search + a public list-progress lookup, no login/API key needed.

Unlike Crunchyroll (whose equivalent data lives behind an OAuth-gated,
Cloudflare-bot-protected API this app won't try to bypass - see
crunchyroll.py), AniList's is fully open for public profiles: a
username is enough to read someone's list, no password or token flow.
Since a lot of people track their watching on AniList regardless of
which app they actually watch in, this gives real progress for
Crunchyroll-provider Anime entries too, not just Stremio's.

Every lookup fails soft (returns None) so a flaky connection, a private
list, or no title match never crashes the tracker UI - it just means no
progress shows up.
"""

import json
import urllib.request

API_URL = "https://graphql.anilist.co"

_SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
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


def _post(query: str, variables: dict, timeout: int):
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
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
