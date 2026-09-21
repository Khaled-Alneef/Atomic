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

AniList files every genre a work has. Its 2,000 most popular anime
(helpers/anime_genre_seed, 40 requests) matched **906** of those rows by
exact title and took Romance from 94 to **357** - Mystery 52 -> 155,
Sport 16 -> 42, Comedy 604 -> 709 - with no request at the moment of the
tick: it is a table shipped with the app, read once.

Deliberately conservative, because inheriting another show's genres is
the failure that matters (rules/integrations.md, the title-match
section): a title must equal an AniList English or romaji title, or a
synonym of eight characters or more that belongs to one work only, after
the same normalisation on both sides; only TV-shaped works are in the
table, since the anime catalogue is Cinemeta's series; and AniList's
genres are mapped onto the app's own vocabulary (server.WATCH_GENRES),
so a row gains Romance or Sport but never a tick the page does not offer.

No live refresh, on purpose. AniList's rate limit is the whole network's
(`anilist.RateLimited`), and a burst of forty requests here could cost
the airing schedule an hour of 403s. The seed is regenerated with
`.claude/skills/test/make_anime_genre_seed.py`; a row it does not know
keeps exactly the genres it had.
"""

import functools
import re
import unicodedata

_table = None


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


@functools.lru_cache(maxsize=8192)
def extra(title) -> tuple:
    """AniList's genres for `title`, in the app's vocabulary, or ().
    Cached: a genre filter asks this for every anime row in the index on
    every tick and scroll pull - 57.7ms over his 1,505 rows uncached."""
    key = norm(title)
    return _load().get(key, ()) if key else ()


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
