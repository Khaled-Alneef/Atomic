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

import concurrent.futures
import threading
import time
import json
import urllib.parse
import urllib.request

from . import mangadex, net, storage

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
# How many extra catalog pages a genre applied to the *rows* may walk -
# see discover_video's `local_genre`. Four pages is 200 rows scanned,
# which on the owner's connection is about a second.
LOCAL_GENRE_PAGES = 4
# ...and how long that walk may take, whatever it has found. Measured 4
# September 2026 on the owner's connection: Romance walked all four
# pages and the whole genre answer took **17.8s**, which is nothing like
# rule 7's second - a Cinemeta page is 0.3-10s depending on whether its
# CDN holds it. Four seconds returns what has been found and stops,
# which is the partial answer that rule asks for; Comedy fills its 50
# inside it and Horror runs the catalog out.
LOCAL_GENRE_BUDGET_S = 4.0
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
        with net.urlopen(req, timeout=step) as resp:
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


# How close a Cinemeta search row's title must sit to an AniList hit
# before the anime section will claim it. The same bar anilist.py's own
# schedule matching uses (_MATCH_THRESHOLD) - identity matching, where
# inheriting a different franchise's row is the failure to avoid.
_ANIME_CONFIRM_THRESHOLD = 0.8

# How long the anime section will hold for AniList's verdict once its own
# rows are in hand. It is started beside the Cinemeta request (see
# _anime_witness), so this is only ever the *remainder* - AniList's own
# leg measured 0.4-0.5s including the global throttle. Past this the
# section draws unfiltered rather than making the user wait, which is
# rule 7: show what there is inside a second.
ANIME_WITNESS_WAIT = 2.0


def _anime_witness(query: str):
    """Start AniList's "what anime is called this" lookup **now**, and
    return a handle to collect it from later.

    Beside the Cinemeta request rather than after it. Serial, this cost
    the anime section a whole extra round trip on the one path a person
    is watching - measured 30 August 2026: the AniList leg is ~0.4-0.5s
    on its own, and `anilist._MIN_REQUEST_GAP` is a *global* 0.7s
    throttle shared with the tracker's schedule lookups, so a search
    fired while the tracker was backfilling queued behind those as well.
    That is the owner's "when I search and press Enter the animes takes
    a while to show". The two requests contend for nothing (different
    hosts), so racing them makes the confirmation free whenever AniList
    answers inside Cinemeta's own time, which is the usual case."""
    import concurrent.futures
    try:
        from . import anilist
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="atomic-anime-witness")
        future = pool.submit(anilist.anime_name_years, query)
        pool.shutdown(wait=False)
        return future
    except Exception:
        return None


def _plain_title(text) -> str:
    """A title reduced to its words, for comparing one against another."""
    keep = [c.lower() if (c.isalnum() or c.isspace()) else " "
            for c in str(text or "")]
    return " ".join("".join(keep).split())


def _row_year(row) -> int:
    """The year a Cinemeta row begins, or 0. Its `year` is "2019-2020",
    "2015-" or "2012" depending on the shape of the title."""
    for field in ("year", "years", "releaseInfo"):
        text = str(row.get(field) or "")
        digits = "".join(c if c.isdigit() else " " for c in text).split()
        for chunk in digits:
            if len(chunk) == 4:
                return int(chunk)
    return 0


def _same_work(title: str, names) -> bool:
    """Is `title` one of `names`, rather than merely starting like one?

    **A ratio was not enough, and the number says why.** This used
    title_match.best_similarity against _ANIME_CONFIRM_THRESHOLD (0.8),
    which is a prefix-tolerant score - measured 2 September 2026 against
    AniList's own name set for "Kingdom":

        Kingdom              1.000     Animal Kingdom     0.667
        Kingdom of Heaven    0.950     The Kingdom        0.778
        The Last Kingdom     0.609

    so Ridley Scott's film scored 0.95 against the anime and was filed
    under Anime. That is the owner's "in the search the anime contains
    movies and series when I searched Kingdom". Any title that merely
    *contains* the one AniList knows scores high, and a search is
    exactly the case where every result contains the query.

    AniList's name set already carries the romaji, the english and the
    synonyms of one work, so a real match is an equal one - "Attack on
    Titan" and "Shingeki no Kyojin" are both in the set. Whole-title
    equality on the words alone is therefore strict without being
    brittle, and it is what separates a work from a phrase it shares a
    word with.
    """
    plain = _plain_title(title)
    if not plain:
        return False
    return any(_plain_title(name) == plain for name in names if name)


def _same_hit(title: str, year: int, hit) -> bool:
    """Is this Cinemeta row the AniList work `hit` describes?

    The title has to match whole (see _same_work) **and the years have to
    agree**, because a title alone does not identify a work: "Kingdom" is
    both a 2012 anime and a 2019 Korean live-action series, and matching
    on the name put the live-action one in the Anime section - the
    owner's "when I searched for kingdom, the animes did not show!!!
    instead there was only one series on the anime section", 2 September
    2026. Once the seasons collapsed to a single row it was the only
    thing left there.

    Three years of slack, and only when both sides state one: AniList
    dates a work by its first broadcast and Cinemeta by its IMDb entry,
    which disagree by a year on split cours and late licensing. An
    unknown year on either side falls back to the name alone, which is
    where this started but is still better than dropping a real hit.
    """
    if isinstance(hit, dict):
        names, anime_year = hit.get("names") or [], hit.get("year") or 0
    else:
        names, anime_year = hit, 0        # an older name-only answer
    if not _same_work(title, names):
        return False
    if not year or not anime_year:
        return True
    return abs(int(year) - int(anime_year)) <= 3


def _anime_confirmed(rows: list, query: str, witness=None) -> list:
    """Search rows narrowed to titles AniList also knows as anime.

    The anime section's search is a plain Cinemeta *series* search -
    search rows carry `genres: null` (measured, see discover_video), so
    Cinemeta alone cannot say which results are anime, and the owner's
    report followed: live-action series listed under Anime. AniList's
    search (type: ANIME, keyless) is the second witness: a row stays
    only when its title matches one of the anime AniList returns for the
    same query - or when the row itself carries an animated genre, which
    a search row does not today but costs nothing to honour.

    Fails soft to the *unfiltered* list: None from anime_name_sets means
    AniList could not be asked (rate limit, timeout), and emptying the
    section on silence would be worse than the mislabelling this fixes.
    An empty name-set list, though, is a real answer - a search for a
    live-action show correctly leaves the anime section empty.

    `witness` is an in-flight `_anime_witness` future when the caller
    started one beside its own request; without it the lookup runs here,
    which is what a direct caller (and every test) gets."""
    try:
        from . import anilist, title_match
        if witness is not None:
            # Bounded: the row list is already in hand, and holding the
            # section back past this for a confirmation is the very cost
            # the witness exists to remove. A timeout reads as silence,
            # which leaves the list unfiltered.
            try:
                name_sets = witness.result(timeout=ANIME_WITNESS_WAIT)
            except Exception:
                name_sets = None
        else:
            name_sets = anilist.anime_name_years(query)
        if name_sets is None:
            return rows
        kept = []
        for row in rows:
            if _is_animated(row):
                kept.append((row, 0))
                continue
            title = row.get("title") or ""
            year = _row_year(row)
            gaps = [abs(int(year) - int(hit.get("year") or 0))
                    if (year and isinstance(hit, dict) and hit.get("year"))
                    else 0
                    for hit in name_sets if hit and _same_hit(title, year, hit)]
            if gaps:
                kept.append((row, min(gaps)))
        # **Among rows naming one work, the year that matches leads.**
        # The owner, 2 September 2026: "the anime Kingdom is not having
        # the correct image cover". Measured that day on his search:
        # Cinemeta answers three series called "Kingdom" (2019 KR, 2014
        # US, 2012 JP), the witness rightly drops the Korean one, but the
        # three-year slack _same_hit needs for split cours also admits
        # the 2014 MMA drama, and it comes *first* in Cinemeta's order -
        # so server._one_per_work, which keeps the leader, showed the
        # anime under that show's poster. The rows stay in Cinemeta's
        # order between works; inside a work the smallest year gap
        # wins, so the exact match is the leader and the slack only
        # rescues a row that has no exact rival. Measured after: the
        # Anime section's "Kingdom" is tt2404499 (2012), 3 of 3 runs.
        lead = {}
        for row, gap in kept:
            key = _plain_title(row.get("title") or "")
            if key not in lead or gap < lead[key][1]:
                lead[key] = (row, gap)
        out, seen = [], set()
        for row, gap in kept:
            key = _plain_title(row.get("title") or "")
            leader = lead[key][0]
            if key not in seen:
                seen.add(key)
                out.append(leader)
                if leader is not row:
                    out.append(row)
            elif row is not leader:
                out.append(row)
        return out
    except Exception:
        return rows


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
        # For the category pages' genre filter - present on catalog
        # rows, absent on search rows (measured; see _is_animated).
        "genres": [str(g).strip() for g in
                   (meta.get("genres") or meta.get("genre") or [])
                   if str(g).strip()],
    }


def discover_video(kind: str, query: str = "", limit: int = 30, deadline=None,
                   genre: str = "", skip: int = 0, reached=None) -> list:
    """Popular or searched titles for one of the video trackers.

    `kind` is "anime", "series" or "movie". An empty `query` returns the
    popular catalog; a non-empty one searches; `genre` (with no query)
    asks the genre's own catalog - the details pages' genre buttons.
    Rows are {title, year, poster, imdb_id, type}. Always a list - [] on
    any failure, an unknown kind, or nothing found; an unknown genre is
    a fast empty answer, never an unfiltered list (measured, see
    _video_catalog_urls).

    `reached` is an optional dict this fills with `{"skip": n}` - how far
    into the catalog the call actually read, which for a genre applied to
    the *rows* is much further than the number of rows it answers with.
    Its caller needs that to page: see _genre_video.

    `skip` pages the browse catalog past its first rows - the category
    pages' load-on-scroll. Cinemeta's standard addon paging, measured
    22 August 2026: `top/skip=100.json` answers 50 fresh rows, a
    non-aligned `skip=30` answers row 31 onward, and the genre form
    combines as `genre=Anime&skip=49` (50 rows, continuing exactly
    where the 49-row first page ended). Ignored for a search - the
    search catalog is one page.

    Anime search is a plain series search at the Cinemeta end: its
    search rows come back with `genres: null` (measured on every search
    tried), so there is nothing to filter on and no genre-scoped search
    form to ask instead. What separates anime from the live-action rows
    sharing those results is a second witness - AniList's own search -
    applied after the fact by _anime_confirmed, failing soft to the
    unfiltered list when AniList cannot be asked."""
    kind = (kind or "").strip().lower()
    content_type = _KIND_TYPES.get(kind)
    if not content_type or limit <= 0:
        return []
    if deadline is None:
        deadline = net.deadline_in(VIDEO_BUDGET)

    query = (query or "").strip()
    genre = (genre or "").strip()
    # Set when the genre has to be applied to the rows instead of to the
    # catalog - see the `elif genre` branch below.
    local_genre = ""
    label = _KIND_LABELS[kind]
    skip = max(0, int(skip or 0))

    # Started before the first Cinemeta request, collected after the last
    # one - see _anime_witness for the measurement that put it here.
    witness = _anime_witness(query) if (kind == "anime" and query) else None

    if query:
        encoded = urllib.parse.quote(query)
        urls = [f"{CINEMETA_URL}/catalog/{content_type}/top/search={encoded}.json"]
    elif genre:
        # **Cinemeta takes one genre per catalog, and anime *is* a
        # genre here.** _video_catalog_urls explains why the anime
        # section asks for `genre=Anime` on the series catalog: there is
        # no anime content type, and the distinction exists only
        # server-side. So "anime AND romance" cannot be asked for at
        # all - and asking `genre=Romance` on the series catalog was
        # answering with plain live-action series that _KIND_LABELS then
        # stamped "Anime". Measured 4 September 2026 on the Anime page's
        # Romance tick: 50 rows, every one labelled Anime, headed by Off
        # Campus, My Life with the Walter Boys and Grey's Anatomy.
        #
        # So the anime catalog is asked and the wanted genre is applied
        # to the rows themselves. Their `genres` are real (a romance
        # anime lists Romance), and a row that names nothing is dropped
        # rather than guessed at - a wrong row on a genre page is worse
        # than a short one.
        if kind == "anime" and genre.strip().lower() != "anime":
            urls = _video_catalog_urls(kind, content_type)
            local_genre = genre
        else:
            urls = [f"{CINEMETA_URL}/catalog/{content_type}/top/"
                    f"genre={urllib.parse.quote(genre)}.json"]
    else:
        urls = _video_catalog_urls(kind, content_type)
    if skip and not query:
        # Into the extra-args segment each URL already carries (or a
        # fresh one), the addon-protocol way: ".../top/skip=30.json",
        # ".../top/genre=Anime&skip=49.json". Applied to every URL in
        # the anime fallback chain alike, so page 2 walks the same
        # chain page 1 did and the first catalog that answers wins.
        urls = [(u[:-5] + f"&skip={skip}.json" if "/top/" in u
                 else u[:-5] + f"/skip={skip}.json") for u in urls]

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
        if local_genre:
            wanted = local_genre.strip().lower()

            def _wanted(batch):
                return [r for r in batch
                        if any(str(g).strip().lower() == wanted
                               for g in (r.get("genres") or ()))]

            rows = _wanted(rows)
            # **A page of the anime catalog holds few of any one genre.**
            # Measured 4 September 2026: one page of 49 answered a single
            # Romance title. Cinemeta pages at 50, so a handful of pages
            # is walked until there are enough or the catalog runs out -
            # bounded, because this is a tick the user just pressed and a
            # thin answer is better than a slow one.
            page = skip
            walk_until = time.monotonic() + LOCAL_GENRE_BUDGET_S
            for _ in range(LOCAL_GENRE_PAGES):
                _note_reached(reached, page + (len(metas) or 50))
                if len(rows) >= limit or time.monotonic() > walk_until:
                    break
                if net.step_timeout(deadline, 1.0) is None:
                    break
                page += len(metas) or 50
                more = _get_json(
                    (url[:-5] + f"&skip={page}.json" if "/top/" in url
                     else url[:-5] + f"/skip={page}.json"),
                    deadline, VIDEO_TIMEOUT)
                metas = (more or {}).get("metas") or []
                if not metas:
                    break
                rows += _wanted(
                    [r for r in (_video_row(m, label) for m in metas) if r])
        # A searched anime section is a plain series search (see the
        # docstring), so a second witness separates anime from the
        # live-action rows sharing the results - see _anime_confirmed.
        if witness is not None and rows:
            rows = _anime_confirmed(rows, query, witness=witness)
        if not local_genre:
            _note_reached(reached, skip + len(metas))
        if rows:
            return rows[:limit]
    return []


def _note_reached(store, page):
    """Record the furthest catalog row a call has read past."""
    if isinstance(store, dict):
        store["skip"] = max(int(store.get("skip") or 0), int(page or 0))


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


def discover_reading_latest(limit: int = 30, deadline=None) -> list:
    """What landed recently - the "Latest" row on Read, and the schedule's
    **Recently Released** group, which are the same list.

    **From the owner's own sites, not MangaDex** (the owner's ask, 21
    August 2026: "take them from the source websites provided so that
    when clicked from the recently released it directly shows the ch
    list"). That is the whole point of the change, and it is not a
    cosmetic one: a MangaDex row carries a MangaDex id and no site url,
    so opening it landed on a details page with nothing to list and the
    question "where do you read this" all over again. A row browsed from
    a configured site carries `url`, `site_id` and `site_name`, and
    `tracker.discover_entry` passes those straight through - so the
    click opens that site's chapter list, first time, every time.

    What is given up, honestly: a scanlation site's listing is *its own
    newest-first order*, not a timestamped global one, so this is "new
    on your sites" rather than "new on the internet". MangaDex could
    order by real upload time and these cannot. That was the original
    reason for asking MangaDex, recorded here so it is not rediscovered
    as a bug - it was traded away deliberately, because a row that opens
    where you read is worth more than a row that is sorted perfectly and
    opens nowhere. It also means this row and Popular Now now draw on
    the same source; the sites' own order is what separates them.

    Fails soft to [] like its neighbours - a Latest row that cannot
    answer is a missing row, never an error."""
    if limit <= 0:
        return []
    rows = discover_reading_sites(query="", limit=limit * 2, deadline=deadline)
    # One row per title: two sites carrying the same series (Hunter X
    # Hunter sat at seven indexes of one cached sweep, 2 September 2026)
    # is one release, not seven - the first site to list it keeps it,
    # in its position.
    seen, unique = set(), []
    for row in _serving_sites_only(rows, deadline):
        key = " ".join(str(row.get("title") or "").lower().split())
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique[:limit]


# Which sites have been seen to actually hand over a chapter list, and
# when that was last established. Session-lived, with a TTL, keyed by
# site id.
# **Both caches below are written to disk.** They were session-only, and
# that was the difference between a cost paid once and a cost paid every
# launch: measured 22 August 2026, a cold category section took 36.4s
# (six site probes plus a MangaDex lookup for each of forty titles) and
# 1.9s once warm. Neither answer moves - a site either serves chapter
# lists or does not, and a series does not change which medium it is -
# so re-deriving them on every start was pure waste, and waste on the
# path the owner was waiting on.
_META_FILE = "reading_meta.json"
_meta_loaded = False

def _load_reading_meta():
    global _meta_loaded
    if _meta_loaded:
        return
    _meta_loaded = True
    try:
        saved = storage.load(_META_FILE, {}) or {}
    except Exception:
        saved = {}
    if not isinstance(saved, dict):
        return
    for title, medium in (saved.get("medium") or {}).items():
        _MEDIUM_CACHE.setdefault(str(title), str(medium))
    # A series does not change what it is filed under either, so these
    # keep forever like the medium verdicts beside them. An empty list
    # is a real answer - "MangaDex carries no genre tags for this" - and
    # is remembered, or every browse would re-ask about the same titles.
    for title, genres in (saved.get("genres") or {}).items():
        if isinstance(genres, list):
            _GENRE_CACHE.setdefault(str(title),
                                    [str(name) for name in genres])
    # Site verdicts keep their own age: a site that was refusing may have
    # been fixed, so these expire where the medium answers never do.
    now = time.time()
    for site_id, row in (saved.get("sites") or {}).items():
        try:
            stamp, ok = float(row[0]), bool(row[1])
        except Exception:
            continue
        if now - stamp < _SERVES_TTL_S:
            # Stored as wall-clock, read back as monotonic-relative, so
            # an entry written last week does not look like one written
            # a moment ago.
            _SERVES_CACHE[str(site_id)] = (
                time.monotonic() - (now - stamp), ok)


def _save_reading_meta():
    """Never raises - losing a cache is a slow next launch, not a bug."""
    try:
        now_wall, now_mono = time.time(), time.monotonic()
        storage.save(_META_FILE, {
            "medium": dict(_MEDIUM_CACHE),
            "genres": dict(_GENRE_CACHE),
            "sites": {site: [now_wall - (now_mono - stamp), ok]
                      for site, (stamp, ok) in _SERVES_CACHE.items()},
        })
    except Exception:
        pass


_SERVES_CACHE = {}
_SERVES_TTL_S = 6 * 60 * 60
# One probe gets this long. A site that refuses answers immediately (a
# 403 is one round trip); a site that works answered in 0.4-0.9s when
# this was measured. Anything past this is not worth a row.
_SERVES_PROBE_S = 8.0


def _serving_sites_only(rows, deadline=None):
    """`rows`, minus every row from a site that cannot actually serve a
    chapter list.

    **The owner's report, with a screenshot: a Recently Released row for
    "Celebrity Lady" that opens to no chapters at all.** It is not a
    fetch bug. Measured 21 August 2026: Mangalek browses and searches
    perfectly, and answers **403 to every series page** it publishes -
    six header shapes tried, both transports, so it is the site refusing
    non-browsers rather than anything this app does. A schedule row that
    cannot open is worse than a shorter schedule, which is the owner's
    own instruction here: "only show the ones in the websites provided
    so that it definitely will show the ch list".

    So each site is asked once - with one of its own rows, so the probe
    is a real chapter list rather than a guess - and the answer is kept
    for six hours. A site that has not been asked yet is *included*: the
    probe runs in the caller's thread and the first build should not
    hang behind six of them, so the cost is paid once and the list is
    right from then on.

    Never raises; a probe that blows up leaves the site included."""
    from . import chapter_source
    _load_reading_meta()
    by_site, order = {}, []
    for row in rows or []:
        site = row.get("site_id") or row.get("site_name") or ""
        if site not in by_site:
            by_site[site] = []
            order.append(site)
        by_site[site].append(row)

    now = time.monotonic()
    unknown = [s for s in order
               if not (_SERVES_CACHE.get(s)
                       and now - _SERVES_CACHE[s][0] < _SERVES_TTL_S)]

    def probe(site_id):
        sample = by_site.get(site_id) or []
        if not sample:
            return site_id, False
        entry = {"title": sample[0].get("title") or "",
                 "type": "Manga", "url": sample[0].get("url") or "",
                 "site_id": site_id}
        try:
            found = chapter_source._site_chapters(
                entry, net.deadline_in(_SERVES_PROBE_S), None)
        except Exception:
            found = []
        return site_id, bool(found)

    if unknown:
        # Together, not one after another - six sites serially is six
        # times the wait for an answer that is the same either way.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(unknown)) as pool:
            for site_id, ok in pool.map(probe, unknown):
                _SERVES_CACHE[site_id] = (time.monotonic(), ok)
        _save_reading_meta()

    kept = []
    for site in order:
        row = _SERVES_CACHE.get(site)
        if row is not None and not row[1]:
            continue        # asked, and it cannot serve one
        kept.extend(by_site[site])
    # Interleaved again, so dropping a site does not leave the list as
    # one site's catalogue in a block.
    out, index = [], 0
    while len(out) < len(kept):
        added = False
        for site in order:
            rows_for = [r for r in kept if (r.get("site_id")
                                            or r.get("site_name") or "") == site]
            if index < len(rows_for):
                out.append(rows_for[index])
                added = True
        if not added:
            break
        index += 1
    return out


# **Which original language is which medium.** This is the split the
# Read page's new sections ask for, and it is not a guess: MangaDex files
# every series under `originalLanguage`, AniList under `countryOfOrigin`
# and MangaUpdates under a plain `type` string, and all three agree on
# the same three buckets. Japanese is manga, Korean is manhwa, Chinese is
# manhua - that *is* the definition of the words, not a heuristic over
# them.
#
# "Other" is everything else MangaDex carries (English originals, French,
# Indonesian, Vietnamese...), which is a real category rather than a
# dumping ground - it is where an OEL webcomic honestly belongs.
MEDIUM_LANGUAGES = {
    "Manga": ("ja",),
    "Manhwa": ("ko",),
    "Manhua": ("zh", "zh-hk"),
}


# One title -> "Manga" / "Manhwa" / "Manhua" / "Other", cached for the
# session. A series does not change what it is, so this is asked once
# per title ever and then answered from memory.
_MEDIUM_CACHE = {}
# And one title -> the genre names MangaDex files it under, filled by
# the same search that answers the medium (see _classify). Kept beside
# it rather than in its own table because it is the same question asked
# of the same answer: one round trip per title, ever.
_GENRE_CACHE = {}
_MEDIUM_LOCK = threading.Lock()
# Titles are classified this many at a time. The catalogues answer in
# 0.3-1.5s each, so a screenful serially would be most of a minute.
MEDIUM_WORKERS = 8


def classify_medium(title: str, timeout: float = 8.0) -> str:
    """What medium `title` is, asked of the catalogues that actually
    record it.

    **MangaDex first**, because its `originalLanguage` is the field the
    answer is defined by - Japanese is manga, Korean is manhwa, Chinese
    is manhua - and one search returns it. **MangaUpdates second**, whose
    `type` field says the word outright and which knows scanlation
    titles MangaDex does not. AniList is not asked here: its
    countryOfOrigin agrees with both, and a third round trip for a title
    the first two already answered is a round trip for nothing.

    "Other" is the honest answer for a title neither knows - it is what
    the Read page's Other section is for - and it is cached like any
    other, so a title nobody carries is asked about once.

    Never raises."""
    return _classify(title, timeout)[0]


def classify_genres(title: str, timeout: float = 8.0) -> list:
    """The genre names MangaDex files `title` under, or [].

    Answered by the *same* search that answers the medium - it is one
    round trip and the tags are already in the reply - so the reading
    genre browse costs nothing on top of the category sections that ran
    before it (see reading_genre_sites). Never raises."""
    return _classify(title, timeout)[1]


def _classify(title: str, timeout: float = 8.0):
    """(medium, genres) for one title, cached forever.

    One MangaDex search answers both: `originalLanguage` is the medium
    and `tags` of the `genre` group are the genres. Asking twice was two
    round trips for one reply.

    **The genres are only kept for a title that genuinely matches**, at
    the same 0.85 bar `reading_genres` uses and for the same reason -
    inheriting another series' tags is the worst failure here, and the
    medium can survive a looser match (a near-miss is nearly always the
    same franchise, so the same language) where a genre list cannot."""
    title = (title or "").strip()
    if not title:
        return "Other", []
    key = title.lower()
    _load_reading_meta()
    with _MEDIUM_LOCK:
        if key in _MEDIUM_CACHE and key in _GENRE_CACHE:
            return _MEDIUM_CACHE[key], list(_GENRE_CACHE[key])
    medium, genres = "", None
    try:
        from . import title_match
        for manga in mangadex._search(title, int(timeout)) or []:
            attributes = manga.get("attributes") or {}
            language = str((attributes.get("originalLanguage") or "")).lower()
            if genres is None and title_match.similarity(
                    title, _reading_title(attributes)) >= 0.85:
                genres = []
                for tag in attributes.get("tags") or []:
                    tag_attributes = tag.get("attributes") or {}
                    if tag_attributes.get("group") != "genre":
                        continue
                    name = (tag_attributes.get("name") or {}).get("en")
                    if name and _allowed_genre(name):
                        genres.append(str(name).strip())
            if not medium:
                for name, codes in MEDIUM_LANGUAGES.items():
                    if language in codes:
                        medium = name
                        break
            if medium and genres is not None:
                break
    except Exception:
        pass
    if not medium:
        try:
            from . import mangaupdates
            medium = mangaupdates.fetch_medium(title, timeout) or ""
        except Exception:
            medium = ""
    medium = medium or "Other"
    genres = genres or []
    with _MEDIUM_LOCK:
        _MEDIUM_CACHE[key] = medium
        _GENRE_CACHE[key] = list(genres)
    return medium, list(genres)


# Genre tags never carried out of this module - the owner, 24 August
# 2026: "remove the Boys love category from the filter in all pages that
# have it!". MangaDex files it as a `genre`-group tag, so it reached the
# reading filters and the details page's genre chips alike; dropped here,
# at the one place tags are read, so no surface has to know about it.
# The aliases are the other spellings the same tag ships under.
BLOCKED_GENRES = frozenset({
    "boys' love", "boys love", "boyslove", "bl",
    "yaoi", "shounen ai", "shonen ai",
    "girls' love", "girls love", "yuri", "shoujo ai", "shojo ai",
})


def _allowed_genre(name) -> bool:
    return str(name or "").strip().lower() not in BLOCKED_GENRES


def cached_genres(title: str) -> list:
    """The genres already on record for `title` - cache only, no
    network, safe on the UI thread. The medium pages classify every row
    they show through `_classify`, which caches genres from the same
    reply, so by the time a reading category is on screen almost every
    row answers here instantly. [] is "not classified yet", which the
    genre filter treats as unknowable rather than as a mismatch."""
    key = (title or "").strip().lower()
    if not key:
        return []
    _load_reading_meta()
    with _MEDIUM_LOCK:
        # Filtered on the way out as well as on the way in: a cache
        # written before BLOCKED_GENRES existed still holds the tag, and
        # these entries keep forever by design.
        return [g for g in _GENRE_CACHE.get(key, []) if _allowed_genre(g)]


def reading_sites_by_medium_all(limit: int = 30, deadline=None) -> dict:
    """**Every** medium's rows in one pass - the four Read category
    sections, browsed and classified once between them.

    Asked per medium, this was the same work four times over: the same
    six-site browse, the same probe of each site, and the same
    classification sweep, thrown away three times because each call kept
    only one medium's rows. Measured on the owner's machine, 22 August
    2026 - one section cost 1.7-5.1s warm and 36.4s cold, and there are
    four of them.

    So the browse happens once and the verdicts are split four ways. The
    draw is wider than any one section will show because a mixed listing
    is mostly not the medium being asked for.

    Never raises; a medium nothing was found for comes back as []."""
    if limit <= 0:
        return {}
    names = list(MEDIUM_LANGUAGES) + ["Other"]
    empty = {name: [] for name in names}
    rows = discover_reading_sites(query="", limit=max(limit * 6, 60),
                                  deadline=deadline)
    # Same rule as Recently Released: a card that cannot open a chapter
    # list has no business being offered. Mangalek browses and searches
    # perfectly and 403s every series page it publishes, so without this
    # it fills these sections with rows that open to nothing.
    rows = _serving_sites_only(rows, deadline)
    if not rows:
        return empty
    titles = [row.get("title") or "" for row in rows]
    verdicts = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MEDIUM_WORKERS, max(1, len(titles)))) as pool:
        for title, found in zip(titles, pool.map(classify_medium, titles)):
            verdicts[title] = found
    _save_reading_meta()
    out = dict(empty)
    for row in rows:
        medium = verdicts.get(row.get("title") or "")
        if medium not in out or len(out[medium]) >= limit:
            continue
        row = dict(row)
        row["type"] = medium if medium in MEDIUM_LANGUAGES else "Manga"
        out[medium].append(row)
    return out


def reading_genre_cached(genre: str, limit: int = 30) -> list:
    """One genre's rows from what is already on disk - no network at
    all, so it answers in the milliseconds a page open is allowed
    (CLAUDE.md rule 7; the owner's ask, 23 August 2026: "the same genre
    page loading takes ~5-10 sec make it <500 ms").

    Where the seconds actually went, measured 23 August 2026: opening a
    reading genre paid the live six-site browse *every time* - 5.47s
    with a handful of uncached classifications, 2.28s with every cache
    warm, of which 2.26s was discover_reading_sites alone. But both
    halves of the answer were already sitting on disk: the category
    sections' browse rows in discover_cache.json (164 distinct titles
    on the owner's machine), and every title's genre verdict in
    reading_meta.json (161 of those 164), written by the same
    classification sweep the medium sections pay for. Filtering one
    against the other answered 41 rows for Action, 34 Fantasy, 30
    Drama - a full page - in ~0ms of network.

    Honest about staleness: these are last browse's rows, at most a day
    old (the discover cache's own disk TTL). [] when the caches cannot
    answer - a cold machine falls through to reading_genre_sites."""
    wanted = (genre or "").strip().lower()
    if not wanted or limit <= 0:
        return []
    _load_reading_meta()
    try:
        stored = storage.load("discover_cache.json", {}) or {}
    except Exception:
        return []
    if not isinstance(stored, dict):
        return []
    rows, seen = [], set()
    # The medium keys first: their rows already passed the serving-site
    # filter, so a card built from them opens a chapter list. The plain
    # browse keys widen coverage behind them.
    kinds = (["medium:%s" % name for name in (*MEDIUM_LANGUAGES, "Other")]
             + ["reading", "reading_latest"])
    for kind in kinds:
        entry = stored.get(kind)
        for row in (entry or {}).get("rows") or []:
            if not isinstance(row, dict):
                continue
            title = (row.get("title") or "").strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            rows.append(row)
    kept = []
    with _MEDIUM_LOCK:
        genre_verdicts = dict(_GENRE_CACHE)
        medium_verdicts = dict(_MEDIUM_CACHE)
    for row in rows:
        key = (row.get("title") or "").strip().lower()
        tags = genre_verdicts.get(key)
        if not tags or wanted not in (t.strip().lower() for t in tags):
            continue
        row = dict(row)
        medium = medium_verdicts.get(key, "")
        row["type"] = medium if medium in MEDIUM_LANGUAGES else "Manga"
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept


def reading_genre_sites(genre: str, limit: int = 30, deadline=None) -> list:
    """One genre's titles, **browsed from the owner's own sites**.

    The owner's ask, 22 August 2026: *"make the same generes page in
    read shows only from the settings websites (TeamX, SWAT, etc...)"*.
    It used to be MangaDex's own tag browse (`discover_reading_genre`,
    still here and still what a *video* page has no equivalent of), so
    every card on the page opened the "where should this be read from"
    flow instead of a chapter list - the same thing that was wrong with
    the Manga/Manhwa/Manhua sections before them.

    Same shape as reading_sites_by_medium_all, and deliberately the same
    browse: the rows come from manga_sites, the sites that cannot serve
    a chapter list are dropped, and the genre is looked up per title
    because no scanlation site records one. The lookup is the *same*
    MangaDex search the medium sections already paid for (see
    _classify), so on a machine that has opened Read once this costs no
    requests at all.

    No MangaDex fallback when a genre matches nothing here. That would
    put back exactly the rows the owner asked to be rid of; an honest
    empty page says the configured sites have nothing under that genre.
    """
    wanted = (genre or "").strip().lower()
    if not wanted or limit <= 0:
        return []
    rows = discover_reading_sites(query="", limit=max(limit * 6, 60),
                                  deadline=deadline)
    rows = _serving_sites_only(rows, deadline)
    if not rows:
        return []
    titles = [row.get("title") or "" for row in rows]
    tagged = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MEDIUM_WORKERS, max(1, len(titles)))) as pool:
        for title, found in zip(titles, pool.map(classify_genres, titles)):
            tagged[title] = {str(name).strip().lower() for name in found or []}
    _save_reading_meta()
    kept = []
    for row in rows:
        if wanted not in tagged.get(row.get("title") or "", ()):
            continue
        row = dict(row)
        row["type"] = classify_medium(row.get("title") or "")
        if row["type"] not in MEDIUM_LANGUAGES:
            row["type"] = "Manga"
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept


def discover_reading_sites(query: str = "", limit: int = 30,
                           deadline=None) -> list:
    """Reading rows from the user's **own** sites - what Discover shows
    now (the owner's ask: the four configured sites, not MangaDex).

    An empty query browses every site's current listing; a typed one
    searches them all. Rows carry `url`, `site_id` and `site_name` on
    top of the usual shape, which is the point: a card picked here opens
    that site's chapter list directly rather than asking again where to
    read it.

    MangaDex is not consulted at all. It remains the chapter-source
    fallback inside chapter_source, and answers the genre queries below
    - it is only no longer what fills the Discover grid.

    Rows are interleaved by site rather than concatenated, so the first
    screenful shows all four sites instead of thirty rows of whichever
    answered first."""
    from . import manga_sites
    if limit <= 0:
        return []
    if deadline is None:
        deadline = net.deadline_in(READING_BUDGET)
    query = (query or "").strip()
    try:
        if query:
            # Its own budget, not READING_TIMEOUT: this one runs while
            # the user is typing, and manga_sites bounds it to answer
            # early rather than waiting on the slowest of six sites.
            found = manga_sites.search_all(query)
        else:
            found = manga_sites.browse_all(limit=limit, deadline=deadline)
    except Exception:
        return []

    by_site = {}
    for row in found or []:
        if not (row.get("title") or "").strip():
            continue
        by_site.setdefault(row.get("site_name") or "", []).append(row)
    interleaved, index = [], 0
    while len(interleaved) < limit and by_site:
        for name in list(by_site):
            rows = by_site[name]
            if index < len(rows):
                interleaved.append(rows[index])
            if index >= len(rows) - 1:
                by_site.pop(name, None)
        index += 1

    # **A typed search is ranked by the title, not by whose turn it is.**
    #
    # The owner, 25 August 2026: *"the 3asq website manga do not appear
    # when searching for them in the discovery, like Kingdom (WAN) and
    # One piece and HxH"*. They did appear - just not where anyone would
    # look. The interleave above exists so a *browse* shows every site
    # rather than thirty rows of whichever answered first, and it was
    # being applied to searches too, so "One Piece" led with "One Piece
    # Special: Boichi Crossover" and "One Piece Strong World 0" and put
    # the actual One Piece fifth, while "Kingdom" buried "Kingdom" and
    # "Kingdom (WAN)" under "Eternal Kingdom" and "How I destroyed my
    # kingdom!".
    #
    # Ranked, not filtered: everything the sites answered is still here
    # and the interleave still decides ties, so no site loses its place
    # and nothing is thrown away on a guess about what the user meant.
    if query:
        from . import title_match
        wanted = title_match.normalize(query)

        def rank(row):
            raw = row.get("title") or ""
            name = title_match.normalize(raw)
            if name == wanted:
                # **The plainest of the exact matches first.** normalize
                # drops the parenthetical, so "One Piece (French)" and
                # "One Piece" both come back exact and the site's own
                # order decided between them - which put the French
                # edition above the manga. Length is what separates
                # them, and it keeps "Kingdom" ahead of "Kingdom (WAN)"
                # while leaving the two adjacent, which is the pair the
                # owner is choosing between.
                return (0, len(raw))
            if name.startswith(wanted):
                return (1, len(raw))
            if wanted in name:
                return (2, len(raw))
            return (3, len(raw))

        interleaved.sort(key=rank)      # stable: ties keep the interleave

    out = []
    for row in interleaved[:limit]:
        out.append({
            "title": row["title"].strip(),
            "year": "",
            "poster": row.get("cover_url") or "",
            "imdb_id": "",
            "type": "Manga",
            # What makes the card open chapters on the site it came from.
            "url": row.get("url") or "",
            "site_id": row.get("site_id"),
            "site_name": row.get("site_name") or "",
        })
    return out


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
        # Retried with the reading site's group tag stripped. Measured
        # 21 August 2026 on the owner's own entry: "Kingdom (WAN)"
        # returned [] in 0.39s while "Kingdom" returns Historical /
        # Action / Drama - the tag is 3asq's scanlation group, and
        # MangaDex takes the query string literally. The 0.85 match
        # below never needed loosening; normalize() already drops the
        # tag on both sides of the comparison.
        for query in title_match.search_variants(title):
            url = (f"{mangadex.BASE_URL}/manga?limit=5"
                   f"&order[followedCount]=desc"
                   f"&title={urllib.parse.quote(query)}")
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
                    if name and _allowed_genre(name):
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
