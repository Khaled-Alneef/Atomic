"""AniList's genres for an anime catalogue row, joined by exact title.

The owner, 21 September 2026: *"when I select a filter in the anime page
like Romance, it shows only few of the anime not all romance!"*. The
Anime page is Cinemeta's catalogue, and a Cinemeta row's genres are
IMDb's - three at most, and "Animation" is always one of them. Measured
that day against Cinemeta's own meta: of 13 well-known romance anime it
answered for, **6** carry Romance; Toradora!, Horimiya, Kaguya-sama,
Kimi ni Todoke, Ao Haru Ride, Tomo-chan and Snow White with the Red Hair
are "Animation / Comedy / Drama". Over the 1,505 anime rows in his
catalogue index, 94 said Romance.

AniList files every genre a work has. Its most popular anime
(helpers/anime_genre_seed) matched **906** of those rows by exact title
and took Romance from 94 to **357** - Mystery 52 -> 155, Sport 16 -> 42,
Comedy 604 -> 709 - with no request at the moment of the tick: it is a
table shipped with the app, read once.

**Every genre AniList files, and five of its tags** (15 October 2026,
the owner: "make it really accurate the genres", and "when I select some
filter like War in the anime page it takes ages to load the cards").
Slice of Life, Supernatural, Psychological, Mecha and Mahou Shoujo had
been dropped for not being ticks; and War, Crime, History, Western and
Family are AniList *tags*, not genres - his 2,175 anime rows held 2
tagged War, so the tick waited on a Cinemeta walk with nothing to find.
With the tags (at AniList's rank 60 and up, never a spoiler) War holds
77, Crime 146, History 72, Family 152, and the tick answers in 0.07s
where it took 2.03s.

Deliberately conservative, because inheriting another show's genres is
the failure that matters (rules/integrations.md, the title-match
section): a title must equal an AniList English or romaji title, or a
synonym of eight characters or more that belongs to one work only, after
the same normalisation on both sides; only TV-shaped works are in the
table, since the anime catalogue is Cinemeta's series.

The seed is regenerated with `.claude/skills/test/make_anime_genre_seed.py`;
a row it does not know keeps exactly the genres it had.
"""

import functools
import re
import threading
import time
import unicodedata

from .content_words import IMDB_TAG, RESTRICTED_GENRE, SUGGESTIVE_GENRE

_table = None
_restricted_years = None
# Names a row is never shown with: the restricted genre marks a row that
# is not shown at all (is_restricted), and the suggestive one is not a
# genre this app draws - not a filter tick, not a details page chip (the
# owner, 15 October 2026).
_NEVER_SHOWN = frozenset({RESTRICTED_GENRE, SUGGESTIVE_GENRE})


def norm(title) -> str:
    """The comparison form of a title: apostrophes dropped (AniList
    writes Frieren's with U+2019, Cinemeta with '), accents folded,
    everything else that is not a letter or digit a single space."""
    text = str(title or "")
    for mark in ("’", "‘", "'", "`"):
        text = text.replace(mark, "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _load():
    global _table
    if _table is None:
        try:
            from . import anime_genre_seed
            names = anime_genre_seed.GENRES
            _table = {key: tuple(names[int(i)] for i in value.split(",") if i)
                      for key, value in anime_genre_seed.TITLES.items()}
        except Exception:
            _table = {}
    return _table


def _years():
    global _restricted_years
    if _restricted_years is None:
        try:
            from . import anime_genre_seed
            _restricted_years = {
                key: {int(y) for y in value.split(",") if y}
                for key, value in getattr(anime_genre_seed, "RESTRICTED_YEARS", {}).items()}
        except Exception:
            _restricted_years = {}
    return _restricted_years


def _year_of(row):
    found = re.search(r"\d{4}", str((row or {}).get("year")
                                     or (row or {}).get("releaseInfo") or ""))
    return int(found.group()) if found else None


def _is_anime_row(row) -> bool:
    data = row or {}
    if str(data.get("type") or "").strip().lower() == "anime":
        return True
    return any(str(g).strip().lower() == "animation" for g in (data.get("genres") or ()))


# **Some anime is not in this app at all.** The owner, 15 October 2026:
# a title he met under his Romance tick was an explicit one - AniList
# files it under its restricted genre and its explicit flag, and the
# first seed kept its Romance and dropped the rest, so this module put it
# there. Every AniList work flagged explicit or filed under the
# restricted genre is in the seed now (all of them, not only the popular
# ones), and a row that is one is dropped where rows are made
# (discover._video_row, catalog_index.rows_for, server._row) rather than
# hidden where they are drawn.


def is_restricted(row) -> bool:
    """Whether `row` is one this app does not show, from its title, year
    and genres.

    By title alone this would be wrong both ways. A 1999 OVA filed under
    an English title "First Love" took Netflix's 2022 drama of that name,
    which his anime catalogue carries, and one called "Teacher's Pet"
    took Disney's 2000 cartoon (measured 15 October 2026) - so a row whose
    year is known must start in the work's own start year, exactly; every
    real one in his index (ten) matched to the year. A row with no year:
    an anime row goes, a series or film row stays, since a live-action
    show is not to be taken for an anime of its name. A details page
    AniList has answered for (`lookup`) adds its own flag and year."""
    data = row or {}
    # IMDb's own word first, through Cinemeta's genres: its tag for
    # exactly this (one row of his index carried it, 15 October 2026);
    # and a Family title is never one.
    tags = {str(g).strip().lower() for g in (data.get("genres") or ())}
    if IMDB_TAG.lower() in tags or RESTRICTED_GENRE.lower() in tags:
        return True
    if "family" in tags:
        return False
    key = norm(data.get("title") or data.get("name"))
    if not key:
        return False
    _disk()
    live = _live.get(key)
    live_flag = live is not None and live[1]
    if not live_flag and RESTRICTED_GENRE not in _load().get(key, ()):
        return False
    years = set(_years().get(key) or ())
    if live_flag and live[2]:
        years.add(int(live[2]))
    year = _year_of(data)
    if years and year:
        return year in years
    return _is_anime_row(data)


def keep(rows) -> list:
    """`rows` without the ones is_restricted refuses."""
    return [r for r in (rows or []) if not is_restricted(r)]


@functools.lru_cache(maxsize=8192)
def extra(title) -> tuple:
    """AniList's genres for `title`, or () - never one of _NEVER_SHOWN.
    Cached: a genre filter asks this for every anime row in the index on
    every tick and scroll pull - 57.7ms over his 1,505 rows uncached. A
    details page's own AniList answer (`lookup`) wins over the seed."""
    key = norm(title)
    if not key:
        return ()
    _disk()
    live = _live.get(key)
    found = live[0] if live is not None else _load().get(key, ())
    return tuple(g for g in found if g not in _NEVER_SHOWN)


def wanted_row(row, genre) -> bool:
    """Whether `row` belongs under a tick of `genre`: one of its genres,
    and never a restricted row."""
    if is_restricted(row):
        return False
    want = str(genre or "").strip().lower()
    return want in {str(g).strip().lower() for g in merged(row)}


def merged(row) -> list:
    """An anime row's genres with AniList's added after Cinemeta's own.
    Any other row, or one AniList does not know, is returned as it was."""
    names = list((row or {}).get("genres") or [])
    if str((row or {}).get("type") or "").strip().lower() != "anime":
        return names
    have = {str(g).strip().lower() for g in names}
    for genre in extra(str((row or {}).get("title") or "")):
        if genre.lower() not in have:
            have.add(genre.lower())
            names.append(genre)
    return names


# ---- one title's genres, asked of AniList ------------------------------
#
# **The details page shows the title's own AniList genres.** The owner,
# 15 October 2026: "make it really accurate the genres on each anime ep
# list page !!!! it is inaccurate or missing some now!". The page drew
# Cinemeta's - IMDb's three at most, "Animation" always one of them. The
# seed is keyed by title alone and unions every work of one name, so the
# page asks AniList for this one work: an exact title (English, romaji
# or a synonym), its start year within one of the card's, the closest
# year winning. One request a title, kept a month (a miss a day),
# through anilist._post's throttle and its 403 cooldown; while AniList
# refuses, the page keeps the seed's genres, or Cinemeta's. Measured on
# the frozen build: Death Note reads Mystery / Psychological /
# Supernatural / Thriller, exactly AniList's.
CACHE_FILE = "anime_genre_cache.json"
HIT_TTL_S = 30 * 24 * 3600
MISS_TTL_S = 24 * 3600
_FIELDS = "title { romaji english } synonyms genres format startDate { year } " \
          "tags { name rank isMediaSpoiler } " + "{flag}"
_RENAME = {"Sports": "Sport"}
# AniList tags that answer a watch-side genre of the app's - see the
# module docstring.
TAG_GENRES = {"War": "War", "Crime": "Crime", "Historical": "History",
              "Cowboys": "Western", "Kids": "Family"}
TAG_MIN_RANK = 60
_live = {}                  # norm title -> (genres, restricted, start year)
_cache = None
_cache_lock = threading.RLock()


def _query() -> str:
    from .content_words import ANILIST_FLAG
    return ("query ($search: String) { Page(page: 1, perPage: 15) { "
            "media(search: $search, type: ANIME, sort: [SEARCH_MATCH]) { "
            + _FIELDS.replace("{flag}", ANILIST_FLAG) + " } } }")


def work_genres(work) -> list:
    """One AniList work's genres in the app's words: AniList's genres,
    Sports spelt Sport, and the TAG_GENRES its strong tags earn."""
    names = [_RENAME.get(g, g) for g in (work or {}).get("genres") or ()]
    for tag in (work or {}).get("tags") or ():
        name = TAG_GENRES.get(tag.get("name"))
        if (name and name not in names and not tag.get("isMediaSpoiler")
                and int(tag.get("rank") or 0) >= TAG_MIN_RANK):
            names.append(name)
    return names


def _disk() -> dict:
    global _cache
    with _cache_lock:
        if _cache is None:
            try:
                from . import storage
                loaded = storage.load(CACHE_FILE, {})
                _cache = loaded if isinstance(loaded, dict) else {}
            except Exception:
                _cache = {}
            for record in list(_cache.values()):
                _learn(record)
        return _cache


def _learn(record):
    """Lets the filters and is_restricted hear what a details page learned."""
    try:
        if not isinstance(record, dict) or record.get("genres") is None:
            return
        key = norm(record.get("title"))
        if key:
            _live[key] = (tuple(record["genres"]), bool(record.get("restricted")),
                          record.get("start"))
            extra.cache_clear()
    except Exception:
        pass


def _cache_key(title, year) -> str:
    return f"{norm(title)}|{year or ''}"


def _fresh(record) -> bool:
    ttl = HIT_TTL_S if record.get("genres") is not None else MISS_TTL_S
    try:
        return time.time() - float(record.get("at") or 0) < ttl
    except (TypeError, ValueError):
        return False


def _pick(media, title, year):
    """The one work `title` (starting `year`) is, or None."""
    want = norm(title)
    exact = []
    for item in media or ():
        names = [(item.get("title") or {}).get("english"),
                 (item.get("title") or {}).get("romaji"), *(item.get("synonyms") or ())]
        if want and want in {norm(n) for n in names if n}:
            exact.append(item)
    if not exact:
        return None
    if not year:
        return exact[0]
    dated = [(abs(int((m.get("startDate") or {}).get("year") or 0) - year), n, m)
             for n, m in enumerate(exact) if (m.get("startDate") or {}).get("year")]
    dated = [d for d in dated if d[0] <= 1]
    return min(dated, key=lambda d: (d[0], d[1]))[2] if dated else None


def cached(title, year=None):
    """What is already known about this title, without the network:
    {"genres": [...], "exact": bool} - `exact` when it is this work's own
    AniList answer, not the seed's by title - or None."""
    record = _disk().get(_cache_key(title, year))
    if record and record.get("genres") is not None:
        return {"genres": [g for g in record["genres"] if g not in _NEVER_SHOWN],
                "exact": True}
    seed = extra(title)
    return {"genres": list(seed), "exact": False} if seed else None


def lookup(title, year=None, timeout: int = 8):
    """This title's own AniList genres - cached()'s shape - or None when
    AniList has no such work. Never raises."""
    try:
        from .content_words import ANILIST_FLAG
        title = str(title or "").strip()
        year = int(year) if year else None
        if not title:
            return None
        key = _cache_key(title, year)
        with _cache_lock:
            record = _disk().get(key)
        if record and _fresh(record):
            return cached(title, year) if record.get("genres") is not None else None
        from . import anilist
        try:
            body = anilist._post(_query(), {"search": title}, timeout)
        except Exception:
            # Refused or timed out: keep whatever was known, ask again later.
            return cached(title, year)
        media = (((body or {}).get("data") or {}).get("Page") or {}).get("media") or []
        work = _pick(media, title, year)
        record = {"at": time.time(), "title": title, "genres": None}
        if work is not None:
            genres = work_genres(work)
            record.update(
                genres=[g for g in genres if g not in _NEVER_SHOWN],
                restricted=bool(work.get(ANILIST_FLAG)) or RESTRICTED_GENRE in genres,
                start=(work.get("startDate") or {}).get("year"))
        with _cache_lock:
            _disk()[key] = record
            try:
                from . import storage
                storage.save(CACHE_FILE, dict(_cache))
            except Exception:
                pass
        _learn(record)
        return cached(title, year) if work is not None else None
    except Exception:
        return None
