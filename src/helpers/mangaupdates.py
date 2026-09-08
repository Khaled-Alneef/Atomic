"""MangaUpdates - series artwork and the medium a title actually is.

The third reading catalogue, beside AniList and MangaDex, and the owner
asked for all three by name. It earns its place twice over:

  * **Artwork.** AniList misses scanlation titles outright - measured 21
    August 2026 over the owner's own list plus a Recently Released
    sample, **3 of 9 had no AniList match at all**, so those pages had no
    ground to draw. MangaUpdates is a scanlation database first, so the
    titles AniList has never heard of are exactly the ones it carries.
  * **Category.** Its `type` field says "Manga", "Manhwa", "Manhua" or
    "OEL" directly, which is the question the Read page's new sections
    ask. AniList answers the same question as `countryOfOrigin`
    (JP/KR/CN) and MangaDex as `originalLanguage` (ja/ko/zh); three
    sources that agree are what makes a category trustworthy.

Keyless and public: the v1 API needs no account. POST for search, GET
for a series. Fails soft to None everywhere, like every source here.
"""

import json
import urllib.parse
import urllib.request

from . import net, title_match

BASE_URL = "https://api.mangaupdates.com/v1"
_UA = "Mozilla/5.0 PC-App/1.0"
DEFAULT_TIMEOUT = 8

# How close a returned name has to be before its artwork or its category
# is believed. The same bar the schedule lookups use - inheriting another
# series' cover is the failure worth avoiding, and it is silent.
MATCH_THRESHOLD = 0.85

# What MangaUpdates' `type` means in this app's own words. Anything else
# it returns (doujinshi, artbook, novel) is not one of the Read page's
# sections and lands in "Other".
_TYPE_TO_MEDIUM = {
    "manga": "Manga",
    "manhwa": "Manhwa",
    "manhua": "Manhua",
}


def _post(path: str, payload: dict, timeout: float):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": _UA})
    deadline = net.deadline_in(timeout)
    with net.urlopen(request, timeout=timeout) as response:
        return json.loads(net.read_text(response, deadline))


def search(title: str, timeout: float = DEFAULT_TIMEOUT, limit: int = 8):
    """Series rows for `title`, best match first, or []. Never raises."""
    title = (title or "").strip()
    if not title:
        return []
    try:
        body = _post("/series/search",
                     {"search": title, "perpage": max(1, min(int(limit), 25))},
                     timeout)
    except Exception:
        return []
    rows = []
    for result in (body or {}).get("results") or []:
        record = result.get("record") or {}
        name = (record.get("title") or "").strip()
        if not name:
            continue
        rows.append(record)
    # Ranked here rather than trusting the API's order: its relevance
    # sort happily puts a spin-off above the series itself.
    rows.sort(key=lambda r: -title_match.similarity(
        title, (r.get("title") or "")))
    return rows


def _best(title: str, timeout: float):
    for record in search(title, timeout):
        if title_match.similarity(title, record.get("title") or "") >= MATCH_THRESHOLD:
            return record
    return None


def fetch_cover_url(title: str, timeout: float = DEFAULT_TIMEOUT):
    """The series' cover URL, or None.

    Portrait, like every cover here - the details page turns a portrait
    into a blurred ground rather than stretching it (see
    DetailsPage.paintEvent), so this is a usable answer for a page that
    would otherwise have no artwork at all."""
    record = _best(title, timeout)
    if not record:
        return None
    image = (record.get("image") or {}).get("url") or {}
    return image.get("original") or image.get("thumb") or None


def fetch_medium(title: str, timeout: float = DEFAULT_TIMEOUT):
    """"Manga" / "Manhwa" / "Manhua" for this title, or None when
    MangaUpdates does not know it or calls it something else."""
    record = _best(title, timeout)
    if not record:
        return None
    return _TYPE_TO_MEDIUM.get(str(record.get("type") or "").strip().lower())
