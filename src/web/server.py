"""Atomic's Home and Discover, over HTTP, with no Qt in it.

Reads the same JSON files the app writes and hands them to the page as
plain rows. Nothing here is a view, and nothing here knows what a card
looks like.

Served over http:// deliberately. Every origin problem that cost days on
the QtWebEngine attempt - covers refused from an opaque origin, a page
the scheme handler could not answer - simply does not arise when a page
and its images share one ordinary origin.
"""

import json
import pathlib
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backend

DATA = backend.DATA_DIR
COVERS = DATA / "image_cache"

if getattr(sys, "frozen", False):
    # PyInstaller unpacks datas to _MEIPASS; __file__ points inside the
    # archive, where nothing can be opened.
    STATIC = pathlib.Path(sys._MEIPASS) / "static"
else:
    STATIC = pathlib.Path(__file__).resolve().parent / "static"

MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8"}

# What discover_cache.json calls each section, and what to call it on
# screen. The file is a *dictionary* keyed by these names, each holding
# {at, rows} - reading it as a list walks the keys instead, which are
# strings, so every row is filtered out and the page shows nothing
# against 1,287 cached titles. That was the bug.
DISCOVER_SECTIONS = (
    ("anime", "Anime"),
    ("series", "Series"),
    ("movie", "Movies"),
    ("reading_latest", "Latest chapters"),
    ("reading", "Reading"),
    ("medium:Manga", "Manga"),
    ("medium:Manhwa", "Manhwa"),
    ("medium:Manhua", "Manhua"),
    ("medium:Other", "Other"),
)

# Per section, not per page. The cache holds well over a thousand rows,
# and a strip that long is a thousand pictures fetched for a row nobody
# scrolls sideways.
DISCOVER_LIMIT = 60


def _rows(name):
    try:
        rows = json.loads((DATA / name).read_text(encoding="utf-8-sig"))
    except Exception:
        return []                # a lost list, never a lost page
    return [r for r in rows if isinstance(r, dict)]


def _history(kinds):
    """Watch or read history, newest first.

    series.json holds only what has been *saved* - three entries on his
    machine against 41 in History - so a page built from saved entries
    alone looks empty while he has plainly been watching things.
    """
    wanted = tuple(k.lower() for k in kinds)
    rows = [r for r in _rows("history.json")
            if str(r.get("type", "")).lower() in wanted]
    rows.sort(key=lambda r: str(r.get("last_opened") or ""), reverse=True)
    return rows


# Every title's watched marks, rebuilt only when history.json changes.
# _row is called once per card and a page can be five hundred of them, so
# reading the file per card would be five hundred reads of the same list.
_MARKS = {"at": None, "index": {}}


def _marks():
    try:
        stamp = (DATA / "history.json").stat().st_mtime
    except OSError:
        stamp = None
    if _MARKS["at"] != stamp:
        found = {}
        for row in _rows("history.json"):
            marks = [str(k) for k in (row.get("watched") or []) if k]
            if not marks:
                continue
            for key in (str(row.get("entry_id") or ""),
                        str(row.get("title") or "").strip().lower()):
                if key:
                    found[key] = marks
        _MARKS["index"] = found
        _MARKS["at"] = stamp
    return _MARKS["index"]


def _last_mark(marks):
    """The furthest episode or chapter actually ticked.

    The owner, 1 September 2026: "the ep num ... is the last marked as
    watched, the ch num is the same". The entry's own `progress` is
    where it was last *opened*, which is a different thing and drifts
    from the ticks as soon as anything is marked by hand - and a card
    that does not move when a chapter is marked reads as marking being
    broken, which is how this was reported.

    History writes episodes as `season:number` (history.episode_key) and
    chapters as `c<number>`, so the two are told apart by shape and
    compared as numbers - "c964" against "c1185" sorts the wrong way as
    text, and 1185 is the one he has read.
    """
    episodes, chapters = [], []
    for mark in marks:
        if ":" in mark:
            season, _, number = mark.partition(":")
            try:
                episodes.append((int(season), int(number)))
            except ValueError:
                pass
        elif mark[:1] in ("c", "C"):
            try:
                chapters.append(float(mark[1:]))
            except ValueError:
                pass
    if episodes:
        season, number = max(episodes)
        return f"S{season:02d}E{number:02d}"
    if chapters:
        return f"Ch {max(chapters):g}"
    return ""


def _marked_progress(entry):
    """This entry's furthest tick, or "" if it has none."""
    index = _marks()
    for key in (str(entry.get("id") or entry.get("entry_id") or ""),
                str(entry.get("title") or "").strip().lower()):
        if key and key in index:
            return _last_mark(index[key])
    return ""


def _progress_text(entry):
    """The number under a card: the furthest tick, else where it stopped.

    The fallback is written the same way the tick is. A reading entry's
    stored `progress` is a bare number, so a shelf mixed "Ch 846" for a
    title with marks against "247" for one without - the same fact in two
    formats, side by side on Home.
    """
    marked = _marked_progress(entry)
    if marked:
        return marked
    progress = str(entry.get("progress") or "").strip()
    if not progress:
        return ""
    reading = str(entry.get("type") or "").strip().lower() in (
        "manga", "manhwa", "manhua", "other")
    if reading and progress.replace(".", "", 1).isdigit():
        return f"Ch {progress}"
    return progress


def _year_text(entry):
    """This entry's year, with an open range closed - see
    backend.years_text, which is the one rule for every surface."""
    return backend.years_text(entry.get("year"))


def _row(entry, kind="title", resume=False):
    """One entry, as the page wants it.

    `kind` is what the app should *do* with a click, decided here where
    the source file is known rather than guessed at the far end. A game
    launches, an app or website opens its targets, everything else opens
    the details page - and getting that wrong sent a clicked game to
    another title's episode list.
    """
    # The tick wins over `progress`: see _last_mark for why they differ
    # and why the difference read as marking being broken.
    bits = [_year_text(entry), _progress_text(entry),
            str(entry.get("quality") or "").strip()]
    # Whether the cover offers a continue ring. Home gave every tracker
    # medium two targets (the owner's ask - anime, series and movies used
    # to be one); games, apps and websites have nothing to resume, and
    # neither does a Discover row, which is not in the library at all -
    # `resume` is opt-in per section for exactly that reason.
    return {
        "kind": kind,
        "resume": bool(resume),
        "id": str(entry.get("id") or entry.get("entry_id")
                  or entry.get("key") or ""),
        "title": str(entry.get("title") or entry.get("name") or "").strip(),
        "type": str(entry.get("type") or ""),
        "meta": "  ".join(b for b in bits if b)[:40],
        "cover": backend.cover_url(entry),
        "url": str(entry.get("url") or ""),
        "imdb": str(entry.get("imdb_id") or ""),
    }


def _saved_titles():
    """Every title already in the library, lowercased.

    The catalogue grid writes a saved row's meta line in ACCENT, which is
    the only mark on a card saying "you have this" - tracker._grid_record
    does the same test against the same three files.
    """
    found = set()
    for name in ("series.json", "tracker.json"):
        for entry in _rows(name):
            title = str(entry.get("title") or "").strip().lower()
            if title:
                found.add(title)
    return found


def _grid_row(entry, saved_titles):
    """One catalogue row, as tracker._grid_record builds it.

    The meta line is year and rating joined by two spaces (web_grid line
    649), the rating being "* 7.4" from a Cinemeta row's imdbRating to
    one decimal - "7.0" written ":g" renders "7", which reads as a
    different number. Reading rows carry neither and get a blank line,
    which is why .m has a min-height.
    """
    row = _row(entry)
    year = _year_text(entry)
    raw = entry.get("imdbRating")
    rating = ""
    if raw not in (None, ""):
        try:
            rating = f"★ {float(raw):.1f}"
        except (TypeError, ValueError):
            rating = f"★ {str(raw).strip()}" if str(raw).strip() else ""
    row["meta"] = "  ".join(part for part in (year, rating) if part)
    row["saved"] = row["title"].strip().lower() in saved_titles
    return row


# How many titles the banner rotates through. Home shows one at a time
# and pages between them; more than a handful is a pager nobody reads.
HERO_SLIDES = 6


def _heroes(candidates):
    """Every banner-worthy entry, in order, without repeats.

    Saved entries are asked before history, because hero_backdrop and
    hero_logo are written onto the saved entry - a history row for the
    same show has neither.
    """
    found, seen = [], set()
    for entry in candidates:
        hero = backend.hero_for(entry)
        if not hero:
            continue
        key = (hero.get("id") or hero.get("title") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(hero)
        if len(found) >= HERO_SLIDES:
            break
    # A cover-as-backdrop fallback lived here for a day, so that Movies
    # and Anime - whose entries carry no hero_backdrop - got a banner
    # too. Removed with the banners themselves; Home is back to drawing
    # exactly the art helpers/hero_art has written, and nothing else.
    return found


def _home():
    """Home: what he has saved, and nothing else.

    **Watching used to be watch history**, on the reasoning in _history
    that series.json holds only saved entries and a page built from them
    "looks empty while he has plainly been watching things". The owner
    settled it on 1 September 2026: "under Watching and Reading in the
    main page show only the saved ones not the history". So Watching is
    series.json filtered to the three video types, exactly as Reading has
    always been tracker.json - two rows of the library, not of activity.

    History is still read, but only to *order* them: the row he last
    opened leads, which is what made the history version feel right, and
    the banner follows the same order.
    """
    saved = _rows("series.json")
    reading = _rows("tracker.json")
    wanted = ("anime", "series", "movie")
    watching = [e for e in saved
                if str(e.get("type", "")).lower() in wanted]

    recent = {}
    for position, row in enumerate(_history(("Anime", "Series", "Movie"))):
        for key in (str(row.get("entry_id") or ""),
                    str(row.get("title") or "").strip().lower()):
            if key:
                recent.setdefault(key, position)

    def _last_opened(entry):
        """Where this entry sits in the history, or after everything."""
        for key in (str(entry.get("id") or ""),
                    str(entry.get("title") or "").strip().lower()):
            if key in recent:
                return recent[key]
        return len(recent) + 1

    watching.sort(key=_last_opened)

    # **The banner leads on whatever was opened last, watched or read.**
    # The owner, 2 September 2026: "make the banner in the home page
    # shows the latest watched/read on the most left point". This used
    # to be every watching entry followed by every reading one, so a
    # chapter read five minutes ago sat behind three shows last touched
    # in August - the medium decided the order, not the clock.
    #
    # Its own index, over every kind rather than the three video ones,
    # because a reading row is invisible to `recent` above and would
    # score "after everything" here too.
    seen_at = {}
    for position, row in enumerate(_history(VIDEO_KINDS + READING_KINDS)):
        for key in (str(row.get("entry_id") or ""),
                    str(row.get("title") or "").strip().lower()):
            if key:
                seen_at.setdefault(key, position)

    def _touched(entry):
        for key in (str(entry.get("id") or ""),
                    str(entry.get("title") or "").strip().lower()):
            if key in seen_at:
                return seen_at[key]
        return len(seen_at) + 1

    ordered = sorted(list(watching) + list(reading), key=_touched)

    def _linked(name, kind):
        """Apps and websites: what they open is the useful second line."""
        out = []
        for entry in _rows(name):
            row = _row(entry, kind)
            # What it opens lives in `targets`, a list of
            # {type, target} - not in a flat `url` field like every
            # other kind of entry here.
            target = ""
            for item in (entry.get("targets") or []):
                if isinstance(item, dict) and item.get("target"):
                    target = str(item["target"])
                    break
            # **No path under the name.** home._build_quick_list draws
            # the icon and the name and nothing else - the owner, 1
            # September 2026, with a screenshot of the Qt page: this is
            # "not at all what I wanted". What it *does* carry is the
            # red "Not found" when the target is gone, on the right.
            row["meta"] = ""
            row["url"] = target
            if kind == "app":
                gone = _missing_targets(entry)
                if gone:
                    row["missing"] = "Not found"
                    row["missing_paths"] = gone
            out.append(row)
        return out

    heroes = _heroes(ordered)
    return {"kind": "rows", "note": "",
            "hero": heroes[0] if heroes else None,
            "heroes": heroes, "sections": [
        {"title": "Watching",
         "rows": [_row(e, resume=True) for e in watching]},
        {"title": "Reading",
         "rows": [_row(e, resume=True) for e in reading]},
        {"title": "Games",
         "rows": [_row(e, "game") for e in _rows("games.json")]},
        # `style: list` - these two were a list of icon, name and link
        # in the Qt Home, not a shelf of posters, and a website has no
        # poster to show anyway.
        {"title": "Quick Apps", "style": "list",
         "rows": _linked("apps.json", "app")},
        {"title": "Websites", "style": "list",
         "rows": _linked("websites.json", "site")},
    ]}


# Which banner pool each discover section feeds, and how often each pool
# is drawn from. The owner's split, 2 September 2026: "30% movie, 30%
# series, 30% anime, 10% reading" - every reading section (the latest
# strip, the popular strip and the four medium strips) is one 10% pool
# together, not six pools, or reading would be drawn six times as often
# as he asked.
_BANNER_POOL = {"anime": "anime", "series": "series", "movie": "movie"}
_BANNER_WEIGHTS = (("movie", 30), ("series", 30), ("anime", 30),
                   ("reading", 10))


def _banner_pick(pools):
    """One discover row to draw the banner from, or None.

    A weighted draw over the mediums that actually have rows, then a
    uniform draw inside the chosen one restricted to rows carrying a
    poster - the banner is the poster, so a row without one would be a
    banner with no picture. A medium with no cached rows drops out and
    its weight is shared by the rest, so a cache holding only anime
    still gets a banner rather than a 70% chance of none."""
    import random
    choices = [(name, weight, [r for r in pools.get(name) or []
                               if r.get("poster") or r.get("cover_url")])
               for name, weight in _BANNER_WEIGHTS]
    choices = [(name, weight, rows) for name, weight, rows in choices if rows]
    if not choices:
        return None
    rows = random.choices([c[2] for c in choices],
                          weights=[c[1] for c in choices], k=1)[0]
    return random.choice(rows)


def _discover():
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        cached = {}
    if not isinstance(cached, dict):
        cached = {}

    sections, total, newest, banner = [], 0, 0.0, None
    pools = {}
    for key, label in DISCOVER_SECTIONS:
        block = cached.get(key)
        if not isinstance(block, dict):
            continue
        rows = [r for r in (block.get("rows") or [])
                if isinstance(r, dict) and r.get("title")]
        if not rows:
            continue
        total += len(rows)
        try:
            newest = max(newest, float(block.get("at") or 0))
        except (TypeError, ValueError):
            pass
        pools.setdefault(_BANNER_POOL.get(key, "reading"), []).extend(rows)
        sections.append({"title": f"{label}  ({len(rows)})",
                         "rows": [_row(e) for e in rows[:DISCOVER_LIMIT]]})

    # **A different banner every visit, weighted by medium.** The owner,
    # 2 September 2026: "make the banner in the discover changes when I
    # reload the page by going to other page then coming back, make it
    # like before it shows: 30% movie, 30% series, 30% anime, 10%
    # reading". It took rows[0] of the first cached section, which is
    # the anime block, so 300 builds of this page measured 300 times the
    # same anime title. Pages rebuild on every visit, so one weighted
    # draw per build is the whole mechanism - no state to keep. The
    # draw is over the mediums first and a row inside the chosen one
    # second, so a 198-row reading block does not outweigh a 30-row
    # movie block. Measured after, 300 builds on his cache: Anime 96 /
    # Movie 86 / Series 83 / Reading 35 (32/29/28/12%), 296 of 299
    # consecutive picks different, 117 distinct titles, and the sections
    # themselves byte-identical to before.
    first = _banner_pick(pools)
    if first is not None:
        # Discover rows carry no hero art - they are search results,
        # not saved entries - so the banner is the row's own poster,
        # widened and dimmed by the page. Better than no banner, and
        # honest about being the poster.
        picture = backend.cover_url(first)
        if picture:
            banner = {"title": str(first.get("title") or ""),
                      "imdb": str(first.get("imdb_id") or ""),
                      "type": str(first.get("type") or ""),
                      "url": str(first.get("url") or ""),
                      # The same picture twice on purpose: blurred and
                      # blown up as the wide backdrop, and sharp at its
                      # own size as the cover. A discover row has no
                      # separate wide art to use.
                      "backdrop": picture, "cover": picture,
                      "logo": "",
                      # **The same four lines Home's banner draws.** The
                      # owner, 2 September 2026: "make the discover page
                      # banner have the same". It had a single `meta`
                      # holding the year, which is also line 2 of the
                      # bullet run - so the year appeared twice and
                      # nothing else appeared at all. backend._bullets
                      # is the one method both banners now go through,
                      # which is what makes them the same by
                      # construction rather than by looking alike today.
                      "hide_title": False, "poster": True,
                      "meta": "",
                      "bullets": backend._bullets(first),
                      "id": ""}

    note = f"{total} titles in {len(sections)} sections"
    if newest:
        age = (time.time() - newest) / 3600.0
        note += f", found {age:.0f}h ago" if age >= 1 else ", found just now"
    if not sections:
        note = "nothing cached - run a discover in the app first"
    return {"kind": "rows", "sections": sections, "note": note, "hero": banner}


def _cached_posters():
    """Every reading poster the discover cache holds, by lowercased title."""
    found = {}
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return found
    if not isinstance(cached, dict):
        return found
    # **The reading sections, not every section.** This says "reading
    # poster" and read the whole cache - so a reading search row with no
    # cover of its own borrowed one from the *anime* block by title, and
    # a manga card came up wearing the anime's art. That is the owner's
    # "why is it showing anime under the reading when I searched Demon
    # Slayer", 2 September 2026: the rows were right and the pictures
    # were from the wrong medium.
    reading_blocks = ("reading", "reading_latest", "medium:Manga",
                      "medium:Manhwa", "medium:Manhua", "medium:Other")
    for key, block in cached.items():
        if key not in reading_blocks or not isinstance(block, dict):
            continue
        for row in (block.get("rows") or []):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip().lower()
            picture = row.get("poster") or row.get("cover_url") or ""
            if title and picture and title not in found:
                found[title] = picture
    return found


# **One row per work in a search, never one per season.** The owner, 2
# September 2026: "do NOT show each season separated on any condition in
# the search results". Cinemeta files every season as its own title, so
# "Kingdom" came back as five identical-looking cards and "Demon Slayer"
# as six - a list of the same show, where he was looking for the show.
#
# Only the *trailing* season marker is cut, and only from the end, so
# "Kingdom" and "Animal Kingdom" stay two works while "Kingdom 3rd
# Season", "Kingdom II" and "Kingdom S3" become one. Applied to the
# search alone: a catalogue page is a library and listing the seasons
# there is right.
_SEASON_TAIL = re.compile(
    r"\s*(?:[:\-–]\s*)?(?:"
    r"season\s*\d+|\d+(?:st|nd|rd|th)\s+season|part\s*\d+|"
    # \b before the bare numeral forms: without it "Taxi" lost its "xi"
    # and "The Matrix" its "ix" (review, 3 September 2026).
    r"cour\s*\d+|s\d{1,2}|\b[ivx]{1,4}|\b\d{1,2}"
    r")\s*$", re.IGNORECASE)


def _work_key(title):
    """A title with its season marker taken off, for grouping."""
    plain = " ".join(str(title or "").strip().lower().split())
    for _ in range(3):          # "kingdom 3rd season" -> "kingdom"
        cut = _SEASON_TAIL.sub("", plain).strip(" :-–")
        if not cut or cut == plain:
            break
        plain = cut
    return plain


def _one_per_work(rows):
    """The first row of each work, in the order they arrived.

    First rather than "best": Cinemeta returns its own relevance order
    and the season the search matched most closely leads it, so keeping
    the leader keeps the answer the source gave.
    """
    seen, out = set(), []
    for row in rows:
        key = _work_key(row.get("title"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


def _search(text):
    """Every source's results for one query, as sections.

    The window's search field sends Enter here through
    WebDiscoverPage.start_search - main._search_in_discover navigates to
    Discover and then calls `start_search`, and a page without that
    method silently showed the ordinary Discover instead, which is
    exactly what the owner reported.

    Anime, series, movies and reading are asked in parallel: serially
    this is the sum of four site searches, and one slow source would
    hold up every other.
    """
    text = str(text or "").strip()
    if not text:
        return {"kind": "rows", "sections": [], "note": ""}

    from concurrent.futures import ThreadPoolExecutor
    from helpers import discover as finder

    def video(kind):
        try:
            return finder.discover_video(kind, text, limit=30) or []
        except Exception:
            return []

    def reading():
        """His own reading sites, and only those.

        The owner, 1 September 2026: "in the search results, show only
        the available readings in the websites from the settings".
        discover_reading asks MangaDex, which knows every manga there is
        and cannot say whether *he* can open it - so a result was as
        likely to be a dead end as a title he could read.

        manga_sites.search_all asks the sites in Settings, in parallel
        and under one deadline, and returns only what they answered with.
        Its own docstring carries the same instruction from an earlier
        round: "only show the ones in the websites provided so that it
        definitely will show the ch list".
        """
        try:
            from helpers import manga_sites
            rows = manga_sites.search_all(text) or []
        except Exception:
            return []
        # **Only sites that can actually open a chapter list.**
        # discover._serving_sites_only exists for this and carries the
        # measurement: Mangalek browses and searches perfectly and
        # answers 403 to every series page it publishes. Its rows are
        # also the ones with no cover - seven of thirteen for "solo
        # leveling", every one of them Mangalek - so this removes the
        # blank cards and the dead ends in one pass. The owner's own
        # instruction, quoted in that function: "only show the ones in
        # the websites provided so that it definitely will show the ch
        # list".
        try:
            from helpers import discover as _finder
            rows = _finder._serving_sites_only(rows) or rows
        except Exception:
            pass
        # **A row with no cover borrows one.** Not every site puts a
        # picture on a search row - seven of thirteen came back without
        # one - and the discover cache already holds a thousand reading
        # rows with posters, indexed by title. Free, local, and it only
        # fills gaps.
        posters = _cached_posters()
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("title"):
                continue
            picture = (row.get("cover_url")
                       or posters.get(str(row.get("title") or "").strip().lower())
                       or "")
            out.append({"title": row.get("title"), "url": row.get("url") or "",
                        "cover_url": picture,
                        "site_id": row.get("site_id") or "",
                        "site_name": row.get("site_name") or "",
                        "type": "Manga", "imdb_id": "", "year": ""})
        return out[:30]

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {"Anime": pool.submit(video, "anime"),
                "Series": pool.submit(video, "series"),
                "Movies": pool.submit(video, "movie"),
                "Reading": pool.submit(reading)}
        found = {name: job.result() for name, job in jobs.items()}

    sections, total = [], 0
    for name in ("Anime", "Series", "Movies", "Reading"):
        rows = [r for r in found.get(name) or [] if isinstance(r, dict)
                and r.get("title")]
        # **Seasons collapse, sequels do not.** A trailing number on a
        # film is another film - review, 3 September 2026, on this
        # function's own key: "Iron Man 2" and "Iron Man 3" both became
        # "iron man" and a search for the series kept only the first
        # one Cinemeta returned. Only the Anime and Series sections list
        # one work as many rows.
        if name in ("Anime", "Series"):
            rows = _one_per_work(rows)
        if not rows:
            continue
        total += len(rows)
        sections.append({"title": f"{name}  ({len(rows)})",
                         "rows": [_row(e) for e in rows]})
    note = (f"{total} results for “{text}”" if total
            else f"nothing found for “{text}”")
    return {"kind": "rows", "sections": sections, "note": note, "hero": None}



# What each of the six catalogue rows reads, and out of which file. The
# app splits the same data the same way: series.json holds what is
# watched and tracker.json what is read.
SECTIONS = {
    "movies": ("series.json", ("Movie", "Movies")),
    "series": ("series.json", ("Series",)),
    "anime": ("series.json", ("Anime",)),
    "manga": ("tracker.json", ("Manga",)),
    "manhwa": ("tracker.json", ("Manhwa",)),
    "manhua": ("tracker.json", ("Manhua",)),
}

# Which discover-cache section holds each medium's catalogue. The cache
# paints instantly; the live call underneath it is a site search and
# takes seconds, so the page asks for that separately (rule 7).
BROWSE_CACHE = {
    "movies": "movie", "series": "series", "anime": "anime",
    "manga": "medium:Manga", "manhwa": "medium:Manhwa",
    "manhua": "medium:Manhua",
}
BROWSE_LIMIT = 60


def _cached_browse(key):
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    block = cached.get(key) if isinstance(cached, dict) else None
    if not isinstance(block, dict):
        return []
    return [r for r in (block.get("rows") or [])
            if isinstance(r, dict) and r.get("title")]


MEDIUM_TITLE = {"movies": "Movies", "series": "Series", "anime": "Anime",
                "manga": "Manga", "manhwa": "Manhwa", "manhua": "Manhua"}


def _category_note(route):
    """tracker._category_note, by the same words it uses."""
    key = BROWSE_CACHE.get(route, "")
    if key.startswith("medium:"):
        return f"Most followed {key.split(':', 1)[1].lower()}"
    return "Most watched"


def _medium(route):
    """One medium's page, laid out as the Qt page it replaces.

    The owner, 1 September 2026: "use the same design as before but with
    the WebView2". Before is a *catalogue grid* and nothing else - the
    page name small at the top, tracker._category_note under it ("Most
    watched", "Most followed manhwa"), then one wrapping grid of every
    row the browse cache holds. It carries no banner and no library
    sections; those were mine and are gone.

    The whole cache, uncapped, exactly as the Qt grid draws it - the card
    CSS carries `content-visibility`, so the rows below the fold cost
    nothing until they are scrolled to.
    """
    rows = _cached_browse(BROWSE_CACHE.get(route, ""))
    saved = _saved_titles()
    return {"kind": "grid", "hero": None, "browse": route,
            "title": MEDIUM_TITLE.get(route, route.title()),
            "note": _category_note(route),
            "rows": [_grid_row(e, saved) for e in rows]}


def _browse(route):
    """A live catalogue for one medium - a site sweep, so its own route.

    **tracker._fetch_browse_rows, not a fresh call of our own.** It is
    what the Qt page has always used: it writes the sweep into the same
    shared cache (memory and disk), fills all four `medium:` keys from
    the one sweep that produced them, and returns the *merged* list
    rather than the page it just fetched - so the depth already on
    screen is kept. Calling helpers.discover directly, as this did for a
    day, bypassed all three.

    Imported here rather than at module scope: tracker pulls in Qt, and
    run.py serves these same routes with no Qt in the process at all.
    """
    kind = BROWSE_CACHE.get(route, "")
    if not kind:
        return {"rows": []}
    try:
        from windows import tracker
        rows = tracker._fetch_browse_rows(kind) or []
    except Exception as error:
        return {"rows": [], "error": str(error)[:120]}
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    saved = _saved_titles()
    return {"rows": [_grid_row(e, saved) for e in rows]}



# ---- the shelves: Games, Apps, Websites ----------------------------
#
# windows/games.py and windows/link_grid.py are twins on purpose (the
# same SORT_OPTIONS, the same CARD_MARGINS, the same drag-reorder
# helper), so this is one route with a table rather than three.
#
# `art` is the difference that matters: a game draws its Steam poster at
# the full 160x216 card cover, an app or a website draws its icon square
# at 160x160 - link_grid's own words, "store icons are square originals,
# and cropping one to the movie tile's portrait would cut the sides off
# every logo".
SHELVES = {
    "games": {"file": "games.json", "title": "Games", "kind": "game",
              "shape": "poster", "noun": ("game", "games"),
              "sorts": ["Custom Order", "Name (A-Z)",
                        "Date Added (Newest)", "Last Played"],
              "stamp": "last_played"},
    "apps": {"file": "apps.json", "title": "Apps", "kind": "app",
             "shape": "square", "noun": ("app", "apps"),
             "sorts": ["Custom Order", "Name (A-Z)",
                       "Date Added (Newest)", "Last Used"],
             "stamp": "last_used"},
    "websites": {"file": "websites.json", "title": "Websites",
                 "kind": "site", "shape": "square",
                 "noun": ("website", "websites"),
                 "sorts": ["Custom Order", "Name (A-Z)",
                           "Date Added (Newest)", "Last Used"],
                 "stamp": "last_used"},
}


def _missing_targets(entry):
    """This entry's app targets that are no longer on disk.

    link_grid.missing_app_targets, by its own rule: app targets only. A
    "site" target is a URL and asking the same question of one means a
    network probe, which that function is explicit about not doing.
    Re-stated here rather than imported because link_grid pulls in Qt and
    run.py serves these routes with no Qt in the process.
    """
    found = []
    for target in (entry.get("targets") or []):
        if not isinstance(target, dict) or target.get("type") != "app":
            continue
        path = str(target.get("target") or "")
        if path and not pathlib.Path(path).exists():
            found.append(path)
    return found


def _shelf_row(entry, shelf):
    row = _row(entry, shelf["kind"])
    row["name"] = str(entry.get("name") or entry.get("title") or "").strip()
    row["title"] = row["name"]
    row["shape"] = shelf["shape"]
    row["cover"] = backend.cover_url(entry)
    row["meta"] = ""

    targets = [t for t in (entry.get("targets") or [])
               if isinstance(t, dict) and t.get("target")]
    missing = _missing_targets(entry)
    if missing:
        # "Not found" only when nothing on this card can launch - an
        # entry that opens three programs and lost one still works, and
        # saying otherwise would be wrong rather than cautious
        # (link_grid's own note).
        row["missing"] = ("Not found" if len(missing) >= len(targets)
                          else f"{len(missing)} of {len(targets)} not found")
        row["missing_paths"] = missing
    # What the page sorts by, so a sort never needs the server again.
    row["added_at"] = str(entry.get("added_at") or "")
    row["used_at"] = str(entry.get(shelf["stamp"]) or "")
    return row


def _shelf(route):
    shelf = SHELVES[route]
    entries = _rows(shelf["file"])
    return {"kind": "shelf", "shelf": route, "hero": None,
            "title": shelf["title"], "sorts": shelf["sorts"],
            "noun": list(shelf["noun"]),
            "rows": [_shelf_row(e, shelf) for e in entries],
            "note": ""}


def _downloads():
    """The queue as it stands, for a page that asks again every second.

    DownloadsPage polls on its own QTimer for the same reason: a download
    has no event to listen to, and a second is short enough that a
    progress bar reads as moving.
    """
    try:
        from helpers import downloads as queue
    except Exception as error:
        return {"kind": "downloads", "rows": [], "error": str(error)[:120]}
    try:
        jobs = list(queue.list_jobs() or [])          # newest first
    except Exception as error:
        return {"kind": "downloads", "rows": [], "error": str(error)[:120]}

    # downloads_page.STATE_TEXT, so the two pages call a state the same
    # thing in front of the user.
    names = {queue.QUEUED: "Queued", queue.RUNNING: "Downloading",
             queue.DONE: "Finished", queue.FAILED: "Failed",
             queue.CANCELLED: "Cancelled", queue.PAUSED: "Paused"}
    active = {queue.QUEUED, queue.RUNNING, queue.PAUSED}
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        state = str(job.get("state") or "")
        rows.append({
            "id": str(job.get("id") or ""),
            "title": str(job.get("label") or ""),
            "state": state,
            "state_text": names.get(state, state.title()),
            # `progress` is a fraction on the record; the bar wants a
            # percentage and nothing else should do that arithmetic.
            "percent": round(100.0 * float(job.get("progress") or 0.0), 1),
            "detail": str(job.get("detail") or "")[:160],
            "active": state in active,
            "can_pause": state in (queue.QUEUED, queue.RUNNING),
            "can_resume": state == queue.PAUSED,
        })
    return {"kind": "downloads", "title": "Downloads", "rows": rows,
            "folder": _download_folder()}


def _download_folder():
    """Where downloads land - downloads_page.saved_folder, without Qt.

    That module reads settings.json's "download_folder" and falls back to
    downloads.default_folder(); it is two lines, and importing it here
    would pull Qt into a server run.py starts without any.
    """
    try:
        stored = json.loads(
            (DATA / "settings.json").read_text(encoding="utf-8-sig"))
        folder = stored.get("download_folder") if isinstance(stored, dict) else None
        if folder:
            return str(folder)
    except Exception:
        pass
    try:
        from helpers import downloads as queue
        return str(queue.default_folder() or "")
    except Exception:
        return ""



# ---- Saved, History and Schedule -----------------------------------
#
# The window's bar opens these three (main.open_section), and they were
# header tabs on each tracker page before that. They had no web route at
# all, so set_active_section fell through and left the page on its own
# default - the owner's "the history/saved/schedule pages take me to the
# series page when I click on them".
VIDEO_KINDS = ("anime", "series", "movie")
READING_KINDS = ("manga", "manhwa", "manhua", "other")

# The two pills every one of these three pages opens with.
TABS = [{"key": "watch", "label": "Watch"}, {"key": "read", "label": "Read"}]

# A week, matching tracker._SCHEDULE_DISK_TTL_S: the calendar covers
# seven days, so even a day-old copy still names most of what is coming
# and _calendar_rows drops whatever has already aired.
CALENDAR_TTL_S = 7 * 24 * 60 * 60
# tracker.SCHEDULE_RELEASED_LIMIT - the Read side lists what landed, not
# the whole sweep.
RELEASED_LIMIT = 20


def _side_rows(side):
    """Everything saved on one side of the Watch/Read split."""
    if side == "read":
        return [e for e in _rows("tracker.json")
                if str(e.get("type", "")).lower() in READING_KINDS
                or not e.get("type")]
    return [e for e in _rows("series.json")
            if str(e.get("type", "")).lower() in VIDEO_KINDS]


def _status_card(entry, resume=True):
    """A saved card: the status under the title, the number under that.

    tracker's own card draws all three, and the number in ACCENT - which
    is the only colour on the card and the thing being looked for.
    """
    row = _row(entry, resume=resume)
    row["status"] = str(entry.get("status") or "").strip()
    row["progress"] = _progress_text(entry)
    return row


def _saved(side="watch"):
    """The library, grouped by status and then by medium."""
    side = side if side in ("watch", "read") else "watch"
    entries = _side_rows(side)
    groups = {}
    for entry in entries:
        status = str(entry.get("status") or "Saved").strip() or "Saved"
        medium = str(entry.get("type") or "").strip() or "Other"
        groups.setdefault((status, medium), []).append(entry)

    sections = []
    for (status, medium), rows in sorted(groups.items()):
        sections.append({
            "title": f"{status}  \u00b7  {medium}  ({len(rows)})",
            "rows": [_status_card(e) for e in rows]})
    total = sum(len(x["rows"]) for x in sections)
    return {"kind": "rows", "hero": None, "title": "Saved",
            "tabs": TABS, "tab": side, "cardstyle": "status",
            "sections": sections,
            "note": f"{total} saved" if total else "nothing saved yet"}


def _history_when(stamp):
    """"Just now" / "3h ago" / "12 Aug" - tracker._history_when, to the
    word. Relative near the present, absolute past a week, which is how
    a person reads "when did I watch this"."""
    from datetime import datetime, timezone
    text = str(stamp or "").strip()
    if not text:
        return ""
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    gap = datetime.now(timezone.utc) - when
    minutes = gap.total_seconds() / 60
    if minutes < 2:
        return "Just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    if minutes < 60 * 24:
        return f"{int(minutes // 60)}h ago"
    if gap.days < 7:
        return f"{gap.days}d ago"
    return when.astimezone().strftime("%d %b")


def _history_page(side="watch"):
    """Everything opened, newest first, on one side of the split.

    **A list of rows, not a strip of posters.** The owner, 2 September
    2026: "the history is not showing like it was in the Qt, make it the
    same design with keeping the WebView2". tracker._build_history_row is
    what it was: a 46x62 cover, the title with its progress under it, and
    on the right when it was opened over an "In Saved" / "Not saved" tag
    - that tag being the whole reason the section exists, since a title
    reached through Discover leaves a trace here and nowhere else.

    The header carries the count and Clear History, exactly as the Qt
    page's did.
    """
    side = side if side in ("watch", "read") else "watch"
    kinds = READING_KINDS if side == "read" else VIDEO_KINDS
    rows = [r for r in _rows("history.json")
            if str(r.get("type", "")).lower() in kinds]
    rows.sort(key=lambda r: str(r.get("last_opened") or ""), reverse=True)
    saved = _saved_titles()

    out = []
    for entry in rows:
        row = _row(entry)
        ticks = len(entry.get("watched") or ())
        reading = str(entry.get("type", "")).lower() in READING_KINDS
        word = "chapter" if reading else "episode"
        bits = [_progress_text(entry),
                (f"{ticks} {word}{'s' if ticks != 1 else ''} marked")
                if ticks else ""]
        row["meta"] = "  ·  ".join(b for b in bits if b)
        row["when"] = _history_when(entry.get("last_opened"))
        row["saved"] = str(entry.get("title") or "").strip().lower() in saved
        out.append(row)
    return {"kind": "history", "hero": None, "title": "History",
            "tabs": TABS, "tab": side, "rows": out,
            "note": (f"{len(out)} title{'s' if len(out) != 1 else ''}"
                     if out else
                     "Nothing here yet. Anything you play or read shows up "
                     "here - including titles you have not saved.")}


def _when_words(at):
    """"Tomorrow", "Today", or the weekday - the Qt page's own headings."""
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return "Scheduled", "", ""
    local = when.astimezone()
    today = datetime.now(timezone.utc).astimezone().date()
    days = (local.date() - today).days
    head = ("Today" if days == 0 else "Tomorrow" if days == 1
            else local.strftime("%A") if 0 < days < 7
            else local.strftime("%d %b"))
    slot = local.strftime("%A %I:%M %p").replace(" 0", " ")
    left = when - datetime.now(timezone.utc)
    total = int(left.total_seconds())
    if total < 0:
        countdown = "now"
    elif total < 3600:
        countdown = f"{total // 60}m"
    elif total < 86400:
        countdown = f"{total // 3600}h {(total % 3600) // 60}m"
    else:
        countdown = f"{total // 86400}d {(total % 86400) // 3600}h"
    return head, slot, countdown


def _calendar_rows():
    """The week's airing calendar, off disk.

    `schedule_cache.json` is written by tracker._save_upcoming_calendar
    and is the *only* thing that ever knew about a show the owner has not
    saved - AniList's 403 is why it exists at all (tracker's own note).
    Read here rather than fetched: this server holds no network budget,
    and tracker.prewarm_discover queues _fetch_upcoming_calendar at every
    launch whenever the in-memory copy is over 12h old - which is what
    keeps the file current now that the Qt Schedule tab, the other thing
    that used to refill it, is no longer built at all.
    """
    from datetime import datetime, timezone
    try:
        stored = json.loads(
            (DATA / "schedule_cache.json").read_text(encoding="utf-8-sig"))
        age = time.time() - float(stored["at"])
    except Exception:
        return []                    # unreadable, or no stamp on it
    if age < 0 or age > CALENDAR_TTL_S:
        return []
    now = datetime.now(timezone.utc)
    found = []
    for row in (stored.get("rows") or []):
        if not isinstance(row, dict) or not row.get("at"):
            continue
        try:
            when = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            continue                  # already out; the Qt page drops it too
        found.append((when.isoformat(), row))
    found.sort(key=lambda item: item[0])
    return found


def _released_rows():
    """The Read side's "Recently Released" - the same rows Discover's
    Latest strip draws, which is the one list the Qt page used here
    (tracker._fetch_released_rows shares its cache entry). No dates:
    nobody announces a scanlation, so these are what has *landed*."""
    try:
        cached = json.loads(
            (DATA / "discover_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    block = cached.get("reading_latest") if isinstance(cached, dict) else None
    rows = (block or {}).get("rows") if isinstance(block, dict) else None
    # **Reading kinds only, one row per title.** The owner, 2 September
    # 2026: "in the reading schedule there is HxH anime!!!!". Measured
    # that day: the row was 3asq's *manga* (its poster is the volume 35
    # cover, the url its chapter list) sitting first because the site's
    # front page leads with a popular-titles slider that
    # manga_sites._browse_html now skips - so the wrong picture was a
    # symptom of the wrong order, not of a video row. This guard is for
    # the day a video row does land in the block: nothing here can show
    # an episode, so a Watch kind is dropped rather than listed as a
    # chapter. Duplicates go too - the same cache held Hunter X Hunter
    # at seven indexes (a slider card and a wall card share a url, but
    # two sites do not), and a schedule listing one title twice reads as
    # two releases.
    out, seen = [], set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip().lower() in _WATCH_KINDS:
            continue
        key = " ".join(str(row.get("title") or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= RELEASED_LIMIT:
            break
    return out


_WATCH_KINDS = frozenset({"anime", "series", "movie", "movies", "video"})


def _schedule_row(entry, when="", saved=False):
    row = _row(entry)
    head, slot, countdown = _when_words(when) if when else ("", "", "")
    row.update({"day": head, "slot": slot, "countdown": countdown,
                "saved": bool(saved), "progress": _progress_text(entry)})
    return row


def _schedule(side="watch"):
    """What is due: everything saved first, then everything else.

    **Two groups, and every saved row above every unsaved one** - the
    owner's ask twice over, most recently 2 September 2026: "in the
    schedule make all appear and the saved on top like the old Qt
    design". tracker._schedule_rows is explicit that this is not one
    sorted list with a tiebreak, because a tiebreak only orders within a
    shared clock slot.

    It showed one row before this, because it read `next_release` off the
    saved files and nothing else - and `next_release` is only ever
    written onto a title the owner keeps. Everything the Qt page listed
    under "Airing Soon" comes from the calendar cache, which this never
    opened (see _calendar_rows), so 40 rows were sitting on disk unread.
    """
    side = side if side in ("watch", "read") else "watch"
    saved_entries = _side_rows(side)
    saved_titles = {str(e.get("title") or "").strip().lower()
                    for e in saved_entries}

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    mine = []
    for entry in saved_entries:
        block = entry.get("next_release")
        at = str((block or {}).get("at") or "") if isinstance(block, dict) else ""
        if not at:
            continue
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= now:
            continue
        mine.append((when.isoformat(), entry))
    mine.sort(key=lambda item: item[0])
    saved_rows = [_schedule_row(entry, when, saved=True)
                  for when, entry in mine]

    rest = []
    if side == "read":
        for item in _released_rows():
            title = str(item.get("title") or "").strip()
            if not title or title.lower() in saved_titles:
                continue
            row = dict(item)
            row.setdefault("cover_url", item.get("poster") or "")
            rest.append(_schedule_row(row))
    else:
        for when, item in _calendar_rows():
            title = str(item.get("title") or "").strip()
            if not title or title.lower() in saved_titles:
                continue
            row = dict(item)
            episode = str(item.get("episode") or "").strip()
            if episode:
                row["progress"] = f"E{episode}"
            rest.append(_schedule_row(row, when))

    blocks = []
    if saved_rows:
        blocks.append({"title": "Saved",
                       "note": "Upcoming releases from your Saved list",
                       "rows": saved_rows})
    if rest:
        blocks.append({"title": ("Recently Released" if side == "read"
                                 else "Airing Soon"),
                       "note": ("The newest chapters from your sites"
                                if side == "read"
                                else "Everything else airing this week"),
                       "rows": rest})
    total = len(saved_rows) + len(rest)
    return {"kind": "schedule", "hero": None, "title": "Schedule",
            "tabs": TABS, "tab": side, "blocks": blocks,
            "note": (f"{len(saved_rows)} saved  ·  {len(rest)} more"
                     if total else "nothing scheduled")}


# How many rows one "load more" asks for. tracker._category_more_worker
# passes its own DISCOVER_LIMIT (30) for both halves of this, and the
# measurement quoted below was taken with that number - so this is 30 and
# deliberately *not* this module's DISCOVER_LIMIT, which is 60 because it
# caps a Discover *section* rather than a page.
#
# **It went missing, and that is worth recording.** A block of this file
# was duplicated - eleven functions defined twice, the stale copy
# shadowing the newer one - and removing the duplicate took this constant
# with it, because it lived only in the removed half. The check
# afterwards looked for names defined *twice* and never for names that
# had become undefined, so `_more` raised NameError on every call and
# answered `{"rows": [], "error": ...}`. The page then counted two dry
# batches and stopped asking: the owner's "the watch and read pages do
# not load when scrolling down", twice.
MORE_LIMIT = 30


def _more(route, have, skip):
    """The next batch past what is already on screen.

    tracker._category_more_worker's own split, and its measurement:
    "Video kinds page Cinemeta by `skip`, the *source's* cursor, not the
    screen's row count - the two drift apart and paging by the screen
    count is what killed the Series section. The reading kinds have no
    offset to ask for: the sweep is re-run wider (limit = `have` + one
    page) and the overlap is dropped by title" - +29 new rows for Manhwa,
    +25 Other, +11 Manhua, +3 Manga on his own sites.
    """
    if route.startswith(("genre:", "cast:")):
        return _more_browse(route, have, skip)
    kind = BROWSE_CACHE.get(route, "")
    if not kind:
        return {"rows": [], "skip": skip}
    try:
        from helpers import discover
        if kind.startswith("medium:"):
            medium = kind.split(":", 1)[1]
            found = discover.reading_sites_by_medium_all(limit=have + MORE_LIMIT)
            rows = (found or {}).get(medium) or []
        else:
            video = "movie" if route == "movies" else route
            rows = discover.discover_video(video, query="", limit=MORE_LIMIT,
                                           skip=skip)
    except Exception as error:
        return {"rows": [], "skip": skip, "error": str(error)[:120]}
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]
    saved = _saved_titles()
    return {"rows": [_grid_row(r, saved) for r in rows],
            "skip": skip + len(rows)}


# One Cinemeta genre page is 50 rows (measured 2 September 2026: a
# genre catalog asked for 60 answered 50, series and movies alike), so a
# scroll batch asks for exactly one page per kind rather than MORE_LIMIT
# - a 30-row slice of a 50-row page is the same request paid twice.
GENRE_PAGE = 50
# The first cast page. TMDB answers a whole filmography in one call
# (62 credits for Alan Ritchson, measured), so this is a slice of memory
# and the size is only how much the page draws before its first scroll.
CAST_PAGE = 60


def _genre_video(name, skip, limit):
    """One page of a video genre, series and movies both, from one
    cursor: (rows, next cursor).

    **One skip for both kinds, advanced by the larger batch.** Two
    cursors packed into the one integer the page hands back was the
    alternative, and it buys nothing: both catalogs page from 0 in
    lockstep while they last, and once the shorter one is spent every
    later skip is past its end and answers [], so nothing is ever served
    twice and the cursor never has to know which of the two ran out.
    Dry when both answer nothing.

    **Both catalogs at once, not one after the other.** Measured 3
    September 2026 on Action: a Cinemeta genre page is 0.3-1.0s when its
    CDN has it and 2-10s when it does not, and the serial version paid
    both in a row - 9.9s for the skip=50 batch, 5.1s for skip=100, 6.6s
    for the empty answer past the end. Two workers make a batch cost its
    slower page rather than the sum: per-kind pages measured serially
    at skips 250-400 summed to 4.3 / 1.4 / 1.3 / 3.3s, while the
    parallel batch (skips 750-900, so not the same pages - Cinemeta's
    jitter is per request) took 1.9 / 1.1 / 2.9 / 1.2s of wall, and the
    dry answer past the end 0.44s. The page stops after two dry
    batches (app.js moreOnScroll), so the end costs under a second."""
    from concurrent.futures import ThreadPoolExecutor
    from helpers import discover

    def _page(kind):
        try:
            return discover.discover_video(kind, genre=name, limit=limit,
                                           skip=skip) or []
        except Exception:
            return []

    rows, advanced = [], 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        for batch in pool.map(_page, ("series", "movie")):
            rows.extend(batch)
            advanced = max(advanced, len(batch))
    return rows, skip + advanced


def _more_browse(route, have, skip):
    """The next batch of a genre or cast page - the owner, 2 September
    2026: "make the same genres/cast member pages load when I scroll
    down until there is no more to load at all". Before this the genre
    page answered no `browse` key at all, so the page never asked (100
    rows of Action, then nothing, measured)."""
    try:
        if route.startswith("cast:"):
            from helpers import people
            rows = people.page(route[len("cast:"):], skip, MORE_LIMIT)
            skip = skip + len(rows)
        else:
            body, _sep, flag = route[len("genre:"):].rpartition(":")
            if flag == "1":
                # No cursor at the reading end: the sweep is re-run wider
                # and the page drops what it already shows by title,
                # exactly as _more does for the medium pages.
                from helpers import discover
                rows = discover.reading_genre_sites(body,
                                                    limit=have + MORE_LIMIT)
                skip = skip + len(rows)
            else:
                rows, skip = _genre_video(body, skip, GENRE_PAGE)
    except Exception as error:
        return {"rows": [], "skip": skip, "error": str(error)[:120]}
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("title")]
    saved = _saved_titles()
    return {"rows": [_grid_row(r, saved) for r in rows], "skip": skip}


def _cast(name):
    """Everything one cast member was in - the cast chips as doors, the
    owner's ask of 2 September 2026 ("make the cast also clickable as
    the genres"). helpers.people asks TMDB and keeps the whole list for
    the session; this draws the first CAST_PAGE and `browse` lets the
    page scroll for the rest."""
    name = str(name or "").strip()
    if not name:
        return {"kind": "grid", "rows": [], "title": "", "note": ""}
    try:
        from helpers import people
        rows, note = people.filmography(name)
    except Exception as error:
        rows, note = [], str(error)[:120]
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    first = rows[:CAST_PAGE]
    saved = _saved_titles()
    return {"kind": "grid", "hero": None, "title": name,
            "browse": f"cast:{name}", "skip": len(first),
            "note": (f"{len(rows)} titles" if rows
                     else (note or "nothing under this name")),
            "rows": [_grid_row(r, saved) for r in first]}


def _genre(name, reading):
    """Everything filed under one genre - the genre browse, as a grid.

    Same sources details.GenreBrowsePage asks: reading genres come from
    the owner's *own* sites (discover.reading_genre_sites, the browse the
    Manga/Manhwa/Manhua sections use) rather than MangaDex's tag browse,
    whose cards carry no site url and can only ever open the "where
    should this be read from" flow. Video genres come from Cinemeta's
    genre catalogs, series and movies both.
    """
    name = str(name or "").strip()
    if not name:
        return {"kind": "grid", "rows": [], "title": "", "note": ""}
    rows, skip = [], 0
    try:
        from helpers import discover
        if reading:
            try:
                rows = list(discover.reading_genre_cached(name, limit=120) or [])
            except Exception:
                rows = []
            if not rows:
                rows = list(discover.reading_genre_sites(name, limit=120) or [])
        else:
            rows, skip = _genre_video(name, 0, GENRE_PAGE)
    except Exception as error:
        return {"kind": "grid", "rows": [], "title": name,
                "note": str(error)[:120]}
    rows = [r for r in rows if isinstance(r, dict) and r.get("title")]
    saved = _saved_titles()
    # `browse` is what makes the page ask for more on scroll (app.js
    # moreOnScroll), and `skip` is the source's cursor after this first
    # page - not the row count, which for a video genre is two catalogs'
    # worth. _more_browse continues from it.
    return {"kind": "grid", "hero": None, "title": name,
            "browse": f"genre:{name}:{'1' if reading else '0'}",
            "skip": skip if not reading else len(rows),
            "note": f"{len(rows)} titles" if rows else "nothing under this one",
            "rows": [_grid_row(r, saved) for r in rows]}


def answer(route, query=None):
    query = query or {}
    one = lambda k, d="": (query.get(k) or [d])[0]      # noqa: E731

    if route in SECTIONS:
        return _medium(route)
    if route == "genre":
        return _genre(one("name"), one("reading") == "1")
    if route == "cast":
        return _cast(one("name"))
    if route == "saved":
        return _saved(one("tab", "watch"))
    if route == "history":
        return _history_page(one("tab", "watch"))
    if route == "schedule":
        return _schedule(one("tab", "watch"))
    if route in SHELVES:
        return _shelf(route)
    if route == "downloads":
        return _downloads()
    if route == "more":
        def _int(name):
            try:
                return int(one(name, "0") or 0)
            except ValueError:
                return 0
        return _more(one("medium"), _int("have"), _int("skip"))
    if route == "browse":
        return _browse(one("medium"))
    if route == "home":
        return _home()
    if route == "discover":
        return _discover()
    if route == "featured":
        return backend.featured_art(one("title"), one("imdb"),
                                    one("type"))
    if route == "search":
        return _search(one("q"))
    if route == "settings":
        return backend.settings()
    if route == "hero":
        return backend.hero_meta(one("id"))
    if route == "chapters":
        return backend.chapters(one("id"), live=one("live") == "1")
    if route == "pages":
        try:
            index = int(one("i", "0"))
        except ValueError:
            index = 0
        return backend.pages(one("id"), index)
    if route == "read_state":
        return backend.read_state(one("id"))
    if route == "mark":
        return backend.mark_read(one("id"), one("key"),
                                 one("read", "1") == "1")
    return {"kind": "rows", "sections": [], "note": "not built yet"}



# **Card art, decoded at the size it is drawn.** The owner: "reading card
# covers still blurry". They are not upscaled - measured, his are 844x1200
# and 764x1200 - they are downscaled by four, in the browser, on every
# paint. Qt never does that: images.thumbnail_or_avatar decodes at the
# target size and hands Qt a pixmap that needs no scaling.
#
# So the server scales instead, once, and caches the result. Through
# QImage because it is already in the process and reads every format the
# covers come in; a build without Qt (run.py serves these same routes)
# simply gets the original, which is what happened before this existed.
_SCALED = {}
_SCALED_CAP = 400


def _wanted_width(query):
    """The `w=` a card asks for, in device pixels, or 0 for the original."""
    try:
        return max(0, min(2000, int((query.get("w") or ["0"])[0])))
    except (TypeError, ValueError):
        return 0


def _remember_scaled(key, value):
    """**Evict the oldest, never refuse the newest.** This used to stop
    scaling altogether once the dict held 400 entries, and a session
    crosses 400 easily - measured 3 September 2026: one Discover answer
    emits 270 distinct cover URLs, Home 32, a catalogue page 60 - after
    which every later picture went out at its original size (780x439
    originals average 382KB against 85KB scaled) and the browser
    downscaled it on every paint: the blurry-cover bug this cache exists
    to fix, back for the rest of the session. The dict is insertion
    ordered, so the oldest entry is the first key."""
    while len(_SCALED) >= _SCALED_CAP:
        try:
            del _SCALED[next(iter(_SCALED))]
        except (StopIteration, KeyError, RuntimeError):
            break
    _SCALED[key] = value


def _scaled(blob, kind, width):
    if not blob or width <= 0:
        return blob, kind
    key = (hash(blob), width)
    hit = _SCALED.get(key)
    if hit is not None:
        return hit
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtGui import QImage
        image = QImage()
        if not image.loadFromData(blob):
            return blob, kind
        if image.width() <= width * 1.15:
            # Already about the right size - scaling it would only cost
            # a decode and lose a little.
            _remember_scaled(key, (blob, kind))
            return blob, kind
        from PyQt6.QtCore import Qt as _Qt
        small = image.scaledToWidth(
            int(width), _Qt.TransformationMode.SmoothTransformation)
        store = QByteArray()
        buffer = QBuffer(store)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        # **PNG when the picture has transparency, JPEG when it does
        # not.** JPEG has no alpha, so Qt writes the transparent pixels
        # as black - which is the owner's "remove the black icon BG from
        # the websites logos", 2 September 2026. It was never a
        # background: every one of his website icons is a 512x512 RGBA
        # PNG, a card asks for 200 device pixels, 512 > 200 x 1.15 so
        # every one of them was scaled - and re-encoded to JPEG on the
        # way out. The four that looked right (the apps' `art`) are
        # RGB with nothing to lose.
        #
        # Covers stay JPEG: they are photographs, they have no alpha,
        # and a 200px PNG of one is several times the bytes.
        if image.hasAlphaChannel():
            saved = small.save(buffer, "PNG")
            out_kind = "image/png"
        else:
            saved = small.save(buffer, "JPG", 92)
            out_kind = "image/jpeg"
        if not saved:
            return blob, kind
        buffer.close()
        out = (bytes(store), out_kind)
    except Exception:
        return blob, kind
    _remember_scaled(key, out)
    return out


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return                    # the console is not a request log

    def _send(self, body, kind, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # Pictures never change under a given URL and are worth keeping.
        # Everything else must not be cached: the app writes these JSON
        # files while the page is open, and a cached answer is a page
        # showing what was true when it last looked. Measured - a fixed
        # discover route kept reporting the old count from cache while
        # the server returned the new one to curl.
        self.send_header("Cache-Control",
                         "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, raw = self.path.partition("?")
        query = urllib.parse.parse_qs(raw)

        if path.startswith("/api/"):
            # A route that raises used to print to a stderr the frozen
            # exe does not have and hand the page a dropped fetch - an
            # empty surface with nothing in atomic.log (review, 3
            # September 2026). Logged, and answered with an error the
            # page can show.
            try:
                body = json.dumps(answer(path[5:], query),
                                  ensure_ascii=False)
            except Exception as error:
                try:
                    from helpers import logs
                    logs.exception(f"web route {path} failed")
                except Exception:
                    pass
                body = json.dumps({"kind": "rows", "sections": [],
                                   "rows": [], "note": "",
                                   "error": str(error)[:160]},
                                  ensure_ascii=False)
            self._send(body.encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path.startswith("/cover/"):
            target = COVERS / pathlib.Path(path[7:]).name
            try:
                blob = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            kind = "image/webp"
            for mime, magic in MAGIC.items():
                if blob.startswith(magic):
                    kind = mime
                    break
            blob, kind = _scaled(blob, kind, _wanted_width(query))
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/img/"):
            blob, kind = backend.fetch_image(path[5:])
            if blob is None:
                self.send_error(404)
                return
            blob, kind = _scaled(blob, kind, _wanted_width(query))
            self._send(blob, kind, cache=True)
            return

        if path.startswith("/static/"):
            target = STATIC / pathlib.Path(path[8:]).name
            if not target.exists():
                self.send_error(404)
                return
            kind = ("text/css" if target.suffix == ".css"
                    else "application/javascript" if target.suffix == ".js"
                    else "text/plain")
            self._send(target.read_bytes(), kind + "; charset=utf-8")
            return

        self._send((STATIC / "index.html").read_bytes(),
                   "text/html; charset=utf-8")


def start(port=0):
    """Serve on a free port; returns the URL and the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/", server
