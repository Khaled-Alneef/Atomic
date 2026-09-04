"""A cast member's filmography, from TMDB - the cast chips as doors.

The owner, 2 September 2026: "make the cast also clickable as the
genres". Cinemeta's meta carries the cast as bare names and nothing
else, so the page behind a name has to come from somewhere that files
titles *by person*. TMDB does, keylessly for us because artwork already
ships a read token: `search/person` names the actor, `person/<id>/
combined_credits` lists everything they were cast in. Measured 2
September 2026 on Alan Ritchson: 0.35s + 0.20s for 62 cast credits
(and 25 crew ones, which are not asked for - a producer credit is not
"as the genres").

Everything is answered from one in-memory list per name, so the first
page and every scroll batch after it are slices of the same answer -
the source has no paging of its own and needs none, a filmography being
a few hundred rows at most (Tom Cruise is the widest name tried).

Fails soft to [] with a `note`: no token, a refused host, an unknown
name, all read as an empty page saying why, never an exception into the
server.
"""
import threading
import urllib.parse

from . import artwork, title_match

# Poster width for a grid tile. artwork.POSTER_SIZE_PATH is w500 for the
# tracker's own cards; w342 is the next size down and still twice what a
# 160px tile draws, and a filmography is a few hundred posters at once.
POSTER_SIZE = "w342"
TIMEOUT = 8

# TMDB genre ids that mark an appearance rather than a role: talk show
# (10767), news (10763), reality (10764). Alan Ritchson's credits carry
# six of these - The Tonight Show, Colbert, Seth Meyers, Kelly Clarkson -
# and every one of them is also "Self" or "Self - Guest" as the
# character, which is the second test below. Animation is 16, used with
# a Japanese origin to file a row as Anime.
_APPEARANCE_GENRES = {10767, 10763, 10764}
_ANIMATION = 16

# Below this the best search hit is somebody else with a similar name.
# The same 0.8 bar the details page's own id lookup uses.
MIN_NAME_MATCH = 0.8

# Bounded so a session that walked a hundred cast pages does not keep a
# hundred filmographies. Keyed by the lowercased name as it was clicked.
_CACHE_LIMIT = 24
_CACHE = {}
_CACHE_ORDER = []
_LOCK = threading.Lock()


def _person_id(name):
    """TMDB's id for an actor by that name, or 0.

    The best *name* match with an acting department - TMDB ranks its
    results by popularity, and a famous director sharing a surname would
    otherwise take an actor's page."""
    url = f"{artwork.API}/search/person?query={urllib.parse.quote(name)}"
    body = artwork._get_json(url, TIMEOUT) or {}
    best, score = 0, 0.0
    for hit in body.get("results") or []:
        if not isinstance(hit, dict) or not hit.get("id"):
            continue
        value = title_match.similarity(name, str(hit.get("name") or ""))
        if str(hit.get("known_for_department") or "") != "Acting":
            value -= 0.1
        if value > score:
            best, score = int(hit["id"]), value
    return best if score >= MIN_NAME_MATCH else 0


def _is_appearance(credit) -> bool:
    if set(credit.get("genre_ids") or []) & _APPEARANCE_GENRES:
        return True
    character = str(credit.get("character") or "").strip().lower()
    # "Self", "Himself", "Herself", "Self - Guest", "Self (archive
    # footage)" - the prefix is the test, the suffix varies.
    return character.startswith(("self", "himself", "herself"))


def _row(credit):
    """One credit as a grid row - the same keys discover._video_row
    writes, so server._grid_row and the details page read it unchanged.
    No IMDb id: TMDB's credit rows carry only TMDB's own, and the details
    page resolves an id-less title through _resolve_id_worker from the
    title and type it is handed."""
    media = str(credit.get("media_type") or "")
    title = str(credit.get("title") or credit.get("name") or "").strip()
    if not title or media not in ("movie", "tv"):
        return None
    date = str(credit.get("release_date") or credit.get("first_air_date") or "")
    poster = str(credit.get("poster_path") or "")
    genres = set(credit.get("genre_ids") or [])
    origin = [str(c).upper() for c in (credit.get("origin_country") or [])]
    if _ANIMATION in genres and ("JP" in origin
                                 or str(credit.get("original_language")) == "ja"):
        label = "Anime"
    elif media == "movie":
        label = "Movie"
    else:
        label = "Series"
    rating = credit.get("vote_average")
    return {
        "title": title,
        "year": date[:4],
        "poster": f"{artwork.CDN}/{POSTER_SIZE}{poster}" if poster else "",
        "imdb_id": "",
        "type": label,
        # Only where enough people voted for the number to mean anything;
        # a single 10 on a short film is not a rating.
        "imdbRating": (f"{float(rating):.1f}"
                       if rating and int(credit.get("vote_count") or 0) >= 20
                       else ""),
        "genres": [],
    }


def _fetch(name):
    """The whole filmography, filtered, sorted and deduplicated - or [].
    Raises nothing; the caller reads an empty list as "nothing here"."""
    person = _person_id(name)
    if not person:
        return []
    url = f"{artwork.API}/person/{person}/combined_credits"
    body = artwork._get_json(url, TIMEOUT) or {}
    credits = [c for c in (body.get("cast") or [])
               if isinstance(c, dict) and not _is_appearance(c)]
    credits.sort(key=lambda c: (float(c.get("popularity") or 0),
                                int(c.get("vote_count") or 0)), reverse=True)
    rows, seen = [], set()
    for credit in credits:
        row = _row(credit)
        if row is None:
            continue
        key = row["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def filmography(name):
    """Every title `name` was cast in, most popular first: (rows, note).

    The list is cached for the session on first ask, so paging it is a
    slice (see `page`). `note` says why a list is empty - the honest
    answer the page shows instead of "Looking around..." for ever."""
    key = str(name or "").strip().lower()
    if not key:
        return [], "no name"
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key], ""
    if not artwork.available():
        return [], "no TMDB key"
    try:
        rows = _fetch(key)
    except Exception as error:
        return [], f"TMDB could not answer: {str(error)[:80]}"
    with _LOCK:
        _CACHE[key] = rows
        _CACHE_ORDER.append(key)
        while len(_CACHE_ORDER) > _CACHE_LIMIT:
            _CACHE.pop(_CACHE_ORDER.pop(0), None)
    return rows, ("" if rows else "nothing under this name")


def page(name, skip, limit):
    """Rows `skip` onward, at most `limit` of them - the scroll batch."""
    rows, _note = filmography(name)
    skip = max(0, int(skip or 0))
    return rows[skip:skip + max(0, int(limit or 0))]


# A face on a card. w185 is TMDB's portrait thumbnail: a cast card draws
# 160 CSS px, 200 device pixels at the owner's 1.25, so w185 is within
# 8% of the drawn size and w342 would be twice the bytes for nothing.
FACE_SIZE = "w185"

# How long a Cast section on Discover keeps the same faces. Discover
# rebuilds on every visit (server._discover) and TMDB's trending list
# changes daily, so a per-visit request would be one call per page view
# for an answer that cannot have changed.
_POPULAR_TTL = 6 * 3600
_POPULAR = {"at": 0.0, "rows": []}


def _face(hit):
    """One `search/person` or `person/popular` row as a card.

    `kind` is "person" so the app opens this name's own page rather than
    a details page - windows/web_pages._from_page routes on it, the same
    way it routes a game to a launch and a title to the details page.
    """
    name = str(hit.get("name") or "").strip()
    if not name:
        return None
    path = str(hit.get("profile_path") or "")
    known = [str((k or {}).get("title") or (k or {}).get("name") or "").strip()
             for k in (hit.get("known_for") or []) if isinstance(k, dict)]
    known = [k for k in known if k][:2]
    return {"title": name,
            "kind": "person",
            "type": "Person",
            "poster": f"{artwork.CDN}/{FACE_SIZE}{path}" if path else "",
            "imdb_id": "",
            "year": "",
            # The meta line under a face: what this person is known for,
            # which is the one fact that tells two same-named actors
            # apart on a card.
            "note": "  ".join(known),
            "genres": []}


def search(query, limit=20):
    """People whose name matches `query`, most prominent first.

    TMDB's own ranking is kept - it is popularity, which is what puts
    the actor above the crew member and the namesake. Acting-department
    rows first, because this is the cast list and a cinematographer
    sharing a name has no filmography to show behind the card.

    Fails soft to [] - a search page must still answer with its other
    sections when TMDB does not.
    """
    query = str(query or "").strip()
    if not query or not artwork.available():
        return []
    try:
        url = (f"{artwork.API}/search/person"
               f"?query={urllib.parse.quote(query)}&include_adult=false")
        body = artwork._get_json(url, TIMEOUT) or {}
    except Exception:
        return []
    hits = [h for h in (body.get("results") or []) if isinstance(h, dict)]
    hits.sort(key=lambda h: (str(h.get("known_for_department") or "")
                             != "Acting",))
    rows = []
    for hit in hits:
        row = _face(hit)
        if row is not None:
            rows.append(row)
        if len(rows) >= max(1, int(limit or 1)):
            break
    return rows


def popular(limit=20):
    """The faces Discover shows when nothing has been typed.

    `person/popular`, not `trending/person/week`, and that was measured
    3 September 2026: trending answers 20 rows whose `known_for` is
    **absent**, so every card came up with a face and a blank line under
    it, while popular carries two titles per row ("Jason Statham -
    Snatch, The Meg") - which is the one fact that tells a face apart
    from another face. Cached for _POPULAR_TTL, because Discover is
    rebuilt on every visit.
    """
    if not artwork.available():
        return []
    import time as _time
    with _LOCK:
        if _POPULAR["rows"] and _time.time() - _POPULAR["at"] < _POPULAR_TTL:
            return _POPULAR["rows"][:limit]
    try:
        body = artwork._get_json(f"{artwork.API}/person/popular", TIMEOUT) or {}
    except Exception:
        return []
    rows = []
    for hit in body.get("results") or []:
        if not isinstance(hit, dict):
            continue
        row = _face(hit)
        # A face with no picture is a letter in a circle, which says
        # nothing about who the person is - dropped rather than drawn.
        if row is not None and row["poster"]:
            rows.append(row)
    if rows:
        with _LOCK:
            _POPULAR["at"] = _time.time()
            _POPULAR["rows"] = rows
    return rows[:limit]
