"""Browse-and-search feed behind the tracker pages' Discover section.

Two keyless sources, both already trusted elsewhere in the app: Cinemeta
(`stremio.py`) for anime/series/movies, MangaDex (`mangadex.py`) for
reading. Nothing here needs a key, and every failure returns [] - a
Discover grid that can't reach its source shows nothing, never an error.

Measured live 20 August 2026, one call per mode at the default limit=30,
every row carrying a poster and a usable id:

    discover_video("anime")              30 rows  1.05s  (49 available)
    discover_video("series")             30 rows  1.23s  (49 available)
    discover_video("movie")              30 rows  0.88s  (50 available)
    discover_video("series", "bleach")    3 rows  0.29s
    discover_video("movie", "akira")     24 rows  0.31s
    discover_video("anime", "frieren")    8 rows  0.29s
    discover_reading()                   30 rows  1.82s  (of 85272 total)
    discover_reading("frieren")          11 rows  0.80s
    discover_reading("berserk")          22 rows  0.86s

The anime catalog came back as Re:Zero / Attack on Titan / One Piece /
Jujutsu Kaisen / Mushoku Tensei / Demon Slayer / My Hero Academia /
Bleach / Vinland Saga - i.e. actually anime, which is the whole reason
for the genre chain below. A dead Cinemeta returns [] in 0.02s.

Three things were measured here that the obvious implementation gets
wrong, all recorded at their call sites below: Cinemeta has an
undocumented `genre=Anime`, its search rows carry no `year`, and
MangaDex's `title` field is usually romaji with the English name filed
under altTitles.
"""

import json
import urllib.parse
import urllib.request

from . import mangadex, net

CINEMETA_URL = "https://v3-cinemeta.strem.io"
MANGADEX_COVERS = "https://uploads.mangadex.org/covers"

# Cinemeta's own response time is the whole cost here - not the network.
# Measured 8 identical GETs of the anime catalog: 0.91 / 0.92 / 1.17 /
# 1.28 / 1.36 / 7.32 / 16.37 / 22.23s, median 1.36s, every one returning
# all 49 rows. Connect was never the problem (TCP+TLS 0.07-1.46s across
# six attempts, always the same IPv6 peer), so the tail is Cloudflare
# missing its cache, not a dead route.
#
# So this is the opposite case to streams.py's "short waits beat long
# ones": there, waiting never rescued a dead release. Here a slow answer
# is a *complete* answer, and cutting it off turns a full grid into an
# empty one. 20s covers all eight measured calls.
VIDEO_TIMEOUT = 20
VIDEO_BUDGET = 30

# MangaDex is consistently quick by comparison - 0.78-2.19s across every
# call in the same session - so it gets a tighter number.
READING_TIMEOUT = 10
READING_BUDGET = 20

# MangaDex rejects anything above 100 with a 400; measured limit=100
# returning exactly 100 rows.
_MANGADEX_MAX_LIMIT = 100

# Cinemeta's catalog type per kind, and the label the row carries back.
# "anime" is not a Cinemeta type - it is series filtered by genre (see
# _video_catalog_urls) - but the card still has to say "Anime".
_KIND_TYPES = {"anime": "series", "series": "series", "movie": "movie"}
_KIND_LABELS = {"anime": "Anime", "series": "Series", "movie": "Movie"}

# Unprompted browse grid, so it stays filtered: measured, the unfiltered
# most-followed list is 12 safe / 12 suggestive / 6 erotica out of 30.
_BROWSE_RATINGS = "&contentRating[]=safe&contentRating[]=suggestive"


def _get_json(url: str, deadline, timeout: float):
    """One bounded Cinemeta GET, or None. Body read through net.read_text
    so a host dribbling one byte at a time can't hold the thread past the
    deadline the way a plain resp.read() would."""
    step = net.step_timeout(deadline, timeout)
    if step is None:
        return None
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 PC-App/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=step) as resp:
            return json.loads(net.read_text(resp, net.deadline_in(step)))
    except Exception:
        return None


def _video_catalog_urls(kind: str, content_type: str) -> list:
    """The catalog URLs to try in order for a popular listing.

    For anime this is a real fallback chain, because the genre that works
    is not in the manifest. Cinemeta's manifest advertises 22 genres for
    series/top and **"Anime" is not among them** - yet
    `catalog/series/top/genre=Anime.json` answers with 49 rows that are
    genuinely all anime (Re:Zero, Attack on Titan, One Piece, Jujutsu
    Kaisen, Demon Slayer, Bleach, Vinland Saga, Frieren...).

    `genre=Animation`, the documented one, is measurably worse for this
    section: same 49 rows' worth, but the head of it is X-Men '97, Rick
    and Morty, The Simpsons, Family Guy, South Park, Invincible. Client-
    side filtering cannot recover the difference either - the rows
    Cinemeta returns for genre=Anime carry `genres: ["Animation", ...]`
    with no "Anime" in them, so the distinction exists only server-side.

    Trying an undocumented genre is safe because a genre Cinemeta doesn't
    know returns an **empty list, not an unfiltered one** - measured,
    `genre=Zzzz` gives 0 rows in 1.27s. So "did it answer with real rows"
    is a sound probe: the failure mode is a fast empty answer that falls
    through to the next URL, never a wrong list silently taking its
    place."""
    if kind == "anime":
        return [
            f"{CINEMETA_URL}/catalog/{content_type}/top/genre=Anime.json",
            f"{CINEMETA_URL}/catalog/{content_type}/top/genre=Animation.json",
            f"{CINEMETA_URL}/catalog/{content_type}/top.json",
        ]
    return [f"{CINEMETA_URL}/catalog/{content_type}/top.json"]


def _is_animated(meta: dict) -> bool:
    """Whether a row admits to being animated at all. Only used on the
    last-resort plain catalog, where nothing has filtered for us - and
    only when the row actually carries genres, since search rows don't."""
    genres = meta.get("genres") or meta.get("genre") or []
    return any(str(g).lower() in ("animation", "anime") for g in genres)


def _video_row(meta: dict, label: str):
    """One Cinemeta meta as a Discover row, or None if there's nothing to
    show."""
    title = (meta.get("name") or "").strip()
    if not title:
        return None

    # `year` is present on catalog rows ("2013-2023", "2016-") and **null
    # on every search row** - measured on both series and movie searches,
    # where releaseInfo carries the same string and year does not exist.
    # Reading only `year` would leave every searched card undated.
    year = meta.get("year") or meta.get("releaseInfo") or ""

    # Kept rather than dropped when it isn't an IMDb id: the contract
    # allows "" here, and every Cinemeta row measured was a tt id anyway
    # (49/49, 50/50, 3/3, 24/24), so this only ever costs the caller a
    # row it can't add - not a row it can't see.
    imdb_id = str(meta.get("id") or "")
    if not imdb_id.startswith("tt"):
        imdb_id = ""

    return {
        "title": title,
        "year": str(year).strip(),
        "poster": meta.get("poster") or "",
        "imdb_id": imdb_id,
        "type": label,
        # Passed through for the UI's ★ chip on the poster corner; ""
        # where Cinemeta has none (search rows often carry it, catalog
        # rows usually do). The UI treats absence as "no chip".
        "imdbRating": str(meta.get("imdbRating") or "").strip(),
    }


def discover_video(kind: str, query: str = "", limit: int = 30, deadline=None,
                   genre: str = "") -> list:
    """Popular or searched titles for one of the video trackers.

    `kind` is "anime", "series" or "movie". An empty `query` returns the
    popular catalog; a non-empty one searches; `genre` (with no query)
    asks the genre's own catalog - the details pages' genre buttons.
    Rows are {title, year, poster, imdb_id, type}. Always a list - [] on
    any failure, an unknown kind, or nothing found; an unknown genre is
    a fast empty answer, never an unfiltered list (measured, see
    _video_catalog_urls).

    Anime search is a plain series search, deliberately: Cinemeta's
    search rows come back with `genres: null` (measured on every search
    tried), so there is nothing to filter on and no genre-scoped search
    form to ask instead. Someone typing a title has already narrowed it
    further than a genre would."""
    kind = (kind or "").strip().lower()
    content_type = _KIND_TYPES.get(kind)
    if not content_type or limit <= 0:
        return []
    if deadline is None:
        deadline = net.deadline_in(VIDEO_BUDGET)

    query = (query or "").strip()
    genre = (genre or "").strip()
    label = _KIND_LABELS[kind]

    if query:
        encoded = urllib.parse.quote(query)
        urls = [f"{CINEMETA_URL}/catalog/{content_type}/top/search={encoded}.json"]
    elif genre:
        urls = [f"{CINEMETA_URL}/catalog/{content_type}/top/"
                f"genre={urllib.parse.quote(genre)}.json"]
    else:
        urls = _video_catalog_urls(kind, content_type)

    for index, url in enumerate(urls):
        body = _get_json(url, deadline, VIDEO_TIMEOUT)
        metas = (body or {}).get("metas") or []
        if not metas:
            continue
        # Only the last URL in the anime chain is unfiltered, so that is
        # the only one that needs filtering after the fact.
        if kind == "anime" and not query and index == len(urls) - 1 and len(urls) > 1:
            metas = [m for m in metas if _is_animated(m)]
        rows = [row for row in (_video_row(m, label) for m in metas) if row]
        if rows:
            return rows[:limit]
    return []


def _reading_title(attributes: dict) -> str:
    """The name to print for a manga.

    MangaDex's `title` is whatever language the entry was filed under,
    which for most of what gets tracked here is romaji - the most-followed
    manga on the site is filed as "Na Honjaman Level-Up", not "Solo
    Leveling". The English name is in altTitles, and **the first English
    altTitle is the right one**: measured over the 12 most-followed
    titles, first-wins gives Solo Leveling / My Dress-Up Darling /
    Frieren: Beyond Journey's End / Komi Can't Communicate, while
    last-wins gives Only I Level up / The Bisque Doll Is Falling In Love /
    Frieren: Remnants Of The Departed / Miss Komi Is Bad at
    Communication. Entries carry up to 5 English alt titles, so a dict
    update over them lands on the obscure one every time."""
    for alt in attributes.get("altTitles") or []:
        english = alt.get("en")
        if english:
            return str(english).strip()
    for name in (attributes.get("title") or {}).values():
        if name:
            return str(name).strip()
    return ""


def _reading_row(manga: dict):
    attributes = manga.get("attributes") or {}
    title = _reading_title(attributes)
    if not title:
        return None

    # fileName only exists because of includes[]=cover_art - without it
    # the relationship is an id and a type and nothing else. The .256.jpg
    # suffix is MangaDex's own thumbnailer: verified live, 200 and 40KB
    # against 318KB for the full-size original.
    poster = ""
    for rel in manga.get("relationships") or []:
        if rel.get("type") != "cover_art":
            continue
        file_name = (rel.get("attributes") or {}).get("fileName")
        if file_name:
            poster = f"{MANGADEX_COVERS}/{manga.get('id')}/{file_name}.256.jpg"
        break

    year = attributes.get("year")
    return {
        "title": title,
        "year": str(year) if year else "",
        "poster": poster,
        "imdb_id": "",
        "type": "Manga",
    }


def discover_reading(query: str = "", limit: int = 30, deadline=None) -> list:
    """Popular or searched manga. Rows are {title, year, poster,
    imdb_id, type}; imdb_id is always "" - MangaDex has no IMDb id and the
    field exists only so the video and reading rows are one shape.

    Both modes order by followedCount, which is the measured difference
    between a usable search and a useless one. MangaDex's default and its
    `order[relevance]=desc` both rank fan works above the series they are
    fan works *of*: searching "frieren" puts a funeral anthology and three
    doujinshi ahead of Frieren: Beyond Journey's End, and "bleach" leads
    with "Bleach 0". Ordering by follower count instead put the real
    series first for 5 of 6 titles tried (frieren, solo leveling, bleach,
    one piece, dandadan) against 3 of 6 for relevance - a doujinshi never
    outranks the work it derives from on followers."""
    if limit <= 0:
        return []
    if deadline is None:
        deadline = net.deadline_in(READING_BUDGET)
    step = net.step_timeout(deadline, READING_TIMEOUT)
    if step is None:
        return []

    query = (query or "").strip()
    count = min(int(limit), _MANGADEX_MAX_LIMIT)
    url = (f"{mangadex.BASE_URL}/manga?limit={count}&includes[]=cover_art"
           f"&order[followedCount]=desc")
    if query:
        # No content-rating filter on a typed search, on purpose. MangaDex
        # rates Berserk `erotica`, so the browse filter hides it outright -
        # measured: "berserk" filtered returns six Berserk-*named* isekai
        # and not Berserk, unfiltered returns Berserk first. Someone typing
        # a title has asked for that title; the filter below is for the
        # grid nobody asked for.
        url += f"&title={urllib.parse.quote(query)}"
    else:
        url += _BROWSE_RATINGS

    try:
        # mangadex._get rather than a local fetch, so this shares the one
        # ~0.35s inter-request throttle with the tracker's own per-entry
        # lookups. A second, independent pacer would let a Discover load
        # and a page refresh collide and get the whole app rate-limited -
        # which is exactly what the shared lock exists to prevent.
        body = mangadex._get(url, step)
    except Exception:
        return []

    rows = [row for row in (_reading_row(m) for m in (body or {}).get("data") or []) if row]
    return rows[:limit]


# ------------------------------------------------------------------
# reading genres - MangaDex tags, for the details pages' genre buttons

# tag name (lowered) -> tag id, filled once per session from /manga/tag.
# MangaDex filters browsing by tag *id* only; the names are what the
# details page shows and the user clicks.
_reading_tags = None


def _reading_tag_ids():
    global _reading_tags
    if _reading_tags is not None:
        return _reading_tags
    try:
        body = mangadex._get(f"{mangadex.BASE_URL}/manga/tag",
                             READING_TIMEOUT)
        tags = {}
        for row in (body or {}).get("data") or []:
            name = ((row.get("attributes") or {}).get("name") or {}).get("en")
            if name and row.get("id"):
                tags[str(name).strip().lower()] = str(row["id"])
        # Only a real answer is remembered - a flaky minute must not
        # blank every genre button for the rest of the session.
        if tags:
            _reading_tags = tags
        return tags
    except Exception:
        return {}


def reading_genres(title: str, limit: int = 6) -> list:
    """The genre tags MangaDex files this title under, or []. What the
    reading details page shows as its genre buttons - the video pages
    get theirs free with Cinemeta's meta, and this is the reading
    equivalent: one search, best follower-ordered match wins, tags of
    the `genre` group only (the theme/format groups hold entries like
    "Full Color" that are not genres)."""
    title = (title or "").strip()
    if not title:
        return []
    try:
        from . import title_match
        url = (f"{mangadex.BASE_URL}/manga?limit=5"
               f"&order[followedCount]=desc"
               f"&title={urllib.parse.quote(title)}")
        body = mangadex._get(url, READING_TIMEOUT)
        for manga in (body or {}).get("data") or []:
            attributes = manga.get("attributes") or {}
            if title_match.similarity(title, _reading_title(attributes)) < 0.85:
                continue
            names = []
            for tag in attributes.get("tags") or []:
                tag_attributes = tag.get("attributes") or {}
                if tag_attributes.get("group") != "genre":
                    continue
                name = (tag_attributes.get("name") or {}).get("en")
                if name:
                    names.append(str(name).strip())
            if names:
                return names[:limit]
        return []
    except Exception:
        return []


def discover_reading_genre(genre: str, limit: int = 30, deadline=None) -> list:
    """The most-followed manga filed under one genre tag - what a
    reading genre button opens. Same row shape as discover_reading;
    [] for an unknown tag or any failure."""
    tag_id = _reading_tag_ids().get((genre or "").strip().lower())
    if not tag_id or limit <= 0:
        return []
    if deadline is None:
        deadline = net.deadline_in(READING_BUDGET)
    step = net.step_timeout(deadline, READING_TIMEOUT)
    if step is None:
        return []
    count = min(int(limit), _MANGADEX_MAX_LIMIT)
    url = (f"{mangadex.BASE_URL}/manga?limit={count}&includes[]=cover_art"
           f"&order[followedCount]=desc"
           f"&includedTags[]={urllib.parse.quote(tag_id)}"
           + _BROWSE_RATINGS)
    try:
        body = mangadex._get(url, step)
    except Exception:
        return []
    rows = [row for row in (_reading_row(m)
                            for m in (body or {}).get("data") or []) if row]
    return rows[:limit]
