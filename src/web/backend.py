"""The web pages' bridge to Atomic's real data.

Of this app's 98 helpers, 37 need Qt and all 37 are UI - so everything
this file reads (storage, history, app_settings, chapter_source) is the
app's own code rather than a second implementation of it. `images` is the
one exception, and is deliberately not used: it builds QPixmaps, and a
browser decodes its own pictures.

Pictures come through this module rather than being linked directly.
Three reasons, each one a bug that happened:

  * covers live in %APPDATA%/Atomic/image_cache, hero art in logo_cache,
    and game art in game_art - all absolute paths on disk, which a page
    served over http:// cannot open;
  * a discover row spells its picture `poster`, a saved entry spells it
    `cover_url`, and a game spells it `cover` - reading only the first
    left every discover card and every game blank;
  * manga hosts check the Referer and serve a placeholder without it.
"""

import hashlib
import os
import re
import pathlib
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime

_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

STAR = "★"
DOT = "·"

from helpers import art_paths, storage                        # noqa: E402


# A running show's years, as a card should show them.
#
# The owner, 1 September 2026: "in all dates on the cards and banners, do
# not use (2001- ) to the unfinished watchable, use just the start date
# (2001)". Cinemeta writes releaseInfo with a trailing dash for anything
# still airing - "2001-", and sometimes with an en or em dash - which on
# a card reads as a number that failed to load rather than as "still
# going". A *closed* range is left exactly as it is: "2001-2005" says
# something a single year does not.
_OPEN_RANGE = re.compile(r"(\d{4})\s*[-‒–—―]\s*\Z")


def years_text(raw):
    match = _OPEN_RANGE.fullmatch(str(raw or "").strip())
    return match.group(1) if match else str(raw or "").strip()

# storage picks src/data from a source tree and %APPDATA%/Atomic only
# when frozen, so a page run from source would show an empty library
# beside a full app. ATOMIC_DATA_DIR overrides it, which is how a test
# points this at a copy rather than the real thing.
DATA_DIR = pathlib.Path(os.environ.get("ATOMIC_DATA_DIR")
                        or (pathlib.Path(os.environ.get("APPDATA", "."))
                            / "Atomic"))
storage.DATA_DIR = DATA_DIR

try:
    from helpers import chapter_source
except Exception:
    chapter_source = None
try:
    from helpers import net
except Exception:
    net = None

READING = ("manga", "manhwa", "manhua")
COVERS = DATA_DIR / "image_cache"

# token -> (where it came from, headers it needs). Remote pictures and
# on-disk ones both land here so the page has one kind of image URL.
_sources = {}
_lock = threading.Lock()


def _token(key):
    return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]


def _load(name):
    try:
        rows = storage.load(name, [])
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)]


def local_url(path):
    """A URL for a file already on this machine."""
    path = str(path or "").strip()
    if not path or not os.path.isfile(path):
        return ""
    token = _token(path)
    with _lock:
        _sources[token] = (path, None)
    return "/img/" + token


def remote_url(url, headers=None):
    """A URL for a picture this server will fetch on the page's behalf."""
    url = str(url or "").strip()
    if not url:
        return ""
    token = _token(url)
    with _lock:
        _sources[token] = (url, dict(headers or {}))
    return "/img/" + token


def cover_url(entry):
    """This entry's picture, wherever it keeps it.

    The order matters: a cached file beats a remote URL because it is
    already on disk, and `poster`/`cover`/`icon` are here because
    discover rows and games spell it those ways. Leaving any of them out
    is a page of blank cards.
    """
    name = pathlib.Path(str(entry.get("cover_path") or "")).name
    if name and (COVERS / name).exists():
        return "/cover/" + name
    # `image` and `art` are what apps and websites call theirs, `cover`
    # and `icon` what games do - all absolute paths on disk. Reading only
    # some of them is a row of blank cards, which is how Apps and
    # Websites first arrived on Home.
    # `cover_path` again, this time as the absolute path it is. The
    # check above only asks whether image_cache holds a file of that
    # *name*, and a schedule or history row written while the app ran
    # from source points at src/data/image_cache instead - so a picture
    # already on the disk was being re-fetched from AniList.
    #
    # Through art_paths.resolve_art_path rather than the path as written:
    # the owner's games.json names every cover inside the *source tree's*
    # image_cache (imported from a source run), which is dead on any
    # other machine - measured 2 September 2026 with those paths hidden,
    # games 0/10 and apps 0/5 resolved. The same file name under the
    # live cache is looked for before giving up.
    # **`art` before `image` and `icon`, and that ordering is the whole
    # of the owner's report of 3 September 2026:** *"the apps images are
    # being taken from the app icon now, it is not good at all, make
    # sure that you will take it from an API for the good quality like
    # it was in Qt!"*
    #
    # Measured that day on his own apps.json. Every app carries both:
    # `image`, a **64x64** icon prised out of the .exe
    # (art_paths.extract_exe_icon), and `art`, the **512x512** store
    # artwork helpers/app_art fetches from Apple's iTunes Search API.
    # `image` came first in this list, so the 64px icon won every time
    # and a 160px tile drew it four times its own size. The Qt page has
    # never done that - windows/link_grid line 556 reads `entry["art"]`
    # and backfills it through app_art.fetch_art, which is exactly the
    # "like it was in Qt" being asked for.
    #
    # `art` is API artwork by construction: only app_art and game_art
    # write that field, and both write a store original. `image` and
    # `icon` are the local extractions, so they are the fallback for an
    # entry the API has nothing for - and art_paths.heal_missing_art now
    # queues that lookup rather than settling for the icon for ever.
    for key in ("cover_path", "art", "cover", "icon", "image"):
        found = local_url(art_paths.resolve_art_path(entry.get(key)))
        if found:
            # **Still ask for the store artwork when only the icon is
            # here.** An app with a working 64px .exe icon and no `art`
            # resolved on the line above and never reached the healer at
            # the foot of this function, so it drew the icon for ever.
            # heal_missing_art is a no-op for anything whose own field
            # is already on disk and runs once per entry per process.
            if key in ("icon", "image"):
                art_paths.heal_missing_art(entry)
            return found
    for key in ("art", "cover_url", "poster", "cover", "image"):
        value = str(entry.get(key) or "")
        if value.startswith("http"):
            return remote_url(value)
    # Nothing on disk and nothing remote: restore it. An app's exe icon
    # is made here, on the server thread, because this page is answered
    # once and not asked again - 1.3ms warm, 24ms cold, once per entry
    # per process (the blank Stremio tile the owner photographed). A
    # game's cover or a site's favicon is a network fetch, queued.
    return local_url(art_paths.heal_missing_art(entry, inline_icon=True))


def hero_for(entry):
    """The banner art an entry carries, if it has any.

    hero_backdrop and hero_logo are absolute paths into logo_cache,
    written by helpers/hero_art. The Qt Home draws exactly these, so the
    web Home draws exactly these - anything else would be a different
    banner that merely looked similar.
    """
    if not isinstance(entry, dict):
        return None
    backdrop = local_url(entry.get("hero_backdrop"))
    if not backdrop:
        return None
    return {
        "title": str(entry.get("title") or ""),
        "backdrop": backdrop,
        "logo": local_url(entry.get("hero_logo")),
        # The entry's own cover, beside the wide art. The banner had
        # only the backdrop and the owner asked for the cover on it.
        "cover": cover_url(entry),
        # The medium decides the second button's wording - View
        # Chapters, View Episodes or View Details - exactly as
        # home._hero_open does.
        "type": str(entry.get("type") or ""),
        "hide_title": bool(entry.get("hero_hide_title")),
        # **Never the progress.** The owner, 2 September 2026: "do not
        # show what is the last season and ep / ch watched in the banner
        # in main page". The banner is what a title *is* - the schedule
        # line when there is one, and the four fact lines below it
        # (_bullets, which reading and watching both go through, so the
        # two banners are the same shape by construction). Where he has
        # got to is on the card, where he chose it.
        "meta": schedule_lines(entry),
        # The bullet line the Qt banner carried - runtime, years, genres
        # - built here so the page only has to join them.
        "bullets": _bullets(entry),
        "id": str(entry.get("id") or entry.get("entry_id") or ""),
        # **What the cards use to ask for a better cover.** The owner, 4
        # September 2026: "the 3asq readings cover image in the banners
        # home and discover pages are not clear (blurry)."
        #
        # A grid card has asked `/api/cover` for a bigger picture since 3
        # September, and that is why a 3asq row on Manga is sharp: its
        # `cover_250x350.jpg` is replaced by the catalogue's 720x972.
        # The banner draws the same entry three times larger - a 196x264
        # CSS cover is 245x330 real pixels - and never asked, so it kept
        # the small file. These two fields are what askForCover needs
        # (the page url to ask the site, and whether the name says the
        # file is small); the page does the rest.
        "url": str(entry.get("url") or ""),
        "thin": _thin_banner_cover(entry),
    }


def _thin_banner_cover(entry):
    """server._thin_cover, asked from here without importing the server.

    The banner is built in this module and that test lives in the other
    one; a plain import would be a cycle, so it is resolved at call time
    and answers False if it cannot be (which only costs the upgrade).
    """
    try:
        from web import server
        return bool(server._thin_cover(entry))
    except Exception:
        return False


def _bullets(entry):
    """The banner's facts, in the four lines the owner asked for.

    His format, 1 September 2026, with a screenshot of the old single
    run: line 1 the runtime, line 2 the rating and the year, line 3 the
    genres - and line 4 the schedule, which hero_for adds because it
    needs the whole entry (see schedule_lines).

    A line with nothing in it is dropped by the page rather than drawn
    empty, which is the rule the bullet run followed before it.
    """
    lines = []

    runtime = str(entry.get("runtime") or entry.get("duration") or "").strip()
    lines.append(runtime if not runtime or "min" in runtime.lower()
                 else f"{runtime} min")

    second = []
    rating = str(entry.get("imdbRating") or entry.get("rating") or "").strip()
    if rating:
        second.append(rating if rating.startswith(STAR)
                      else f"{STAR} {rating} IMDb")
    years = years_text(entry.get("years") or entry.get("year"))
    if years:
        second.append(years)
    lines.append(f"  {DOT}  ".join(second))

    genres = entry.get("genres")
    if isinstance(genres, list) and genres:
        lines.append(f"  {DOT}  ".join(str(g).strip() for g in genres[:4]
                                       if str(g).strip()))
    elif str(genres or "").strip():
        lines.append(str(genres).strip())
    else:
        # Not a genre, but the one word that says what this is while
        # Cinemeta has not answered yet.
        lines.append(str(entry.get("type") or "").strip())
    return lines


# See fetch_image: the largest picture the proxy will carry.
#
# **32MB, and 16 was measured throwing a chapter page away.** The
# owner, 3 September 2026: "the 3asq readings are still not loading".
# Measured that day, every page of Kingdom (WAN) chapter 886 fetched
# directly: twenty pages at 1.3-2.4MB and **page 20 at 21,386,176
# bytes** - a fifth of a megabyte over 20MB, against a 16MB cap. So
# `net.read_bytes` raised "response body over the size cap", fetch_image
# answered None, the proxy answered 404 and the chapter had a hole in
# its last page. Nothing about it was slow or refused; it was
# discarded here.
#
# The cap is not decoration - a runaway response must still be bounded -
# so it is raised to where the largest real page has room rather than
# removed, and the drop now names the size (see fetch_image) so the next
# one over it arrives with its cause attached.
IMAGE_MAX_BYTES = 32 * 1024 * 1024
# The wall clock a picture gets. Scaled with the cap above: 12s was
# fine for a 2MB cover and is not enough for a 20MB page on a slow
# minute - a transfer cut by the deadline is the same hole in the
# chapter as one cut by the cap.
IMAGE_DEADLINE_S = 25.0


def fetch_image(token):
    """One picture: read off disk, or fetched with the headers it needs.

    Through net.urlopen/read_bytes, never resp.read(): urlopen's timeout
    bounds one socket operation and not the transfer, so a host sending a
    byte a second holds the connection open forever.
    """
    with _lock:
        found = _sources.get(token)
    if not found:
        return None, None
    where, headers = found

    if headers is None:                      # a file on this machine
        try:
            blob = pathlib.Path(where).read_bytes()
        except OSError:
            return None, None
        return blob, _kind(blob[:8])

    disk = COVERS / ("web_" + token)
    if disk.exists():
        try:
            blob = disk.read_bytes()
            return blob, _kind(blob[:8])
        except OSError:
            pass
    if net is None:
        return None, None
    try:
        request = urllib.request.Request(net.ascii_url(where), headers=headers)
        deadline = net.deadline_in(IMAGE_DEADLINE_S)
        with net.urlopen(request, timeout=12.0) as response:
            # **A picture's own cap, above net's default.** Three of the
            # Manga page's 3asq covers are 5.9-6.6MB uploads (measured 3
            # September 2026: 6,530,291 / 6,601,443 / 5,874,369 bytes)
            # and net.MAX_RESPONSE_BYTES threw them away as "over the
            # size cap" - three blank cards, invisible until this fetch
            # started logging. Chapter pages measured up to 2.4MB. The
            # deadline still bounds the transfer.
            blob = net.read_bytes(response, deadline,
                                  max_bytes=IMAGE_MAX_BYTES)
    except Exception as error:
        # A blank card had no cause on record (review, 3 September
        # 2026). The host and the error kind only - a cover URL can
        # carry a token.
        # (urllib.parse is imported at module scope on purpose: an
        # `import urllib.parse` here made `urllib` a local of the whole
        # function, and the Request above raised UnboundLocalError for
        # every remote picture - caught on the frozen build's log, 3
        # September 2026.)
        try:
            from helpers import logs
            # **The size when that is what went wrong.** A page dropped
            # for being over the cap read as an ordinary ValueError and
            # said nothing about how far over it was, which is why a
            # 20.4MB chapter page took a measurement to find rather
            # than a log line (see IMAGE_MAX_BYTES).
            said = str(error)[:60] if isinstance(error, ValueError) else ""
            logs.info(f"image fetch failed for "
                      f"{urllib.parse.urlsplit(where).netloc}: "
                      f"{type(error).__name__}"
                      + (f" ({said})" if said else ""))
        except Exception:
            pass
        return None, None
    if not blob:
        return None, None
    try:
        COVERS.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(blob)
    except OSError:
        pass                                 # a cold cache, never a lost page
    return blob, _kind(blob[:8])


# **Fetching a page's pictures before the page asks for them.**
#
# The owner, 3 September 2026: *"in manhwa and movies pages the cards
# transition is a bit delayed than the other watch and read pages"*.
# Measured that day in a Chromium at his own window size, against his
# own data, timing how long until every cover on the first screenful of
# 24 cards was actually decoded:
#
#     cold cache   movies 94ms   anime 154ms   series 460ms
#                  manga 4,604ms   manhwa >14,000ms   manhua >14,000ms
#     warm cache   movies 31ms    manga 31ms    manhua 63ms
#                  manhwa 1,669ms
#
# So the difference is not the page - it renders in 8-22ms on every one
# of the six (app.js sayRender, read out of atomic.log on the frozen
# build) - and it is not this proxy either: 24 already-fetched covers
# come back in 0.08-0.79s of wall. It is the **first** sight of a cover.
# A video row's art is Cinemeta's CDN; a reading row's is the scanlation
# site itself (3asq, olympustaff, lavascans, meshmanga), and those hosts
# take 0.2-3.0s each. A reading catalogue also churns - the sweep brings
# titles this machine has never seen - so the reading pages meet cold
# covers over and over while the video pages do not.
#
# Three earlier theories were measured and refuted before this one, and
# they are written down so the same ground is not re-dug: the lazy
# sweep's rect reads (0.6ms for 900 pending), the fold's own preparation
# (4.0ms at 60 cards, 5.2ms at 930), and a second document load per
# arrival (a control run of the same tree drew one page per arrival
# either way). A fourth - that a background sweep starves the server -
# measured /api/series at 3ms *while* a sweep and four cover streams
# were in flight.
#
# The fix is to stop paying for the first sight at the moment he is
# looking: the route hands its covers here as it is answered and they
# are fetched behind it, so the disk cache is warm before the browser
# gets round to asking. Four workers, because these are four different
# hosts and one of them being slow must not hold the rest; and a bounded
# head, because a catalogue page scrolled deep is hundreds of rows and
# the ones below the fold can wait for the scroll that reaches them.
_WARM_WORKERS = 4
_WARM_HEAD = 200
_warm_pool = None
_warming = set()


def warm(tokens):
    """Fetch these pictures now, in the background. Never raises."""
    global _warm_pool
    try:
        from concurrent.futures import ThreadPoolExecutor
        with _lock:
            if _warm_pool is None:
                _warm_pool = ThreadPoolExecutor(
                    max_workers=_WARM_WORKERS,
                    thread_name_prefix="cover-warm")
            fresh = []
            for token in list(tokens)[:_WARM_HEAD]:
                token = str(token or "").strip()
                if not token or token in _warming:
                    continue
                where, headers = _sources.get(token, (None, None))
                if headers is None:
                    continue          # a file already on this machine
                if (COVERS / ("web_" + token)).exists():
                    continue          # already paid for
                _warming.add(token)
                fresh.append(token)
        for token in fresh:
            _warm_pool.submit(_warm_one, token)
    except Exception:
        pass                          # a cold cache, never a lost page


def _warm_one(token):
    try:
        fetch_image(token)
    except Exception:
        pass
    finally:
        with _lock:
            _warming.discard(token)


def _kind(head):
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF":
        return "image/webp"
    if head.startswith(b"GIF8"):
        return "image/gif"
    return "application/octet-stream"


def schedule_lines(entry):
    """The banner's schedule, as the lines it is drawn on.

    release_schedule.tooltip_lines is the wording, and it is called here
    rather than re-implemented: "Next Chapter: 886", then "Expected:
    Monday 8:00 PM" - always the weekday name, never a date, "so every
    card reads the same way", and no source anywhere in it.

    The banner had been printing the stored next_release record instead,
    which put the raw date and "via mangadex" on screen - the owner, 1
    September 2026: "make the discover and home pages banners shows
    labels the same as old e.g do not show the manga site". The estimate
    is still declared, by the word "Expected" that carries it on every
    other surface (see .claude/rules/integrations.md on why a MangaDex
    date must never read as announced).
    """
    try:
        from helpers import release_schedule
        kind = str(entry.get("type") or "").strip().lower()
        # Manhwa and manhua are scheduled exactly as manga is; the
        # constant is the *medium* the formatter branches on, not the
        # entry's own word for itself.
        if kind in ("manga", "manhwa", "manhua", "other"):
            medium = release_schedule.MEDIUM_MANGA
        elif kind == "anime":
            medium = release_schedule.MEDIUM_ANIME
        else:
            medium = release_schedule.MEDIUM_SERIES
        lines = release_schedule.tooltip_lines(entry, medium) or []
    except Exception:
        return []
    lines = [str(line) for line in lines if line]
    if not lines:
        return []
    # **The chapter on its own line, the timing under it.** The owner, 2
    # September 2026: "in the readings banners in the main page, make the
    # next chapter label in one line then the expected and the countdown
    # in the line below it". release_schedule.tooltip_lines gives three -
    # "Next Chapter: 886", "Expected: Monday 8:00 PM", "Countdown: 2d 5h"
    # - and all three were joined into one run, which is what made the
    # banner's schedule read as a sentence rather than a fact and a time.
    #
    # A video entry has no chapter line, so it falls through as the one
    # joined line it always was.
    if lines[0].startswith("Next Chapter"):
        return [lines[0], "  ·  ".join(lines[1:])] if len(lines) > 1 else lines
    return ["  ·  ".join(lines)]


# ---- titles that are not in the library ------------------------------
# **A chapter opened from the Manga/Manhwa/Manhua catalogue on an unsaved
# title used to answer "that title is not in your library"** - the
# owner, 2 September 2026: "why is it showing me that this is not in my
# lib when I try to read a ch directly from the manga/manhwa/manhua
# pages". web_pages._from_page builds a transient {title, type, url}
# for a card `_find` misses, and the reader's route is `read/<id>/<n>`,
# so an entry with no id routed to `read//0` and entry_by_id, which reads
# only the saved files, found nothing. Measured on the harness before the
# fix: `_entry_id_for` -> '' and chapters/pages both erroring in 0.00s.
#
# The chapter source never needed the library: list_chapters reads url +
# type and keys its cache on the url when there is no id (chapter_source.
# _cache_key), so the whole missing piece is an id the routes can hand
# back. This registry is it - in memory only, deterministic so the same
# card gets the same id across openings within a run, and bounded because
# every catalogue card he clicks would otherwise stay here for the life
# of the process. Nothing here is ever written to a file: the stored copy
# carries no "id", so history.set_watched/touch record `entry_id: None`
# exactly as they do for any unsaved title, and a later save of the same
# title links up by history.title_key rather than by this id.
TRANSIENT_PREFIX = "t-"
_TRANSIENT_LIMIT = 200
_transient = {}


def transient_id(entry):
    """The registry id for this entry - the url when it has one (that
    is what the chapter cache keys on too), else the medium and title."""
    entry = entry or {}
    seed = str(entry.get("url") or "").strip()
    if not seed:
        seed = (str(entry.get("type") or "").strip().lower() + ":"
                + " ".join(str(entry.get("title") or "").strip().lower().split()))
    if seed in ("", ":"):
        return ""
    return TRANSIENT_PREFIX + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def register_transient(entry):
    """Remember an unsaved entry so the reader routes can find it by id.

    Returns the id, or "" for an entry with neither url nor title. The
    stored copy deliberately drops any "id"/"entry_id" so nothing
    downstream mistakes it for a saved row.
    """
    entry_id = transient_id(entry)
    if not entry_id:
        return ""
    copy = {k: v for k, v in dict(entry).items() if k not in ("id", "entry_id")}
    with _lock:
        _transient.pop(entry_id, None)          # re-insert moves it newest
        _transient[entry_id] = copy
        while len(_transient) > _TRANSIENT_LIMIT:
            _transient.pop(next(iter(_transient)), None)
    return entry_id


def entry_by_id(entry_id):
    if not entry_id:
        return None
    entry_id = str(entry_id)
    # A registry id can never collide with a saved uuid or a history key
    # (those carry no "t-" prefix), so it is answered from memory before
    # any file is opened; every other id keeps the saved files first.
    if entry_id.startswith(TRANSIENT_PREFIX):
        with _lock:
            found = _transient.get(entry_id)
        return dict(found) if found is not None else None
    for name in ("tracker.json", "series.json", "games.json"):
        for row in _load(name):
            if str(row.get("id") or "") == entry_id:
                return row
    for row in _load("history.json"):
        if entry_id in (str(row.get("entry_id") or ""),
                        str(row.get("key") or "")):
            return dict(row, id=entry_id)
    return None


def settings():
    """What Settings shows, through app_settings' public getters.

    Not its `_load`: each getter carries the default and the coercion for
    its own key, and reading the raw dictionary would duplicate both.
    """
    rows, keys = [], []
    try:
        from helpers import app_settings as aps
    except Exception:
        return {"rows": [], "keys": [], "data_dir": str(DATA_DIR)}

    for label, getter in (
            ("Full screen on startup", "get_fullscreen_on_startup"),
            ("Preferred resolution", "get_preferred_resolution"),
            ("Pick a source automatically", "get_auto_pick_source"),
            ("Last version seen", "get_last_seen_version"),
            ("Sidebar order", "get_nav_order"),
            ("Hidden sections", "get_hidden_sections")):
        fn = getattr(aps, getter, None)
        if not callable(fn):
            continue
        try:
            value = fn()
        except Exception:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if isinstance(value, bool):
            value = "on" if value else "off"
        rows.append({"label": label, "value": str(value)[:200]})

    # Reported as set or not set, never shown: a page that prints the
    # owner's key is a page that leaks it.
    for name, getter in (("TMDB", "get_tmdb_key"), ("SubDL", "get_subdl_key"),
                         ("Debrid", "get_debrid_key")):
        fn = getattr(aps, getter, None)
        if callable(fn):
            try:
                keys.append({"label": name, "set": bool((fn() or "").strip())})
            except Exception:
                pass
    return {"rows": rows, "keys": keys, "data_dir": str(DATA_DIR)}


# ---- the reader ------------------------------------------------------
# A full chapter list was measured at 21.7s cold on the owner's own
# 249-chapter entry, so `chapters` is its own route and the page asks for
# it after it has drawn something (rule 7). `cached_chapters` is a
# dictionary lookup and answers in the same frame.

def _chapter_row(chapter, index):
    number = chapter.get("number")
    name = str(chapter.get("title") or "").strip()
    label = f"Chapter {number}" if number not in (None, "") else (name or "?")
    return {"i": index,
            "key": f"c{number}" if number not in (None, "") else "",
            "label": label,
            "sub": name if name and str(number) not in name else ""}


def chapters(entry_id, live=False):
    """This entry's chapters. `live` goes to the site; otherwise cache."""
    entry = entry_by_id(entry_id)
    if chapter_source is None:
        return {"items": [], "error": "this build has no chapter reader"}
    if entry is None:
        # Says which of the two it is. "no reader here" covered both and
        # told him nothing - see web_reader._entry_id_for for the empty
        # id that used to land here.
        return {"items": [], "error": "that title is not in your library"}
    try:
        # Not live: the fresh cache, then the last list seen whatever
        # its age - the list the reader's jump list and next/previous
        # index into, which has to be the one pages() reads.
        found = (chapter_source.list_chapters(entry) if live
                 else (chapter_source.cached_chapters(entry)
                       or chapter_source.stale_chapters(entry))) or []
    except Exception as exc:
        return {"items": [], "error": str(exc)[:160]}
    return {"items": [_chapter_row(c, i) for i, c in enumerate(found)],
            "title": str(entry.get("title") or ""),
            "cached": not live}


def pages(entry_id, index):
    """The images of one chapter, as URLs this server will proxy.

    Proxied rather than linked because these hosts check the Referer and
    serve a placeholder to anyone who omits it - chapter_pages hands the
    headers back with the list, and they are replayed by fetch_image.
    """
    entry = entry_by_id(entry_id)
    if chapter_source is None:
        return {"pages": [], "error": "this build has no chapter reader"}
    if entry is None:
        return {"pages": [], "error": "that title is not in your library"}
    try:
        # The fresh cache, then the last list seen whatever its age -
        # the list the details page and reader._web_chapter_index index
        # into - and only then the site. Reading the site here would
        # renumber the list under an index taken from the stale one
        # (a chapter published since shifts everything by one).
        found = (chapter_source.cached_chapters(entry)
                 or chapter_source.stale_chapters(entry) or [])
        if not found or index >= len(found):
            found = chapter_source.list_chapters(entry) or []
        if index < 0 or index >= len(found):
            return {"pages": [], "error": "no such chapter"}
        chapter = found[index]
        answer = chapter_source.chapter_pages(chapter) or {}
    except Exception as exc:
        return {"pages": [], "error": str(exc)[:160]}

    head = dict(answer.get("headers") or {})
    urls = [remote_url(u, head) for u in (answer.get("pages") or [])]
    row = _chapter_row(chapter, index)
    # The medium decides how wide a page is drawn - reader.py's
    # MEDIUM_TARGET_WIDTH, manga 1100 against manhwa/manhua 762. It is
    # the difference between a page and a vertical strip, and reading
    # both at one width is what made the reader feel unlike the old one.
    medium = str(entry.get("type") or "").strip().lower()
    return {"pages": urls, "count": len(urls), "label": row["label"],
            "key": row["key"], "index": index, "total": len(found),
            "medium": medium if medium in READING else "manga",
            "title": str(entry.get("title") or "")}


def mark_read(entry_id, key, read=True):
    """Record a chapter as read, the way the Qt reader does."""
    entry = entry_by_id(entry_id)
    if entry is None or not key:
        return {"ok": False}
    try:
        from helpers import history
        history.set_watched(entry, [key], watched=bool(read))
        history.touch(entry)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    return {"ok": True}


def read_state(entry_id):
    """Which chapter keys this entry already has marked read."""
    entry = entry_by_id(entry_id)
    if entry is None:
        return {"watched": []}
    # The row that mark_read wrote is the one keyed by history.title_key
    # - "read:kingdom" for a manga, "imdb:tt..." for a show - so that is
    # asked first. The bare title match stays as the fallback for a
    # saved entry whose row predates the key, but it must not come
    # first: measured 2 September 2026 on the owner's copy, the *anime*
    # Kingdom's history row (imdb:tt2404499, title "Kingdom") sat ahead
    # of the manga's, so the transient manga's marks were written to
    # one row and read back from another - an empty round trip.
    try:
        from helpers import history
        wanted = history.title_key(entry)
    except Exception:
        wanted = ""
    rows = _load("history.json")
    saved_id = str(entry.get("id") or "")
    for row in rows:
        if ((wanted and str(row.get("key") or "") == wanted)
                or (saved_id and str(row.get("entry_id") or "") == saved_id)):
            return {"watched": sorted(str(k) for k in (row.get("watched") or []))}
    title = str(entry.get("title") or "").strip().lower()
    for row in rows:
        if title and str(row.get("title") or "").strip().lower() == title:
            return {"watched": sorted(str(k) for k in (row.get("watched") or []))}
    return {"watched": []}


def hero_meta(entry_id):
    """The banner's fact line, the way the details page builds it.

    Deliberately its own route, and asked for *after* the banner is
    drawn: runtime, years and rating come from Cinemeta and that is a
    network call. The banner shows what the entry already knows first
    and this replaces it when it arrives - never the other way round
    (rule 7).

    The order and the wording are details._fill_facts's, so the two
    surfaces cannot drift: runtime, then years, then the rating with its
    star and "IMDb".
    """
    entry = entry_by_id(entry_id)
    if entry is None:
        return {"bullets": []}
    imdb = str(entry.get("imdb_id") or "").strip()
    if not imdb:
        return {"bullets": []}
    kind = "movie" if str(entry.get("type") or "").lower().startswith("movie")         else "series"
    try:
        from helpers import stremio
        meta = stremio.fetch_meta_cached(imdb, kind) or {}
    except Exception:
        return {"bullets": []}

    # The same three-line shape _bullets returns, so the live answer
    # replaces the lines in place instead of collapsing them back into
    # one run. details._fill_facts' wording throughout.
    runtime = str(meta.get("runtime") or "").strip()

    second = []
    rating = str(meta.get("imdbRating") or "").strip()
    if rating:
        second.append(f"{STAR} {rating} IMDb")
    years = years_text(meta.get("releaseInfo") or meta.get("year"))
    if years:
        second.append(years)

    genres = meta.get("genres")
    third = (f"  {DOT}  ".join(str(g).strip() for g in genres[:4] if str(g).strip())
             if isinstance(genres, list) else str(genres or "").strip())
    return {"bullets": [runtime, f"  {DOT}  ".join(second), third]}


def featured_art(title, imdb="", kind=""):
    """A discover title's wide still and title treatment.

    The same three calls tracker._featured_backdrop_worker makes: the
    small backdrop first because it is there in a moment, then the
    full-resolution one, then the logo. A discover row has no saved
    entry, so everything is found by name (and by IMDb id where the row
    carries one).
    """
    title = str(title or "").strip()
    if not title:
        return {"backdrop": "", "logo": ""}
    probe = {"title": title, "imdb_id": str(imdb or ""),
             "type": str(kind or "")}
    backdrop = logo = ""
    try:
        from helpers import artwork
        found = artwork.backdrop_path(probe) or artwork.backdrop_fast_path(probe)
        backdrop = local_url(found) if found else ""
    except Exception:
        pass
    try:
        from helpers import artwork
        found = artwork.logo_path(probe) or artwork.logo_path_by_title(title)
        logo = local_url(found) if found else ""
    except Exception:
        pass
    return {"backdrop": backdrop, "logo": logo}
