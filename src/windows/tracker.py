"""Anime & Manga & Series tracker: three pages sharing this list-widget
implementation. Anime/Manga share one data file split by the entry's
`type` field (so you can reclassify one into the other); Series has its
own file since it's a different domain entirely.

Typing a title searches a matching source in the background - Stremio's
Cinemeta catalog for Anime/Series, every reading site configured in
Settings for Manga - and picking a suggestion auto-fills the title/cover
and a direct link (a stremio:// deep link for Anime/Series, or the
matched manga's page on whichever site it came from) so opening it
jumps straight there. You can also just type your own title if it's not
listed there.
"""

import copy
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QSize, Qt,
                          QTimer, QVariantAnimation)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QColor, QCursor, QIcon, QLinearGradient, QPainter,
                         QPixmap, QPolygonF)
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from helpers import (
    anilist, anime_sites, app_settings, history, images, logs, lookup_pool,
    manga_sites, release_schedule, storage, stremio, theme, title_match,
)
from helpers.widgets import (
    Card, CardDragReorder, CardTextLabel, GlassPage, HeroBanner,
    SideScroller, confirm,
    defer_grid_rebuild, finish_toast, frameless_dialog, inform, scroll_area,
    search_field, show_toast, show_undo_toast, use_hover_cursor,
)

# Soft, for the same reason reader.py soft-imports chapter_source: a
# build (or a working tree) without it must lose the Discover tab's
# contents and say so, not take the whole tracker down at import time.
try:
    from helpers import discover
except Exception:                                   # pragma: no cover
    discover = None

SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Updated"]

# No on-screen hint about dragging any more: it named a right-click
# Move Up/Down that no longer exists, and every page is draggable, so the
# line was explaining the ordinary case at the cost of a row of text on
# every page. Dragging is still off while a search or filter hides part of
# the grid (see _grid_is_narrowed) - a drop writes the order that is on
# screen, which isn't the whole order then.

# Manga/Manhwa/Manhua are just regional flavors of the same reading
# medium - same statuses, same search/open behavior - so they're treated
# as one group everywhere except the Type dropdown itself.
MANGA_TYPES = ("Manga", "Manhwa", "Manhua")

# Types that are watched rather than read. Stremio, the Video Website
# list and the progress sync all apply to exactly these - written once
# here because the rule used to be spelled `== "Anime"` in a dozen
# places, which is why Series could never open on a chosen site.
VIDEO_TYPES = ("Anime", "Series", "Movie")

# How a type reads as a section heading, where a page draws one row per
# type. Only where the plural isn't the word itself ("Series" already is
# one, and heading a row "Seriess" is how that goes wrong).
SECTION_TYPE_PLURAL = {"Movie": "Movies"}

# Status wording differs by content type: you "watch" Anime/Series/Movie
# but "read" Manga/Manhwa/Manhua.
_WATCHING_STATUSES = ["Watching", "Completed", "On Hold", "Dropped", "Plan to Watch"]
_READING_STATUSES = ["Reading", "Completed", "On Hold", "Dropped", "Plan to Read"]
STATUSES_BY_TYPE = {
    **{t: list(_WATCHING_STATUSES) for t in VIDEO_TYPES},
    **{t: _READING_STATUSES for t in MANGA_TYPES},
}
IN_PROGRESS_STATUSES = {"Watching", "Reading"}

# A film is one video: there is no episode to be on, so nothing about
# progress applies to it - no number on the card, no season/episode in
# Add/Edit, no Stremio sync. Asked as a question here rather than spelled
# `!= "Movie"` wherever it matters, which is the mistake `== "Anime"` was
# before VIDEO_TYPES existed.
UNTRACKED_TYPES = ("Movie",)


def tracks_progress(entry_type) -> bool:
    return entry_type not in UNTRACKED_TYPES


def shows_last_watched(entry) -> bool:
    """Whether this entry's own last-watched number is on show, per the
    "Show Last Watched" tick in Add/Edit.

    Defaults on for an entry saved before that tick existed: those
    already displayed their progress, and defaulting off would read as
    the app having lost it."""
    if not tracks_progress(entry.get("type")):
        return False
    stored = entry.get("show_last_watched")
    return True if stored is None else bool(stored)


def last_watched_is_editable(entry) -> bool:
    """Whether that number was yours to set by hand.

    Nothing in the UI sets progress by hand any more - the +/- on the
    card and the last-watched spinners in Add/Edit are gone, and
    record_progress writes what you actually open instead. This is kept
    only because windows/player.py still gates on it while it is being
    moved over to record_progress; once it is, this has no callers left
    and should go with them.

    The rule it encodes: Stremio owns the number on a Stremio-backed
    entry (site_id None) and overwrites it on the next sync, so setting
    it there fought the sync and lost. An entry pinned to Netflix or
    Crunchyroll has no source that can ever fill it in, and Manga has
    none whatsoever."""
    if not shows_last_watched(entry):
        return False
    if entry.get("type") in MANGA_TYPES:
        return True
    return entry.get("site_id") is not None

# Older saves used one shared "Watching/Reading"-style status list; this
# maps those legacy values onto the new per-type wording during migration.
_LEGACY_STATUS_MAP = {
    "Watching/Reading": {"Anime": "Watching", "Series": "Watching", "Manga": "Reading"},
    "Plan to Watch/Read": {"Anime": "Plan to Watch", "Series": "Plan to Watch", "Manga": "Plan to Read"},
}

# What kind of search/launch each content type uses. Manga/Manhwa/Manhua
# have no Stremio presence (it's not video), so they search the Settings-
# configured reading sites instead and open a plain browser tab.
SEARCH_PROVIDER_BY_TYPE = {**{t: "stremio" for t in VIDEO_TYPES},
                            **{t: "manga_sites" for t in MANGA_TYPES}}
# Cinemeta keeps films in their own catalog, so a Movie searched against
# the series catalog finds nothing at all.
STREMIO_CATALOG_BY_TYPE = {"Anime": "series", "Series": "series", "Movie": "movie"}

# Bundled with the exe by Atomic.spec's `datas` - a file that only exists
# next to main.py in the source tree is absent from the frozen build (the
# same trap app_icon.ico is commented for in the spec).
FILTER_ICON = "filter_icon.png"
FILTER_ICON_HEIGHT = 18

POSTER_SIZE = (160, 216)
PREVIEW_SIZE = (90, 120)
# Pins a run of text to a left-to-right base direction. Needed wherever
# a number sits beside Arabic content: Qt shapes digits with the
# paragraph's resolved direction, so "3 titles" on a page of Arabic rows
# came out as "٣ titles" (the owner asked what that character was).
# chr(), not the literal - an invisible codepoint does not survive a
# tool re-encoding this file (CLAUDE.md records that happening twice).
_LTR_MARK = chr(0x200E)
# The cover on a Schedule row - small enough that a screenful of rows
# reads as a list rather than a second poster grid.
SCHEDULE_COVER_SIZE = (46, 62)

# The sub-sections every tracker page carries. "saved" is the page as it
# has always been - the sort row, the search, the sections grid, select
# mode and drag-reorder all live there and nowhere else; the rest are
# read-only views built on demand. Discover leads the tuple and is where
# these pages open (the owner's ask): this order is what the section
# sidebar lists, top to bottom, until the user drags their own.
TAB_SAVED = "saved"
TAB_DISCOVER = "discover"
TAB_HISTORY = "history"
TAB_SCHEDULE = "schedule"
TABS = ((TAB_DISCOVER, "Discover"), (TAB_SAVED, "Saved"),
        (TAB_HISTORY, "History"), (TAB_SCHEDULE, "Schedule"))
DEFAULT_TAB = TAB_DISCOVER

# **Category sections, between Discover and Saved** (the owner's ask):
# Watch gets Series / Anime / Movies, Read gets Manga / Manhwa / Manhua /
# Other. Each is a browse of one category, so "show me manhwa" is a
# sidebar row rather than a search anyone has to think of.
#
# (section key, sidebar label, the catalogue kind behind it, entry type)
#
# The reading four are decided by MangaDex's `originalLanguage` -
# Japanese is manga, Korean is manhwa, Chinese is manhua, and that is the
# definition of the words rather than a guess at them. AniList
# (countryOfOrigin) and MangaUpdates (its plain `type` field) answer the
# same question the same way, and helpers/mangaupdates.py exists so a
# single title can be asked directly. See discover.MEDIUM_LANGUAGES.
WATCH_CATEGORIES = (("cat_movies", "Movies", "movie", "Movie"),
                    ("cat_series", "Series", "series", "Series"),
                    ("cat_anime", "Anime", "anime", "Anime"))
READ_CATEGORIES = (("cat_manga", "Manga", "medium:Manga", "Manga"),
                   ("cat_manhwa", "Manhwa", "medium:Manhwa", "Manhwa"),
                   ("cat_manhua", "Manhua", "medium:Manhua", "Manhua"),
                   ("cat_other", "Other", "medium:Other", "Manga"))


# **The section rail in three blocks, with a row of air between them**
# (the owner's ask, 22 August 2026, spelled out row by row: Discover /
# gap / Movies, Series, Anime / gap / Saved, Schedule, History - and the
# reading mirror of it). main.py draws the gaps; this names the blocks.
#
# It is not TABS' order, and TABS keeps its own: that tuple is the tab
# *table*, read by _set_tab and the saved view state, not a layout.
#
# Read gets a fourth category row the owner's list does not name -
# "Other". It is kept because dropping it would leave every title that
# is not Japanese, Korean or Chinese with no section that can reach it
# (see discover.MEDIUM_LANGUAGES), which is a row missing rather than a
# row too many. It sits last in its block for the same reason.
_SECTION_HEAD = (TAB_DISCOVER,)
_SECTION_TAIL = (TAB_SAVED, TAB_SCHEDULE, TAB_HISTORY)


def _section_groups(categories):
    """The three blocks, each a tuple of (key, label)."""
    labels = dict(TABS)
    return (tuple((key, labels[key]) for key in _SECTION_HEAD),
            tuple((c[0], c[1]) for c in categories),
            tuple((key, labels[key]) for key in _SECTION_TAIL))


def _sections_with(categories):
    """Every section, in rail order, flattened - what a page publishes as
    SECTIONS for anything that only wants the keys."""
    return tuple(row for group in _section_groups(categories) for row in group)

# How many titles one Discover row asks for. Bounded because every one of
# them is a poster download queued onto the shared lookup pool.
DISCOVER_LIMIT = 30

# Discover rows already fetched this session, keyed (kind, query) ->
# (monotonic stamp, rows). Pages rebuild from scratch on every visit
# (.claude/rules/ui.md), so without this every walk to a tracker page
# re-asked Cinemeta/MangaDex for the identical popular lists and the tab
# sat on "Looking around..." for seconds each time - the single thing
# the owner called out as slow. A quarter of an hour is well inside how
# often these catalogs actually move; the worker refreshes an expired
# key in place, so a stale answer is shown once and corrected, never
# shown forever.
_DISCOVER_CACHE = {}
_DISCOVER_CACHE_TTL_S = 15 * 60

# The same browse rows, kept on disk so the *first* visit after a launch
# is instant too - not only the second. The in-memory cache is warmed at
# launch (prewarm_discover) but that fetch takes a few seconds to finish,
# and a user who opens Read straight away still waited; last session's
# rows on disk cover exactly that gap. Only the browse rows (query "")
# are persisted - a typed search is transient - and each is stamped with
# wall-clock time, since a monotonic stamp means nothing across runs.
# The worker already shows a stale list and refreshes it in place, so a
# day-old disk row is drawn at once and quietly corrected.
_DISCOVER_CACHE_FILE = "discover_cache.json"
_DISCOVER_DISK_TTL_S = 24 * 60 * 60
_discover_disk_loaded = False


def _load_discover_cache():
    """Fold last session's browse rows into the in-memory cache, once.

    A disk entry D seconds old is given an in-memory monotonic stamp of
    now-D, so the existing TTL/refresh logic treats it exactly as it
    would a row fetched D seconds ago - fresh enough to serve, stale
    enough to refresh past _DISCOVER_CACHE_TTL_S. Fails soft: a missing
    or corrupt file just means a cold start, the way it always was."""
    global _discover_disk_loaded
    if _discover_disk_loaded:
        return
    _discover_disk_loaded = True
    try:
        stored = storage.load(_DISCOVER_CACHE_FILE, {})
        if not isinstance(stored, dict):
            return
        wall = time.time()
        mono = time.monotonic()
        for kind, entry in stored.items():
            if not isinstance(entry, dict):
                continue
            rows = entry.get("rows")
            at = entry.get("at")
            if not rows or not isinstance(rows, list) or at is None:
                continue
            age = wall - float(at)
            if age < 0 or age > _DISCOVER_DISK_TTL_S:
                continue
            # Don't clobber a fresher row already fetched this run.
            key = (kind, "")
            if key in _DISCOVER_CACHE:
                continue
            _DISCOVER_CACHE[key] = (mono - age,
                                    [r for r in rows if isinstance(r, dict)])
    except Exception:
        return


def _save_discover_row(kind, rows):
    """Write one browse row through to disk. Best-effort; a cache that
    cannot be written is a convenience lost, never an error."""
    try:
        stored = storage.load(_DISCOVER_CACHE_FILE, {})
        if not isinstance(stored, dict):
            stored = {}
        stored[kind] = {"at": time.time(), "rows": list(rows)}
        storage.save(_DISCOVER_CACHE_FILE, stored)
    except Exception:
        return

# Every browse row the two tracker pages draw, as (kind, fetch). Named
# once here because prewarm_discover and _discover_row_worker have to
# agree on both the cache key and the call, and a row that drifted
# between them would warm one key and read another.
_BROWSE_ROWS = (
    ("reading", lambda: discover.discover_reading_sites(
        query="", limit=DISCOVER_LIMIT)),
    ("reading_latest", lambda: discover.discover_reading_latest(
        limit=DISCOVER_LIMIT)),
    ("anime", lambda: discover.discover_video("anime", query="",
                                              limit=DISCOVER_LIMIT)),
    ("series", lambda: discover.discover_video("series", query="",
                                               limit=DISCOVER_LIMIT)),
    ("movie", lambda: discover.discover_video("movie", query="",
                                              limit=DISCOVER_LIMIT)),
)

# The Read page's four category sections, whose kinds are "medium:X".
# **They are one fetch, not four** - the browse, the per-site probe and
# the classification sweep behind them are identical, and asking per
# medium threw three quarters of every sweep away (see
# discover.reading_sites_by_medium_all). So they warm together, under
# one key each, and a section reads its own key like any other browse
# row.
_MEDIUM_KINDS = (
    tuple(f"medium:{name}" for name in (*discover.MEDIUM_LANGUAGES, "Other"))
    if discover is not None else ())


def _fetch_browse_rows(kind):
    """One browse kind's rows, fetched and written into the shared cache
    (memory and disk). Never raises - it runs on pool workers, where an
    exception takes every queued lookup with it.

    The `medium:` kinds fill *all four* of their keys from the single
    sweep that produced them, so opening Manhwa after Manga costs
    nothing at all."""
    if discover is None:
        return []
    try:
        if kind.startswith("medium:"):
            found = discover.reading_sites_by_medium_all(limit=DISCOVER_LIMIT)
            rows = []
            for name, medium_rows in (found or {}).items():
                clean = [r for r in (medium_rows or []) if isinstance(r, dict)]
                _remember_browse_rows(f"medium:{name}", clean)
                if f"medium:{name}" == kind:
                    rows = clean
            return rows
        for key, fetch in _BROWSE_ROWS:
            if key == kind:
                rows = [r for r in (fetch() or []) if isinstance(r, dict)]
                _remember_browse_rows(kind, rows)
                return rows
        rows = [r for r in (discover.discover_video(
            kind, limit=DISCOVER_LIMIT) or []) if isinstance(r, dict)]
        _remember_browse_rows(kind, rows)
        return rows
    except Exception:
        logs.exception("browse rows failed for %s" % kind)
        return []


def _remember_browse_rows(kind, rows):
    """Write one kind's rows into both caches. Empty rows are *not*
    stored: a source that answered nothing this minute must not blank a
    section that has real rows on disk from last session."""
    if not rows:
        return
    _DISCOVER_CACHE[(kind, "")] = (time.monotonic(), rows)
    _save_discover_row(kind, rows)


def prewarm_discover():
    """Fetch every Discover browse row before anyone opens Read or Watch.

    **The owner's ask**: opening either page and waiting "a few sec" for
    the lists. Nothing about the fetch is slow - measured, a row answers
    in well under a second - it simply had not *started* until the page
    was opened and asked for it. So it starts at launch instead, while
    the window is sitting on Home doing nothing.

    Writes the same (kind, "") keys `_discover_row_worker` reads, so a
    warmed page finds its rows in hand and draws them on the first
    paint rather than showing "Looking around...".

    Onto the shared four-worker pool, never a thread per row: this is
    the same rule every other background fan-out here follows
    (lookup_pool), and it also means these queue *behind* anything a
    visible page has already asked for. Each job is wrapped - a pool
    worker that raises takes every queued lookup with it - and a key
    that is already fresh is skipped, so this costs nothing on a second
    call."""
    _load_discover_cache()
    now = time.monotonic()
    warm = [kind for kind, _fetch in _BROWSE_ROWS]
    # The category sections are warmed too, not only the Discover rows
    # (the owner's ask: the sections "take too much time to load"). The
    # video kinds are already in the list above under the same keys -
    # a category *is* a browse of one kind - so only the reading four
    # are added, and those four share one sweep (see _fetch_browse_rows),
    # which is why just the first uncached one is queued.
    for kind in _MEDIUM_KINDS:
        cached = _DISCOVER_CACHE.get((kind, ""))
        if not (cached and now - cached[0] < _DISCOVER_CACHE_TTL_S):
            warm.append(kind)
            break
    for kind in warm:
        cached = _DISCOVER_CACHE.get((kind, ""))
        if cached and now - cached[0] < _DISCOVER_CACHE_TTL_S:
            continue
        lookup_pool.submit_browse(_fetch_browse_rows, kind)
    # The Watch Schedule's calendar gets the same treatment (the owner's
    # "the schedule is taking too long to load"): warmed at launch so the
    # tab's first build finds its rows in hand. Covers are not fetched
    # here - the tab downloads the few it is missing when actually
    # opened.
    if _cached_upcoming_calendar() is None:
        lookup_pool.submit_browse(_fetch_upcoming_calendar)


# The Watch Schedule's airing calendar, module-side like _DISCOVER_CACHE
# and for the same reason: pages rebuild from scratch on every visit
# (.claude/rules/ui.md), so rows kept only on the page died with it and
# every walk back to the tab re-asked AniList for the same week.
# Measured 21 August 2026 on the owner's data: the calendar answers in
# ~0.5s warm but took 7.8s on the session's first call, re-paid per
# visit before this cache. Only "upcoming" lives here - the Read page's
# rows share _DISCOVER_CACHE's ("reading_latest", "") key with the
# Discover tab's Latest row (same fetch both times), which
# prewarm_discover already warms at launch. Cover downloads write their
# local path back onto these cached dicts, so art survives rebuilds too.
_SCHEDULE_UPCOMING_CACHE = {}

# How many latest-chapter rows the Read schedule lists - fewer than the
# DISCOVER_LIMIT the shared cache holds; the schedule is a list, not a
# poster grid.
SCHEDULE_RELEASED_LIMIT = 20


def _stamp_cached_covers(rows):
    """Fill cover_path for every row whose art is already on disk - a
    stat apiece, never a download. Rows the disk doesn't have keep None
    and are fetched one pool job each (_queue_schedule_covers): the old
    loop downloaded every cover serially *before* the rows were emitted,
    so the section showed nothing until the last of 40 downloads -
    measured 73s with a cold cover cache against 0.5s for the calendar
    itself, and that was the owner's "taking too long"."""
    for row in rows or []:
        if row.get("cover_path"):
            continue
        url = row.get("cover_url") or ""
        path = images.cache_path_for_url(url) if url else None
        row["cover_path"] = (str(path)
                             if path is not None and path.exists() else None)


def _cached_upcoming_calendar():
    """The airing calendar if a fresh one is in hand - never a fetch."""
    cached = _SCHEDULE_UPCOMING_CACHE.get("upcoming")
    if cached and time.monotonic() - cached[0] < _DISCOVER_CACHE_TTL_S:
        return cached[1]
    return None


def _fetch_upcoming_calendar():
    """AniList's airing week as schedule rows, cached. Never raises - it
    runs on pool workers (prewarm_discover and the schedule's own
    fetch), and RateLimited deliberately reads as no rows: the schedule
    keeps its saved rows, the honest answer and not an error dialog."""
    try:
        rows = anilist.fetch_upcoming_airing(hours=168, limit=40)
    except Exception:
        return []
    _stamp_cached_covers(rows)
    if rows:
        _SCHEDULE_UPCOMING_CACHE["upcoming"] = (time.monotonic(), rows)
    return rows


def _prepare_released_rows(found):
    rows = []
    for item in (found or [])[:SCHEDULE_RELEASED_LIMIT]:
        if not isinstance(item, dict):
            continue
        item["cover_url"] = item.get("poster") or ""
        rows.append(item)
    _stamp_cached_covers(rows)
    return rows


def _cached_released_rows():
    """The Read schedule's rows out of the Discover cache, or None."""
    cached = _DISCOVER_CACHE.get(("reading_latest", ""))
    if not cached or time.monotonic() - cached[0] >= _DISCOVER_CACHE_TTL_S:
        return None
    return _prepare_released_rows(cached[1])


def _fetch_released_rows():
    """The latest-chapter rows, fetched and cached under Discover's own
    ("reading_latest", "") key - the schedule's Recently Released group
    and Discover's Latest row are the same list, so they share one cache
    entry and one fetch. Never raises."""
    try:
        found = (discover.discover_reading_latest(limit=DISCOVER_LIMIT)
                 if discover is not None else [])
    except Exception:
        return []
    found = [row for row in (found or []) if isinstance(row, dict)]
    if found:
        _DISCOVER_CACHE[("reading_latest", "")] = (time.monotonic(), found)
        _save_discover_row("reading_latest", found)
    return _prepare_released_rows(found)


# Typing here is debounced far longer than the entry form's title search
# (SEARCH_DEBOUNCE_MS, 250): that one fills a dropdown the user is
# staring at mid-word, this one rebuilds rows of poster cards and fires
# a catalog search per row.
DISCOVER_DEBOUNCE_MS = 450
# Tall enough to read as the page's own search bar rather than a field in
# a form, and rounded to half its height so it is a true pill.
DISCOVER_SEARCH_HEIGHT = 46

# How many cards of a Discover row are built before the event loop gets
# a turn. A strip is sideways-scrolling and shows about seven at the
# window sizes this runs at, so this is a screenful plus one - and every
# card past it is behind an arrow the user has to press.
DISCOVER_STRIP_CHUNK = 8

# The width a Discover card's title is actually laid out at: the card is
# POSTER_SIZE[0] + 20 wide and its layout keeps 8px of margin each side.
DISCOVER_CARD_TEXT_WIDTH = POSTER_SIZE[0] + 20 - 16
# And how long between chunks. **Not zero**, and that is measured: a
# zero timer posts an event the loop drains in the same round it is
# already draining, so all ninety of Watch's cards were still built
# before Qt sent a single paint - the page's first frame did not reach
# the screen until 254-486ms after the click (paint-spy measurement, 22
# August 2026). One frame's worth of delay puts a paint between every
# pair of chunks instead, so the page is on screen while the rest of the
# rows fill in behind it.
DISCOVER_CHUNK_MS = 16
# 450 originally; halved at the owner's ask ("the suggestion search is
# too slow") - the debounce was a flat 450ms added on top of the network
# round trip before a search even started. 250 still coalesces normal
# typing (inter-key gaps are ~100-200ms) without holding a finished
# title hostage for half a second.
SEARCH_DEBOUNCE_MS = 250

# How long Save will hold for the Video Website page-url lookup still in
# flight behind it. The lookup itself always reports back, success or
# failure (see _resolve_video_site_url), so this is only a backstop for
# one whose sockets outlive their own timeouts - measured, a Crunchyroll
# resolve takes ~1.5s, and the worst case is a variant loop of 6s
# timeouts. Past this the entry saves without a page url, which is the
# old behaviour, not a new failure.
VIDEO_URL_SAVE_WAIT_MS = 20000

_EPISODE_SEASON_RE = re.compile(r"^s(\d+)\s*e(\d+)$", re.IGNORECASE)
_EPISODE_ONLY_RE = re.compile(r"^e?(\d+)$", re.IGNORECASE)
_IMDB_ID_RE = re.compile(r"tt\d+")


def _imdb_id_from_url(url):
    """Pull the "tt1234567" id back out of a saved stremio:// deep link,
    so a saved entry can be re-queried later without having re-run the
    original title search. Only ever applied to a stremio:// url: an
    Anime entry pointing at a Video Website saves that site's own page
    url, and a site slug that happens to contain "tt" followed by digits
    would otherwise read as a Cinemeta id and send every later lookup to
    the wrong show."""
    if not (url or "").startswith("stremio://"):
        return None
    match = _IMDB_ID_RE.search(url)
    return match.group(0) if match else None


def _entry_imdb_id(entry):
    """The Cinemeta id to sync progress against - stored directly on the
    entry (see EntryForm._save) since an Anime entry set to open on a
    Video Website other than the built-in "Stremio" one saves that
    site's page url instead of a stremio:// link, which would otherwise
    leave no way to recover it. Falls back to pulling it out of a saved
    stremio:// url for older entries saved before this field existed
    (see _migrate)."""
    return entry.get("imdb_id") or _imdb_id_from_url(entry.get("url"))


def _release_content(stored):
    """What a stored `next_release` says is *coming* - the episode or
    chapter number - as opposed to when it lands.

    This, and not the whole record, is what decides whether the refresh
    button turned up anything new. The time on its own can't: the manga
    estimate is extrapolated from a title's release rhythm (see
    mangadex._predict), so once a predicted slot has come and gone the
    next check simply projects the slot after it. That moving timestamp
    means nothing has happened yet, not that something has - reporting it
    as an update would make "no new chapter" indistinguishable from a
    chapter actually landing."""
    if not isinstance(stored, dict):
        return None
    return (stored.get("season"), stored.get("episode"), stored.get("chapter"))


def _latest_known_chapter(entry):
    """The highest chapter number this entry knows about - whichever is
    further along of the reading site's latest release and the user's own
    last-read chapter. Scanlation sites routinely run ahead of MangaDex,
    so this is what "next chapter" gets counted from rather than however
    far MangaDex's own feed happens to have got (see mangadex.
    fetch_next_chapter)."""
    if entry.get("type") not in MANGA_TYPES:
        return None
    return max(parse_chapter_progress(entry.get("progress")),
               entry.get("last_watched_chapter") or 0.0)


def parse_episode_progress(text):
    """"S1E10" / "E10" / "10" -> (season, episode) ints, or (0, 0) if the
    text doesn't match any of those (e.g. old freeform notes) - the form
    falls back to preserving that original text on save rather than
    silently replacing it with a blank/zero progress."""
    text = (text or "").strip()
    match = _EPISODE_SEASON_RE.match(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _EPISODE_ONLY_RE.match(text)
    if match:
        return 0, int(match.group(1))
    return 0, 0


def format_episode_progress(season, episode):
    if not season and not episode:
        return ""
    if season:
        return f"S{season:02d}E{episode:02d}"
    return f"E{episode:02d}"


def parse_chapter_progress(text):
    """Chapter numbers are sometimes fractional (scanlations split a
    chapter into e.g. "24.5"), so this is a float, not an int."""
    try:
        return float((text or "").strip())
    except ValueError:
        return 0.0


def format_chapter_progress(value):
    return "" if not value else f"{value:g}"


def progress_moves_forward(entry, season, episode) -> bool:
    """Whether S`season`E`episode` is actually further along than what
    `entry` already stores.

    The one comparison rule, shared by every writer (record_progress
    below and the Stremio sync in _on_progress_synced) so there cannot be
    two answers to "is this newer". Re-opening an old episode is an
    ordinary thing to do and must never be recorded as having lost the
    rest.

    A season of 0 means "this source doesn't number seasons", not
    "season zero": measured against a plain episode list, S01E04 stored
    against an incoming E05 compares (0,5) < (1,4) and would refuse every
    episode forever. So an unnumbered season is compared on the episode
    alone, against whatever season the entry is already on."""
    seen_season, seen_episode = parse_episode_progress(entry.get("progress"))
    if not season:
        return episode > seen_episode
    return (season, episode) > (seen_season, seen_episode)


def _progress_data_file(entry) -> str:
    """Which tracker file this entry lives in. Everything watched -
    Anime included, since it merged into the Movies & Series page - is
    series.json; the reading types keep tracker.json (see main.py's
    one-time _merge_anime_into_series for how existing Anime rows moved
    over)."""
    return (SeriesPage.DATA_FILE if entry.get("type") in VIDEO_TYPES
            else MangaPage.DATA_FILE)


def _entry_medium(entry) -> str:
    """Which release-schedule source answers for this entry.

    Per entry rather than per page now: the merged watch page holds
    Anime (AniList's airing schedule) beside Series/Movie (TVmaze), so
    one page-level MEDIUM would send every anime's lookup to the wrong
    service."""
    entry_type = (entry or {}).get("type")
    if entry_type in MANGA_TYPES:
        return release_schedule.MEDIUM_MANGA
    if entry_type == "Anime":
        return release_schedule.MEDIUM_ANIME
    return release_schedule.MEDIUM_SERIES


def correct_progress(entry, *, season=None, episode=None, chapter=None) -> bool:
    """Set progress to an exact number, including a lower one.

    The deliberate escape hatch from record_progress's forward-only
    rule. That rule is right for automatic writes - re-opening chapter 3
    of something you are 300 into must not erase 300 - but with the
    manual +/- controls gone it left no way at all to undo a wrong
    number, and wrong numbers do happen: an episode matched to the wrong
    season, a chapter list that renumbered, a Stremio sync that ran
    ahead. A number nobody can fix is worse than one that can move both
    ways when a person asks it to.

    Separate function rather than a flag on record_progress, so nothing
    automatic can reach it by accident - every caller of this one is a
    person typing a number."""
    return _write_progress(entry, season=season, episode=episode,
                           chapter=chapter, forward_only=False)


def record_progress(entry, *, season=None, episode=None, chapter=None) -> bool:
    """Record what was just opened - the automatic path, forward only.

    The player calls it with `season`/`episode` for what it played, the
    reader with `chapter` for what it opened. Returns True when the
    stored number actually moved.

    Forward-only is the whole point: opening chapter 3 of something you
    are 300 chapters into must leave 300 alone. To set a number
    deliberately, including downwards, use correct_progress."""
    return _write_progress(entry, season=season, episode=episode,
                           chapter=chapter, forward_only=True)


def _record_history(entry, *, season=None, episode=None, chapter=None):
    """Note an open in History, and tick what was opened.

    Never raises and never blocks the write it rides along with: a
    history file that cannot be written costs a list, not playback.
    Ticks forward only in the sense that opening something marks it -
    unticking is a deliberate act through the details page's menu."""
    try:
        if entry.get("type") in MANGA_TYPES:
            mark = history.chapter_key(chapter)
            shown = f"Ch {float(chapter):g}" if chapter is not None else None
        else:
            mark = history.episode_key(season, episode)
            shown = (format_episode_progress(int(season or 0), int(episode))
                     if episode else None)
        history.touch(entry, progress=shown)
        if mark:
            history.set_watched(entry, mark, True)
    except Exception:
        pass


def _write_progress(entry, *, season=None, episode=None, chapter=None,
                    forward_only: bool = True) -> bool:
    """Record what was just opened onto `entry` - the single place
    progress is written now that nothing sets it by hand.

    The player calls it with `season`/`episode` for what it played, the
    reader with `chapter` for what it opened; each passes only what it
    actually has. Returns True when the stored number moved (and so the
    caller may want to redraw), False for every refusal below.

    Updates the caller's own dict *and* the saved file, through
    storage.update_entry for this one entry - never a whole list back
    from a page, see .claude/rules/ui.md.

    Refusals, all deliberate:
      * a type that has no progress at all (tracks_progress - a Movie is
        one video, there is no episode to be on),
      * a number that is not further along than the stored one. This is
        the rule the whole path exists around: opening chapter 3 of
        something you are 300 chapters into must leave 300 alone. The
        comparison is progress_moves_forward, not each caller's own.
      * an entry with no id, which nothing could be written against.

    Deliberately *not* refused: an entry whose progress Stremio also
    syncs. Atomic played it, so Atomic knows; the sync is held to the
    same forward-only rule at its own end (_on_progress_synced) rather
    than the two overwriting each other in turn.

    History is written first and under none of these rules: an unsaved
    title has no id and a film has no episode, and both of those are
    still things the user watched (see helpers/history)."""
    if not isinstance(entry, dict):
        return False
    entry_type = entry.get("type")
    _record_history(entry, season=season, episode=episode, chapter=chapter)
    if not entry.get("id"):
        return False
    if not tracks_progress(entry_type):
        return False

    if entry_type in MANGA_TYPES:
        try:
            number = float(chapter)
        except (TypeError, ValueError):
            return False
        # Chapters are floats (scanlations split one into "24.5" - see
        # parse_chapter_progress), and this is the field the card, Home
        # and the release schedule all read. `progress` is not touched:
        # for reading that holds the *site's* latest release, which is a
        # different number entirely.
        if forward_only and number <= (entry.get("last_watched_chapter") or 0.0):
            return False
        fields = {"last_watched_chapter": number}
    else:
        try:
            season_n, episode_n = int(season or 0), int(episode)
        except (TypeError, ValueError):
            return False
        if not episode_n:
            return False
        if forward_only and not progress_moves_forward(entry, season_n, episode_n):
            return False
        if not season_n:
            # Keep the season the entry is already on rather than writing
            # "E05" over "S02E04": dropping the season would read as a
            # move backwards to the very next comparison.
            season_n = parse_episode_progress(entry.get("progress"))[0]
        fields = {"progress": format_episode_progress(season_n, episode_n),
                  # Playing it is the confirmation. Without this the card
                  # goes on hiding the number (_progress_display shows
                  # nothing unverified) and playing would look like it
                  # recorded nothing.
                  "progress_verified": True,
                  "progress_source": SOURCE_IN_APP}

    fields["updated_at"] = storage.now_iso()
    entry.update(fields)
    return storage.update_entry(_progress_data_file(entry), entry["id"], fields)


def _top_window(widget):
    """The main window above this widget, for the player/reader to cover.

    window() rather than walking parents by hand, and not mapToGlobal-
    adjacent in any way: the pages size themselves from the window's own
    geometry (see .claude/rules/ui.md on why anything else lands on the
    wrong monitor)."""
    try:
        return widget.window() if widget is not None else None
    except (AttributeError, RuntimeError):
        return None


def open_in_app(parent, entry, resume=True) -> bool:
    """Open this entry inside Atomic - the video player for Anime/Series/
    Movie, the reader for Manga/Manhwa/Manhua.

    `resume` is which of a card's two targets was pressed - the round
    continue button on its cover, or the rest of the card - and it means
    the corresponding thing for each medium:

      * reading: False opens the reader on the chapter list instead of
        jumping back to where reading stopped;
      * watching: False plays the *next* episode, which is what a card
        has always done. True goes back to an episode left part-watched
        if there is one (player.resume_point), and falls through to the
        same next episode when there is not - so the button is never a
        dead control.

    Returns False when in-app opening isn't possible at all, so the
    caller can fall back to the browser exactly as before. Deliberately
    soft: this is new, the browser path is what has worked for versions,
    and an import error or a missing engine must degrade to the old
    behaviour rather than leave a card that does nothing when clicked."""
    window = _top_window(parent)
    if window is None:
        return False
    entry_type = (entry or {}).get("type")
    try:
        # The body of a card browses; only the continue button resumes.
        # Browsing is the details page now - artwork, facts, and the
        # episode/chapter list with watched marks - for reading and
        # video alike. Before it existed the video body played the next
        # episode, which made it indistinguishable from continue.
        if not resume and entry_type in MANGA_TYPES + VIDEO_TYPES:
            from windows import details
            page = details.open_details(window, entry)
            _wire_overlay_refresh(page, parent, entry)
            return bool(page)
        if entry_type in MANGA_TYPES:
            from windows import reader
            page = reader.open_reader(window, entry, resume=resume)
            _wire_overlay_refresh(page, parent, entry)
            return bool(page)
        if entry_type in VIDEO_TYPES:
            from windows import player
            season = episode = None
            point = player.resume_point(entry)
            if point:
                season, episode = point
            page = player.open_player(window, entry, season=season,
                                      episode=episode)
            _wire_overlay_refresh(page, parent, entry)
            return bool(page)
    except Exception:
        logs.exception("in-app open failed; falling back to the browser")
        return False
    return False


def _wire_overlay_refresh(page, parent, entry):
    """Tell the page that opened an overlay when that overlay closes, so
    its cards can redraw the new progress immediately - no page switch,
    no Sync click (the owner's ask).

    Soft on both ends: a caller with no `_on_inapp_closed` (Home, a
    test) simply is not notified, and the hook call is wrapped because
    the page can have been rebuilt or torn down while the player ran -
    a RuntimeError escaping a Qt slot is an abort, not a traceback."""
    hook = getattr(parent, "_on_inapp_closed", None)
    if page is None or not callable(hook):
        return

    def notify():
        try:
            # The id is read when the overlay closes, not captured when
            # it opened: a Discover title arrives here with no id at all
            # and gains one if the details page's Save writes it, and a
            # captured None would leave the page unable to pick the new
            # entry up. Asked of the overlay's own dict as well: the
            # details page works on a *copy* (`self.entry = dict(entry)`
            # in its __init__), so the id its Save stamps in place never
            # reaches the dict this closure holds - measured: the row hit
            # disk, closed fired, and the hook still got None, which is
            # why a title saved off Discover didn't show in Saved until
            # the page was rebuilt.
            page_entry = getattr(page, "entry", None)
            fallback = page_entry.get("id") if isinstance(page_entry, dict) else None
            hook((entry or {}).get("id") or fallback)
        except RuntimeError:
            pass        # the page is gone; the next visit rebuilds anyway
    try:
        page.closed.connect(notify)
    except AttributeError:
        pass            # an overlay with no closed signal - nothing to wire


def open_tracker_entry(parent, entry, resume=True):
    """Open an entry's page: for Manga, its matched page on its reading
    site (or that site's search results for the title, if no specific
    page was matched) - the configured Manga Music site (if any) opens
    first, so it starts loading/playing before the reading tab takes
    focus. For Anime, a saved stremio:// deep link (opens Stremio) if the
    entry uses the built-in Stremio option, or its configured Video
    Website's search results otherwise. For Series, a saved stremio://
    link or plain URL as-is. Returns False if there's nothing to open.

    `resume` reaches the in-app reader only (see open_in_app); the
    browser fallbacks below have one page to open either way."""
    # In-app first: the player for video, the reader for everything read.
    # Falls through to the browser routes below whenever that can't work
    # (no engine, an unreadable source, an import that isn't there), so
    # the behaviour every previous version had is still what happens when
    # the new path can't.
    if open_in_app(parent, entry, resume=resume):
        return True
    if entry.get("type") in MANGA_TYPES:
        return _open_manga_entry(parent, entry)
    # Series and Movie take the same route as Anime now that they can
    # carry a Video Website of their own - an entry pinned to Netflix
    # has to open there, not on whatever url a Stremio match left behind.
    if entry.get("type") in VIDEO_TYPES and entry.get("site_id"):
        return _open_anime_entry(parent, entry)
    if entry.get("type") == "Anime":
        return _open_anime_entry(parent, entry)

    url = entry.get("url")
    if url:
        if url.startswith("stremio://"):
            stremio.launch(url)
        else:
            webbrowser.open(url)
        return True
    return False


def _open_manga_entry(parent, entry):
    opened_url = entry.get("url")
    if not opened_url:
        site = manga_sites.get_site(entry["site_id"]) if entry.get("site_id") else None
        if site and site.get("base_url"):
            opened_url = manga_sites.search_page_url(site["base_url"], entry["title"])
    if not opened_url:
        return False
    music_url = app_settings.get_manga_music_url()
    if music_url:
        webbrowser.open(music_url)
    webbrowser.open(opened_url)
    return True


def _open_anime_entry(parent, entry):
    url = entry.get("url")
    if url:
        if url.startswith("stremio://"):
            stremio.launch(url)
        else:
            webbrowser.open(url)
        return True
    site = anime_sites.get_site(entry["site_id"]) if entry.get("site_id") else None
    if not (site and site.get("base_url")):
        return False
    # No saved url, but a site to ask. Two ways an entry gets here
    # without ever having been asked: saved before the add-form's
    # background lookup landed, and carried over by _migrate (which sets
    # site_id off the retired "Anime Opens In" setting and has no url to
    # set). Both used to mean the search page forever, since nothing
    # re-resolved one. Ask once - the answer, hit or miss, is recorded on
    # the entry so a genuine no-match doesn't re-ask on every open.
    if not entry.get("site_url_checked_at"):
        threading.Thread(target=_resolve_then_open_anime, args=(entry, site), daemon=True).start()
        return True
    # Asked already and there was no page - that site's search results
    # for the title is all that's left; a fallback, never the target.
    webbrowser.open(anime_sites.search_page_url(site["base_url"], entry.get("title", "")))
    return True


def _resolve_then_open_anime(entry, site):
    """Resolve `entry`'s page on `site`, cache it, and open it - or the
    site's search page if there is none. Runs on a worker thread and so
    must never raise: an uncaught exception here would kill the thread
    and the click would silently open nothing at all."""
    title = entry.get("title", "")
    url = None
    try:
        url = anime_sites.resolve_page_url(site, title)
    except Exception:
        url = None
    try:
        fields = {"site_url_checked_at": storage.now_iso()}
        if url:
            fields["url"] = url
        entry.update(fields)  # the in-memory copy this page is holding
        storage.update_entry(_progress_data_file(entry), entry.get("id"), fields)
    except Exception:
        pass  # a page that opens beats a page that saved
    try:
        webbrowser.open(url or anime_sites.search_page_url(site["base_url"], title))
    except Exception:
        pass


def _migrate(entries, data_file):
    changed = False
    # Anime used to have one global Settings toggle ("Anime Opens In":
    # Stremio or Crunchyroll) instead of today's per-entry Video Website.
    # Entries saved under the old Crunchyroll choice have no url and no
    # site_id - point them at a Crunchyroll site (adding the default one
    # back if the user had since removed it) so they keep opening the
    # same place instead of silently going nowhere.
    old_provider = storage.load(app_settings.SETTINGS_FILE, {}).get("anime_provider")
    crunchyroll_site_id = None
    if old_provider == "crunchyroll":
        crunchyroll_site_id = next(
            (s["id"] for s in anime_sites.list_sites() if s["name"] == "Crunchyroll"), None)
        if crunchyroll_site_id is None:
            crunchyroll_site_id = anime_sites.add_site(
                "Crunchyroll", "https://www.crunchyroll.com/")["id"]

    for entry in entries:
        if not entry.get("id"):
            # Everything that touches one entry rather than the whole
            # list keys off this - update_entry, the background lookups
            # reporting back, and now drag-to-reorder, which would
            # otherwise see every id-less entry as the same one.
            entry["id"] = str(uuid.uuid4())
            changed = True
        if (crunchyroll_site_id and entry.get("type") == "Anime"
                and not entry.get("url") and not entry.get("site_id")):
            entry["site_id"] = crunchyroll_site_id
            changed = True
        if "added_at" not in entry:
            entry["added_at"] = storage.now_iso()
            changed = True
        if "updated_at" not in entry:
            entry["updated_at"] = entry["added_at"]
            changed = True
        if "url" not in entry:
            entry["url"] = ""
            changed = True
        # Fix up links saved by earlier versions: missing 3rd slash
        # (Stremio parsed "detail" as an addon-manifest host instead of
        # a path), and a trailing videoId segment that made Stremio jump
        # into a specific episode instead of the show's overview page.
        if entry.get("url", "").startswith("stremio://detail/"):
            entry["url"] = entry["url"].replace("stremio://detail/", "stremio:///detail/", 1)
            changed = True
        if entry.get("url", "").startswith("stremio:///detail/"):
            parts = entry["url"].split("/")
            if len(parts) >= 2 and parts[-1] == parts[-2]:
                entry["url"] = "/".join(parts[:-1])
                changed = True
        # Backfill the id progress-syncing needs from a saved stremio://
        # link, for entries saved before it got its own field (see
        # _entry_imdb_id/EntryForm._save) - a Video-Website Anime entry
        # has no stremio:// url to pull this from, so those need re-picking
        # the suggestion once to start syncing, but that's a one-time gap
        # for entries saved in the narrow window before this existed.
        if not entry.get("imdb_id"):
            found_id = _imdb_id_from_url(entry.get("url"))
            if found_id:
                entry["imdb_id"] = found_id
                changed = True
        # Older saves used one shared "Watching/Reading" status wording;
        # split it into the per-type wording (Watching vs Reading).
        legacy = _LEGACY_STATUS_MAP.get(entry.get("status"))
        if legacy:
            entry["status"] = legacy.get(entry.get("type"), entry["status"])
            changed = True
    if changed:
        storage.save(data_file, entries)
    return entries


class _ProgressSyncSignals(QObject):
    # entry id, progress season/episode, whether a real (not estimated)
    # progress result was found, total available season/episode, whether
    # this came from the bulk "refresh all" (vs one entry's right-click
    # Sync Progress) - silent ones skip the per-entry not-found popup -
    # why nothing was found, when that is known ("" when it isn't) -
    # not-found has several causes that look identical on screen and the
    # user has to be able to tell them apart, see REASON_* below - and
    # which source the number that *was* found came from, so a wrong one
    # is never anonymous again (see SOURCE_* below).
    resolved = Signal(str, int, int, bool, int, int, bool, str, str)


# Where a synced number came from. Stored on the entry and shown on
# hover: a progress number the user disagrees with is unarguable until
# it says who said it.
SOURCE_STREMIO = "Stremio"
SOURCE_MANUAL = "you"
# Written by record_progress - the app watched you open it. This is now
# the ordinary case: nothing sets progress by hand any more, so
# SOURCE_MANUAL only ever appears on numbers saved by an older version.
SOURCE_IN_APP = "Atomic"

# The two things that can stop a sync and that the user can fix - no
# Stremio account connected, or one whose saved session Stremio no longer
# accepts. Everything else is a genuine no-match.
REASON_NO_STREMIO_ACCOUNT = "no_stremio_account"
# A connected account that has stopped answering (stremio.AuthFailed).
# Separate from the one above because the fix is different - reconnect,
# not connect - and separate from a no-match because Stremio is the only
# progress source there is: when the key dies, *nothing* syncs, and the
# whole point of this code is that the app says that out loud rather than
# reporting "not in your library" about every entry forever.
REASON_STREMIO_AUTH_FAILED = "stremio_auth_failed"

# Said once per app run, not once per page arrival. The arrival sync is
# deliberately silent (see TrackerPage._auto_refresh), so this is the only
# way the silent path can speak at all - but a dead key stays dead, and
# walking between Anime and Series would otherwise repeat the same warning
# every few seconds.
_auth_warning_shown = False
_STREMIO_AUTH_TOAST = "Stremio Sign-In Needs Refreshing - Reconnect In Settings"

# Services whose watch history no app can read. Kept only to explain why
# an entry on one of them never fills itself in; nothing tries to sync
# them any more.
_UNREADABLE_NAMES = {"netflix": "Netflix", "crunchyroll": "Crunchyroll"}


class _CoverSignals(QObject):
    ready = Signal(str, str, str)  # entry id, new cover_url, local cache path


class _ScheduleSignals(QObject):
    # entry id, next_release dict (or None when nothing's scheduled), the
    # MangaDex id the title resolved to (or None - manga only, see
    # release_schedule.fetch), and which refresh run asked for it
    # (0 = none, see TrackerPage._refresh_run)
    resolved = Signal(str, object, object, int)


def _if_alive(callback):
    """Run `callback`, unless the widget it belongs to has been deleted.

    Everything deferred past a page's first paint (see
    TrackerPage._after_first_paint) can outlive the page - walking to
    another section, or another page, is one click away and Qt deletes
    the old one - and touching a deleted QWidget from Python raises
    RuntimeError inside a slot, which takes the whole process down
    (helpers/logs.py records that trap)."""
    try:
        callback()
    except RuntimeError:
        pass


def _clear_layout(layout):
    """Empty a layout, with what was in it gone from the screen *now*.

    `setParent(None)` before `deleteLater()`, and that order is the
    point: deleteLater only queues the destruction, and the widget goes
    on painting until the event loop gets round to the deferred delete.
    Measured on the Discover tab - re-running a search drew the new
    "Results" heading directly over the old "Popular Now" one, both
    legible at once. Unparenting hides it in the same call.

    The Saved grid's own _refresh_grid deliberately still does it the old
    way: it has always worked there, its rebuild is followed by a layout
    pass either way, and this task is not the place to change what that
    page does.

    **Nested layouts are emptied too, and that is a bug fix.** An item
    holding a layout answers `widget()` with None, so a header added
    with `addLayout` survived every rebuild: History's "N titles" line
    and its Clear button stayed on screen as a floating label, and
    clearing the history left the *old count* sitting above the empty
    section (the owner reported both). Recursing into the child layout
    empties it before it is dropped, so nothing it held outlives the
    rebuild."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # hide() before the unparent - see details._clear_rows for
            # the whole story: without it a queued show lands on a
            # parentless widget and Qt gives it a desktop window of its
            # own, which is what flashed white during a fetch.
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()


class _DiscoverSignals(QObject):
    # Discover's crossings back to the UI thread. The search-driven
    # ones carry the run number that asked for them: typing fires a
    # new lookup per debounce pause, and a slow row answering after
    # the query moved on would otherwise fill the rows under a
    # different search. The schedule/history ones deliberately don't -
    # see each signal's own note.
    #
    # results: row kind ("anime"/"series"/"movie"/"reading"), the rows'
    # dicts as helpers/discover returns them, run.
    results = Signal(str, list, int)
    # poster: row kind, index within that row, the cached file path ("" =
    # nothing downloaded), the cover URL the file came from ("" when the
    # row already carried one - it is only reported when the worker had
    # to resolve it from the series page, so the UI thread can write it
    # back onto the row's dict; see _on_discover_poster), run.
    poster = Signal(str, int, str, str, int)
    # The hero banner's ground - a local backdrop path, run.
    featured_backdrop = Signal(str, int)
    # A History row's cover: the history key, the downloaded path. No
    # run number - History is not a search, so nothing can arrive
    # "under a later query"; the key is what says which row it belongs
    # to and a stale one simply matches nothing.
    history_cover = Signal(str, str)
    # The Schedule tab's catalogue-wide rows - everything airing
    # soon (or, on the Read page, just released), not only what is
    # saved. [row dicts], ok. **No run number, like history_cover
    # and unlike everything above**: the schedule is not a search -
    # any answer is the current calendar - and stamping it with
    # _discover_run meant a Discover visit or keystroke while the
    # rows were in flight threw them away for good (the owner's
    # "the schedule is not working"). `ok` False means the fetch
    # failed and should be retried on the next visit.
    schedule_upcoming = Signal(object, bool)
    # One schedule row's cover, downloaded after the rows were already
    # drawn (they no longer wait on art - see _schedule_upcoming_worker):
    # the row's own item dict, whose identity keys _schedule_cover_labels,
    # and the local file path. No run number, same reason as above.
    schedule_cover = Signal(object, str)
    # A catalogue schedule row resolved to something the details page
    # can hold: the clicked row's item dict, the resolved catalogue
    # row ({} = nothing matched well enough to open).
    schedule_open = Signal(object, object)


class _StayOpenMenu(QMenu):
    """A menu that stays open while its checkable items are ticked.

    Ticking one filter used to close the whole menu, so choosing two
    statuses meant opening it twice and the grid only ever changed after
    the menu had vanished. Qt closes on mouse release; for a checkable
    item this triggers the action itself - which is what redraws the grid,
    so the change lands while the menu is still up - and returns before
    the base class can dismiss it."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


# Each page's search text and filter ticks, keyed by page class name,
# for as long as the process lives. Pages rebuild from scratch on every
# visit (.claude/rules/ui.md), so without this a search typed on Anime
# was gone the moment you looked at Home and came back.
#
# Deliberately in memory and never written to storage: a filter narrowing
# the grid on a fresh launch, with nothing on screen saying why entries
# are missing, is worse than today's reset. Quitting clears it, which is
# the visible escape hatch.
_SESSION_VIEW_STATE = {}


# ---- the continue control on a reading card ------------------------------
# A reading card has two targets. The round button in the middle of its
# cover resumes where reading stopped; everything else on the card opens
# the chapter list. So the cover frosts over on hover - which is what
# says the artwork has stopped being the subject and the button has
# become it - and the button appears over the frost.
#
# **The blur is gone**, at the owner's ask ("do not make a blur... make
# it icy"): the artwork stays sharp under a translucent sheet of the
# palette's own ice, so a hovered card reads as glass laid over the
# cover rather than the cover being degraded. One pixmap built once and
# cross-faded by opacity - still nothing per paint.
#
# The frost, top to bottom, as (stop, r, g, b, a). A cover is as often
# pale as dark and the gold button has to read against both, so this is
# the palette's warm smoke with a pale champagne highlight along the
# top - and it keeps the darkening the button needs where the button
# actually sits.
COVER_FROST = (
    (0.0, 245, 233, 205, 70),      # pale champagne catching the light
    (0.35, 61, 50, 30, 150),       # the palette's warm smoke, mid-panel
    (1.0, 16, 13, 8, 190),         # deepest at the foot, where meta sits
)
CONTINUE_BUTTON_SIZE = 46
# Freeze and thaw take different times on purpose. In: slow enough to
# read as a transition rather than a flash while sweeping a shelf. Out:
# noticeably quicker, because the owner's ask is that the artwork is
# back "the moment" the pointer leaves - a thaw as slow as the freeze
# read as the frost sticking.
COVER_FADE_IN_MS = 220
COVER_FADE_OUT_MS = 110


def frosted_cover(pixmap: QPixmap) -> QPixmap:
    """`pixmap` under the ice gradient - the hovered state of a cover.

    The artwork is copied sharp (no downscale round trip any more) and
    the frost painted over it. devicePixelRatio is carried across by
    hand because QPixmap.copy keeps it but the paint must land in device
    pixels (.claude/rules/ui.md)."""
    if pixmap is None or pixmap.isNull():
        return pixmap
    ratio = pixmap.devicePixelRatio() or 1.0
    frosted = QPixmap(pixmap)
    painter = QPainter(frosted)
    # SourceAtop, not the default SourceOver: covers come rounded now
    # (images._round_corners), and an over-composite would paint the
    # frost into the transparent corners too - square frost corners
    # hanging past rounded art. Atop lands only where the art has
    # pixels, so the frost inherits the clip for free.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
    gradient = QLinearGradient(0.0, 0.0, 0.0, float(frosted.height()))
    for stop, red, green, blue, alpha in COVER_FROST:
        gradient.setColorAt(stop, QColor(red, green, blue, alpha))
    painter.fillRect(frosted.rect(), gradient)
    painter.end()
    frosted.setDevicePixelRatio(ratio)
    return frosted


class _ContinueButton(QPushButton):
    """The round resume control that appears in the middle of a hovered
    cover.

    The triangle is painted rather than set as a Segoe Fluent glyph. The
    player's play disc uses the glyph and is right to - it is 62px. This
    one is 46, and at that size the glyph's own side bearings put it
    visibly left of the circle's centre, where a polygon can be placed on
    the centre and given the small nudge a triangle needs to *look*
    centred (its visual weight sits behind its tip)."""

    def __init__(self, parent=None, tooltip="Continue from where you stopped"):
        super().__init__(parent)
        self.setFixedSize(CONTINUE_BUTTON_SIZE, CONTINUE_BUTTON_SIZE)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        radius = CONTINUE_BUTTON_SIZE // 2
        # A hollow ring, not a filled disc (the owner's ask): accent
        # border, empty middle, so the artwork stays visible through the
        # control until it is actually aimed at - then it fills, which
        # is the hover answering "this is the thing you press".
        # padding: 0px explicitly. The app-wide QPushButton rule sets
        # 8px 16px, which on a small fixed square eats the whole content
        # box - player.py and reader.py both carry the same note, from
        # the same measured clipping.
        self.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" border: 2px solid {theme.ACCENT}; padding: 0px;"
            f" border-radius: {radius}px; }}"
            f"QPushButton:hover {{"
            f" background: {theme.accent_gradient(0, 0, 0, 1, hover=True)};"
            f" border: 2px solid {theme.ACCENT_HOVER}; }}"
            f"QPushButton:pressed {{ background: {theme.ACCENT_ACTIVE}; }}")
        use_hover_cursor(self)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        # The triangle follows the fill: accent while the ring is empty,
        # ON_ACCENT once the hover fills it - never white, which on this
        # gold computes to 1.8:1 (see the palette note in theme.py).
        painter.setBrush(QColor(theme.ON_ACCENT if self.underMouse()
                                else theme.ACCENT))
        side = self.width()
        height = side * 0.36
        width = height * 0.86
        centre_x = side / 2 + side * 0.04
        centre_y = side / 2
        painter.drawPolygon(QPolygonF([
            QPointF(centre_x - width / 2, centre_y - height / 2),
            QPointF(centre_x - width / 2, centre_y + height / 2),
            QPointF(centre_x + width / 2, centre_y)]))
        painter.end()


class ContinueCover(QLabel):
    """A cover that frosts over and offers a continue button while its
    card is hovered.

    The button is a child of this label rather than a row in the card's
    layout, and that is the point: a layout row would make every reading
    card taller than every other card on the page, and the whole grid
    would reflow the moment the pointer arrived."""

    def __init__(self, pixmap, size, on_continue, parent=None, tooltip=None):
        super().__init__(parent)
        self.setFixedSize(*size)
        self._sharp = pixmap
        self._frosted = None
        # How much of the frosted copy is on screen, 0-1. Painted by
        # hand rather than swapped with setPixmap: a swap is a step, and
        # a step across a shelf of covers is a flash. See paintEvent.
        self._mix = 0.0
        self._fade = QVariantAnimation(self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade)
        self.button = (_ContinueButton(self, tooltip) if tooltip
                       else _ContinueButton(self))
        # Wrapped so the callback is invoked with NO arguments.
        # QPushButton.clicked emits `clicked(checked: bool)`, and every
        # caller here passes a lambda whose first default argument is the
        # entry - so Qt's bool silently *replaced the entry*, and every
        # continue press died with "'bool' object has no attribute
        # 'get'" (nine times in the owner's log). Fixing it here fixes
        # every caller at once, including Home's.
        self.button.clicked.connect(lambda checked=False: on_continue())
        self.button.hide()
        self.button.move((self.width() - self.button.width()) // 2,
                         (self.height() - self.button.height()) // 2)

    def _on_fade(self, value):
        self._mix = float(value)
        # The button belongs to the frosted state, so it arrives with it
        # rather than a beat before: shown as soon as there is any frost
        # to sit on, gone only once the artwork is sharp again.
        self.button.setVisible(self._mix > 0.02)
        self.update()

    def set_hovered(self, hovered):
        if hovered and self._frosted is None:
            # Built on the first hover and kept, not built up front: a
            # Reading page can hold a hundred covers, and frosting all of
            # them at build time is a hundred paints for the one card
            # the pointer will actually reach.
            self._frosted = frosted_cover(self._sharp)
        target = 1.0 if (hovered and self._frosted is not None) else 0.0
        if abs(target - self._mix) < 1e-3:
            return
        self._fade.stop()
        # Freeze slow, thaw fast - see COVER_FADE_IN_MS/_OUT_MS.
        self._fade.setDuration(COVER_FADE_IN_MS if target > self._mix
                               else COVER_FADE_OUT_MS)
        # From wherever it actually is, not from 0/1: crossing back over
        # a card mid-thaw must not restart the whole fade.
        self._fade.setStartValue(self._mix)
        self._fade.setEndValue(target)
        self._fade.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self._sharp is not None and not self._sharp.isNull():
            painter.drawPixmap(0, 0, self._sharp)
        if self._mix > 0.0 and self._frosted is not None:
            painter.setOpacity(self._mix)
            painter.drawPixmap(0, 0, self._frosted)
        painter.end()


class _CardHoverRelay(QObject):
    """Turns "the pointer is somewhere on this card" into one on/off for
    a ContinueCover.

    It cannot simply be the card's own Enter/Leave. Qt sends a widget
    Leave the moment the pointer crosses onto one of its children, so a
    card with a cover, a title and a button inside it produces a Leave
    every time the pointer moves from one of them to the next - and the
    button would vanish at the exact moment it was being aimed at.

    So: any Enter shows, any Leave *schedules* a hide one turn of the
    event loop later, and an Enter arriving before that turn cancels it.
    A Leave and the Enter that follows it are delivered together, so
    crossing between children never hides anything, while genuinely
    leaving the card has no Enter behind it and does. This needs nothing
    from underMouse() and does not depend on which of the pair Qt sends
    first."""

    # How often the safety poll re-checks a hovered card. Cheap - it runs
    # only while exactly one card is frosted, and stops the moment it
    # thaws.
    POLL_MS = 250

    def __init__(self, cover, card, parent=None):
        super().__init__(parent)
        self._cover = cover
        self._card = card
        self._leaving = False
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._verify)

    def _pointer_on_card(self) -> bool:
        """Whether the widget under the cursor belongs to this card.

        A widget-tree hit-test, never coordinate arithmetic: the first
        version compared `mapFromGlobal(QCursor.pos())` against the
        card's rect, and on this machine's mixed-DPI monitors that maps
        through the wrong screen's scale factor (the same failure
        .claude/rules/ui.md records for mapToGlobal). The symptom was
        both halves of what the owner reported - a frost stuck on after
        the pointer left, and the continue button vanishing under a
        pointer that was still on the card - because the answer was
        wrong in both directions depending on which monitor."""
        under = QApplication.widgetAt(QCursor.pos())
        while under is not None:
            if under is self._card:
                return True
            under = under.parentWidget()
        return False

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Enter:
            self._leaving = False
            self._cover.set_hovered(True)
            self._poll.start()
        elif event.type() == QEvent.Type.Leave:
            self._leaving = True
            QTimer.singleShot(0, self._settle)
        return False

    def _settle(self):
        if not self._leaving:
            return
        try:
            # Where the pointer actually is beats what the events said:
            # the continue button's own hover filter and tooltip can
            # leave the card holding a Leave with no Enter behind it
            # while the pointer never left. See _pointer_on_card.
            if self._pointer_on_card():
                self._leaving = False
                return
            self._unhover()
        except RuntimeError:
            # The page rebuilt and took the cover with it - pages here are
            # thrown away and rebuilt on every visit (.claude/rules/ui.md),
            # so a queued callback outliving its widget is ordinary.
            pass

    def _verify(self):
        """The backstop for the events that never come. Clicking a card
        opens the player or the details page *over* it - the pointer
        never moves, so no Leave ever fires, and the frost stayed on
        under the overlay and was still there when it closed. While a
        card is frosted this asks every quarter-second whether the
        cursor is still genuinely on it (an overlay on top means it is
        not - widgetAt answers with the overlay), and thaws it the
        moment it is not."""
        try:
            if not self._pointer_on_card():
                self._unhover()
        except RuntimeError:
            self._poll.stop()

    def _unhover(self):
        self._poll.stop()
        self._leaving = False
        self._cover.set_hovered(False)


def attach_continue_cover(card, cover):
    """Wire `cover`'s frost/button to hover anywhere on `card`.

    The filter goes on the card *and* on every widget inside it, because
    each of those steals the hover from the card as the pointer crosses
    it - see _CardHoverRelay. An event filter rather than a Card subclass:
    Card is shared by every page in the app, and this belongs to reading
    cards only."""
    relay = _CardHoverRelay(cover, card, card)
    card.installEventFilter(relay)
    for child in card.findChildren(QWidget):
        child.installEventFilter(relay)
    return relay


# The Discover hero is widgets.HeroBanner - Harbor's backdrop-and-scrim
# banner, shared with Home's continue hero so the two heads of the app
# stay one design. It replaces the poster-beside-facts panel (the
# owner's ask: "change the feature design ... to make it look like
# Harbor").


def _history_when(stamp) -> str:
    """"Just now" / "3h ago" / "12 Aug" for a history row's timestamp.
    Relative near the present, absolute past a week - which is how a
    person actually reads "when did I watch this"."""
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


def discover_entry(item, entry_type):
    """A catalog row (a Discover card, a genre-browse pick) as an entry
    dict the details page (and a quick save) can hold. Deliberately
    id-less: an id is what says "this is in Saved", and it is written
    only when a save actually happens - the details page's Save button
    keys its whole offer off that. Module-level because the details
    page's genre browse builds these too."""
    return {
        "title": (item.get("title") or "").strip(),
        "type": entry_type,
        "status": STATUSES_BY_TYPE.get(entry_type, _WATCHING_STATUSES)[0],
        "progress": "",
        "progress_verified": False,
        "latest_available": "",
        "last_watched_chapter": None,
        "show_last_watched": True,
        # The site a reading row came from, carried through so the
        # details page lists that site's chapters straight away instead
        # of asking where to read it (the owner's ask). Empty for the
        # video rows, which have no such binding.
        "url": item.get("url") or "",
        "cover_url": item.get("poster") or None,
        "cover_path": None,
        "site_id": item.get("site_id"),
        "imdb_id": item.get("imdb_id") or None,
    }


class TrackerPage(GlassPage):
    """Base for the Anime/Manga/Series pages. Subclasses set
    DATA_FILE/ENTRY_TYPES/TITLE/TYPE_OPTIONS/PROGRESS_COLUMNS.

    Entries show as a poster grid (click to open, right-click for Edit/
    Move/Delete) - the auto-tracked progress that used to be its own tree
    columns is now a hover tooltip on each poster instead."""

    DATA_FILE = "tracker.json"
    ENTRY_TYPES = ("Anime",)
    TITLE = "Anime"
    TYPE_OPTIONS = ["Anime", "Manga"]
    PROGRESS_COLUMNS = ["Last Released Season", "Last Released Episode"]
    # Whether a status section is drawn as one row per entry type. Only
    # the films-and-shows page wants it: Anime/Reading hold one kind of
    # thing each, and splitting Manga from Manhwa would be three rows of
    # the same shape saying nothing.
    SPLIT_SECTIONS_BY_TYPE = False
    # Manga has no Stremio presence to sync progress against.
    SUPPORTS_PROGRESS_SYNC = True
    # No page-level MEDIUM: the schedule source is per entry now that
    # Anime shares a page with Series/Movie - see _entry_medium.
    # The rows the Discover tab draws, as (kind, row label, entry type).
    # `kind` is what helpers/discover is asked for - the special
    # "reading" asks discover_reading, everything else discover_video -
    # and `entry type` is what the Add form opens on when one of that
    # row's cards is picked. A page with more than one row labels them
    # (Movies & Series shows two); a page with one doesn't, since a
    # heading over a single row only repeats the page's own title.
    DISCOVER_ROWS = (("anime", "Anime", "Anime"),)
    # Per-row heading/subheading overrides, and the rows that only
    # make sense while browsing - see MangaPage for the one page
    # that uses them.
    DISCOVER_HEADINGS = {}
    DISCOVER_SUBHEADINGS = {}
    DISCOVER_BROWSE_ONLY = ()
    # The page's sub-sections, published for main.py's contextual section
    # sidebar - its presence is how a page is detected as sectioned, and
    # current_section/set_active_section below are how it is driven. The
    # same tuple as TABS on purpose: a second set of keys beside
    # TAB_SAVED/TAB_DISCOVER/TAB_SCHEDULE would only drift from them.
    SECTIONS = TABS
    CATEGORY_SECTIONS = ()

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        self._sync_signals = _ProgressSyncSignals()
        self._sync_signals.resolved.connect(self._on_progress_synced)
        self._sync_pending = 0
        self._sync_changed = False
        self._sync_batch_run = 0

        # The refresh button's run, if one is in progress: the "Updating..."
        # toast waiting to be told how it went, how many background lookups
        # are still out, and whether any of them found anything new. Only
        # ever touched on the UI thread - every lookup reports back through
        # a signal - so no locking is needed.
        #
        # _refresh_run numbers the runs, and every lookup carries the
        # number of the run that asked for it. The same lookups also run on
        # their own at page load, and one of those landing mid-refresh
        # would otherwise be counted as one of the run's own results -
        # ending it early, on a verdict that came from a different lookup
        # than the one still out. Carrying the number keeps that exact.
        self._refresh_run = 0
        self._refresh_toast = None
        self._refresh_pending = 0
        self._refresh_changed = False
        self._refresh_before = {}
        # Set by any result in the current batch that came back
        # REASON_STREMIO_AUTH_FAILED, read once when the batch finishes.
        # A batch is one verdict, and one dead key fails all of it, so the
        # message is said once rather than per entry.
        self._stremio_auth_broken = False

        self._cover_signals = _CoverSignals()
        self._cover_signals.ready.connect(self._on_sharper_cover_ready)

        self._schedule_signals = _ScheduleSignals()
        self._schedule_signals.resolved.connect(self._on_schedule_resolved)

        self._discover_signals = _DiscoverSignals()
        self._discover_signals.results.connect(self._on_discover_results)
        self._discover_signals.poster.connect(self._on_discover_poster)
        self._discover_signals.schedule_upcoming.connect(
            self._on_schedule_upcoming)
        self._discover_signals.schedule_open.connect(self._on_schedule_open)
        self._discover_signals.schedule_cover.connect(self._on_schedule_cover)
        # The Schedule tab's catalogue-wide rows, and whether a fetch
        # for them is out or has already answered (see
        # _request_schedule_upcoming - a failed fetch clears the flag
        # so the next visit retries).
        self._schedule_upcoming = []
        self._schedule_upcoming_asked = False
        # The catalogue schedule row being resolved on click (its
        # title), and the sticky toast saying so. One at a time: a
        # second click while one is out is ignored rather than queued.
        self._schedule_opening = None
        self._schedule_open_toast = None
        self._discover_signals.featured_backdrop.connect(self._on_featured_backdrop)
        self._discover_signals.history_cover.connect(self._on_history_cover)
        # History row key -> (cover label, title), refilled by every
        # _build_history; nothing in it outlives that rebuild.
        self._history_covers = {}
        # Which Discover lookup is the current one. Everything a lookup
        # produces - a row's results, each poster download - carries this
        # number and is dropped if it no longer matches, so a slow row
        # answering after the query moved on cannot fill the rows under a
        # different search (the same rule as _refresh_run above).
        self._discover_run = 0
        self._discover_built = False
        # (kind, index) -> the drawn card's cover label, its title and the
        # size that cover was built at, so an arriving poster can replace
        # one pixmap instead of rebuilding a row. Nothing in here outlives
        # the rebuild that filled it.
        self._discover_cards = {}
        self._discover_holders = {}
        self._discover_query = ""

        # Which sub-section is on screen. Restored from the session store
        # at the end of __init__ like the search text and the filter
        # ticks. There is no in-page switcher any more - the section
        # sidebar main.py slides in over these pages drives this through
        # set_active_section, so nothing here paints tab state.
        self._active_tab = DEFAULT_TAB

        # The Schedule rows' countdown labels, refreshed in place while
        # that section is on screen - a countdown that only moved when
        # the page was rebuilt sat wrong within a minute of being read.
        # (label, when) pairs, reset by every _build_schedule.
        self._schedule_countdowns = []
        # Catalogue item id() -> its drawn cover label, so art landing
        # after the rows were drawn swaps one pixmap in place (see
        # _on_schedule_cover) - the same shape as _discover_cards.
        # Refilled by every _build_schedule; nothing here outlives it.
        self._schedule_cover_labels = {}
        self._schedule_tick = QTimer(self)
        self._schedule_tick.setInterval(30_000)
        self._schedule_tick.timeout.connect(self._refresh_schedule_countdowns)
        self._schedule_tick.start()

        self.entries = _migrate(storage.load(self.DATA_FILE, []), self.DATA_FILE)
        # What was on disk when this page loaded. _save_entries needs it
        # to tell "another page deleted this while I was open" from "I
        # have just added this and it isn't saved yet" - both look like
        # an entry of someone else's type that disk doesn't have.
        self._ids_at_load = {e.get("id") for e in self.entries if e.get("id")}

        # Filter selections last as long as the app run, not as long as
        # the page: restored here from _SESSION_VIEW_STATE and written
        # back on every change, so navigating away and returning shows
        # what was left on screen. Copied out of the store rather than
        # aliased, so a half-applied filter can't leak between page
        # instances. Empty means "everything".
        remembered = _SESSION_VIEW_STATE.get(self._view_state_key(), {})
        self._status_filter = set(remembered.get("status", ()))
        self._type_filter = set(remembered.get("type", ()))
        # (action, kind, value) for every item in the filter menu, so the
        # ticks can be re-read off the sets while the menu is still open.
        self._filter_actions = []

        # Multi-select state - see the block above _build_selection_bar
        # for the shape and the two rules it rests on. Ids rather than
        # entries: a page rebuilds every card from scratch on every
        # redraw, so anything holding widgets or dicts across one is
        # holding what has just been deleted.
        self._select_mode = False
        self._selected_ids = set()
        # entry id -> (card, badge) for the cards currently drawn, so a
        # click can repaint one mark instead of rebuilding the grid.
        # Rebuilt by _refresh_grid, which is also what makes it safe:
        # nothing in here outlives the redraw that filled it.
        self._selection_cards = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        # Named, not fire-and-forget: the heading reads as the *section*
        # now ("Discover" / "Saved" / "Schedule", the owner's ask - the
        # page's own name already sits highlighted in the sidebar), so
        # _set_tab rewrites it on every section switch.
        self.title_label = QLabel(self.TITLE, objectName="PanelTitle")
        header.addWidget(self.title_label)
        header.addStretch()
        # No refresh button: opening the page is the refresh (see
        # _auto_refresh). A button that had to be pressed, and then sat
        # on "Updating..." for as long as the slowest source took, was
        # asking the user to do the app's job and wait for it.
        # **No "+" button.** The owner's ask, 22 August 2026: take it
        # off every watch and read page. Adding a title is what Discover
        # and the search box are for - a blank form asking for a title,
        # a type and a cover by hand is the slowest route to the same
        # entry, and it sat in the corner of every page suggesting
        # otherwise. `_open_form` stays: the Edit action on a card still
        # uses it, and so does the entry dialog's own New button.
        layout.addLayout(header)

        # No tab bar between the header and the content: switching
        # sub-sections is the section sidebar's job (main.py), which
        # replaced the pill row that briefly lived here - one switcher,
        # not two.

        # Everything below is one tab's worth of page. The Saved one is
        # this page exactly as it was - the same widgets in the same
        # order, at the same spacing - only now inside a container of its
        # own instead of directly in the panel, so the other two tabs can
        # take its place by being shown while it is hidden. A hidden
        # widget contributes neither size nor spacing to a QVBoxLayout,
        # which is why these are three siblings rather than a
        # QStackedWidget: a stack sizes itself to the *largest* page, so
        # Discover's content would have set Saved's minimum height.
        self.saved_tab = QWidget(objectName="Bare")
        saved_layout = QVBoxLayout(self.saved_tab)
        saved_layout.setContentsMargins(0, 0, 0, 0)
        saved_layout.setSpacing(14)   # the panel's own spacing, so the
                                      # rows below sit exactly as they did
        layout.addWidget(self.saved_tab, stretch=1)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        top_row.addStretch()
        self.search_box = search_field("Search titles...", width=220)
        # Restored before textChanged is wired up, not after: setText
        # fires it, which would start the debounce timer for a redraw the
        # _refresh_grid at the end of __init__ is about to do anyway.
        self.search_box.setText(remembered.get("search", ""))
        # Debounced rather than filtering on every keystroke: each redraw
        # rebuilds every card from scratch (pages hold no state - see
        # .claude/rules/ui.md), so typing six characters would otherwise
        # rebuild the whole grid six times.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh_grid)
        self.search_box.textChanged.connect(self._on_search_text_changed)
        top_row.addWidget(self.search_box)
        # Symbol only, no label - it sits beside a search box that already
        # says what this row is for. The menu is attached with setMenu
        # rather than exec'd at a point worked out here, so Qt places it
        # against the button itself and there is no mapToGlobal to get
        # wrong across two monitors at different scale factors (see
        # .claude/rules/ui.md).
        # A real icon, no text: "▽" rendered as a thin sliver of a
        # missing-glyph box on the left of the button, beside Qt's own
        # menu arrow. Both are gone - the arrow via QSS (theme.py), the
        # sliver by the button having no text at all. Tinted to match the
        # "+" beside it, with a brighter pixmap for hover, since the PNG
        # is white and would otherwise sit brighter than its neighbours.
        self.filter_btn = QPushButton(objectName="Icon")
        self.filter_btn.setFixedSize(40, 40)
        self.filter_btn.setToolTip("Filter")
        dpr = QApplication.primaryScreen().devicePixelRatio()
        filter_icon = QIcon()
        filter_icon.addPixmap(
            images.tinted_asset(FILTER_ICON, theme.TEXT_MUTED, FILTER_ICON_HEIGHT, dpr),
            QIcon.Mode.Normal)
        filter_icon.addPixmap(
            images.tinted_asset(FILTER_ICON, theme.TEXT, FILTER_ICON_HEIGHT, dpr),
            QIcon.Mode.Active)
        self.filter_btn.setIcon(filter_icon)
        self.filter_btn.setIconSize(QSize(FILTER_ICON_HEIGHT, FILTER_ICON_HEIGHT))
        self._filter_menu = _StayOpenMenu(self)
        self._filter_menu.aboutToShow.connect(self._build_filter_menu)
        self.filter_btn.setMenu(self._filter_menu)
        top_row.addWidget(self.filter_btn)
        # Selection mode's switch. Its label is the state - "Select" when
        # off, "Done" when on - rather than a checkable button: theme.py
        # gives a plain QPushButton no :checked rule, so a ticked one
        # would look identical to an unticked one, and a mode nobody can
        # see they are in is the whole failure this is written around.
        self.select_btn = QPushButton("Select")
        self.select_btn.setFixedHeight(40)
        self.select_btn.setToolTip("Pick several entries and change or delete them at once")
        self.select_btn.clicked.connect(self._toggle_select_mode)
        top_row.addWidget(self.select_btn)
        saved_layout.addLayout(top_row)

        saved_layout.addWidget(self._build_selection_bar())

        self.grid_body = QWidget()
        # One row per section, each scrolling sideways on its own, rather
        # than one grid wrapping at a fixed column count. That grid cut
        # its 9th card
        # of a row off the right edge with no way to reach it: the page's
        # scroll area has its horizontal bar switched off, so anything
        # past the viewport simply wasn't there.
        self.grid_layout = QVBoxLayout(self.grid_body)
        self.grid_layout.setSpacing(18)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        saved_layout.addWidget(scroll_area(self.grid_body, ground=theme.PANEL_FILL), stretch=1)

        self._drag_reorder = CardDragReorder(
            self.grid_body, self._begin_custom_order, self._drop_reorder)

        # Empty shells: neither is filled until its tab is first shown
        # (see _set_tab), so a page that is never taken off Saved does no
        # Discover work at all - no catalog request, no poster download.
        self.discover_tab = QWidget(objectName="Bare")
        self._discover_layout = QVBoxLayout(self.discover_tab)
        self._discover_layout.setContentsMargins(0, 0, 0, 0)
        self._discover_layout.setSpacing(14)
        self.discover_tab.setVisible(False)
        layout.addWidget(self.discover_tab, stretch=1)

        self.schedule_tab = QWidget(objectName="Bare")
        self._schedule_layout = QVBoxLayout(self.schedule_tab)
        self._schedule_layout.setContentsMargins(0, 0, 0, 0)
        self._schedule_layout.setSpacing(14)
        self.schedule_tab.setVisible(False)
        layout.addWidget(self.schedule_tab, stretch=1)

        # History: what has actually been opened, saved or not (see
        # helpers/history). Built on arrival like the two above.
        self.history_tab = QWidget(objectName="Bare")
        self._history_layout = QVBoxLayout(self.history_tab)
        self._history_layout.setContentsMargins(0, 0, 0, 0)
        self._history_layout.setSpacing(14)
        self.history_tab.setVisible(False)
        layout.addWidget(self.history_tab, stretch=1)

        # One widget for every category section, refilled on each visit
        # rather than one per category: only ever one is on screen, and
        # four idle grids of poster cards is four grids' worth of
        # pixmaps held for nothing.
        self.category_tab = QWidget(objectName="Bare")
        self._category_layout = QVBoxLayout(self.category_tab)
        self._category_layout.setContentsMargins(0, 0, 0, 0)
        self._category_layout.setSpacing(14)
        self.category_body = QWidget()
        self._category_body_layout = QVBoxLayout(self.category_body)
        self._category_body_layout.setSpacing(16)
        self._category_body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._category_layout.addWidget(
            scroll_area(self.category_body, ground=theme.PANEL_FILL), stretch=1)
        self.category_tab.setVisible(False)
        layout.addWidget(self.category_tab, stretch=1)
        # Which category is showing, and its own run counter - a slow
        # answer for Manhwa must not fill the grid after the user has
        # moved to Manhua.
        self._category_key = ""
        self._category_run = 0
        self._category_signals = _DiscoverSignals()
        self._category_signals.results.connect(self._on_category_results)

        # **The Saved grid is not built until Saved is looked at.**
        # These pages open on Discover every time (see below), so every
        # card in the grid was being built, laid out and styled on the
        # way to a tab that is not the one showing. Measured on the
        # owner's data, 22 August 2026: Home -> Watch reached the screen
        # in 337ms, of which _refresh_grid and the four sideways strips
        # it builds were 32ms of main-thread work standing in front of
        # the first paint.
        #
        # Deferring it by a timer was tried first and measured as doing
        # nothing: a zero timer posts an event the loop drains in the
        # round it is already draining, so the grid was still built
        # before Qt sent a paint. Lazy is what actually moves it.
        #
        # Nothing else needs it: _selection_cards is initialised above,
        # _card_providers is read through a getattr default, and
        # _on_schedule_resolved deliberately never rebuilds the grid
        # (the tooltip is generated on hover from the entry itself).
        self._grid_built = False
        # Work that must not stand in front of the page's first frame -
        # see _after_first_paint. Set up before _set_tab below, which is
        # what fires the Discover lookups that queue onto it.
        self._painted = False
        self._after_paint = []
        # Last, after every tab's shell exists: showing Discover *is*
        # what fires that tab's first lookup. Always Discover, every visit (the owner, twice - the
        # remembered-section design was tried and reversed): these pages
        # open on Discover no matter which section was up when the user
        # walked away. Search text and the filter ticks are still
        # restored from the session store above; only the section isn't.
        self._set_tab(DEFAULT_TAB)
        if self.SUPPORTS_PROGRESS_SYNC:
            self._backfill_missing_latest_available()
        self._backfill_sharper_covers()
        self._refresh_schedules()
        self._auto_refresh()

    # ------------------------------------------------------------------
    # How long after an automatic sync the page will leave it alone.
    # Opening a page is now the refresh, and a page can be opened several
    # times a minute while navigating - without this, that is one Stremio
    # request per entry every time.
    _AUTO_SYNC_INTERVAL = 10 * 60

    def _auto_refresh(self):
        """Refresh on arrival, quietly.

        The schedules already refresh themselves on page load and are
        cached for 12h (release_schedule.needs_refresh), so what this
        adds is the progress sync - throttled, because navigating back
        and forth would otherwise re-ask Stremio about every entry each
        time.

        Deliberately silent: no "Updating..." toast. The old button
        showed one and then held it for as long as the slowest source
        took, which is what made a refresh feel like something the user
        was waiting on rather than something already done. Results land
        on the cards as they arrive."""
        if not self.SUPPORTS_PROGRESS_SYNC:
            return
        _, auth_key = app_settings.get_stremio_auth()
        if not auth_key:
            return  # nothing to sync from - the page notice says so
        now = time.time()
        if now - app_settings.get_last_auto_sync(self.DATA_FILE) < self._AUTO_SYNC_INTERVAL:
            return
        app_settings.set_last_auto_sync(self.DATA_FILE, now)
        self._sync_all_progress()

    def _refresh_everything(self):
        """Re-check everything on this page - progress and release dates
        - and say how it went. Kept for anything that asks for a full
        refresh explicitly; the page itself uses _auto_refresh.

        Says so while it works, and says how it went when it's finished
        (see _refresh_step_done) - every lookup runs on its own background
        thread, so without that it reads as doing nothing at all for
        however long the slowest source takes to answer."""
        if self._refresh_toast is not None:
            return  # already running - let it finish rather than double it
        self._refresh_run += 1
        self._refresh_changed = False
        # What every entry says right now, to judge the results against.
        # Against this rather than against whatever is stored by the time
        # a result lands: the same lookups fire on their own when a page
        # opens, so one of those finishing first would have already
        # absorbed the news, and the run would then compare the answer
        # with itself and report nothing new.
        self._refresh_before = {
            entry["id"]: (_release_content(entry.get("next_release")),
                          entry.get("progress"), entry.get("latest_available"))
            for entry in self.entries if entry["type"] in self.ENTRY_TYPES
        }
        # Starts at one and is released at the end: the lookups below
        # report back through the event loop, so they cannot land before
        # this method returns - but a page with nothing to look up would
        # otherwise sit at zero pending and never report at all.
        self._refresh_pending = 1
        self._refresh_toast = show_toast(self, "Updating...", duration_ms=None)

        self._refresh_schedules(force=True)
        if self.SUPPORTS_PROGRESS_SYNC:
            self._sync_all_progress()
        self._refresh_step_done(self._refresh_run)

    def _refresh_step_started(self):
        """Count one more background lookup into the running refresh and
        return which run it belongs to, for it to report back with. 0 when
        no refresh is running - the same lookups also happen on page load,
        where there is nothing to report to."""
        if self._refresh_toast is None:
            return 0
        self._refresh_pending += 1
        return self._refresh_run

    def _refresh_step_done(self, run, found_something_new=False):
        """One lookup back. The last one to return reports the verdict.

        Anything from another run (or from no run at all) is ignored
        rather than counted: it was never counted in, so counting it out
        would end this run one result early."""
        if not run or run != self._refresh_run or self._refresh_toast is None:
            return
        self._refresh_changed = self._refresh_changed or found_something_new
        self._refresh_pending -= 1
        if self._refresh_pending > 0:
            return
        toast, self._refresh_toast = self._refresh_toast, None
        self._refresh_before = {}
        if self._stremio_auth_broken:
            # Outranks both ordinary verdicts: with the session rejected,
            # no progress was read at all, so "There is No New Update" is
            # true and useless - it is the sentence that hid this bug.
            self._stremio_auth_broken = False
            self._warn_stremio_auth_once(toast)
            return
        finish_toast(toast, self, "Updated Successfully" if self._refresh_changed
                     else "There is No New Update")

    def _refresh_schedules(self, force=False):
        """Look up when each entry's next episode/chapter lands, in the
        background. Cached on the entry (see release_schedule.needs_
        refresh), so a normal page visit usually fires no requests at all
        - only entries with nothing stored, a stale check, or a release
        time that's already passed get looked up again.

        Queued onto the bounded pool rather than given a thread each: a
        first run on a long list needs every entry looked up, and one
        connection per entry all at once is what slowed the user's whole
        network down (see lookup_pool)."""
        for entry in self.entries:
            if entry["type"] not in self.ENTRY_TYPES:
                continue
            if not release_schedule.needs_refresh(entry, force):
                continue
            run = self._refresh_step_started()
            lookup_pool.submit(self._fetch_schedule, entry, run)

    def _fetch_schedule(self, entry, run=0):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        try:
            found, manga_id = release_schedule.fetch(
                _entry_medium(entry), entry["title"], imdb_id=_entry_imdb_id(entry),
                known_latest_chapter=_latest_known_chapter(entry),
                manga_id=entry.get("mangadex_id"))
        except Exception:
            found, manga_id = None, None
        self._schedule_signals.resolved.emit(entry["id"], found, manga_id, run)

    def _on_schedule_resolved(self, entry_id, found, manga_id=None, run=0):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if not entry:
            self._refresh_step_done(run)
            return
        snapshot = self._refresh_before.get(entry_id)
        before = snapshot[0] if snapshot else _release_content(entry.get("next_release"))
        self._refresh_step_done(run, _release_content(found) != before)
        # Stored even when nothing was found, so a title with no schedule
        # (finished airing, not on the source) isn't re-looked-up on every
        # single page visit - the timestamp is what needs_refresh rate-
        # limits against.
        fields = {"next_release": found, "next_release_checked_at": storage.now_iso()}
        # Which MangaDex title this entry is, once it has been worked out:
        # kept so the next refresh can go straight to the chapter feed
        # instead of searching for the same title again (the single
        # biggest cost of a Reading refresh - see mangadex.
        # fetch_next_chapter). Written, never cleared here: a lookup that
        # finds nothing says the series is quiet, not that the id went
        # wrong, and the one thing that can invalidate it - the title being
        # edited - clears it at the point of the edit (see _on_form_save).
        if manga_id:
            fields["mangadex_id"] = manga_id
        entry.update(fields)
        # Just these fields, not a wholesale save of self.entries - Anime
        # and Manga are separate pages backed by the same tracker.json
        # and each holds its own copy of it (see storage.update_entry).
        storage.update_entry(self.DATA_FILE, entry["id"], fields)
        # No grid rebuild: the tooltip is generated on hover from the
        # entry itself (see _build_card), so it picks this up as-is.

    # ------------------------------------------------------------------
    def _backfill_sharper_covers(self):
        """Manga covers saved before manga_sites.py started stripping
        WordPress's size-suffixed crops / TeamX's thumbnail_ prefix off
        search-result covers (see manga_sites.upgrade_cover_url) are
        stuck on those blurry small images forever otherwise - nothing
        else ever re-derives cover_path from cover_url after the initial
        save. Silently re-download the sharper original in the
        background, same one-time-catch-up pattern as
        _backfill_missing_latest_available.

        The same catch-up also fills a cover that never landed at all
        (the owner's "Kingdom (WAN)" report): an entry can hold a
        cover_url with cover_path still None - the save-time download
        failed once, or the title was saved before its poster resolved -
        and, before this, nothing ever retried, so Saved and Home drew
        the blank tile forever while the Discover card showed the art."""
        for entry in self.entries:
            wanted = None
            if entry["type"] in MANGA_TYPES:
                upgraded = manga_sites.upgrade_cover_url(entry.get("cover_url"))
                if upgraded and upgraded != entry.get("cover_url"):
                    wanted = upgraded
            if (wanted is None and entry.get("type") in self.ENTRY_TYPES
                    and entry.get("cover_url") and not entry.get("cover_path")):
                wanted = entry["cover_url"]
            if wanted:
                # Bounded, like every other per-entry loop here: this one
                # downloads a full-size image per entry, so it is the
                # heaviest of them to fire all at once.
                lookup_pool.submit(self._fetch_sharper_cover, entry["id"],
                                   wanted, "", entry.get("title") or "")

    def _fetch_sharper_cover(self, entry_id, new_url, page_url="",
                             title=""):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        #
        # `page_url` is the reading-row fallback: a Madara-style search
        # result names no cover at all, so a save can arrive here with
        # no URL and only the series page to ask - the same lookup the
        # Discover card itself uses (_fetch_discover_poster).
        try:
            if not new_url and page_url:
                details = manga_sites.fetch_manga_details(
                    page_url, title=title) or {}
                new_url = details.get("cover_url") or ""
            # Nothing on the page and nothing beside it: ask the
            # catalogues by name rather than leaving a blank tile.
            if not new_url and title:
                new_url = manga_sites.cover_for_title(title) or ""
            # Same one retry as the Discover card's own fetch - see
            # _fetch_discover_poster.
            path = None
            if new_url:
                path = (images.download(new_url)
                        or images.download(new_url, timeout=20))
        except Exception:
            path = None
        if path:
            self._cover_signals.ready.emit(entry_id, new_url, str(path))

    def _on_sharper_cover_ready(self, entry_id, new_url, path):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if not entry:
            return
        entry["cover_url"] = new_url
        entry["cover_path"] = path
        self._save_entries()
        self._refresh_grid()

    def _backfill_missing_latest_available(self):
        """Entries saved before latest_available was tracked (or from a
        pick where the background lookup didn't finish/land) leave the
        Last Released Season/Episode tooltip permanently blank otherwise -
        nothing else ever re-fetches it later. Silently top those up in
        the background on load, the same way Sync Progress would for one
        entry by hand, instead of leaving the user to notice and fix it."""
        targets = [e for e in self.entries
                   if e["type"] in self.ENTRY_TYPES and tracks_progress(e["type"])
                   and not e.get("latest_available") and _entry_imdb_id(e)]
        if not targets:
            return
        self._sync_pending += len(targets)
        for entry in targets:
            lookup_pool.submit(
                self._fetch_real_progress, entry["id"], _entry_imdb_id(entry),
                entry["title"], True)

    # ------------------------------------------------------------------
    @classmethod
    def _view_state_key(cls) -> str:
        """Which slot of _SESSION_VIEW_STATE this page owns.

        The class name, not TITLE: a subclass can inherit TITLE from the
        base class, so two pages could end up sharing one slot if the
        titles were ever made to collide."""
        return cls.__name__

    def _remember_view_state(self):
        """Write the current search text and ticks to the session store.

        Called on every change rather than on teardown - a page is
        replaced, not closed, and there is no hook that reliably runs
        before it goes."""
        _SESSION_VIEW_STATE[self._view_state_key()] = {
            "search": self.search_box.text(),
            "status": set(self._status_filter),
            "type": set(self._type_filter),
            "tab": self._active_tab,
        }

    def _on_search_text_changed(self, _text):
        self._remember_view_state()
        self._search_timer.start()

    # ------------------------------------------------------------------
    # The three sub-sections.
    #
    # Their switcher lives outside the page: main.py slides a section
    # sidebar in over any page exposing SECTIONS and drives these two
    # methods. The pill bar that briefly sat under the header is gone -
    # the sidebar is the one way to switch, so nothing in here paints
    # which section is active.
    def current_section(self) -> str:
        """The active section's key, for the section sidebar's checks."""
        return self._active_tab

    def set_active_section(self, key):
        """Show one sub-section - exactly what clicking the old pill
        did, the lazy first build of Discover/Schedule included, and
        remembered in the session store (see _set_tab)."""
        self._set_tab(key)

    def _set_tab(self, key):
        """Show one sub-section.

        Does the work even when `key` is already active: this is also
        the restore path at the end of __init__, where the remembered
        section has to be applied over the default."""
        categories = {row[0]: row for row in self.CATEGORY_SECTIONS}
        if key not in dict(TABS) and key not in categories:
            key = DEFAULT_TAB
        self._active_tab = key
        labels = dict(TABS)
        labels.update({row[0]: row[1] for row in self.CATEGORY_SECTIONS})
        self.title_label.setText(labels.get(key, key))
        self.saved_tab.setVisible(key == TAB_SAVED)
        self.discover_tab.setVisible(key == TAB_DISCOVER)
        self.schedule_tab.setVisible(key == TAB_SCHEDULE)
        self.history_tab.setVisible(key == TAB_HISTORY)
        self.category_tab.setVisible(key in categories)
        if key in categories:
            self._show_category(categories[key])
            self._remember_view_state()
            return
        if key == TAB_DISCOVER:
            self._show_discover()
        elif key == TAB_SAVED:
            # The same self-healing the sections below get by rebuilding:
            # the details page (and the other tracker page sharing this
            # file) writes straight to disk, and before this the Saved
            # grid only ever redrew what the page already held.
            self._adopt_disk_rows()
            # _adopt_disk_rows only rebuilds when something moved, and on
            # the first visit nothing has - the grid is built lazily now
            # (see __init__), so this is where it first appears.
            if not self._grid_built:
                self._refresh_grid()
        elif key == TAB_SCHEDULE:
            # Rebuilt on every visit rather than kept: it is read
            # entirely off self.entries, and those move under it (a
            # lookup landing, an entry saved or deleted) with nothing
            # that would tell a built list to catch up.
            self._build_schedule()
        elif key == TAB_HISTORY:
            # Same reasoning, more so: history.json is written by the
            # player, the reader and the details page, none of which
            # tell this page anything.
            self._build_history()
        self._remember_view_state()

    def _search_query(self) -> str:
        # getattr because _refresh_grid can run before the box exists on
        # a page still being built.
        box = getattr(self, "search_box", None)
        return box.text().strip().lower() if box else ""

    def _build_filter_menu(self):
        """Built once per opening. The ticks are then kept right by
        _sync_filter_menu_checks rather than by rebuilding, because the
        menu stays open while they are used and clearing its contents
        under a visible popup would dismiss it."""
        menu = self._filter_menu
        menu.clear()
        self._filter_actions = []
        everything = menu.addAction("All")
        everything.setCheckable(True)
        everything.triggered.connect(self._clear_filters)
        self._filter_actions.append((everything, "all", None))
        # Only where there is a choice - Anime has one type, so a type
        # section there would be a single tick that can't be turned off.
        if len(self.TYPE_OPTIONS) > 1:
            menu.addSeparator()
            for name in self.TYPE_OPTIONS:
                action = menu.addAction(name)
                action.setCheckable(True)
                action.triggered.connect(
                    lambda checked, n=name: self._toggle_filter(self._type_filter, n, checked))
                self._filter_actions.append((action, "type", name))
        menu.addSeparator()
        for status in self._page_statuses():
            action = menu.addAction(status)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, s=status: self._toggle_filter(self._status_filter, s, checked))
            self._filter_actions.append((action, "status", status))
        self._sync_filter_menu_checks()

    def _sync_filter_menu_checks(self):
        """Tick states read back off the filter sets, without touching the
        menu's contents. Needed while it is open: choosing All has to
        un-tick everything else there and then, and All itself has to go
        back on when the last individual tick comes off."""
        for action, kind, value in self._filter_actions:
            if kind == "all":
                action.setChecked(not self._status_filter and not self._type_filter)
            elif kind == "type":
                action.setChecked(value in self._type_filter)
            else:
                action.setChecked(value in self._status_filter)

    def _toggle_filter(self, selected, value, checked):
        if checked:
            selected.add(value)
        else:
            selected.discard(value)
        self._sync_filter_menu_checks()
        self._remember_view_state()
        self._refresh_grid()

    def _clear_filters(self, *_args):
        # *_args because this is wired to triggered(bool) as well as being
        # called directly.
        self._status_filter.clear()
        self._type_filter.clear()
        self._sync_filter_menu_checks()
        self._remember_view_state()
        self._refresh_grid()

    def _page_statuses(self):
        """The statuses this page's entries can hold.

        One list per page, not per entry: a page's types are either all
        watched or all read (VIDEO_TYPES vs MANGA_TYPES), so Series and
        Movie share a list and Manga/Manhwa/Manhua share the other. The
        filter menu has always assumed this; the bulk status menu below
        relies on the same thing, which is what lets a mixed Series/Movie
        selection be given one status."""
        return STATUSES_BY_TYPE.get(self.ENTRY_TYPES[0], _WATCHING_STATUSES)

    # ------------------------------------------------------------------
    # Multi-select: a mode, not a modifier.
    #
    # A left click on a card opens the entry - it is the page's primary
    # gesture - so selection cannot share it. Ctrl+click was the obvious
    # alternative and was rejected twice over: nothing on screen would
    # ever say the page could do this, and `Card.clicked` carries no
    # modifiers, so it would have meant reworking the click path every
    # page in the app uses. A mode announces itself (the button reads
    # "Done", a bar appears, every card carries a mark) and confines the
    # change in what a click means to while it is switched on.
    #
    # Two rules decide how it coexists with what is already here:
    #
    # * **Only what is on screen can be selected.** The selection is
    #   pruned to the visible entries on every redraw (_refresh_grid), so
    #   a search, a filter or a status change can never leave an entry
    #   selected that the user cannot see, and "Select All" means the
    #   ones in front of them. A narrowed grid is *safe* here in a way it
    #   is not for dragging: a drop rewrites the whole saved order from a
    #   partial view (see _grid_is_narrowed), while a bulk status change
    #   writes only the entries actually picked, one
    #   storage.update_entry each. Filter to Plan to Watch, Select All,
    #   set Dropped is the point of this feature rather than a hazard -
    #   and it is what makes Delete safe on a narrowed grid too, since a
    #   batch delete removes only the entries actually picked.
    # * **Dragging is off while selecting.** Both want the same press,
    #   and drag-reorder is already off whenever the grid is narrowed -
    #   this is the same switch with one more reason in it.
    #
    # Changing a status moves those entries to another section, or out of
    # the view entirely under a status filter. So the selection is
    # cleared once the change lands and the mode stays on: keeping cards
    # selected across a move would mean a selection scattered over
    # sections the user never picked from, and dropping out of the mode
    # would fight the common case of doing a second batch. The toast is
    # what says how many moved, which matters exactly when they left the
    # view and there is nothing on screen to show for it.
    #
    # The mode is deliberately not remembered across a page visit the way
    # search and filter are (_SESSION_VIEW_STATE): arriving on a page
    # where clicking a card silently fails to open it would read as the
    # app being broken.
    def _build_selection_bar(self):
        """The row that appears under the toolbar while selecting.

        Built once and hidden, not built on entering the mode: it sits in
        the page's layout above the grid, and adding it later would push
        every section down by its height at the moment the mode is
        switched on."""
        bar = QWidget(objectName="Bare")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.selection_label = QLabel("", objectName="Muted")
        row.addWidget(self.selection_label)
        row.addStretch()
        # A checkbox rather than a button: "select everything" is a
        # state, not an action, and a tick can show that everything is
        # already picked where a button can only ever say it.
        self.select_all_check = QCheckBox("Select All")
        self.select_all_check.toggled.connect(self._on_select_all_toggled)
        row.addWidget(self.select_all_check)
        self.bulk_status_btn = QPushButton("Set Status", objectName="Accent")
        # setMenu rather than exec'ing at a computed point: Qt places it
        # against the button itself, so there is no mapToGlobal to get
        # wrong across two monitors at different scale factors (see
        # .claude/rules/ui.md), the same reasoning as the filter button.
        self._bulk_status_menu = QMenu(self)
        for status in self._page_statuses():
            self._bulk_status_menu.addAction(
                status, lambda checked=False, s=status: self._apply_bulk_status(s))
        self.bulk_status_btn.setMenu(self._bulk_status_menu)
        row.addWidget(self.bulk_status_btn)
        # Last in the row and the only "Danger" thing on the page, so the
        # button that destroys data is not the one nearest the pointer
        # after picking a card, and doesn't read as another neutral
        # toolbar action beside Select All and Clear.
        self.bulk_delete_btn = QPushButton("Delete", objectName="Danger")
        self.bulk_delete_btn.clicked.connect(self._delete_selected)
        row.addWidget(self.bulk_delete_btn)
        bar.setVisible(False)
        self.selection_bar = bar
        return bar

    @staticmethod
    def _entry_count(count) -> str:
        return f"{count} entry" if count == 1 else f"{count} entries"

    def _update_selection_bar(self):
        # getattr because _refresh_grid can run before the bar exists on
        # a page still being built, the same reason _search_query guards
        # its own box.
        label = getattr(self, "selection_label", None)
        if label is None:
            return
        count = len(self._selected_ids)
        label.setText(f"{self._entry_count(count)} selected" if count
                      else "Click cards to pick them")
        self.bulk_status_btn.setEnabled(count > 0)
        self.bulk_delete_btn.setEnabled(count > 0)
        # Blocked, because this is the tick catching up with the
        # selection - not the user asking for one. Unblocked it would
        # re-enter _on_select_all_toggled and re-apply what it is only
        # reporting.
        self.select_all_check.blockSignals(True)
        self.select_all_check.setChecked(self._everything_visible_selected())
        self.select_all_check.blockSignals(False)

    def _toggle_select_mode(self):
        self._set_select_mode(not self._select_mode)

    def _set_select_mode(self, on, first_id=None):
        self._select_mode = on
        self._selected_ids = {first_id} if on and first_id else set()
        self.select_btn.setText("Done" if on else "Select")
        self.selection_bar.setVisible(on)
        # A full rebuild, unlike picking a card below: every card has to
        # gain or lose its mark, and what its click does changes with it.
        self._refresh_grid()

    def _toggle_selected(self, entry_id):
        """Pick or unpick one card, repainting that card alone.

        Not _refresh_grid: each section is its own sideways-scrolling
        strip (see _build_section_strip), and rebuilding the grid puts
        every one of them back to scroll position 0 - so picking the
        twelfth card in a row would scroll the row away from under the
        pointer before the next click."""
        if entry_id in self._selected_ids:
            self._selected_ids.discard(entry_id)
        else:
            self._selected_ids.add(entry_id)
        drawn = self._selection_cards.get(entry_id)
        if drawn:
            self._paint_selection(*drawn, entry_id in self._selected_ids)
        self._update_selection_bar()

    def _everything_visible_selected(self) -> bool:
        """Whether every entry currently on screen is already picked -
        what turns Select All into Unselect All. Measured against the
        visible entries, so under a search or a filter "everything" means
        what is in front of the user, exactly as the selection does."""
        visible = {entry.get("id") for entry in self._visible_entries()}
        return bool(visible) and visible <= self._selected_ids

    def _on_select_all_toggled(self, checked):
        if checked:
            self._select_all_visible()
        else:
            self._clear_selection()

    def _select_all_visible(self):
        # _selection_cards is exactly the cards currently drawn, which is
        # the whole point: under a search or a filter this picks what is
        # on screen and nothing else.
        self._selected_ids = {entry_id for entry_id in self._selection_cards if entry_id}
        self._repaint_all_selection()

    def _clear_selection(self):
        self._selected_ids.clear()
        self._repaint_all_selection()

    def _repaint_all_selection(self):
        for entry_id, (card, badge) in self._selection_cards.items():
            self._paint_selection(card, badge, entry_id in self._selected_ids)
        self._update_selection_bar()

    def _paint_selection(self, card, badge, selected):
        """Mark or unmark one card.

        The badge over the poster's corner is the mark that decides it;
        the accent border is only there so a picked tile reads as picked
        from across the page. Measured, and the reason round that way: an
        accent border is exactly what theme.py's #Card:hover rule already
        draws, so the card under the pointer looks bordered whether or
        not it is picked - the badge is what tells them apart. No glyph
        in it, because the filter button's "▽" rendered as a sliver of a
        missing-glyph box (see the comment on filter_btn) and a filled
        accent disc needs no font at all.

        Border stays 1px in both states: QSS border width comes out of
        the widget's content rect, so a thicker one would shift every
        label inside the card by a pixel the moment it was picked."""
        badge.setStyleSheet(
            f"background: {theme.ACCENT if selected else theme.BG}; "
            f"border: 2px solid {theme.TEXT if selected else theme.TEXT_MUTED}; "
            f"border-radius: 11px;")
        # Scoped to QFrame#Card so it cannot reach the labels inside the
        # card, and naming only `border` so the app stylesheet's gradient
        # fill still paints - a widget stylesheet is merged with the
        # application one property by property, not instead of it.
        card.setStyleSheet(f"QFrame#Card {{ border: 1px solid {theme.ACCENT}; }}"
                           if selected else "")

    def _apply_bulk_status(self, status):
        """Give every picked entry the same status.

        One storage.update_entry per entry, never a whole-list write:
        Anime and Reading are both backed by tracker.json and each page
        holds its own copy loaded when it was built, so writing the list
        back would restore the other page's entries as they were then
        (.claude/rules/ui.md, and see _save_entries for the defect that
        rule came from)."""
        if not self._selected_ids:
            return
        self._apply_status_to_ids(set(self._selected_ids), status)

    def _apply_status_to_ids(self, ids, status):
        """The body of a bulk status change, against an explicit set of
        ids rather than the current selection - so Ctrl+Y can re-apply
        the batch it just undid, by which point nothing is selected."""
        stamp = storage.now_iso()
        previous = []   # (id, status, updated_at) as they were, for undo
        for entry in self.entries:
            entry_id = entry.get("id")
            if entry_id not in ids or entry.get("status") == status:
                continue
            before = (entry_id, entry.get("status"), entry.get("updated_at"))
            # Disk first, memory second. A False here means the entry is
            # no longer in the file - another page deleted it while this
            # one was open - and changing this page's copy anyway would
            # show a status that nothing on disk agrees with.
            if not storage.update_entry(self.DATA_FILE, entry_id,
                                        {"status": status, "updated_at": stamp}):
                continue
            entry["status"] = status
            entry["updated_at"] = stamp
            previous.append(before)
        self._selected_ids.clear()
        self._refresh_grid()
        if not previous:
            show_toast(self, f"Already {status}")
            return
        show_undo_toast(
            self, f"Moved {self._entry_count(len(previous))} to {status} - Click to Undo",
            lambda: self._undo_bulk_status(previous),
            on_redo=lambda ids={before[0] for before in previous}:
                self._apply_status_to_ids(ids, status))

    def _undo_bulk_status(self, previous):
        by_id = {e.get("id"): e for e in self.entries}
        restored = 0
        for entry_id, status, updated_at in previous:
            fields = {"status": status}
            # Only when there was one: an entry saved before updated_at
            # existed has no such field, and writing None back would put
            # one there that the app never wrote.
            if updated_at is not None:
                fields["updated_at"] = updated_at
            if not storage.update_entry(self.DATA_FILE, entry_id, fields):
                continue
            entry = by_id.get(entry_id)
            if entry is not None:
                entry.update(fields)
            restored += 1
        self._refresh_grid()
        return f"Restored {self._entry_count(restored)}"

    def _delete_selected(self):
        """Delete every picked entry, asked about once.

        One question for the whole batch rather than one per entry: the
        point of picking eight cards is not to answer eight dialogs, and
        the count in the question is what the user checks the selection
        against. The undo offer is what lets it be a single question -
        the whole batch comes back from it, every field intact.

        Through _save_entries, not storage.update_entry: update_entry
        merges fields into an entry and has no way to remove one, and
        _save_entries is already this page's re-read-then-apply path. It
        reloads the file and lets this page speak only for its own entry
        types, so the other page's entries (Anime and Reading share
        tracker.json) are carried over as they are on disk rather than as
        this page last saw them - the same call the single-entry delete
        makes, for the same reason."""
        if not self._selected_ids:
            return
        # Indices recorded against this page's list before anything is
        # removed: undo puts each record back at the index it came from,
        # and an index read after the first removal would name the wrong
        # slot. The record is the whole entry, deep-copied - an entry
        # carries far more than the card shows (cover, link, the cached
        # schedule and its checked-at stamp, the resolved MangaDex/site
        # ids) and undo has to give all of it back, not re-add a title.
        removed = [(index, copy.deepcopy(entry))
                   for index, entry in enumerate(self.entries)
                   if entry.get("id") in self._selected_ids]
        if not removed:
            return
        if not confirm(self, "Delete Entries",
                       f"Delete {self._entry_count(len(removed))}?"):
            return
        gone = {entry.get("id") for _index, entry in removed}
        # In place, not a rebind: the schedule and cover lookups already
        # in flight hold this list, not the attribute.
        self.entries[:] = [e for e in self.entries if e.get("id") not in gone]
        # Selection cleared, mode left on - same as a bulk status change:
        # the common case after one batch is a second one.
        self._selected_ids.clear()
        self._save_entries()
        self._refresh_grid()
        show_undo_toast(
            self, f"Deleted {self._entry_count(len(removed))} - Click to Undo",
            lambda: self._restore_entries(removed),
            on_redo=lambda ids=[e.get("id") for _i, e in removed]: self._delete_ids(ids))

    def _delete_ids(self, ids):
        """Delete by id with nothing asked - what Ctrl+Y re-applies. The
        batch was already confirmed once; redo is an explicit request to
        put it back as it was. It raises a fresh undo offer, so Ctrl+Z
        still works after a redo."""
        wanted = set(ids)
        removed = [(index, entry) for index, entry in enumerate(self.entries)
                   if entry.get("id") in wanted]
        if not removed:
            return
        gone = {entry.get("id") for _index, entry in removed}
        self.entries[:] = [e for e in self.entries if e.get("id") not in gone]
        self._save_entries()
        self._refresh_grid()
        show_undo_toast(
            self, f"Deleted {self._entry_count(len(removed))} - Click to Undo",
            lambda: self._restore_entries(removed),
            on_redo=lambda: self._delete_ids(ids))

    def _restore_entries(self, removed):
        """Put a deleted batch back, each record at the index it held."""
        # Ascending, so each insert lands before the ones still to come
        # and the list ends up in the order it was - and clamped, because
        # another page can have shortened it since (_save_entries lets a
        # delete made elsewhere stand).
        for index, entry in sorted(removed, key=lambda pair: pair[0]):
            self.entries.insert(min(index, len(self.entries)), entry)
        # _save_entries re-reads the file and lets this page's own copy
        # win for its own entry types, so putting the records back into
        # self.entries is what writes them back.
        self._save_entries()
        self._refresh_grid()
        return f"Restored {self._entry_count(len(removed))}"

    # ------------------------------------------------------------------
    def _grid_is_narrowed(self) -> bool:
        """Whether the grid is showing less than the whole page.

        Dragging is off while it is: a drop saves the order that is on
        screen (see _begin_custom_order), and that isn't the whole order
        when entries are hidden - as true of a filter as of a search, and
        reordering under one would silently rewrite the rest."""
        return bool(self._search_query() or self._status_filter or self._type_filter)

    def _visible_entries(self):
        entries = [e for e in self.entries if e["type"] in self.ENTRY_TYPES]
        if self._type_filter:
            entries = [e for e in entries if e["type"] in self._type_filter]
        if self._status_filter:
            entries = [e for e in entries if e.get("status") in self._status_filter]
        query = self._search_query()
        if query:
            # Plain case-insensitive substring, not a fuzzy match: the
            # user is looking for a title they know is there, and a fuzzy
            # rank that quietly includes near-misses would make a short
            # query look like it had failed to filter at all.
            entries = [e for e in entries if query in (e.get("title") or "").lower()]
        mode = self.sort_box.currentText()
        if mode == "Name (A-Z)":
            entries = sorted(entries, key=lambda e: e["title"].lower())
        elif mode == "Date Added (Newest)":
            entries = sorted(entries, key=lambda e: e.get("added_at") or "", reverse=True)
        elif mode == "Last Updated":
            entries = sorted(entries, key=lambda e: e.get("updated_at") or "", reverse=True)
        return entries

    def _progress_columns(self, entry):
        """Values matching self.PROGRESS_COLUMNS for the hover tooltip.
        Manga has no "total available" concept worth fetching, so it
        still shows its own read progress. Anime/Series show the latest
        season/episode available out there (how far the release currently
        goes) rather than what you've watched - that's the always-visible
        label under the cover already, see _progress_display."""
        # A film has no episode to be on, so neither column means
        # anything - zip() in _tooltip_html drops the labels with it.
        if not tracks_progress(entry["type"]):
            return []
        if entry["type"] in MANGA_TYPES:
            text = entry.get("progress") or ""
            value = parse_chapter_progress(text)
            return [format_chapter_progress(value) if value else text]
        text = entry.get("latest_available") or ""
        season, episode = parse_episode_progress(text)
        if not season and not episode and text:
            return ["", text]  # legacy freeform text that doesn't parse - keep it visible
        return [str(season) if season else "", str(episode) if episode else ""]

    def _tooltip_html(self, entry, provider=None):
        """Built fresh on every hover (see _build_card) rather than once
        per card, because the countdown at the end of it is only right at
        the moment it's shown.

        `provider` is passed in rather than looked up here: this runs on
        every hover, and resolving it reads the saved sites file."""
        rows = [f"<b>{entry['title']}</b>", entry["status"]]
        for label, value in zip(self.PROGRESS_COLUMNS, self._progress_columns(entry)):
            if value:
                rows.append(f"{label}: {value}")
        rows.extend(release_schedule.tooltip_lines(entry, _entry_medium(entry)))
        # Who said so. A number the user disagrees with ("I'm on episode
        # 2, why does this say 7?") is unarguable while it is anonymous -
        # naming the source is what turns it into something checkable.
        source = entry.get("progress_source")
        if source and entry.get("progress_verified"):
            if source == SOURCE_IN_APP:
                rows.append("Progress from what you opened here")
            else:
                rows.append("Progress set by you" if source == SOURCE_MANUAL
                            else f"Progress from {source}")
        # Only while there is nothing real to show: once progress is
        # confirmed this is just noise on a card that works.
        if provider and not entry.get("progress_verified"):
            name = _UNREADABLE_NAMES.get(provider, provider.title())
            rows.append(f"<i>{name} can't report watch progress - set it by hand</i>")
        return "<br>".join(rows)

    # ------------------------------------------------------------------
    def _sections(self):
        """This page's entries grouped into the sections the grid draws,
        as [(status, [entry, ...]), ...] in the order they appear.

        Split out from _refresh_grid because the drag-to-reorder switch to
        Custom Order has to save the order that is on screen (see
        _begin_custom_order), and on this page that is the sections read
        top to bottom - not what _visible_entries returns, which is one
        flat list the grouping below then cuts up."""
        # Grouped into sections by status (Watching/Reading first, same
        # order as STATUSES_BY_TYPE) instead of one flat mixed grid - the
        # sort dropdown still controls ordering *within* each section.
        known_statuses = STATUSES_BY_TYPE.get(self.ENTRY_TYPES[0], STATUSES_BY_TYPE["Anime"])
        grouped = {status: [] for status in known_statuses}
        for entry in self._visible_entries():
            grouped.setdefault(entry.get("status") or known_statuses[0], []).append(entry)
        extra_statuses = [s for s in grouped if s not in known_statuses]
        sections = [(status, grouped[status])
                    for status in [*known_statuses, *extra_statuses] if grouped.get(status)]
        if not self.SPLIT_SECTIONS_BY_TYPE:
            return sections
        # Films and shows share every status but nothing else - one is a
        # single sitting, the other is a season and an episode number -
        # so "Watching" holding both put two unlike things on one row.
        split = []
        for status, group in sections:
            by_type = {entry_type: [] for entry_type in self.ENTRY_TYPES}
            for entry in group:
                by_type.setdefault(entry.get("type") or self.ENTRY_TYPES[0], []).append(entry)
            for entry_type, entries in by_type.items():
                if entries:
                    split.append((f"{status} · {SECTION_TYPE_PLURAL.get(entry_type, entry_type)}",
                                  entries))
        return split

    # ------------------------------------------------------------------
    def paintEvent(self, event):
        super().paintEvent(event)
        if self._painted:
            return
        self._painted = True
        pending, self._after_paint = self._after_paint, []
        for callback in pending:
            # Off a timer, never called from inside a paint: these build
            # and re-lay-out widgets, and doing that while Qt is painting
            # the very widget tree they are in is how a repaint ends up
            # recursing into itself.
            QTimer.singleShot(0, lambda cb=callback: _if_alive(cb))

    def _after_first_paint(self, callback, delay_ms=0):
        """Run `callback` once this page has been on screen.

        **Deferring by a timer is not enough, and that is measured.** A
        zero timer - and a 16ms one - posts an event the loop drains in
        the round it is already draining, so a chain of them still ran
        to completion before Qt sent a single paint: Watch's ninety
        Discover cards were all built in front of the first frame, which
        did not reach the screen for 254-486ms after the click (paint-spy
        measurement, 22 August 2026, against the owner's 100ms budget).
        Hanging the work off the *paint itself* is what actually puts
        the page on screen first."""
        if self._painted:
            QTimer.singleShot(delay_ms, lambda: _if_alive(callback))
        else:
            self._after_paint.append(callback)

    def _refresh_grid(self, *_args):
        self._grid_built = True
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        # Emptied here, before the cards it names are deleted: every
        # entry in it points at a widget this loop has just taken out of
        # the layout, and repainting one of those afterwards is a call
        # into a C++ object that is on its way out.
        self._selection_cards = {}

        self._card_providers = anime_sites.streaming_provider_map()

        # Dragging is disabled while a search is narrowing the grid: a
        # drop writes the order that is on screen (see
        # _begin_custom_order), and the order on screen is not the whole
        # order while entries are hidden.
        narrowed = self._grid_is_narrowed()

        sections = self._sections()
        # The selection can only ever hold what is on screen. A search, a
        # filter, a re-sort or a status change that moved an entry out of
        # the view all arrive here, and each drops whatever it hid - so
        # "3 entries selected" always names three cards the user can see
        # and check before pressing Set Status.
        visible_ids = {entry.get("id") for _status, group in sections for entry in group}
        self._selected_ids &= visible_ids
        self._update_selection_bar()

        if not sections:
            if self._search_query():
                message = f"Nothing here matches '{self.search_box.text().strip()}'."
            elif narrowed:
                message = "Nothing matches the filter - clear it from the filter button."
            else:
                message = f"No {self.TITLE.lower()} yet - click '+' to create one."
            # No row/column arguments: this layout became a QVBoxLayout
            # when sections turned into sideways-scrolling rows, and the
            # grid form left here raised TypeError on the one path that
            # still used it - an empty page, or a search matching
            # nothing. Raised inside a slot, which takes the process down
            # rather than showing an error.
            self.grid_layout.addWidget(QLabel(message, objectName="Muted"))
            self.grid_layout.addStretch()
            return

        for status, group in sections:
            self.grid_layout.addWidget(
                QLabel(f"{status} ({len(group)})", objectName="SectionTitle"))
            self.grid_layout.addWidget(self._build_section_strip(group))
        # Somewhere for the leftover height to go. The page's scroll area
        # resizes this widget to the viewport, and with nothing elastic in
        # the layout that spare height is handed to whatever can take it -
        # measured: one section on a full-height page stretched its own
        # header from 23px to 272px, leaving the row stranded mid-page.
        # Two sections hid it, because there was no spare height left.
        self.grid_layout.addStretch()

    def _build_section_strip(self, group):
        """One section's cards on a single line that scrolls sideways.

        Its own scroll area per section, not one for the page: sections
        are different lengths, and a shared sideways scroll would drag a
        short section off screen to reach the end of a long one.

        Vertical wheel is left alone (the bar is off, so Qt hands the
        event up to the page, which is what should scroll); Shift+wheel,
        the bar itself, and the arrows SideScroller lays over each end
        move the row."""
        return self._build_card_strip([self._build_card(entry) for entry in group])

    def _build_card_strip(self, cards):
        """The sideways-scrolling row itself, given the cards to fill it.

        Split out from _build_section_strip so the Discover rows are the
        same strip with different cards in it - height maths, scrollbar
        allowance and SideScroller wrapping included - rather than a
        second row mechanism that would drift from this one."""
        strip = QWidget(objectName="Bare")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(14)
        for card in cards:
            strip_layout.addWidget(card)
        strip_layout.addStretch()

        area = QScrollArea(objectName="Bare")
        area.setWidget(strip)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.viewport().setAutoFillBackground(False)
        strip.setAutoFillBackground(False)
        # Height is the cards' own, plus room for the bar when it appears.
        # Without a fixed height the area claims the whole page and one
        # section fills the window.
        strip.adjustSize()
        # **The tallest card's own hint, not the strip's.** Same reason
        # as CardTextLabel above: a strip holding a word-wrapped title
        # reports a height its cards do not fit in, and this row is
        # setFixedHeight - so whatever it under-reports is clipped for
        # good. Taking the max over the cards means one long title sets
        # the row height for all of them, which is what a row of equal
        # cards should do anyway.
        # **ensurePolished first, or the measurement is taken with the
        # wrong font.** A card's title takes its weight and size from
        # QSS (#CardTitle), and Qt does not apply a stylesheet to a
        # widget until it is polished - which normally happens on show,
        # long after this runs. Measuring first gave 290px for cards
        # that lay out at 306 (it had been 274 before CardTextLabel), so
        # the row still clipped 16px of every hover border. Polishing
        # here makes heightForWidth answer for the font the text will
        # actually be drawn in.
        for card in cards:
            card.ensurePolished()
            for child in card.findChildren(QLabel):
                child.ensurePolished()
        tallest = max([c.sizeHint().height() for c in cards] or [0])
        area.setFixedHeight(max(strip.sizeHint().height(), tallest)
                            + area.horizontalScrollBar().sizeHint().height())
        # Wrapped, not replaced: the arrows and their fades are drawn over
        # this same area, which keeps scrolling exactly as it did.
        scroller = SideScroller(area)
        # The row a later chunk appends to (see _fill_row_rest). Hung off
        # the returned widget rather than looked up through
        # area.widget().layout(), which is three hops through objects
        # this file does not own.
        scroller._strip_layout = strip_layout
        # **And the way to re-fit it.** The height above is computed from
        # the cards that exist *now*, which is the first
        # DISCOVER_STRIP_CHUNK of them - _fill_row_rest appends the rest
        # a chunk at a time afterwards. A later card with a longer title
        # is taller, so the strip grows past the height this row was
        # pinned to and the overflow is clipped. Measured 22 August 2026
        # on the Read page's Popular Now row: titles hint 16px (one
        # line), 32px (two) and 48px (three) depending on the name, the
        # row was pinned at a 290px viewport from its first eight cards,
        # and every card then laid out at **306px** - so 16px of each
        # one, including the bottom of its hover border, sat below the
        # visible row. That is the owner's "the hover highlight is going
        # down more than the scrollable row could show".
        scroller._refit_height = lambda: self._fit_strip_height(area, strip)
        return scroller

    @staticmethod
    def _fit_strip_height(area, strip):
        """Grow a sideways row to whatever its cards now need.

        Only ever grows: shrinking mid-fill would make the row jump
        about as chunks land, and a row that is briefly too tall reads
        as spacing while one that is too short clips its own cards."""
        try:
            cards = [w for w in strip.findChildren(Card)]
            for card in cards:
                card.ensurePolished()
                for child in card.findChildren(QLabel):
                    child.ensurePolished()
            tallest = max([c.sizeHint().height() for c in cards] or [0])
            needed = (max(strip.sizeHint().height(), tallest)
                      + area.horizontalScrollBar().sizeHint().height())
            if needed > area.height():
                area.setFixedHeight(needed)
        except RuntimeError:
            pass        # the row was torn down under a queued chunk

    def _build_card(self, entry):
        card = Card(hoverable=True)
        card.setFixedWidth(POSTER_SIZE[0] + 20)
        provider = getattr(self, "_card_providers", {}).get(entry.get("site_id"))
        card.set_tooltip_provider(lambda en=entry, pv=provider: self._tooltip_html(en, pv))
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 10, 8, 10)
        card_layout.setSpacing(6)

        pixmap = images.thumbnail_or_avatar(entry.get("cover_path"),
                                            entry["title"], POSTER_SIZE)
        # The round continue button is back on the tracker cards, and on
        # every medium now (the owner's ask, reversing the earlier
        # removal): hovering a card offers the hollow ring in the middle
        # of its cover, which resumes exactly where that entry stopped -
        # the reader's saved page, or the player's saved position. The
        # card's body stays one target, the episode/chapter list (the
        # details page). Selection mode keeps the plain cover: its click
        # is a pick, and a resume control inside a pick target is a trap.
        continuable = (not self._select_mode
                       and entry.get("type") in MANGA_TYPES + VIDEO_TYPES)
        if continuable:
            cover = ContinueCover(
                pixmap, POSTER_SIZE,
                lambda en=entry: self._open_entry(en, resume=True))
        else:
            cover = QLabel()
            cover.setFixedSize(*POSTER_SIZE)
            cover.setPixmap(pixmap)
        card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

        if self._select_mode:
            # A child of the cover at a fixed corner offset, not a row in
            # the card's layout: a layout row would make every card taller
            # the moment the mode came on, so entering selection would
            # reflow the whole page under the pointer.
            badge = QLabel(cover)
            badge.setFixedSize(22, 22)
            badge.move(8, 8)
            # The card is what handles the click; a badge that took the
            # press would leave a 22px hole in the middle of its own
            # target.
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._selection_cards[entry.get("id")] = (card, badge)
            self._paint_selection(card, badge, entry.get("id") in self._selected_ids)

        title = QLabel(entry["title"], objectName="CardTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(title)

        status = QLabel(entry["status"], objectName="CardMeta")
        status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        card_layout.addWidget(status)

        # Always visible, not just on hover (the tooltip alone was too easy
        # to miss - this is the answer to "where's the progress").
        progress_text = self._progress_display(entry)
        if progress_text:
            progress_label = QLabel(progress_text)
            progress_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            progress_label.setStyleSheet(
                f"color: {theme.ACCENT}; font-weight: 700; font-size: 9pt; background: transparent;")
            card_layout.addWidget(progress_label)

        # No +/- here any more. Progress is recorded from what you
        # actually open - the reader writes the chapter it opened, the
        # player the episode it played, both through record_progress - so
        # a pair of buttons that guessed the same number by hand is a
        # second, worse answer to a question already answered.

        if self._select_mode:
            card.clicked.connect(lambda en_id=entry.get("id"): self._toggle_selected(en_id))
        else:
            # One target: the details page - the episode/chapter list.
            # Resuming is that page's Continue button. resume=False is
            # what routes a video/reading entry there (open_in_app);
            # types with no details page keep their old open unchanged.
            card.clicked.connect(
                lambda en=entry: self._open_entry(en, resume=False))
        card.rightClicked.connect(lambda event, en=entry: self._show_context_menu(event, en))
        # Dragging is off while selecting as well as while the grid is
        # narrowed: both a drag and a pick want the same left press, and
        # attaching the drag also moves `clicked` from press to release.
        if not self._grid_is_narrowed() and not self._select_mode:
            self._drag_reorder.attach(card, entry.get("id"))
        if continuable:
            # After every child exists - the relay watches each of them,
            # since each takes the hover off the card as the pointer
            # crosses onto it (see attach_continue_cover).
            attach_continue_cover(card, cover)
        return card

    def _progress_display(self, entry):
        # Hidden by the entry's own "Show Last Watched" tick, and always
        # for a film, which has no episode to be on.
        if not shows_last_watched(entry):
            return ""
        if entry["type"] in MANGA_TYPES:
            watched = entry.get("last_watched_chapter")
            return f"Ch {format_chapter_progress(watched)}" if watched else ""
        # Anime/Series progress is only ever auto-filled with a *guess*
        # (the latest episode currently out, not necessarily what you've
        # watched) unless it came from a connected account or you typed it
        # in yourself - only show it here once it's actually confirmed as
        # real, so the card never states a guess as fact.
        if not entry.get("progress_verified"):
            return ""
        return entry.get("progress") or ""

    def _open_entry(self, entry, resume=True):
        """Open an entry. `resume=False` is the reading card's body -
        the chapter list rather than the chapter that was open; the round
        button on its cover passes True."""
        if not open_tracker_entry(self, entry, resume=resume):
            self._open_form(edit=True, entry=entry)

    def _on_inapp_closed(self, entry_id):
        """The player, reader or details page opened from here has
        closed - pick up whatever progress it wrote and redraw, so the
        card shows the new number the moment the overlay is gone
        (previously it took a page switch or a Sync click).

        The entry is re-read from disk rather than trusted in memory:
        the reader and the details page work on *copies* of the entry
        dict, so their writes reach the file and not this page's own
        object."""
        fresh = next((e for e in storage.load(self.DATA_FILE, [])
                      if e.get("id") == entry_id), None)
        if fresh:
            entry = next((e for e in self.entries
                          if e.get("id") == entry_id), None)
            if entry is not None:
                entry.update(fresh)
            elif fresh.get("type") in self.ENTRY_TYPES:
                # Saved from a details page opened off Discover - the
                # entry was written straight to disk and this page has
                # never held it. Adopting it here is what makes it appear
                # in Saved (and earn its chip) without a page rebuild.
                self.entries.append(fresh)
                self._refresh_schedules()
                self._sync_discover_saved()
        self._refresh_grid()
        # The overlay can have moved progress on, which moves a schedule
        # row's wording with it - rebuild only when that tab is the one
        # actually on screen.
        if self._active_tab == TAB_SCHEDULE:
            self._build_schedule()

    def _adopt_disk_rows(self):
        """Fold whatever DATA_FILE has gained since this page was built
        into self.entries - the Saved tab's version of the rebuild that
        Discover/Schedule/History get on every visit (_set_tab). The
        details page writes a Discover save straight to disk, and until
        the overlay hands _on_inapp_closed a usable id the page holds no
        trace of it - the row stayed invisible until a page rebuild, and
        worse, a reorder's _save_entries would read "on disk, my type,
        not held" as "this page just deleted it" and erase the entry.

        Merges instead of swapping the list: in-flight schedule/cover
        lookups hold references to the dicts already in self.entries
        (_on_schedule_resolved finds its entry there by id), so held
        dicts are updated in place and only genuinely new rows appended.
        """
        held = {e.get("id"): e for e in self.entries if e.get("id")}
        changed = added = False
        for row in storage.load(self.DATA_FILE, []):
            row_id = row.get("id")
            if not row_id:
                continue
            entry = held.get(row_id)
            if entry is not None:
                if any(entry.get(key) != value for key, value in row.items()):
                    entry.update(row)
                    changed = True
            elif row.get("type") in self.ENTRY_TYPES:
                self.entries.append(row)
                held[row_id] = row
                changed = added = True
        if added:
            # New rows have nothing cached, so this fires lookups for
            # exactly them (needs_refresh skips everything else) - and
            # any built Discover card for the title earns its chip.
            self._refresh_schedules()
            self._sync_discover_saved()
        if changed:
            self._refresh_grid()

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_form(edit=True, entry=entry))
        # The second way into selection mode, from the card the user is
        # already pointing at - the toolbar button alone means noticing a
        # word at the far end of the row before knowing to look for it.
        if not self._select_mode:
            menu.addAction("Select", lambda: self._set_select_mode(True, entry.get("id")))
        if entry.get("type") in ("Anime", "Series") and _entry_imdb_id(entry):
            menu.addAction("Sync Progress", lambda: self._sync_progress(entry))
        # No Move Up/Move Down: every page reorders by dragging a card,
        # and two menu items that only appeared under one sort mode were
        # a second way to do the same thing, worse.
        menu.addAction("Delete", lambda: self._delete_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    def _not_found_message(self, reason: str) -> str:
        """What to say when a sync found no progress.

        The causes the user can act on are a Stremio account that was
        never connected - saying "not found" for that made the app look
        broken rather than unconfigured, which cost its owner a long
        time before anyone worked it out - and one that was connected and
        has since been rejected, which reads identically on screen and is
        worse, because it starts working and then stops."""
        codes = set((reason or "").split(","))
        if REASON_STREMIO_AUTH_FAILED in codes:
            return ("Stremio rejected the saved sign-in, so it can't be asked "
                    "what you've watched - this is not about this title. It "
                    "happens after a password change or a \"log out "
                    "everywhere\". Reconnect the account in Settings > "
                    "Stremio Account and sync again.")
        if REASON_NO_STREMIO_ACCOUNT in codes:
            return ("No Stremio account is connected, so there is nothing to "
                    "read your progress from. Connect one in Settings > "
                    "Stremio Account - it's the only service that publishes "
                    "what you've actually watched.")
        return ("No real progress found for this title. Either it isn't in "
                "your Stremio library, or Stremio has no specific episode "
                "recorded for it - that only happens once you've actually "
                "pressed play on one through Stremio itself, not just added "
                "it to your library. You can still set your progress by hand "
                "in Edit.")

    def _entry_provider(self, entry):
        """The unreadable-service name for this entry, or None. Read here
        on the UI thread rather than in the worker - it reads the saved
        sites file."""
        return anime_sites.streaming_provider(entry.get("site_id"))

    def _sync_progress(self, entry):
        """Re-fetch this one entry's *real* progress from your connected
        Stremio account and overwrite the stored value with it. Unlike
        the initial add-time
        guess, this can be re-run any time, so a saved entry never has to
        stay stuck on a fallback estimate."""
        imdb_id = _entry_imdb_id(entry)
        if not imdb_id:
            return
        # Its own thread, not the bounded pool: this is one lookup the
        # user asked for by hand and is watching for, so it must not
        # queue behind a page-load backfill of every other entry.
        threading.Thread(
            target=self._fetch_real_progress,
            args=(entry["id"], imdb_id, entry["title"], False),
            daemon=True).start()

    def _sync_all_progress(self):
        """Re-sync every linked entry on this page at once, instead of
        right-clicking each card individually."""
        # Films are skipped: nothing displays their progress now, so
        # syncing one would be a request per film for a number no card
        # ever shows.
        targets = [e for e in self.entries if e["type"] in self.ENTRY_TYPES
                   and tracks_progress(e["type"]) and _entry_imdb_id(e)]
        if not targets:
            # Only said during an explicit refresh. On arrival there is
            # nothing to report - a page opening with nothing to sync is
            # the ordinary case, not news.
            if self._refresh_toast is not None:
                show_toast(self, "Nothing to sync yet - no linked entries")
            return
        # The whole batch counts as one step of the refresh, closed out
        # when the last of them comes back (see _on_progress_synced) -
        # they already have _sync_pending counting them individually.
        self._sync_batch_run = self._refresh_step_started()
        self._sync_pending += len(targets)
        for entry in targets:
            lookup_pool.submit(
                self._fetch_real_progress, entry["id"], _entry_imdb_id(entry),
                entry["title"], True)

    def _fetch_real_progress(self, entry_id, imdb_id, title, silent):
        """Your real progress for one entry, from your Stremio account.

        Stremio is the only source, deliberately. Everything else that
        could answer this was tried and removed: Crunchyroll publishes
        nothing without a login it grants no one, and the list services
        only ever knew whatever some other tracker had written to them -
        which is how a card confidently showed an episode its owner had
        never reached. One source that is right beats three that
        disagree."""
        result = None
        source = ""
        reasons = []
        _, auth_key = app_settings.get_stremio_auth()
        if auth_key:
            try:
                result = stremio.fetch_watch_progress(imdb_id, auth_key)
                source = SOURCE_STREMIO if result else ""
            except stremio.AuthFailed:
                # The saved key is dead, not the title missing. This used
                # to land in the bare except below and read as "not in
                # your library" - for every entry, indefinitely, because
                # Stremio is the only source and nothing retries a wrong
                # answer that never looked wrong.
                reasons.append(REASON_STREMIO_AUTH_FAILED)
                result = None
            except Exception:
                result = None
        else:
            reasons.append(REASON_NO_STREMIO_ACCOUNT)
        try:
            total = stremio.fetch_latest_episode(imdb_id, "series")
        except Exception:
            total = None
        season, episode = result or (0, 0)
        total_season, total_episode = total or (0, 0)
        self._sync_signals.resolved.emit(
            entry_id, season, episode, result is not None, total_season, total_episode,
            silent, ",".join(reasons), source)

    def _on_progress_synced(self, entry_id, season, episode, found, total_season,
                            total_episode, silent, reason="", source=""):
        entry = next((e for e in self.entries if e["id"] == entry_id), None)
        if entry:
            snapshot = self._refresh_before.get(entry_id)
            before = (snapshot[1:] if snapshot
                      else (entry.get("progress"), entry.get("latest_available")))
            # Forward-only, same rule as record_progress. Atomic plays
            # video itself now, so Stremio is no longer the only thing
            # that knows what was watched - and it knows nothing about an
            # episode played here. Letting a lower synced number win
            # would erase that play seconds after it happened, which is
            # the "editing fights the sync" problem with the winner
            # reversed. Nothing is lost the other way: a genuinely higher
            # Stremio number still lands.
            if found and progress_moves_forward(entry, season, episode):
                entry["progress"] = format_episode_progress(season, episode)
                entry["progress_verified"] = True
                entry["progress_source"] = source
                entry["updated_at"] = storage.now_iso()
            if total_season or total_episode:
                entry["latest_available"] = format_episode_progress(total_season, total_episode)
            # Only the bulk path feeds the refresh button's verdict - a
            # single entry synced by hand from its right-click menu isn't
            # part of anything being reported on.
            if silent and (entry.get("progress"), entry.get("latest_available")) != before:
                self._sync_changed = True

        if silent and REASON_STREMIO_AUTH_FAILED in set((reason or "").split(",")):
            self._stremio_auth_broken = True

        if not silent:
            if not found:
                inform(self, "Sync Progress", self._not_found_message(reason))
                return
            self._save_entries()
            self._refresh_grid()
            return

        # Bulk sync: results trickle in from several background threads, so
        # only save/redraw once they've all come back. Whether any of them
        # actually moved anything is what the refresh button reports; a
        # title Stremio has no per-episode history for (see the
        # single-entry message above) is not a failure, just nothing new.
        self._sync_pending -= 1
        if self._sync_pending <= 0:
            self._save_entries()
            self._refresh_grid()
            changed, self._sync_changed = self._sync_changed, False
            run, self._sync_batch_run = self._sync_batch_run, 0
            # A refresh has a toast of its own to say this through, and
            # two toasts share one corner - so only the arrival sync,
            # which has none, raises its own here.
            if self._stremio_auth_broken and self._refresh_toast is None:
                self._stremio_auth_broken = False
                self._warn_stremio_auth_once()
            self._refresh_step_done(run, changed)

    def _warn_stremio_auth_once(self, toast=None):
        """Say that the saved Stremio session has stopped being accepted.

        Given a live "Updating..." toast, it finishes into that one - the
        refresh was asked for, so it answers every time and must not leave
        the sticky toast up. On the silent arrival path there is nothing
        to finish, so a fresh toast is raised, once per app run (see
        _auth_warning_shown)."""
        global _auth_warning_shown
        if toast is not None:
            _auth_warning_shown = True
            finish_toast(toast, self, _STREMIO_AUTH_TOAST, 6000)
            return
        if _auth_warning_shown:
            return
        _auth_warning_shown = True
        show_toast(self, _STREMIO_AUTH_TOAST, 6000)

    def _save_entries(self):
        """Write this page's entries back without discarding another
        page's.

        Anime and Reading are both backed by tracker.json, and each holds
        its own copy of the *whole* file loaded when it was built. Saving
        that copy wholesale writes back the other page's entries as they
        were at build time, so whichever page saved last silently undid
        the other - a synced episode number reverting the next time the
        Reading page saved anything, which is what "progress doesn't
        track" turned out to be. Reproduced end to end before this
        existed: progress written as S09E99, then back to S01E04 the
        moment the other page saved.

        So: this page's own entry types come from this page, everything
        else is re-read from disk, and anything a page added since this
        one loaded is carried over rather than dropped. `update_entry`
        remains the right call for a single field on a single entry (see
        .claude/rules/ui.md); this is for the paths that genuinely
        rewrite the list, like reordering."""
        on_disk = storage.load(self.DATA_FILE, [])
        fresh = {e.get("id"): e for e in on_disk if e.get("id")}
        merged = []
        for entry in self.entries:
            if entry.get("type") in self.ENTRY_TYPES:
                merged.append(entry)          # mine - this page's copy wins
                continue
            other_pages_copy = fresh.get(entry.get("id"))
            if other_pages_copy is not None:
                merged.append(other_pages_copy)   # theirs - disk is fresher
            elif entry.get("id") not in self._ids_at_load:
                merged.append(entry)          # added here, not saved yet
            # else: it was there when this page loaded and is gone from
            # disk now, so another page deleted it - let the delete stand
        # Anything on disk this page doesn't have: added by another page
        # since this one loaded, so carry it over - *unless* it is one of
        # this page's own types, which means this page just deleted it.
        # Without that distinction a delete is silently undone, because
        # "someone else added it" and "I removed it" look identical from
        # here. Caught by test, not by reading it back.
        known = {e.get("id") for e in merged}
        merged.extend(e for e in on_disk
                      if e.get("id") not in known
                      and e.get("type") not in self.ENTRY_TYPES)
        storage.save(self.DATA_FILE, merged)

    # ------------------------------------------------------------------
    def _begin_custom_order(self):
        """A drag has started, so this page is in Custom Order from here
        on: any other sort is re-applied on the next redraw and would put
        the dragged card straight back where it came from.

        The order already on screen is written out as the custom one
        first, so the switch itself moves nothing and only the drag that
        follows changes anything. The dropdown is then set with its signal
        blocked - the grid would only be rebuilt into the arrangement it
        is already showing, and rebuilding it here would delete the card
        currently being dragged."""
        if self.sort_box.currentText() == "Custom Order":
            return
        # Section by section, top to bottom - the order the user is
        # actually looking at, not the flat _visible_entries one.
        order = [entry.get("id") for _status, group in self._sections() for entry in group]
        storage.apply_custom_order(self.DATA_FILE, order)
        # This page's own copy follows, in place rather than reloaded:
        # every card on screen holds a reference to one of these dicts.
        storage.order_by_ids(self.entries, order)
        self.sort_box.blockSignals(True)
        self.sort_box.setCurrentText("Custom Order")
        self.sort_box.blockSignals(False)

    def _drop_reorder(self, moved_id, target_id):
        """The dragged entry takes the dropped-on entry's place in the
        saved list.

        Against the list, not against grid coordinates: this page draws
        one section per status, so where a card sits on screen says
        nothing about where its entry sits in the file. Dropping onto a
        card in a *different* status section therefore moves the entry in
        the list without appearing to move it much on screen - it still
        belongs to its own section. Reordering within a section, which is
        what the sections are there to make easy, does exactly what it
        looks like."""
        if not storage.move_entry(self.DATA_FILE, moved_id, target_id):
            return
        storage.move_in_list(self.entries, moved_id, target_id)
        defer_grid_rebuild(self._refresh_grid)

    def _delete_entry(self, entry):
        if not confirm(self, "Delete Entry", f"Delete '{entry['title']}'?"):
            return
        # By id, not list.remove(): two entries compare equal to Python
        # the moment their fields match, and the index is wanted anyway so
        # undo can put this one back where it was rather than at the end.
        index = next((i for i, e in enumerate(self.entries)
                      if e.get("id") == entry.get("id")), None)
        if index is None:
            return
        # The whole record, copied before it goes: an entry carries far
        # more than the form shows - cover, link, the cached schedule and
        # its checked-at stamp, the resolved MangaDex/site ids - and undo
        # has to give all of it back, not just re-add a title.
        removed = copy.deepcopy(self.entries.pop(index))
        self._save_entries()
        self._refresh_grid()
        show_undo_toast(self, f"Deleted '{removed['title']}' - Click to Undo",
                        lambda: self._restore_entry(removed, index),
                        on_redo=lambda: self._delete_ids([removed.get("id")]))

    def _restore_entry(self, entry, index):
        # _save_entries re-reads the file and lets this page's own copy
        # win for its own entry types, so putting the record back into
        # self.entries is what writes it back - and it lands in the list
        # where it was, which is the order the grid draws.
        self.entries.insert(min(index, len(self.entries)), entry)
        self._save_entries()
        self._refresh_grid()
        return f"Restored '{entry['title']}'"

    # ------------------------------------------------------------------
    # Discover: the catalog, rather than your list.
    #
    # Laid out after the Harbor client's Discover page at the owner's ask
    # - a wide pill search bar, one large "featured" card built from the
    # top result, then rows of poster cards carrying chips drawn on the
    # artwork - but in Atomic's own palette throughout. Not one colour
    # here is a literal: a borrowed layout must not arrive with a
    # borrowed accent.
    #
    # Nothing in this tab writes an entry. Picking a card opens the
    # ordinary Add form with the title filled in, so the matching, cover
    # download and link resolution that form already does all run exactly
    # as they do for a title typed by hand - one save path, not two.
    def _discover_key(self) -> str:
        """This page's slot in lookup_pool's newest-wins queue.

        Per page, so Anime looking something up doesn't cancel Reading's
        row - and one key for the *whole* tab, not one per row: a key
        holds a single pending job and drops whatever it replaces, so two
        rows submitted under one key would be one row."""
        return f"discover-{self._view_state_key()}"

    def _show_discover(self):
        """Build the tab the first time it is looked at, and only then.

        Every later visit shows what is already there: the rows survive
        a tab switch, so walking to Schedule and back is not another
        catalog request and another thirty poster downloads."""
        if self._discover_built:
            return
        self._build_discover_tab()
        self._discover_built = True
        self._start_discover("")
        self._warm_categories()

    def _warm_categories(self):
        """Fetch each category's rows in the background, before anyone
        clicks one.

        **This is the owner's "the same genre takes 5-9 seconds".**
        Measured 22 August 2026 with the catalogue cache deleted, on the
        owner's own data:

            Read  · Manga    first visit  7.24s     repeat 0.03s
            Read  · Manhwa   first visit  0.02s     repeat 0.02s
            Watch · Movies   first visit  0.03s     repeat 0.02s

        so the machinery is not slow - a category that has been fetched
        once opens in a frame. What cost seven seconds was doing the
        fetch *while the user was looking at an empty section*, because
        nothing had ever asked for it before. The reading catalogues are
        the slow ones: they browse several sites, which is exactly why
        they get their own pool (lookup_pool.BROWSE_WORKERS).

        So the fetch happens when the page opens instead of when the
        section is picked. Cached ones are skipped outright, so this
        costs nothing on a page opened twice, and it goes on the browse
        pool - never the shared queue, which the covers and the
        chapter lists are draining."""
        try:
            _load_discover_cache()
        except Exception:
            return
        sweeps = False
        for key, _label, kind, _entry_type in self.CATEGORY_SECTIONS:
            cached = _DISCOVER_CACHE.get((kind, ""))
            if cached and time.monotonic() - cached[0] < _DISCOVER_CACHE_TTL_S:
                continue        # already fresh; clicking it is a redraw
            # **_fetch_browse_rows directly, not _category_worker.** The
            # worker emits its answer, and _on_category_results *draws*
            # it - which for a warm-up means a section nobody asked for
            # replacing whatever the user is looking at. This only fills
            # the cache; the draw happens when the section is actually
            # picked, straight from that cache.
            if kind.startswith("medium:"):
                # One sweep fills all four medium: keys (see
                # _fetch_browse_rows), so asking for the others would be
                # three more identical browses of the same six sites.
                if sweeps:
                    continue
                sweeps = True
            lookup_pool.submit_browse(_fetch_browse_rows, kind)

    def _build_discover_tab(self):
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.discover_search = search_field(f"Search {self.TITLE.lower()}...")
        self.discover_search.setFixedHeight(DISCOVER_SEARCH_HEIGHT)
        # A pill, where the app's own QLineEdit rule is a RADIUS_SM box.
        # Spelled out per widget rather than added to theme.py, which is
        # not this task's to touch - and every value in it is a token.
        self.discover_search.setStyleSheet(
            f"QLineEdit {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {DISCOVER_SEARCH_HEIGHT // 2}px;"
            f" padding: 6px 18px; font-size: 11pt; }}"
            f"QLineEdit:focus {{ border: 1px solid {theme.ACCENT}; }}")
        # Debounced, and long (see DISCOVER_DEBOUNCE_MS): every pause
        # rebuilds rows of poster cards behind a catalog request.
        self._discover_timer = QTimer(self)
        self._discover_timer.setSingleShot(True)
        self._discover_timer.setInterval(DISCOVER_DEBOUNCE_MS)
        self._discover_timer.timeout.connect(self._on_discover_search)
        self.discover_search.textChanged.connect(
            lambda _text: self._discover_timer.start())
        search_row.addWidget(self.discover_search, stretch=1)
        self._discover_layout.addLayout(search_row)

        self.discover_body = QWidget()
        self._discover_body_layout = QVBoxLayout(self.discover_body)
        self._discover_body_layout.setSpacing(16)
        self._discover_body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._discover_layout.addWidget(scroll_area(self.discover_body, ground=theme.PANEL_FILL), stretch=1)

    def _on_discover_search(self):
        query = self.discover_search.text().strip()
        # Against the query the current rows were built from, not against
        # the box's previous text: typing a space and taking it away
        # again debounces to the same search, and re-running it would
        # throw away rows to redraw the identical ones.
        if query == self._discover_query:
            return
        self._start_discover(query)

    def _start_discover(self, query):
        """Draw the tab's skeleton for `query` and fire the one lookup
        that fills it. Empty query means "what's popular" - the contract
        helpers/discover answers a blank search with."""
        self._discover_run += 1
        run = self._discover_run
        self._discover_query = query
        self._discover_cards = {}
        self._discover_holders = {}
        self._featured_banner = None
        self._featured_save_btn = None
        self._featured_title = ""
        _clear_layout(self._discover_body_layout)

        if discover is None:
            # The one honest thing to say. A source that quietly answers
            # nothing is indistinguishable from a catalog with nothing in
            # it (.claude/rules/integrations.md).
            self._discover_body_layout.addWidget(QLabel(
                "Discover isn't available in this build.", objectName="Muted"))
            return

        # Filled by the first row's results, hidden until then - an empty
        # frame reserving space above a row that may never answer reads
        # as something that failed to load.
        self._discover_featured = QWidget(objectName="Bare")
        featured_layout = QVBoxLayout(self._discover_featured)
        featured_layout.setContentsMargins(0, 0, 0, 0)
        featured_layout.setSpacing(8)
        self._discover_featured.setVisible(False)
        self._discover_body_layout.addWidget(self._discover_featured)

        # Browse-only rows are left out entirely while a search is
        # running, rather than shown empty: "Latest" cannot answer a
        # query, and a heading over "Nothing found" reads as a
        # broken row (see DISCOVER_BROWSE_ONLY).
        rows = [row for row in self.DISCOVER_ROWS
                if not (self._discover_query
                        and row[0] in self.DISCOVER_BROWSE_ONLY)]
        labelled = len(rows) > 1
        for kind, label, _entry_type in rows:
            section = QWidget(objectName="Bare")
            column = QVBoxLayout(section)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(QLabel(self._discover_heading(kind, label, labelled),
                                    objectName="SectionTitle"))
            column.addWidget(QLabel(self._discover_subheading(kind),
                                    objectName="Muted"))
            holder = QWidget(objectName="Bare")
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(0, 8, 0, 0)
            holder_layout.setSpacing(0)
            holder_layout.addWidget(QLabel("Looking around...", objectName="Muted"))
            column.addWidget(holder)
            self._discover_holders[kind] = holder
            self._discover_body_layout.addWidget(section)

        # One job per row rather than one job walking every row in turn:
        # the rows are independent requests to independent hosts, and
        # serialized they cost their *sum* - measured ~1.3s each against
        # Cinemeta, so the merged three-row page paid ~4s where the
        # slowest single row is all it has to. submit_latest keyed per
        # row keeps the newest-wins behaviour typing relies on.
        for kind, _label, _entry_type in rows:
            lookup_pool.submit_latest(f"{self._discover_key()}-{kind}",
                                      self._discover_row_worker, kind, query, run)

    def _discover_heading(self, kind, label, labelled) -> str:
        if self._discover_query:
            return f"{label} Results" if labelled else "Results"
        named = self.DISCOVER_HEADINGS.get(kind)
        if named:
            return named
        return f"Popular {label}" if labelled else "Popular Now"

    # ---- the category sections (Series/Anime/Movies, Manga/Manhwa/...) --
    def _show_category(self, section):
        """Fill the category tab for one section, newest request wins.

        Deliberately the same cards, the same cache and the same worker
        pool Discover uses - a category is a browse of one kind, not a
        second kind of page. What differs is that it draws as a wrapping
        grid rather than a sideways strip: a section the user chose by
        name should show everything it has, not a row to scroll through
        sideways."""
        key, label, kind, entry_type = section
        self._category_key = key
        self._category_run += 1
        run = self._category_run

        # **The cache is read here, on the UI thread, not on a worker.**
        # These rows are the same rows Discover's are - same catalogue,
        # same key - so a warmed section has them in hand already and
        # the only thing between the click and the grid is a dictionary
        # lookup. Going through the pool for a hit cost a queue hop and
        # a signal round trip for nothing, and the section still had to
        # paint "Looking around..." first. Measured on the owner's data
        # before this: 237-393ms for a video section and 1.7-5.1s for a
        # reading one, against a 100ms budget (CLAUDE.md rule 7).
        _load_discover_cache()
        cached = _DISCOVER_CACHE.get((kind, ""))
        if cached:
            self._draw_category(kind, list(cached[1]), label, entry_type, run)
            if time.monotonic() - cached[0] < _DISCOVER_CACHE_TTL_S:
                return
            # Stale: what is on screen is right enough to look at while
            # it is corrected in place, which is the same contract
            # _discover_row_worker keeps.
            lookup_pool.submit_browse(self._category_worker, kind, run)
            return

        _clear_layout(self._category_body_layout)
        heading = QLabel(label, objectName="SectionTitle")
        self._category_body_layout.addWidget(heading)
        self._category_body_layout.addWidget(
            QLabel(self._category_note(kind), objectName="Muted"))
        self._category_body_layout.addWidget(
            QLabel("Looking around...", objectName="Muted"))
        lookup_pool.submit_browse(self._category_worker, kind, run)

    @staticmethod
    def _category_note(kind):
        if kind.startswith("medium:"):
            medium = kind.split(":", 1)[1]
            if medium == "Other":
                return ("Originally published somewhere other than Japan, "
                        "Korea or China")
            return f"Most followed {medium.lower()}"
        return "Most watched"

    def _category_worker(self, kind, run):
        """One category's rows, and the cache they are written into.

        Never raises - a dead pool worker takes every queued lookup in
        the app with it - and always reports, so a section that finds
        nothing says so rather than sitting on "Looking around..."."""
        rows = _fetch_browse_rows(kind)
        self._category_signals.results.emit(
            kind, [r for r in (rows or []) if isinstance(r, dict)], run)

    def _on_category_results(self, kind, results, run):
        if run != self._category_run:
            return
        section = next((row for row in self.CATEGORY_SECTIONS
                        if row[2] == kind), None)
        if section is None:
            return
        _key, label, _kind, entry_type = section
        self._draw_category(kind, results, label, entry_type, run)

    def _draw_category(self, kind, results, label, entry_type, run):
        """Put one category's rows on screen. Shared by the cache hit
        (drawn straight from _show_category) and the worker's answer, so
        both land identically."""
        _clear_layout(self._category_body_layout)
        self._category_body_layout.addWidget(
            QLabel(label, objectName="SectionTitle"))
        self._category_body_layout.addWidget(
            QLabel(self._category_note(kind), objectName="Muted"))
        if not results:
            self._category_body_layout.addWidget(
                QLabel("Nothing to show right now.", objectName="Muted"))
            return
        # **The card set is stamped with _discover_run, not the run this
        # was called with, and that is the whole of the owner's "the
        # images are not there when I come back".**
        #
        # `run` here is _category_run, which counts category *redraws*.
        # The cards it builds go through _request_poster, whose answers
        # land in _on_discover_poster - and that guard compares against
        # _discover_run. The two counters are independent, so they agree
        # exactly once and then drift apart for good. Measured 22 August
        # 2026 over four visits to Movies:
        #
        #     visit 1  cards 89  blank  0  delivered 76  dropped  0
        #     visit 2  cards 89  blank 30  delivered  0  dropped 30
        #     visit 3  cards 89  blank 30  delivered  0  dropped 30
        #     visit 4  cards 89  blank 30  delivered  0  dropped 30
        #
        # Every cover after the first visit was fetched, decoded, and
        # then thrown away by a run check against the wrong counter.
        #
        # So this does what _start_discover does at the same moment in
        # its own life: a rebuilt card set is a new generation, and both
        # the registry and the counter that guards it are reset together.
        # _category_run keeps its own job - guarding the row *worker* -
        # which is why it is not simply merged into this one.
        self._discover_run += 1
        card_run = self._discover_run
        self._discover_cards = {}
        cards = [self._build_discover_card(kind, index, item, entry_type,
                                           card_run)
                 for index, item in enumerate(results)]
        self._category_body_layout.addWidget(self._build_card_grid(cards))

    def _discover_subheading(self, kind="") -> str:
        if self._discover_query:
            return f"Matches for '{self._discover_query}'"
        return (self.DISCOVER_SUBHEADINGS.get(kind)
                or "What the catalog is watching")

    def _discover_row_worker(self, kind, query, run):
        """One row's rows, cached for the session.

        Must never raise - an uncaught exception here kills the pool's
        worker thread - and must always report, even when a source
        answers nothing, or a row that never reports sits on "Looking
        around..." for good.

        The cache is answered *and refilled* here on the worker, so a
        fresh hit costs no request at all and an expired one is shown
        immediately and then corrected - the tab is never blank while a
        list that was on screen ten seconds ago is re-fetched."""
        key = (kind, query)
        _load_discover_cache()
        stamp_and_rows = _DISCOVER_CACHE.get(key)
        if stamp_and_rows is not None:
            stamp, rows = stamp_and_rows
            self._discover_signals.results.emit(kind, list(rows), run)
            if time.monotonic() - stamp < _DISCOVER_CACHE_TTL_S:
                return
        try:
            if kind == "reading_latest":
                # MangaDex by newest chapter, not the site browse
                # above - see discover.discover_reading_latest for
                # why a second site-browse row would have been the
                # same titles twice.
                found = discover.discover_reading_latest(
                    limit=DISCOVER_LIMIT)
            elif kind == "reading":
                # The user's own four sites, not MangaDex (the owner's
                # ask) - and each row remembers which site it came from,
                # so pressing it opens that site's chapters directly.
                found = discover.discover_reading_sites(query=query,
                                                        limit=DISCOVER_LIMIT)
            else:
                found = discover.discover_video(kind, query=query,
                                                limit=DISCOVER_LIMIT)
        except Exception:
            found = None
        rows = [item for item in (found or []) if isinstance(item, dict)]
        if rows:
            _DISCOVER_CACHE[key] = (time.monotonic(), list(rows))
            if not query:
                _save_discover_row(kind, rows)
        if stamp_and_rows is not None and not rows:
            return    # keep the stale-but-real list over an empty refresh
        self._discover_signals.results.emit(kind, rows, run)

    def _on_discover_results(self, kind, results, run):
        if run != self._discover_run:
            return
        # **Nothing is drawn before the page's own first frame.** These
        # rows come out of a cache warmed at launch, so all of Watch's
        # answered inside the same event-loop turn the page was built
        # in - three strips and a hero banner standing between the click
        # and anything at all on screen. Measured on the owner's data,
        # 22 August 2026: 72ms of this method alone, on a 100ms budget
        # for the whole move (CLAUDE.md rule 7). Held back, the page
        # arrives with its headings and fills in a frame later; on the
        # ordinary path - a row the network is still answering - this
        # changes nothing, because the first paint has long since
        # happened by the time a result lands.
        if not self._painted:
            self._after_first_paint(
                lambda: self._on_discover_results(kind, results, run))
            return
        holder = self._discover_holders.get(kind)
        if holder is None:
            return
        entry_type = next((t for k, _l, t in self.DISCOVER_ROWS if k == kind),
                          self.ENTRY_TYPES[0])
        rows = list(results)
        # The top of the first row is drawn large instead of being
        # repeated behind itself in the strip below.
        if rows and kind == self.DISCOVER_ROWS[0][0]:
            self._fill_featured(rows[0], entry_type, run)
            rows = rows[1:]
        if rows:
            # **The first screenful now, the rest on the event loop.**
            # These rows come out of a cache, so all three of Watch's
            # answered inside the same event-loop turn the page was
            # created in - ninety cards and a hero banner built before
            # Qt was allowed to paint anything, which is why the page
            # itself did not reach the screen for 336-580ms (measured on
            # the owner's data with a paint spy, 22 August 2026, against
            # a 100ms budget). The rest of a row is off screen anyway:
            # a strip is sideways-scrolling and shows about seven.
            head = rows[:DISCOVER_STRIP_CHUNK]
            cards = [self._build_discover_card(kind, index, item, entry_type, run)
                     for index, item in enumerate(head)]
            # Browsing keeps the sideways strips; a typed search wraps
            # into a grid instead (the owner's ask: "when the row is
            # full start a new row") - thirty matches on one sideways
            # line hides all but the first handful.
            content = (self._build_card_grid(cards) if self._discover_query
                       else self._build_card_strip(cards))
            rest = rows[DISCOVER_STRIP_CHUNK:]
            if rest:
                self._after_first_paint(
                    lambda: self._fill_row_rest(content, kind, rest,
                                                entry_type, run,
                                                DISCOVER_STRIP_CHUNK),
                    DISCOVER_CHUNK_MS)
        else:
            content = QLabel(
                f"Nothing found for '{self._discover_query}'."
                if self._discover_query else "Nothing to show right now.",
                objectName="Muted")
        self._replace_content(holder, content)

    def _fill_row_rest(self, content, kind, rows, entry_type, run, offset):
        """The rest of one Discover row, a chunk per event-loop turn.

        Chained on zero timers rather than looped, for the same reason
        the reader's chapter list is: the point is that the page stays
        responsive while it fills, and a scroll, a click or a walk to
        another page all get their turn between chunks.

        Gives up the moment the tab has moved on - `run` names the
        lookup that asked for these - and on a RuntimeError, which is
        what a strip deleted under the timer raises."""
        try:
            if run != self._discover_run:
                return
        except RuntimeError:
            return      # the page went away under the timer
        chunk = rows[:DISCOVER_STRIP_CHUNK]
        rest = rows[DISCOVER_STRIP_CHUNK:]
        try:
            layout = content.layout() if content.layout() is not None else None
            inner = getattr(content, "_strip_layout", None)
            target = inner if inner is not None else layout
            if target is None:
                return
            for index, item in enumerate(chunk):
                card = self._build_discover_card(kind, offset + index, item,
                                                 entry_type, run)
                if inner is not None:
                    # Before the trailing stretch, or every card lands
                    # right of it and the row reads as empty.
                    target.insertWidget(target.count() - 1, card)
                else:
                    columns = max(1, getattr(content, "_grid_columns", 5))
                    position = offset + index
                    target.addWidget(card, position // columns,
                                     position % columns)
            # A chunk can carry the longest title in the row; see
            # _build_card_strip for why the row would otherwise clip it.
            refit = getattr(content, "_refit_height", None)
            if callable(refit):
                refit()
        except RuntimeError:
            return      # the row went away under the timer
        if rest:
            QTimer.singleShot(
                DISCOVER_CHUNK_MS,
                lambda: _if_alive(
                    lambda: self._fill_row_rest(content, kind, rest,
                                                entry_type, run,
                                                offset + len(chunk))))

    def relayout_for_sidebar(self):
        """Re-flow the category / search grid because the sidebar folded.

        **These grids had no re-flow at all, which is the owner's "make
        them 9 per row not 8 when the sidebar is folded".** The column
        count is worked out from the body's real width in
        _build_card_grid, and the width the owner was getting was the
        one the page happened to be built at. Measured 22 August 2026 at
        1920x1080:

            sidebar open    body 1604px  ->  8 columns
            sidebar folded  body 1756px  ->  9 columns

        so the arithmetic was already right and simply never re-run -
        fold the rail on a page that was built expanded and it kept the
        eight-wide grid until the next visit. Same hook and same reason
        as link_grid.relayout_for_sidebar; main calls it on whatever
        page is showing.

        Redraws from what is already in hand - the cached rows - so this
        costs a rebuild of the cards, never a request."""
        key = getattr(self, "_category_key", "")
        section = next((row for row in self.CATEGORY_SECTIONS
                        if row[0] == key), None)
        if section is None or not self.category_tab.isVisible():
            return
        # Straight through _show_category: it reads the same cache the
        # grid was drawn from, so a fold re-wraps rather than re-fetches.
        self._show_category(section)

    @staticmethod
    def _replace_content(holder, widget):
        layout = holder.layout()
        _clear_layout(layout)
        layout.addWidget(widget)

    def _build_card_grid(self, cards):
        """Search results as a wrapping grid - the genre-browse page's
        shape, not the strips'. Column count is taken from the body's
        real width at build time; a search re-runs this whole build, so
        there is no live re-wrap to keep correct, only this one. The
        fallback is for a body that hasn't been laid out yet (results
        landing before the first show), where width() is meaningless."""
        span = POSTER_SIZE[0] + 20 + 14           # card + grid spacing
        width = self.discover_body.width()
        columns = max(2, width // span) if width > span else 5
        host = QWidget(objectName="Bare")
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for index, card in enumerate(cards):
            grid.addWidget(card, index // columns, index % columns)
        # Kept for _fill_row_rest, which places later cards into the same
        # arrangement - a second guess at the column count would wrap
        # the tail differently from the head.
        host._grid_columns = columns
        return host

    # ---- the cards ---------------------------------------------------
    def _saved_titles(self):
        """This page's titles, lowercased, for "is it already saved".

        Title, not id: a Discover result carries a catalog's ids and the
        entry was very likely added from the same catalog, but it may
        equally have been typed in by hand - and the thing the user is
        looking at is the title."""
        return {(entry.get("title") or "").strip().lower()
                for entry in self.entries if entry.get("type") in self.ENTRY_TYPES}

    def _chip(self, parent, text, accent=True):
        """A little label drawn *on* the artwork - Harbor's shape, this
        palette's colours. Mouse-transparent for the same reason the
        selection badge is: the card is what handles the click, and a
        chip that took the press would leave a hole in its own target."""
        chip = QLabel(text, parent)
        chip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if accent:
            chip.setStyleSheet(
                f"background: {theme.ACCENT_GRADIENT}; color: {theme.ON_ACCENT};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 2px 8px;"
                f" font-size: 8pt; font-weight: 700;")
        else:
            chip.setStyleSheet(
                f"background: {theme.BG}; color: {theme.TEXT};"
                f" border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 2px 7px;"
                f" font-size: 8pt; font-weight: 700;")
        chip.adjustSize()
        # A child added to an already-visible parent does not show
        # itself - which is the case when _sync_discover_saved puts a
        # Saved chip on a card the user is looking at.
        chip.show()
        return chip

    def _place_saved_badge(self, record):
        """Put the Saved chip on one card's artwork, where that card
        keeps it: the top-left corner on a row card, and the foot of the
        featured one, whose top-left already carries FEATURED. The
        height has to be measured rather than assumed - the chip is
        sized by its font and its stylesheet padding."""
        chip = self._chip(record["cover"], "Saved")
        if record.get("badge_at_foot"):
            chip.move(8, record["size"][1] - chip.height() - 8)
        else:
            chip.move(8, 8)
        record["badge"] = chip
        return chip

    @staticmethod
    def _rating_text(item) -> str:
        """"★ 7.4" from a Cinemeta row's imdbRating, or "".

        The star is plain text, like the sidebar's own ◈ bullet
        (theme.NAV_BULLET) - not the missing-glyph case the filter
        button's "▽" turned out to be, which was a *button label* Qt
        laid out beside its menu arrow."""
        raw = (item or {}).get("imdbRating")
        if raw in (None, ""):
            return ""
        try:
            # One decimal, not :g like the chapter numbers elsewhere here:
            # a rating of 7.0 is written "7.0" everywhere it is published,
            # and ":g" renders it "7", which reads as a different number.
            return f"★ {float(raw):.1f}"
        except (TypeError, ValueError):
            text = str(raw).strip()
            return f"★ {text}" if text else ""

    def _request_poster(self, kind, index, item, run):
        url = (item or {}).get("poster")
        # A reading row can arrive with no cover at all: the Madara
        # search endpoint returns titles and links only, which is why a
        # search for "kingdom" drew a wall of letter avatars while the
        # browsed rows all had art (the owner's screenshot). The series
        # page does carry one, so it is fetched here - lazily, per card,
        # and only for the rows that need it.
        page_url = (item or {}).get("url") or ""
        title = (item or {}).get("title") or ""
        # A title alone is now enough to try: the catalogues are asked by
        # name, so a row with neither art nor a series URL is no longer
        # a guaranteed blank (see _fetch_discover_poster).
        if not url and not page_url.startswith("http") and not title:
            return
        # The shared, bounded pool: a row is up to DISCOVER_LIMIT images
        # and a page can have two rows, so a thread each is exactly the
        # shape that once put 651 connections in flight (see lookup_pool).
        lookup_pool.submit(self._fetch_discover_poster, kind, index, url, run,
                           page_url, title)

    def _fetch_discover_poster(self, kind, index, url, run, page_url="",
                               title=""):
        """Resolve one card's art, in three widening steps.

        **The title is passed now, and that is the fix.** Step two asks
        the site's own series page, which falls back to MangaDex and
        AniList when the page carries no cover - but only ever under the
        title it is *given*, and every caller here used to give none. It
        then matched on `_title_from_slug(page_url)`, a URL slug, against
        a 0.85 threshold: an Arabic or transliterated slug misses, so
        the cover came back None and the card stayed blank (the owner's
        wall of empty tiles in Discover). Step three covers the rows
        that have no series URL to scrape either.

        Must never raise - see _discover_row_worker."""
        path, resolved = None, ""
        try:
            if not url and page_url:
                details = manga_sites.fetch_manga_details(
                    page_url, title=title) or {}
                url = resolved = details.get("cover_url") or ""
            if not url and title:
                url = resolved = manga_sites.cover_for_title(title) or ""
            if url:
                # One retry, with a longer budget. Measured: the reading
                # covers come from a host that intermittently takes more
                # than the default 8s to hand over a 17-477KB image, and
                # the URL is right - the next visit fetches it fine. A
                # blank tile that fixes itself tomorrow is exactly the
                # failure the owner has reported twice, and the second
                # attempt costs nothing when the first works.
                path = images.download(url) or images.download(url, timeout=20)
        except Exception:
            path = None
        if path:
            # **Decode here, on this thread, not in the slot.** Profiled
            # 22 August 2026 during a Home -> Watch transition: 40
            # _on_discover_poster calls cost **337ms on the UI thread**,
            # almost all of it PIL - 24 JPEG decodes (174ms) and 24 PNG
            # tile writes (106ms) - which starved the page-slide tween
            # to a single frame in 619ms. The card is drawn by
            # thumbnail_or_avatar either way; warming _fitted first
            # leaves the slot with only the ~0.1ms QPixmap conversion,
            # which is the same split images.prewarm was built for.
            # Fails soft: a miss just decodes in the slot as before.
            try:
                images._fitted(path, tuple(POSTER_SIZE), images._stamp(path))
            except Exception:
                pass
        self._discover_signals.poster.emit(kind, index,
                                           str(path) if path else "",
                                           resolved, run)

    def _on_discover_poster(self, kind, index, path, resolved_url, run):
        if run != self._discover_run or not path:
            return
        record = self._discover_cards.get((kind, index))
        if not record:
            return
        # A cover the worker had to dig out of the series page is written
        # back onto the row's dict, on this thread with every other
        # reader of it: discover_entry copies `poster` into cover_url at
        # pick/save time, and without this a reading row whose search
        # result carried no art (the Madara shape) saved with
        # cover_url=None - its card showed the cover, Saved and Home
        # never did (the Kingdom (WAN) report).
        if resolved_url and not (record.get("item") or {}).get("poster"):
            record["item"]["poster"] = resolved_url
        try:
            record["cover"].setPixmap(images.thumbnail_or_avatar(
                path, record["title"], record["size"]))
        except RuntimeError:
            pass    # the row was rebuilt under it; the new one asks again

    def _build_discover_card(self, kind, index, item, entry_type, run):
        title_text = (item.get("title") or "").strip()
        card = Card(hoverable=True)
        card.setFixedWidth(POSTER_SIZE[0] + 20)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 10, 8, 10)
        card_layout.setSpacing(6)

        # The blank tile stands in until the download lands - the same
        # placeholder a saved entry with no cover keeps for good, so a
        # loading card and a coverless one look alike rather than a
        # loading card looking broken.
        cover = QLabel()
        cover.setFixedSize(*POSTER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(None, title_text, POSTER_SIZE))
        card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

        # `item` is the same dict the click lambda captures, so a poster
        # URL resolved late (see _on_discover_poster) reaches a later
        # pick/save through it.
        record = {"cover": cover, "title": title_text, "size": POSTER_SIZE,
                  "item": item, "badge": None, "badge_at_foot": False}
        if title_text.lower() in self._saved_titles():
            self._place_saved_badge(record)
        rating = self._rating_text(item)
        if rating:
            chip = self._chip(cover, rating, accent=False)
            chip.move(8, POSTER_SIZE[1] - chip.height() - 8)

        # **CardTextLabel, not a plain wrapped QLabel - and that is what
        # made the hover box overflow the row.** A wrapped QLabel's
        # sizeHint is a heuristic (see link_grid.CardTextLabel for the
        # full description), so the strip's sizeHint under-reports how
        # tall a two-line title really is, and _build_card_strip sizes
        # the scroll area from exactly that number. Measured 22 August
        # 2026 on the Read page's Popular Now row: the cards laid out
        # **306px** tall inside a **274px** viewport, so 32px of every
        # card - including the bottom edge of its hover border - was
        # clipped by the row. That is the owner's "the hover highlight
        # is going down more than the scrollable row could show".
        title = CardTextLabel(title_text, DISCOVER_CARD_TEXT_WIDTH)
        title.setObjectName("CardTitle")
        card_layout.addWidget(title)

        year = str(item.get("year") or "").strip()
        if year:
            year_label = QLabel(year, objectName="CardMeta")
            year_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            card_layout.addWidget(year_label)

        card.clicked.connect(
            lambda it=item, et=entry_type: self._on_discover_pick(it, et))
        self._discover_cards[(kind, index)] = record
        self._request_poster(kind, index, item, run)
        return card

    def _fill_featured(self, item, entry_type, run):
        holder = getattr(self, "_discover_featured", None)
        if holder is None:
            return
        layout = holder.layout()
        _clear_layout(layout)
        # No heading over it: the banner's own eyebrow chip says what it
        # is, the Harbor way, and a SectionTitle above a hero read as a
        # caption on a poster.
        layout.addWidget(self._build_featured_banner(item, entry_type, run))
        holder.setVisible(True)

    def _build_featured_banner(self, item, entry_type, run):
        """The top result as Harbor's hero: the title's backdrop filling
        a rounded banner, the facts over its scrim, then View (the
        details page) and a watchlist button. Every colour is a token -
        a borrowed layout must not arrive with a borrowed accent."""
        title_text = (item.get("title") or "").strip()
        # theme.PANEL_FILL, not BG: Discover's banner sits on the panel
        # scroll body (#1a1712 measured through the corners), and that is
        # the colour the banner paints its corner outsides back to.
        banner = HeroBanner(theme.PANEL_FILL)
        column = QVBoxLayout(banner)
        column.setContentsMargins(32, 22, 32, 26)
        column.setSpacing(10)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        eyebrow = QLabel("TOP RESULT" if self._discover_query else "FEATURED")
        eyebrow.setStyleSheet(
            f"color: {theme.ON_ACCENT}; background: {theme.ACCENT_GRADIENT};"
            f" border-radius: {theme.RADIUS_SM}px; padding: 3px 10px;"
            f" font-size: 8.5pt; font-weight: 700; letter-spacing: 1px;")
        chip_row.addWidget(eyebrow)
        chip_row.addStretch(1)
        column.addLayout(chip_row)
        column.addStretch(1)

        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 27pt; font-weight: 800;"
            f" background: transparent;")
        column.addWidget(title)

        facts = QHBoxLayout()
        facts.setSpacing(10)
        meta_bits = [str(item.get("year") or "").strip(),
                     str(item.get("type") or entry_type)]
        meta = QLabel("   ·   ".join(bit for bit in meta_bits if bit))
        meta.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; font-weight: 600;"
            f" background: transparent;")
        facts.addWidget(meta)
        rating = self._rating_text(item)
        if rating:
            chip = QLabel(rating)
            chip.setStyleSheet(
                f"background: {theme.rgba(theme.BG, 180)}; color: {theme.ACCENT};"
                f" border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 2px 9px;"
                f" font-size: 9.5pt; font-weight: 700;")
            facts.addWidget(chip)
        facts.addStretch(1)
        column.addLayout(facts)

        saved = title_text.lower() in self._saved_titles()
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.setContentsMargins(0, 8, 0, 0)
        view_btn = QPushButton("▶  View", objectName="Accent")
        view_btn.setFixedHeight(44)
        view_btn.clicked.connect(
            lambda _checked=False, it=item, et=entry_type:
            self._on_discover_pick(it, et))
        use_hover_cursor(view_btn)
        buttons.addWidget(view_btn)
        # Harbor's "Add to Watchlist": an outlined pill on the artwork,
        # translucent so the backdrop reads through it.
        save_btn = QPushButton("In Saved" if saved else "+  Add to Saved")
        save_btn.setFixedHeight(44)
        save_btn.setEnabled(not saved)
        save_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.rgba(theme.BG, 160)}; color: {theme.TEXT};"
            f" border: 1px solid {theme.TEXT_MUTED}; border-radius: {theme.RADIUS}px;"
            f" padding: 8px 18px; font-weight: 700; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
            f" color: {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_MUTED};"
            f" border: 1px solid {theme.BORDER}; }}")
        use_hover_cursor(save_btn)

        def save_featured(_checked=False, it=item, et=entry_type, btn=save_btn):
            if self._save_discover_item(it, et):
                btn.setText("In Saved")
                btn.setEnabled(False)

        save_btn.clicked.connect(save_featured)
        buttons.addWidget(save_btn)
        buttons.addStretch(1)
        column.addLayout(buttons)

        banner.clicked.connect(
            lambda it=item, et=entry_type: self._on_discover_pick(it, et))
        self._featured_banner = banner
        self._featured_save_btn = save_btn
        self._featured_title = title_text
        # Its own thread, like the details page's art worker and for the
        # same reason: the shared pool is busy with this very tab's rows
        # and posters, and the hero's ground should not queue behind
        # thirty thumbnails.
        threading.Thread(target=self._featured_backdrop_worker,
                         args=(dict(item), entry_type, run), daemon=True).start()
        return banner

    def _featured_backdrop_worker(self, item, entry_type, run):
        """The hero's ground, off the UI thread: TMDB's backdrop by IMDb
        id for the video kinds (artwork caches it on disk, so a revisit
        costs a stat), AniList's banner for reading - the same two
        sources the details page grounds itself with. Never raises."""
        try:
            if entry_type in MANGA_TYPES:
                url = anilist.fetch_manga_artwork(item.get("title") or "")
                found = images.download(url) if url else None
                if found:
                    self._discover_signals.featured_backdrop.emit(str(found), run)
                return
            if not item.get("imdb_id"):
                return
            from helpers import artwork
            probe = {"imdb_id": item.get("imdb_id"),
                     "title": item.get("title") or ""}
            # Small copy first, then the full-resolution original - the
            # details page's pattern. Taking the small one with `or`
            # (what this did) meant the original was never fetched and a
            # banner far wider than 780px was drawn from a 780px image.
            quick = artwork.backdrop_fast_path(probe)
            if quick:
                self._discover_signals.featured_backdrop.emit(str(quick), run)
            full = artwork.backdrop_path(probe)
            if full:
                self._discover_signals.featured_backdrop.emit(str(full), run)
        except Exception:
            return          # no art just keeps the banner's flat panel

    def _on_featured_backdrop(self, path, run):
        if run != self._discover_run:
            return
        banner = getattr(self, "_featured_banner", None)
        if banner is None:
            return
        try:
            # No fade for the sharp original arriving over the small
            # copy - it is the same picture, and dissolving a photo into
            # itself reads as a flicker.
            banner.set_backdrop(path, fade=not banner.has_backdrop())
        except RuntimeError:
            pass    # the tab rebuilt under the fetch

    def _find_saved(self, title):
        """This page's saved entry for `title`, or None. Title-matched,
        the same rule _saved_titles states: the user is looking at the
        title, whatever ids the catalogs disagree about."""
        wanted = (title or "").strip().lower()
        if not wanted:
            return None
        return next((e for e in self.entries
                     if e.get("type") in self.ENTRY_TYPES
                     and (e.get("title") or "").strip().lower() == wanted), None)

    def _discover_entry(self, item, entry_type):
        return discover_entry(item, entry_type)

    def _on_discover_pick(self, item, entry_type):
        """A Discover card opens the title's details page - the episode
        or chapter list - never the Add form (the owner's ask). A title
        already in Saved opens its own entry, progress marks and all;
        anything else goes over as a transient record whose Save button
        on that page is what writes it into Saved."""
        entry = (self._find_saved(item.get("title"))
                 or self._discover_entry(item, entry_type))
        window = _top_window(self)
        if window is None:
            return
        try:
            from windows import details
            page = details.open_details(window, entry)
        except Exception:
            logs.exception("discover could not open the details page")
            return
        _wire_overlay_refresh(page, self, entry)

    def _save_discover_item(self, item, entry_type) -> bool:
        """Write one Discover result straight into Saved - the hero's
        watchlist button. Through _on_form_save, so the id stamp, the
        file write, the redraw, the schedule lookup and the Saved chips
        all happen exactly as a form save makes them happen."""
        title_text = (item.get("title") or "").strip()
        if not title_text:
            return False
        if self._find_saved(title_text):
            show_toast(self, "Already in Saved")
            return True
        entry = self._discover_entry(item, entry_type)
        entry["id"] = str(uuid.uuid4())
        self._on_form_save(entry, True)
        # A reading row can arrive with no cover_url (its own card had
        # to resolve one from the series page) - hand the page URL along
        # so the same resolve happens for the save, instead of the entry
        # sitting coverless forever.
        page_url = (entry.get("url") or "") if entry_type in MANGA_TYPES else ""
        if entry.get("cover_url") or page_url:
            # The poster lands as the entry's cover through the same
            # download-then-update path the sharper-cover backfill uses.
            lookup_pool.submit(self._fetch_sharper_cover, entry["id"],
                               entry.get("cover_url") or "", page_url,
                               entry.get("title") or "")
        show_toast(self, f"'{title_text}' Added to Saved")
        return True

    def _sync_discover_saved(self):
        """Put a Saved chip on any Discover card whose title has since
        been added. Only ever adds one: a card cannot become unsaved
        while this tab is up (deleting happens on Saved, which rebuilds
        the whole page's grid anyway)."""
        saved = self._saved_titles()
        for record in self._discover_cards.values():
            if record.get("badge") is not None:
                continue
            if record["title"].lower() not in saved:
                continue
            try:
                self._place_saved_badge(record)
            except RuntimeError:
                pass    # the row went away under it
        # The hero's watchlist button follows too - a title saved from
        # its own row, or from the details page, must not leave the
        # banner still offering to add it.
        button = getattr(self, "_featured_save_btn", None)
        if (button is not None
                and getattr(self, "_featured_title", "").lower() in saved):
            try:
                button.setText("In Saved")
                button.setEnabled(False)
            except RuntimeError:
                pass    # the tab rebuilt under it

    # ------------------------------------------------------------------
    # Schedule: the page's own entries first, then the catalogue.
    #
    # The saved rows cost no network at all - each is read off an
    # entry's stored `next_release`, which the page's own lookups fill
    # in and cache (see _refresh_schedules) - so that group is a second
    # reading of what the card tooltips already say, never a second
    # source that could disagree with them. Below a visible rule, the
    # catalogue group(s): what SCHEDULE_CATALOGUE names. Every group
    # carries its own heading now, the saved one included ("Saved" -
    # the owner's ask).
    #
    # "upcoming" (Watch): AniList's airing calendar - anime is the only
    # medium with a published forward schedule. "released" (Read): the
    # chapters that just landed, from MangaDex by upload time. Not
    # upcoming, and deliberately so - nobody publishes scanlation
    # dates, and printing a guessed date as a schedule is the failure
    # rules/integrations.md exists to prevent. The saved reading rows'
    # extrapolated dates keep their "Estimated" tag for the same reason.
    SCHEDULE_CATALOGUE = None

    def _request_schedule_upcoming(self):
        """Ask the catalogue what is coming (or just landed), once per
        answer, off the UI thread.

        A good answer is kept module-side (with the Discover cache's own
        TTL) as well as for the page's life - switching tabs rebuilds
        the section, and pages themselves rebuild from scratch per
        visit, so re-asking on every switch was a request per glance and
        a fresh fetch per page walk. A *failed* fetch clears the asked
        flag so the next visit tries again instead of remembering one
        bad minute forever. Never blocks the build - a cache hit fills
        the rows for the build that is about to read them, and a miss
        draws the saved rows now and gains the rest when the fetch
        lands.

        **Deliberately not stamped with _discover_run.** That counter
        numbers Discover searches and advances on every visit and
        keystroke there; stamping this fetch with it meant a Discover
        search while the rows were in flight discarded them for good -
        asked stayed True, so the schedule silently stayed saved-only
        (the owner's "the schedule is not working"). The schedule is
        not a search: any answer is the current calendar, so nothing
        can arrive stale, and the only guard needed is one-request-
        at-a-time, which the asked flag is."""
        if not self.SCHEDULE_CATALOGUE or self._schedule_upcoming_asked:
            return
        # Module-side cache first: rows fetched by an earlier page life
        # (or by prewarm_discover at launch) fill in synchronously, so
        # the _build_schedule that called here draws them in the same
        # pass - zero network, zero wait.
        rows = (_cached_released_rows()
                if self.SCHEDULE_CATALOGUE == "released"
                else _cached_upcoming_calendar())
        if rows:
            self._schedule_upcoming_asked = True
            self._schedule_upcoming = list(rows)
            self._queue_schedule_covers(rows)
            return
        self._schedule_upcoming_asked = True
        lookup_pool.submit(self._schedule_upcoming_worker)

    def _schedule_upcoming_worker(self):
        """Must never raise - it runs on a shared pool worker, and an
        uncaught exception takes every queued lookup with it (the fetch
        helpers swallow everything, RateLimited included: the schedule
        keeps its saved rows, the honest answer and not an error
        dialog).

        Emits as soon as the calendar answers. The covers are *not*
        downloaded here any more: the old loop fetched every row's art
        serially before emitting anything, so the section sat on its
        saved rows until the last of 40 downloads - measured 73.2s with
        a cold cover cache against 0.5s for the calendar itself. Rows
        now draw with the blank tile immediately (the same placeholder
        Discover cards start on) and each missing cover arrives as its
        own pool job, swapped in place by _on_schedule_cover."""
        if self.SCHEDULE_CATALOGUE == "released":
            # The Read page: what just landed, newest first. The rows
            # are Discover-shaped ({title, poster, ...}), so a click can
            # go through _on_discover_pick unchanged.
            rows = _fetch_released_rows()
        else:
            rows = _fetch_upcoming_calendar()
        # An empty answer counts as a failure: both sources fail soft
        # to [] (rules/integrations.md), so no rows is far more likely
        # a refused request than a genuinely empty calendar - and
        # caching it as final is exactly the one-shot failure being
        # fixed here.
        try:
            self._discover_signals.schedule_upcoming.emit(rows, bool(rows))
        except RuntimeError:
            return  # the page was torn down under the fetch
        self._queue_schedule_covers(rows)

    def _on_schedule_upcoming(self, rows, ok):
        if not ok:
            # Retryable: the next Schedule build asks again, rather
            # than remembering one failed fetch for the page's life.
            self._schedule_upcoming_asked = False
            return
        self._schedule_upcoming = list(rows or [])
        # Only redraw the section the user is actually looking at.
        if self._active_tab == TAB_SCHEDULE:
            self._build_schedule()

    def _queue_schedule_covers(self, rows):
        """One pool job per missing cover - bounded by the pool's four
        workers, never a serial loop, and art already on disk was
        stamped by _stamp_cached_covers so it costs nothing here. Safe
        to call again across page lives: a landed cover writes
        cover_path onto the module-cached dict and drops out of this
        loop for good."""
        for row in rows or []:
            if row.get("cover_path") or not row.get("cover_url"):
                continue
            lookup_pool.submit(self._schedule_cover_worker, row)

    def _schedule_cover_worker(self, row):
        # Never raises - an exception here kills the pool's worker.
        try:
            path = images.download(row.get("cover_url") or "")
        except Exception:
            path = None
        if not path:
            return
        # Written onto the module-cached dict, so every later rebuild
        # draws it without asking; the signal only repaints the row
        # already on screen.
        row["cover_path"] = str(path)
        # Decoded here rather than in _on_schedule_cover, for the reason
        # measured in _fetch_discover_poster: the slot runs on the UI
        # thread, and a PIL decode plus a tile write there is 10ms a
        # cover with forty of them arriving at once.
        try:
            images._fitted(path, tuple(SCHEDULE_COVER_SIZE), images._stamp(path))
        except Exception:
            pass
        try:
            self._discover_signals.schedule_cover.emit(row, str(path))
        except RuntimeError:
            pass    # page torn down; the cache keeps the path either way

    def _on_schedule_cover(self, item, path):
        label = self._schedule_cover_labels.get(id(item))
        if label is None:
            return  # a rebuild replaced the section and read cover_path
        try:
            label.setPixmap(images.thumbnail_or_avatar(
                path, item.get("title") or "", SCHEDULE_COVER_SIZE))
        except RuntimeError:
            pass    # the label died with a rebuild mid-flight

    def _schedule_rows(self):
        """(saved rows, catalogue rows), each [(when, entry)].

        Two lists, not one sorted with a tiebreak: **every** saved row
        sits above **every** catalogue one (the owner's ask - the old
        `(when, not saved)` key only led within a shared clock slot),
        and _build_schedule draws a rule between the groups. Each list
        is soonest-first on its own; a "released" catalogue (the Read
        page) normally has no timestamps at all, so its `when` is None
        and the source's own order - newest chapter first - is kept as
        given. A released item that *genuinely* carries a forward `at`
        (an aware datetime - no source does today) keeps it, and it
        sorts ahead of the undated rows; nothing here invents one
        (rules/integrations.md). The "Releasing Soon" group that used
        to receive those rows is gone - see _build_schedule.

        A catalogue entry carries its source item under
        `_catalogue_item`, the same dict held in _schedule_upcoming for
        the page's life - which is what lets a click resolve it once
        and remember the answer across rebuilds (_on_schedule_open).
        """
        now = datetime.now(timezone.utc)
        saved_rows = []
        for entry in self.entries:
            if entry.get("type") not in self.ENTRY_TYPES:
                continue
            stored = entry.get("next_release")
            if not isinstance(stored, dict) or not stored.get("at"):
                continue
            try:
                when = datetime.fromisoformat(stored["at"])
            except (TypeError, ValueError):
                continue
            # A naive timestamp is read as UTC, exactly as
            # release_schedule._parse does - a hand-edited or older data
            # file may carry one, and comparing it to an aware `now`
            # would raise inside a slot.
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= now:
                continue
            saved_rows.append((when, entry))
        saved_rows.sort(key=lambda row: row[0])
        saved_titles = {(e.get("title") or "").strip().lower()
                        for _w, e in saved_rows}

        catalogue_rows = []
        released = self.SCHEDULE_CATALOGUE == "released"
        for item in getattr(self, "_schedule_upcoming", None) or []:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            # Title-matched against the saved rows, the same rule
            # _find_saved uses: the saved row carries the owner's
            # own progress and cover and must not be duplicated by
            # a catalogue row for the same show.
            if title.lower() in saved_titles:
                continue
            if released:
                when = item.get("at")
                if not isinstance(when, datetime):
                    when = None
                else:
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when <= now:
                        when = None     # already out: a released row
                row = {
                    "title": title,
                    "type": (item.get("type")
                             if item.get("type") in self.ENTRY_TYPES
                             else self.ENTRY_TYPES[0]),
                    "cover_path": item.get("cover_path") or None,
                    "_catalogue_item": item,
                }
                if when is not None:
                    release = {}
                    if item.get("chapter") is not None:
                        release["chapter"] = item.get("chapter")
                    if item.get("estimated"):
                        # The row keeps its "Estimated" tag - a
                        # projection stated as fact is the failure that
                        # flag exists to prevent.
                        release["estimated"] = True
                    if release:
                        row["next_release"] = release
                catalogue_rows.append((when, row))
                continue
            when = item.get("at")
            if not when or when <= now:
                continue
            catalogue_rows.append((when, {
                "title": title,
                "type": "Anime",
                "cover_path": item.get("cover_path") or None,
                "next_release": {"episode": item.get("episode") or 0,
                                 "season": 0},
                "_catalogue_item": item,
            }))
        if not released:
            catalogue_rows.sort(key=lambda row: row[0])
        return saved_rows, catalogue_rows

    @staticmethod
    def _schedule_group(when) -> str:
        """Which band a release falls in, in *local* time - the day the
        user would call it, not the UTC date it is stored as."""
        today = datetime.now().astimezone().date()
        day = when.astimezone().date()
        if day == today:
            return "Today"
        if day == today + timedelta(days=1):
            return "Tomorrow"
        if day <= today + timedelta(days=6):
            return "This Week"
        return "Later"

    def _refresh_schedule_countdowns(self):
        """Re-word every countdown on the Schedule section from the
        clock, without rebuilding the rows (a rebuild would put the list
        back to the top under the reader). Skipped while the section is
        off screen - the labels are rebuilt on arrival anyway."""
        if self._active_tab != TAB_SCHEDULE or not self._schedule_countdowns:
            return
        now = datetime.now(timezone.utc)
        for label, when in self._schedule_countdowns:
            try:
                label.setText(release_schedule.format_countdown(when - now))
            except RuntimeError:
                # The section rebuilt under the timer; the fresh build
                # refilled the list, and any stale pair dies with it.
                pass

    def _schedule_band_label(self, text):
        label = QLabel(text, objectName="Muted")
        label.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;"
                            f" font-weight: 700; padding-top: 6px;")
        return label

    def _build_schedule(self):
        _clear_layout(self._schedule_layout)
        self._schedule_countdowns = []
        self._schedule_cover_labels = {}
        self._request_schedule_upcoming()
        saved_rows, catalogue_rows = self._schedule_rows()
        if not saved_rows and not catalogue_rows:
            self._schedule_layout.addWidget(QLabel(
                "Nothing scheduled yet - schedules fill in as your Saved "
                "entries are looked up.", objectName="Muted"))
            self._schedule_layout.addStretch()
            return

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setSpacing(8)
        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        # The saved group first, whole, and named (the owner's ask: the
        # first group used to be implicit) - every saved row above every
        # catalogue one, with the day bands inside it. The rows are
        # already ascending, so the bands come out in order and a
        # heading is needed only where the band changes.
        body_layout.addWidget(QLabel("Saved", objectName="SectionTitle"))
        body_layout.addWidget(QLabel(
            "Upcoming releases from your Saved list", objectName="Muted"))
        current = None
        for when, entry in saved_rows:
            band = self._schedule_group(when)
            if band != current:
                current = band
                body_layout.addWidget(self._schedule_band_label(band))
            body_layout.addWidget(self._build_schedule_row(entry, when, True))
        if not saved_rows:
            # The heading still needs something under it, or the page
            # opens on an unexplained title.
            body_layout.addWidget(QLabel(
                "Nothing from your Saved list is scheduled yet.",
                objectName="Muted"))

        # The catalogue groups below the saved ones. Watch has one,
        # "Airing Soon". Read has one too, now: "Recently Released".
        #
        # **"Releasing Soon" is gone, at the owner's ask (22 August
        # 2026).** It could never hold anything: it took only rows
        # carrying a genuine forward date, and no reading source
        # publishes one because nobody announces scanlation dates
        # (rules/integrations.md). So it drew as a heading over a line
        # of explanation, every single time - a section whose whole
        # content was an apology for being empty. The expected dates on
        # his Saved titles above, extrapolated from each title's own
        # release history, remain the only honest forecast and are
        # unchanged.
        #
        # A dated row is still not dropped on the floor: if a source
        # ever does supply one it is sorted in with the rest rather
        # than vanishing with the heading that used to carry it.
        released = self.SCHEDULE_CATALOGUE == "released"
        if released:
            dated = sorted((row for row in catalogue_rows
                            if row[0] is not None), key=lambda row: row[0])
            undated = [row for row in catalogue_rows if row[0] is None]
            groups = [
                ("Recently Released",
                 # "newest first" is the only recency the released rows
                 # can honestly claim - MangaDex orders them by upload
                 # time but its row shape carries no timestamp to print.
                 "New chapters across the catalogue, newest first",
                 dated + undated, None),
            ]
        else:
            groups = [("Airing Soon", "Everything else airing this week",
                       catalogue_rows, None)]
        saved_titles = self._saved_titles()
        for group_title, subtitle, rows, empty_note in groups:
            if not rows and empty_note is None:
                continue
            # The visible line between "yours" and "the catalogue"
            # (the owner's ask): a 1px rule, then the group's own name.
            rule = QFrame()
            rule.setFixedHeight(1)
            rule.setStyleSheet(f"background: {theme.BORDER}; border: none;")
            body_layout.addSpacing(10)
            body_layout.addWidget(rule)
            body_layout.addWidget(QLabel(group_title,
                                         objectName="SectionTitle"))
            if not rows:
                # The empty note stands in for the subtitle - heading,
                # subtitle *and* explanation stacked three deep would
                # say the same thing twice.
                body_layout.addWidget(QLabel(empty_note, objectName="Muted"))
                continue
            body_layout.addWidget(QLabel(subtitle, objectName="Muted"))
            current = None
            for when, entry in rows:
                if when is not None:
                    band = self._schedule_group(when)
                    if band != current:
                        current = band
                        body_layout.addWidget(self._schedule_band_label(band))
                # The chip still keys off Saved itself: a saved title
                # with no cached schedule of its own lands in this
                # group, and it must not read like one never heard of.
                body_layout.addWidget(self._build_schedule_row(
                    entry, when,
                    (entry.get("title") or "").strip().lower() in saved_titles))
        self._schedule_layout.addWidget(scroll_area(body, ground=theme.PANEL_FILL), stretch=1)

    def _schedule_coming(self, entry) -> str:
        """What is coming, in the wording that medium uses."""
        stored = entry.get("next_release") or {}
        if entry.get("type") in MANGA_TYPES:
            try:
                return f"Ch {float(stored.get('chapter')):g}"
            except (TypeError, ValueError):
                return "Next chapter"
        try:
            episode = int(stored.get("episode"))
        except (TypeError, ValueError):
            return "Next episode"
        # format_episode_progress, not a format string of this tab's
        # own: it is what every other number on this page is written
        # with, and it already knows that a source with no season
        # numbering says "E05" rather than claiming season zero.
        return format_episode_progress(int(stored.get("season") or 0), episode) or "Next episode"

    def _build_schedule_row(self, entry, when, saved=True):
        card = Card(hoverable=True, matte=True)
        # Same lift as the featured card, same reason: SURFACE on this
        # page's SURFACE panel is invisible without the old border.
        card.setStyleSheet(
            f"QFrame#Card {{ background: {theme.SURFACE_HOVER}; }}"
            f"QFrame#Card:hover {{ background: {theme.SURFACE_ACTIVE}; }}")
        row = QHBoxLayout(card)
        row.setContentsMargins(10, 8, 14, 8)
        row.setSpacing(12)

        cover = QLabel()
        cover.setFixedSize(*SCHEDULE_COVER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(
            entry.get("cover_path"), entry.get("title") or "", SCHEDULE_COVER_SIZE))
        row.addWidget(cover)
        item = entry.get("_catalogue_item")
        if item is not None and not entry.get("cover_path"):
            # Art still in flight: remember the label so the download
            # landing swaps this one pixmap (_on_schedule_cover) rather
            # than waiting for the next rebuild.
            self._schedule_cover_labels[id(item)] = cover

        column = QVBoxLayout()
        column.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(QLabel(entry.get("title") or "",
                                   objectName="CardTitle"))
        if saved:
            # Which of these are the owner's own - the schedule
            # lists the whole catalogue now, and without this a
            # tracked show reads exactly like one never heard of.
            chip = QLabel("Saved")
            chip.setStyleSheet(
                f"color: {theme.ON_ACCENT}; background: {theme.ACCENT};"
                f" border-radius: {theme.RADIUS_SM}px; padding: 1px 8px;"
                f" font-size: 8pt; font-weight: 700;")
            title_row.addWidget(chip, alignment=Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        column.addLayout(title_row)
        column.addWidget(QLabel(self._schedule_coming(entry), objectName="CardMeta"))
        row.addLayout(column, stretch=1)

        right = QVBoxLayout()
        right.setSpacing(2)
        # **`when` is None for a "released" row** - the Read page's
        # catalogue group is chapters that already landed, and MangaDex's
        # browse shape carries no upload timestamp to print. It gets a
        # word instead of a clock, and no countdown: counting down to
        # something that has already happened is nonsense, and the old
        # code would have called .astimezone() on None to find out.
        if when is None:
            out = QLabel("Out now", objectName="CardMeta")
            out.setAlignment(Qt.AlignmentFlag.AlignRight)
            out.setStyleSheet(f"color: {theme.SUCCESS}; font-weight: 700;"
                              f" font-size: 9pt; background: transparent;")
            right.addWidget(out)
        else:
            slot = QLabel(release_schedule.format_slot(when.astimezone()),
                          objectName="CardMeta")
            slot.setAlignment(Qt.AlignmentFlag.AlignRight)
            right.addWidget(slot)
            countdown = QLabel(release_schedule.format_countdown(
                when - datetime.now(timezone.utc)))
            countdown.setAlignment(Qt.AlignmentFlag.AlignRight)
            countdown.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 700;"
                                    f" font-size: 9pt; background: transparent;")
            right.addWidget(countdown)
            # Registered for the 30s tick, so the number keeps counting
            # while the section sits on screen.
            self._schedule_countdowns.append((countdown, when))
        if (entry.get("next_release") or {}).get("estimated"):
            # Said out loud, not implied: a manga date is extrapolated
            # from release history (see mangadex._predict), and a
            # projection stated as fact is the failure that flag exists
            # to prevent.
            tag = QLabel("Estimated", objectName="Muted")
            tag.setAlignment(Qt.AlignmentFlag.AlignRight)
            tag.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;"
                              f" font-size: 8pt;")
            right.addWidget(tag)
        row.addLayout(right)

        card.clicked.connect(lambda en=entry: self._open_schedule_entry(en))
        return card

    def _open_schedule_entry(self, entry):
        """A schedule row opens the entry's details page - the same
        target the card's body has on Saved.

        **A catalogue row has to be resolved first, and that is the
        whole of a reported bug.** Rows in the lower group are built
        from a calendar entry carrying a title, a cover and an episode
        number - no id of any kind. Handed to the details page as-is,
        that produced "This entry has no matched title, so there is no
        episode list to show" over an empty list (the owner's
        screenshot, on Slime season 4), which is why the schedule "only
        opens correctly when saved".

        A saved row, or one already resolved by an earlier click, opens
        immediately. Anything else is looked up the way Discover looks
        a title up, off the UI thread, and opens when the answer lands.

        Soft, like every other route into an overlay here: an import
        error must leave the row inert rather than take a slot down with
        it."""
        item = entry.get("_catalogue_item")
        if item is None or entry.get("id") or entry.get("imdb_id"):
            self._show_details_for(entry)
            return
        # A reading catalogue row already *is* a Discover row - it came
        # from the same discover.discover_reading_latest shape, carrying
        # its site url - so it needs no network at all.
        if entry.get("type") in MANGA_TYPES:
            self._show_details_for(discover_entry(item, entry["type"]))
            return
        cached = item.get("_resolved")
        if cached:
            self._show_details_for(dict(cached))
            return
        title = (entry.get("title") or "").strip()
        if not title or self._schedule_opening:
            return          # one lookup at a time; a second click waits
        self._schedule_opening = title
        self._schedule_open_toast = show_toast(
            self, f"Opening '{title}'...", duration_ms=None)
        lookup_pool.submit(self._schedule_open_worker, item, title,
                           entry.get("type") or "Anime")

    def _show_details_for(self, entry):
        window = _top_window(self)
        if window is None:
            return
        try:
            from windows import details
            page = details.open_details(window, entry)
        except Exception:
            logs.exception("opening details from the schedule failed")
            return
        _wire_overlay_refresh(page, self, entry)

    def _schedule_open_worker(self, item, title, entry_type):
        """Find the catalogue row for `title` so its details page has an
        id to list episodes from.

        Must never raise - it runs on a shared pool worker, and an
        uncaught exception there takes every queued lookup with it.

        Asked of the same catalog Discover asks, and matched with the
        same strictness the rest of this app uses: a schedule row that
        opened *some other show's* episode list would be worse than one
        that opened nothing, because nothing about the page would say it
        was the wrong show."""
        resolved = {}
        try:
            rows = []
            for kind in ("anime", "series"):
                rows = (discover.discover_video(kind, query=title, limit=6)
                        if discover is not None else []) or []
                if rows:
                    break
            best, score = None, 0.0
            for row in rows:
                value = title_match.similarity(title, row.get("title") or "")
                if value > score:
                    best, score = row, value
            # 0.8, the same bar anilist's cover lookup uses. Below it the
            # honest answer is "open it unresolved and say so" rather
            # than a confident wrong match.
            if best is not None and score >= 0.8:
                resolved = discover_entry(best, entry_type)
                resolved["imdb_id"] = best.get("imdb_id") or ""
        except Exception:
            resolved = {}
        try:
            self._discover_signals.schedule_open.emit(item, resolved)
        except RuntimeError:
            pass        # the page went away under the lookup

    def _on_schedule_open(self, item, resolved):
        """The lookup landed: open what it found.

        The answer is remembered on the catalogue item itself, which
        lives for the page's life in `_schedule_upcoming`, so clicking
        the same row again costs nothing.

        No run number here either (see schedule_upcoming): the only
        thing in flight is the one click this page made, and
        `_schedule_opening` is what serialises it."""
        self._schedule_opening = None
        toast, self._schedule_open_toast = self._schedule_open_toast, None
        if not resolved:
            finish_toast(toast, self,
                         "Couldn't match that title in the catalog")
            return
        if toast is not None:
            try:
                toast.close()
            except RuntimeError:
                pass
        try:
            item["_resolved"] = dict(resolved)
        except Exception:
            pass        # not worth failing the open over
        self._show_details_for(dict(resolved))

    # ------------------------------------------------------------------
    # History: what has actually been opened, saved or not.

    def _history_rows(self) -> list:
        """This page's history rows, newest first. Narrowed to the types
        this page owns, so Watch never lists manga and Read never lists
        episodes."""
        return history.recent(self.ENTRY_TYPES)

    def _history_cover_worker(self, key, url):
        # Never raises - an exception here kills the pool's worker.
        try:
            path = images.download(url)
        except Exception:
            path = None
        if path and key:
            self._discover_signals.history_cover.emit(str(key), str(path))

    def _on_history_cover(self, key, path):
        pair = self._history_covers.get(key)
        if pair is None:
            return
        label, title = pair
        try:
            label.setPixmap(images.thumbnail_or_avatar(path, title,
                                                       SCHEDULE_COVER_SIZE))
        except RuntimeError:
            pass        # the section rebuilt under the download

    def _build_history(self):
        _clear_layout(self._history_layout)
        # Emptied with the rows it names - nothing in it outlives the
        # rebuild that filled it (.claude/rules/ui.md).
        self._history_covers = {}
        rows = self._history_rows()
        if not rows:
            self._history_layout.addWidget(QLabel(
                "Nothing here yet. Anything you play or read shows up "
                "here - including titles you have not saved.",
                objectName="Muted"))
            self._history_layout.addStretch(1)
            return

        header = QHBoxLayout()
        # Led by a left-to-right mark. Without it Qt shapes the count
        # with the *paragraph's* resolved direction, and on a page whose
        # rows are Arabic titles that turns "3" into the Arabic-Indic
        # "٣" - the owner's screenshot, asking what the character before
        # "titles" was. The same LRM trick details._row_card uses.
        header.addWidget(QLabel(
            f"{_LTR_MARK}{len(rows)} title{'s' if len(rows) != 1 else ''}",
            objectName="Muted"))
        header.addStretch(1)
        clear_btn = QPushButton("Clear History")
        use_hover_cursor(clear_btn)
        clear_btn.clicked.connect(self._clear_history)
        header.addWidget(clear_btn)
        self._history_layout.addLayout(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setSpacing(8)
        body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        for row in rows:
            body_layout.addWidget(self._build_history_row(row))
        self._history_layout.addWidget(scroll_area(body, ground=theme.PANEL_FILL), stretch=1)

    def _build_history_row(self, row):
        card = Card(hoverable=True, matte=True)
        # Same lift as the schedule rows, same reason: SURFACE on this
        # page's SURFACE panel is invisible without the old border.
        card.setStyleSheet(
            f"QFrame#Card {{ background: {theme.SURFACE_HOVER}; }}"
            f"QFrame#Card:hover {{ background: {theme.SURFACE_ACTIVE}; }}")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(10, 8, 14, 8)
        layout.setSpacing(12)

        cover = QLabel()
        cover.setFixedSize(*SCHEDULE_COVER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(
            row.get("cover_path"), row.get("title") or "", SCHEDULE_COVER_SIZE))
        # A title that was never saved has no downloaded cover_path -
        # only the remote cover_url its Discover card was drawn from -
        # so History showed art for saved titles and a letter avatar for
        # everything else (the owner's screenshot: Lookism with a cover
        # in Discover and none here). Fetch it the same way the Discover
        # rows do; images.download caches, so this is one request ever.
        if not row.get("cover_path") and row.get("cover_url"):
            self._history_covers[row.get("key")] = (cover, row.get("title") or "")
            lookup_pool.submit(self._history_cover_worker, row.get("key"),
                               row.get("cover_url"))
        layout.addWidget(cover)

        column = QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(QLabel(row.get("title") or "", objectName="CardTitle"))
        ticks = len(row.get("watched") or ())
        reading = row.get("type") in MANGA_TYPES
        bits = [row.get("progress") or "",
                (f"{ticks} {'chapter' if reading else 'episode'}"
                 f"{'s' if ticks != 1 else ''} marked") if ticks else ""]
        column.addWidget(QLabel("  ·  ".join(b for b in bits if b),
                                objectName="CardMeta"))
        layout.addLayout(column, stretch=1)

        right = QVBoxLayout()
        right.setSpacing(2)
        when = QLabel(_history_when(row.get("last_opened")), objectName="CardMeta")
        when.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(when)
        # Says which store this title lives in. A history row for
        # something unsaved is the case this whole section exists for,
        # so it is labelled rather than left to look identical to a
        # saved one.
        saved = self._find_saved(row.get("title")) is not None
        tag = QLabel("In Saved" if saved else "Not saved")
        tag.setAlignment(Qt.AlignmentFlag.AlignRight)
        tag.setStyleSheet(
            f"color: {theme.ACCENT if saved else theme.TEXT_DIM};"
            f" background: transparent; font-size: 8pt; font-weight: 700;")
        right.addWidget(tag)
        layout.addLayout(right)

        card.clicked.connect(lambda r=row: self._open_history_row(r))
        card.rightClicked.connect(
            lambda event, r=row: self._history_menu(event, r))
        return card

    def _history_entry(self, row):
        """A history row as an entry the details page can hold: the
        saved entry when there is one (so progress marks and Save state
        are real), else a transient record built from what History
        stored."""
        found = self._find_saved(row.get("title"))
        if found is not None:
            return found
        entry_type = row.get("type") or self.ENTRY_TYPES[0]
        entry = discover_entry({"title": row.get("title"),
                                "poster": row.get("cover_url"),
                                "imdb_id": row.get("imdb_id")}, entry_type)
        # The site binding a reading title was opened with, so the
        # chapter list comes straight back rather than asking again.
        entry["url"] = row.get("url") or ""
        entry["site_id"] = row.get("site_id")
        entry["cover_path"] = row.get("cover_path")
        return entry

    def _open_history_row(self, row):
        window = _top_window(self)
        if window is None:
            return
        entry = self._history_entry(row)
        try:
            from windows import details
            page = details.open_details(window, entry)
        except Exception:
            logs.exception("opening details from history failed")
            return
        _wire_overlay_refresh(page, self, entry)

    def _history_menu(self, event, row):
        menu = _StayOpenMenu(self)
        remove = menu.addAction("Remove from History")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is remove:
            if history.forget(row.get("key")):
                show_toast(self, "Removed from History")
                self._build_history()

    def _clear_history(self):
        if not confirm(self, "Clear History",
                       "Forget every title in History? The episode and "
                       "chapter marks stored here go with them. Saved "
                       "entries and their progress are untouched.",
                       yes_text="Clear", danger=True):
            return
        history.clear()
        self._build_history()
        show_toast(self, "History Cleared")

    # ------------------------------------------------------------------
    def _open_form(self, edit=False, entry=None, initial_title="", default_type=None):
        """`initial_title`/`default_type` are Discover's: a picked card
        opens this form already searching for that title, on the type of
        the row it came from (Movies & Series has one row of each)."""
        if edit and entry is None:
            return
        EntryForm(self, entry if edit else None,
                  default_type=default_type or self.ENTRY_TYPES[0],
                  type_options=self.TYPE_OPTIONS,
                  on_save=self._on_form_save, initial_title=initial_title)

    def _on_form_save(self, entry, is_new):
        entry["updated_at"] = storage.now_iso()
        if is_new:
            entry["added_at"] = entry["updated_at"]
            self.entries.append(entry)
        # A new entry has no schedule yet, and an edited one may have had
        # the title it's looked up by changed out from under the old one -
        # either way the stored lookup is worth redoing, so clear what
        # rate-limits it before saving. The cached MangaDex id goes with
        # it: it was resolved from the *old* title, and reusing it would
        # keep answering for the series the user just corrected away from.
        entry.pop("next_release_checked_at", None)
        entry.pop("mangadex_id", None)
        # Same reasoning for the Video Website page lookup: a re-saved
        # entry deserves a fresh attempt, and one whose title just
        # changed *needs* one - the recorded miss was for the old title.
        if not entry.get("url"):
            entry.pop("site_url_checked_at", None)
        self._save_entries()
        self._refresh_grid()
        self._refresh_schedules()
        # A title added from Discover is still on screen behind this
        # form; without this it would go on offering to add what it has
        # just added.
        self._sync_discover_saved()


# No AnimePage any more: Anime merged into the Movies & Series page (the
# owner's ask - one watch page under the camera glyph). Its rows moved
# from tracker.json into series.json once, at startup (main.py's
# _merge_anime_into_series), so tracker.json is the reading file now.


class MangaPage(TrackerPage):
    DATA_FILE = "tracker.json"
    ENTRY_TYPES = MANGA_TYPES
    # "Read" (the owner's ask) - the sidebar's verb, like "Watch".
    TITLE = "Read"
    # Reading's Schedule shows more than the owner's own rows now (their
    # ask), but what it can honestly add is **chapters that already
    # landed**, not ones to come: nobody publishes scanlation dates, so
    # there is no forward calendar to read. The saved rows above it keep
    # their extrapolated dates and their "Estimated" tag.
    SCHEDULE_CATALOGUE = "released"
    TYPE_OPTIONS = list(MANGA_TYPES)
    PROGRESS_COLUMNS = ["Last Released Chapter"]
    SUPPORTS_PROGRESS_SYNC = False
    # "reading" is the one kind that goes to discover_reading rather than
    # discover_video; the entry type is Manga, which is the first of the
    # three reading flavours and the one the form opens on.
    DISCOVER_ROWS = (("reading", "Manga", "Manga"),
                     ("reading_latest", "Latest", "Manga"))
    # "Popular <label>" is the default wording and is wrong for
    # both of these, so both name themselves.
    DISCOVER_HEADINGS = {"reading": "Popular Now",
                         "reading_latest": "Latest"}
    DISCOVER_SUBHEADINGS = {"reading_latest":
                            "Where the newest chapters landed"}
    # Latest is a browse, not an answer to a query - a typed search
    # would return the same titles as the row above it.
    DISCOVER_BROWSE_ONLY = ("reading_latest",)
    CATEGORY_SECTIONS = READ_CATEGORIES
    SECTIONS = _sections_with(READ_CATEGORIES)
    # The same rows in the three blocks the rail draws them in,
    # with a row of air between each pair - see _section_groups.
    SECTION_GROUPS = _section_groups(READ_CATEGORIES)


class SeriesPage(TrackerPage):
    DATA_FILE = "series.json"
    # Anime leads: this owner's list is anime-heavy, so it fronts each
    # status section and is what the Add form opens on.
    ENTRY_TYPES = ("Anime", "Series", "Movie")
    SPLIT_SECTIONS_BY_TYPE = True
    # "Watch" (the owner's ask) - the sidebar's verb; the heading itself
    # is rewritten to the showing section's name by _set_tab anyway.
    TITLE = "Watch"
    TYPE_OPTIONS = ["Anime", "Series", "Movie"]
    # The one page with a real *forward* schedule to show beyond its
    # own saved rows: AniList publishes an airing calendar.
    SCHEDULE_CATALOGUE = "upcoming"
    # One row per kind. Anime and Series are both Cinemeta's series
    # catalog underneath (discover.py's genre chain does the splitting);
    # picking from a row opens the form on that row's type - the same
    # split _search_catalogs makes in the Add form.
    DISCOVER_ROWS = (("anime", "Anime", "Anime"),
                     ("series", "Series", "Series"),
                     ("movie", "Movies", "Movie"))
    CATEGORY_SECTIONS = WATCH_CATEGORIES
    SECTIONS = _sections_with(WATCH_CATEGORIES)
    # The same rows in the three blocks the rail draws them in,
    # with a row of air between each pair - see _section_groups.
    SECTION_GROUPS = _section_groups(WATCH_CATEGORIES)


class _SearchSignals(QObject):
    results = Signal(str, list, int)  # provider ("stremio"/"manga_sites"), results, search sequence #
    cover_ready = Signal(str, object)
    manga_details_resolved = Signal(str, str, float)  # page url, cover url, latest chapter
    latest_episode_resolved = Signal(str, int, int)  # identity (stremio url), latest available season/episode
    video_url_resolved = Signal(str, str, str)  # identity (site id + title), site name, resolved page url ("" = none)


class EntryForm(QDialog):
    def __init__(self, parent, entry, default_type, type_options, on_save,
                 initial_title=""):
        """`initial_title` opens a *new* entry already searching for that
        title - what the Discover tab hands over when a card is picked.
        It goes through the same path a keystroke does (see the end of
        this method), so the cover, the link and the id all come from the
        existing matching code rather than from anything Discover knows."""
        super().__init__(parent)
        self.on_save = on_save
        self.entry = entry
        self.is_new = entry is None
        self.type_options = type_options

        self.selected_cover_url = entry.get("cover_url") if entry else None
        self.selected_cover_path = entry.get("cover_path") if entry else None
        # The Cinemeta id to sync progress against later, independent of
        # url (which for an Anime entry set to a Video Website other than
        # the built-in "Stremio" one holds that site's page, not a
        # stremio:// link) - see _entry_imdb_id/_save.
        self.selected_imdb_id = entry.get("imdb_id") if entry else None
        # Which Video Website this entry is pinned to, if any. There is no
        # dropdown for it any more - video plays inside Atomic, so "where
        # does this open" stopped being a question the user is asked - but
        # the saved value is kept exactly as it was and written back
        # unchanged on save: it is what tells the player a Netflix or
        # Crunchyroll entry is DRM and unplayable (anime_sites.
        # streaming_provider), and dropping it here would be a data
        # migration disguised as a UI change.
        self._saved_site_id = entry.get("site_id") if entry else None
        # Same, for the reading progress the reader writes. Nothing in
        # this dialog sets it any more; it must survive an edit of the
        # title or status untouched.
        self._saved_watched_chapter = entry.get("last_watched_chapter") if entry else None
        self._search_results = {}
        self._search_seq = 0
        # Whether the last search spanned more than one type, which is
        # what decides if a suggestion has to name its type to be told
        # apart (see _label_for_result).
        self._searched_several_types = False
        self._status_parts = {}  # "cover"/"progress" -> current message, composed onto status_label
        self._pending_episode_identity = None  # tracks staleness for the async episode-progress lookup
        self._pending_video_identity = None  # same, for the Video Website page-url lookup
        # Which identity that lookup last *reported* on. Pending alone
        # can't answer "is one still in flight" - it stays set after a
        # result lands - and Save has to know (see _save).
        self._resolved_video_identity = None
        self._save_waiting_on_video = False  # a Save is queued behind the lookup
        self._video_wait_used = False  # ...and Save only ever waits the once
        self._latest_available = entry.get("latest_available", "") if entry else ""
        # Whether the stored progress is *confirmed* (played here, or
        # fetched from a connected Stremio account) rather than the
        # unconfirmed "latest episode out" guess - only verified progress
        # shows on the card. Carried through this dialog unchanged: with
        # the spinners gone there is nothing here that could confirm or
        # unconfirm it, and saving an unrelated field must not claim to.
        self._progress_verified = bool(entry and entry.get("progress_verified"))
        # Whether a suggestion has been applied yet (by click or by exact-
        # title auto-match - see _apply_search_results). Starts True for
        # an existing entry that's already resolved, so re-opening it to
        # edit unrelated fields doesn't get its cover/link silently
        # swapped out by a coincidental search hit.
        self._suggestion_applied = bool(entry and (entry.get("cover_url") or entry.get("url")))
        self._signals = _SearchSignals()
        self._signals.results.connect(self._apply_search_results)
        self._signals.cover_ready.connect(self._on_cover_downloaded)
        self._signals.manga_details_resolved.connect(self._on_manga_details_resolved)
        self._signals.latest_episode_resolved.connect(self._on_latest_episode_resolved)
        self._signals.video_url_resolved.connect(self._on_video_url_resolved)

        self.setWindowTitle("Edit Entry" if entry else "Add Entry")
        # 620 tall while it held the last-watched spinners and the Video
        # Website dropdown; without them an Anime entry left a ~240px void
        # above the buttons. Measured with both status lines carrying
        # their longest wrapped message: the layout needs 382px on Anime
        # and 506px on a reading type (which keeps Last Released Chapter
        # and the Reading Website dropdown), and the Type dropdown can
        # turn one into the other while the dialog is open - so it is
        # sized for the taller case, not the current one. 562, up from
        # the framed version's 530: the frameless panel carries its own
        # heading now, where the native title bar used to.
        self.setFixedSize(460, 562)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._trigger_search)

        form = QVBoxLayout(self)
        form.setContentsMargins(24, 20, 24, 16)
        form.setSpacing(4)

        top = QHBoxLayout()
        fields = QVBoxLayout()
        fields.setSpacing(4)

        self.title_label = QLabel()
        fields.addWidget(self.title_label)
        self.title_combo = QComboBox()
        self.title_combo.setEditable(True)
        self.title_combo.setCurrentText(entry["title"] if entry else "")
        self.title_combo.lineEdit().textEdited.connect(self._on_title_edited)
        self.title_combo.textActivated.connect(self._on_suggestion_selected)
        fields.addWidget(self.title_combo)
        # Directly under the box it reports on. It used to share the one
        # status line at the bottom of the form, which put "Searching..."
        # about as far from what you were typing as this dialog allows.
        self.search_status_label = QLabel("", objectName="Muted")
        self.search_status_label.setWordWrap(True)
        fields.addWidget(self.search_status_label)

        # Type dropdown only makes sense when a page's data file mixes
        # more than one type (Anime/Manga share tracker.json); Series has
        # nothing to switch to, so it's set silently instead.
        self.type_box = QComboBox()
        self.type_box.addItems(type_options)
        self.type_box.setCurrentText(entry["type"] if entry else default_type)
        self.type_box.currentTextChanged.connect(self._on_type_changed)
        if len(type_options) > 1:
            fields.addSpacing(8)
            fields.addWidget(QLabel("Type"))
            fields.addWidget(self.type_box)

        top.addLayout(fields, stretch=1)

        preview_col = QVBoxLayout()
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(*PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_col.addWidget(self.preview_label)
        preview_col.addStretch()
        top.addLayout(preview_col)
        self._refresh_preview()

        form.addLayout(top)

        form.addSpacing(8)
        form.addWidget(QLabel("Status"))
        self.status_box = QComboBox()
        self._populate_status_options(entry["status"] if entry else None)
        form.addWidget(self.status_box)

        # Kept as-is through this dialog: with the last-watched spinners
        # gone, nothing here sets it. It is written by record_progress
        # from what the player actually played, and by the Stremio sync.
        self._original_progress = entry.get("progress", "") if entry else ""

        form.addSpacing(8)
        # Per-entry, because whether a last-watched number belongs on the
        # card depends on the entry and not the page: a film has none.
        # "&&" and not "&": QCheckBox reads a single one as a mnemonic
        # marker and swallows it, which is what drew "Movies  Series".
        self.show_watched_check = QCheckBox("Show Last Watched Season && Episode")
        # On by default now, where it used to be off for a new entry. That
        # default guarded against the form's own auto-filled "latest
        # episode out" being revealed as if it were how far you had
        # watched - nothing seeds progress in here any more, and the card
        # shows a video number only once something has verified it
        # (_progress_display), so defaulting off would only have hidden
        # real progress the moment it was recorded.
        self.show_watched_check.setChecked(shows_last_watched(entry) if entry else True)
        self.show_watched_check.toggled.connect(self._update_progress_visibility)
        form.addWidget(self.show_watched_check)

        # No "Correct Last Watched/Read" row here any more. It was a
        # typed season/episode pair that wrote progress in either
        # direction, and the owner asked for it gone from Add/Edit: the
        # spinners read as a way to *count* through episodes in a form
        # that otherwise never touches progress. The capability itself
        # stays - `correct_progress()` and `_write_progress(...,
        # forward_only=False)` are untouched, and are what a "mark as
        # watched / unwatched" control hangs off.
        self.chapter_row = QWidget()
        chapter_layout = QHBoxLayout(self.chapter_row)
        chapter_layout.setContentsMargins(0, 0, 0, 0)
        chapter_layout.setSpacing(14)

        available_col = QVBoxLayout()
        available_col.setSpacing(2)
        available_col.addWidget(QLabel("Last Released Chapter"))
        self.chapter_spin = QDoubleSpinBox()
        self.chapter_spin.setRange(0, 99999)
        self.chapter_spin.setDecimals(1)
        self.chapter_spin.setValue(parse_chapter_progress(self._original_progress))
        # Read-only: this is the reading site's latest release, never
        # anything you set. Named "Last Chapter" and editable, it read as
        # "the chapter I'm on", so a hand-typed number sat in the one
        # field the site's next lookup overwrites - the number you meant
        # belongs in Last Watched Chapter beside it. The arrows go too, or
        # a spinner that ignores clicks still invites them.
        self.chapter_spin.setReadOnly(True)
        self.chapter_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        available_col.addWidget(self.chapter_spin)
        chapter_layout.addLayout(available_col)
        chapter_layout.addStretch()

        form.addWidget(self.chapter_row)

        # No Last Watched Chapter / Season / Episode boxes any more, and
        # no hint naming Stremio as the only thing that fills them in.
        # Both are recorded from what you actually open - the reader
        # writes the chapter, the player the episode, through
        # record_progress - so a box to type the same number into was a
        # second answer that could disagree with the first.

        # Never shown (see _update_url_and_site_visibility) - every type
        # this form offers derives its open target rather than having one
        # typed. It stays as the form's internal holder for the matched
        # link: a manga page url, a stremio:// deep link, or the page a
        # pinned Video Website resolved to.
        self.url_row = QWidget()
        url_layout = QVBoxLayout(self.url_row)
        url_layout.setContentsMargins(0, 8, 0, 0)
        url_layout.setSpacing(4)
        self.url_label = QLabel()
        url_layout.addWidget(self.url_label)
        self.url_edit = QLineEdit(entry.get("url", "") if entry else "")
        url_layout.addWidget(self.url_edit)
        form.addWidget(self.url_row)

        # Reading only now: which of the Settings-configured reading sites
        # this entry opens to. Anime/Series/Movie used to have the same
        # dropdown offering Stremio, Netflix and Crunchyroll - video plays
        # inside Atomic, so there is nothing left to choose between; an
        # entry already pinned to one keeps that pinning (see
        # _saved_site_id), it is just no longer presented as a choice.
        self.site_row = QWidget()
        site_layout = QVBoxLayout(self.site_row)
        site_layout.setContentsMargins(0, 8, 0, 0)
        site_layout.setSpacing(4)
        self.site_label = QLabel()
        site_layout.addWidget(self.site_label)
        self.site_box = QComboBox()
        self._populate_site_options(entry.get("site_id") if entry else None)
        site_layout.addWidget(self.site_box)
        form.addWidget(self.site_row)

        self._update_url_and_site_visibility()
        self._update_progress_visibility()

        self.status_label = QLabel("", objectName="Muted")
        form.addWidget(self.status_label)

        form.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.save_btn = QPushButton("Save", objectName="Accent")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)
        form.addLayout(btn_row)

        self._update_labels()
        if initial_title and self.is_new:
            self.title_combo.setCurrentText(initial_title)
            # _on_title_edited by hand, because setCurrentText does not
            # fire textEdited - that signal is user input only. Calling
            # it rather than _trigger_search directly is the point: it is
            # the whole keystroke path, debounce included, so a prefilled
            # title searches exactly as a typed one does. The timer fires
            # inside the exec() below.
            self._on_title_edited(initial_title)
            self._refresh_preview()
        frameless_dialog(self, title=self.windowTitle())
        self.exec()

    # ------------------------------------------------------------------
    def _set_status_part(self, key, text):
        """The cover download and the progress auto-fill happen in
        parallel background threads and each has something to report -
        compose their messages instead of one blindly overwriting the
        other's (e.g. "Loading cover..." stomping the "not your progress"
        hint, or vice versa)."""
        self._status_parts[key] = text
        self.status_label.setText(" · ".join(p for p in self._status_parts.values() if p))

    def _provider(self):
        return SEARCH_PROVIDER_BY_TYPE.get(self.type_box.currentText(), "manga_sites")

    def _update_labels(self):
        if self._provider() == "stremio":
            self.title_label.setText("Title (type to search)")
            self.url_label.setText("Stremio link (opens Stremio)")
        else:
            self.title_label.setText("Title (type to search)")
        # Only reading has a site to pick now, so the label no longer has
        # two cases (see the site_row comment in __init__).
        self.site_label.setText("Reading Website (opens directly)")

    def _status_options(self):
        return STATUSES_BY_TYPE.get(self.type_box.currentText(), STATUSES_BY_TYPE["Anime"])

    def _populate_status_options(self, current_status=None):
        options = self._status_options()
        self.status_box.blockSignals(True)
        self.status_box.clear()
        self.status_box.addItems(options)
        self.status_box.setCurrentText(current_status if current_status in options else options[0])
        self.status_box.blockSignals(False)

    def _on_type_changed(self, _text):
        # Deliberately doesn't touch selected_cover_url/path - switching
        # e.g. Manga -> Manhwa (or even Anime -> Manga) shouldn't throw
        # away a cover the user already matched. Only a title edit or a
        # fresh suggestion pick invalidates the current cover.
        self._update_labels()
        self._populate_status_options(self.status_box.currentText())
        self._populate_site_options(self.site_box.currentData())
        self._update_url_and_site_visibility()
        self._update_progress_visibility()
        self._trigger_search()

    def _update_url_and_site_visibility(self):
        # url_edit is internal for every type (see its comment in
        # __init__), and the site dropdown is reading-only now.
        self.url_row.setVisible(False)
        self.site_row.setVisible(self.type_box.currentText() in MANGA_TYPES)

    def _update_progress_visibility(self, *_args):
        current_type = self.type_box.currentText()
        is_manga = current_type in MANGA_TYPES
        # The tick goes for a film, rather than being left on screen
        # controlling nothing. It is the only progress control left here:
        # it decides whether the card shows the number, never what the
        # number is.
        self.show_watched_check.setVisible(tracks_progress(current_type))
        self.show_watched_check.setText(
            "Show Last Watched Chapter" if is_manga
            else "Show Last Watched Season && Episode")
        # Last Released Chapter - the reading site's own number, not
        # yours, and nothing to do with the tick above it.
        self.chapter_row.setVisible(is_manga)

    def _refresh_preview(self):
        title = self.title_combo.currentText() or "?"
        self.preview_label.setPixmap(images.thumbnail_or_avatar(self.selected_cover_path, title, PREVIEW_SIZE))

    def _populate_site_options(self, current_site_id=None):
        """Reading sites only. Video types leave the box empty - it isn't
        on screen for them (see _update_url_and_site_visibility), and
        filling it would put a stray currentData() in reach of _save,
        which is exactly how a pinned site would get reassigned."""
        self.site_box.blockSignals(True)
        self.site_box.clear()
        if self.type_box.currentText() in MANGA_TYPES:
            self.site_box.addItem("— None —", None)
            for site in manga_sites.list_sites():
                self.site_box.addItem(site["name"], site["id"])
        if current_site_id:
            idx = self.site_box.findData(current_site_id)
            if idx >= 0:
                self.site_box.setCurrentIndex(idx)
        self.site_box.blockSignals(False)

    def _on_title_edited(self, _text):
        self.selected_cover_url = None
        self.selected_cover_path = None
        self.selected_imdb_id = None
        self._suggestion_applied = False
        self._search_timer.start(SEARCH_DEBOUNCE_MS)

    def _trigger_search(self):
        text = self.title_combo.currentText().strip()
        if len(text) < 2:
            return
        self._search_seq += 1
        seq = self._search_seq
        provider = self._provider()
        self.search_status_label.setText("Searching...")
        # Catalogs are worked out here, on the UI thread. The worker used
        # to read the Type dropdown itself, which is a widget touched from
        # a background thread (.claude/rules/integrations.md).
        catalogs = self._search_catalogs()
        self._searched_several_types = len(catalogs) > 1
        # Not a bare thread, for the same reason the Video Website lookup
        # below isn't one: this fires per debounce pause, so typing faster
        # than a search answers used to run several concurrently with
        # nothing capping them - and a dual-catalog video search
        # (_search_catalogs) is two Cinemeta requests each, not one.
        # submit_latest drops a superseded search before it ever runs; the
        # seq check in _apply_search_results stays as the second line of
        # defence for the one that was already running.
        lookup_pool.submit_latest("entry-search", self._search_worker,
                                  provider, text, seq, catalogs)
        # An entry already pinned to a Video Website still re-resolves its
        # page when the title is edited - the site is no longer choosable,
        # but the pinning it already has must not rot into a search-page
        # link. Off `_saved_site_id`, since there is no dropdown to read.
        if self.type_box.currentText() in VIDEO_TYPES and self._saved_site_id is not None:
            self._start_video_site_resolution(self._saved_site_id, text)

    def _search_catalogs(self):
        """(entry type, Cinemeta catalog) pairs this search should ask.

        A page offering both Series and Movie asks both. Films live in
        their own Cinemeta catalog, so typing a film's name with Type on
        Series - which is what the form opens on - returned only series
        and nothing said why: measured, "Inception" gave 3 series matches
        and not the film. Picking a film's suggestion sets Type to Movie
        with it (see _on_suggestion_selected), so the entry doesn't save
        as a Series with episode tracking it has no use for."""
        current = self.type_box.currentText()
        if current not in VIDEO_TYPES:
            return []
        types = [t for t in self.type_options if t in VIDEO_TYPES]
        if current not in types:
            types = [current]
        # One request per *catalog*, not per type: Anime and Series are
        # both Cinemeta's series catalog now that they share a form, and
        # asking it twice would return every show twice under two
        # labels. The current Type claims the shared catalog - a form on
        # Anime tags series-catalog picks Anime, one on Series tags them
        # Series - and search rows carry no genres to decide it better
        # (measured in helpers/discover).
        ordered = [current] + [t for t in types if t != current]
        seen, catalogs = set(), []
        for entry_type in ordered:
            catalog = STREMIO_CATALOG_BY_TYPE.get(entry_type, "series")
            if catalog in seen:
                continue
            seen.add(catalog)
            catalogs.append((entry_type, catalog))
        return catalogs

    def _search_worker(self, provider, text, seq, catalogs=()):
        # Must never raise. On a bare thread a failure only lost this one
        # search; on the shared submit_latest worker it would also be the
        # last thing this dialog said, leaving "Searching..." on screen
        # forever. Emit an empty result instead - the status line then
        # reads "No matches", which is both true and dismissable.
        try:
            if provider == "stremio":
                # Both catalogs at once, not one after the other: a
                # dual-type search (Series + Movie) was two sequential
                # Cinemeta round trips, and the second doubled how long
                # "Searching..." sat there. Each result remembers which
                # catalog answered, because that is what the entry's
                # Type has to become.
                import concurrent.futures
                results = []
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=max(1, len(catalogs))) as pool:
                    futures = [
                        (entry_type, pool.submit(stremio.search, text, catalog))
                        for entry_type, catalog in catalogs]
                    for entry_type, future in futures:
                        try:
                            found = future.result()
                        except Exception:
                            found = []
                        results.extend({**r, "_entry_type": entry_type}
                                       for r in found)
            else:
                results = manga_sites.search_all(text)
        except Exception:
            results = []
        self._signals.results.emit(provider, results, seq)

    # A video search used to fan every Cinemeta match out into one
    # suggestion per Video Website ("One Piece (Stremio)", "One Piece
    # (Netflix)", "One Piece (Crunchyroll)") - that fan-out *was* the
    # per-entry source choice, presented in the title dropdown, so it goes
    # with the dropdown. One suggestion per title now.

    def _label_for_result(self, provider, r):
        if provider == "manga_sites":
            return f"{r['title']} ({r['site_name']})"
        # The type only earns its space when both catalogs were searched -
        # otherwise every suggestion would carry the same word.
        if self._searched_several_types and r.get("_entry_type"):
            return f"{r['title']} — {r['_entry_type']}"
        suffix = f" ({r['format']})" if r.get("format") else ""
        return f"{r['title']}{suffix}"

    def _apply_search_results(self, provider, results, seq):
        if seq != self._search_seq:
            return

        self._search_results = {}
        labels = []
        for r in results:
            label, n = self._label_for_result(provider, r), 2
            base_label = label
            while label in self._search_results:
                label = f"{base_label} #{n}"
                n += 1
            self._search_results[label] = {**r, "_provider": provider}
            labels.append(label)

        current_text = self.title_combo.currentText()
        self.title_combo.blockSignals(True)
        self.title_combo.clear()
        self.title_combo.addItems(labels)
        self.title_combo.setCurrentText(current_text)
        self.title_combo.blockSignals(False)

        # Deliberately not naming the source. Which service was searched
        # is an implementation detail of the Type dropdown, and spelling
        # it out ("No matches from Stremio", "...from your manga
        # websites") only invited the question of why that one was asked.
        if labels:
            # "1 Match found" / "4 Matches found". The dropdown opens on
            # its own right below, so the old "- pick one below" was
            # telling the user what they were already looking at.
            noun = "Match" if len(labels) == 1 else "Matches"
            self.search_status_label.setText(f"{len(labels)} {noun} found")
            self.title_combo.showPopup()
        else:
            self.search_status_label.setText("No matches - you can still save this title as-is")

        # If what's typed already exactly matches one of the results (not
        # a suffixed dupe label, the actual title), apply it automatically
        # instead of waiting for a click on the dropdown - otherwise
        # typing the exact right name and just hitting Save skips the
        # cover/link/progress lookups entirely, which they're not there
        # to fix by hand.
        if not self._suggestion_applied:
            exact_label = next((label for label, r in self._search_results.items()
                                  if r["title"].strip().lower() == current_text.strip().lower()), None)
            if exact_label:
                self._on_suggestion_selected(exact_label)

    def _on_suggestion_selected(self, label):
        self._suggestion_applied = True
        result = self._search_results.get(label)
        if not result:
            return
        self._status_parts = {}  # a fresh pick supersedes any prior cover/progress status
        # A film picked from a dual-catalog search has to bring its Type
        # with it, or it saves as a Series - wrong statuses, and episode
        # tracking for something that is one video. Signals blocked so
        # this doesn't re-enter the very search that produced it.
        picked_type = result.get("_entry_type")
        if picked_type and picked_type != self.type_box.currentText():
            self.type_box.blockSignals(True)
            self.type_box.setCurrentText(picked_type)
            self.type_box.blockSignals(False)
            self._update_labels()
            self._populate_status_options(self.status_box.currentText())
            self._populate_site_options(self.site_box.currentData())
            self._update_url_and_site_visibility()
            self._update_progress_visibility()
        self.title_combo.setCurrentText(result["title"])
        self.selected_cover_url = result.get("cover_url")
        self.selected_cover_path = None

        if result.get("_provider") == "manga_sites":
            # A different page than the number on screen came from means
            # that number belongs to a different manga. Measured: typing
            # "Kingdom (WAN)" auto-applies plain "Kingdom" the moment the
            # first word is a full match, whose page lookup then wrote 798
            # into the field; the real pick (884) arrived after and was
            # refused by the "don't overwrite what's already there" rule
            # below, so the entry read 798 for a manga on chapter 884.
            # Cleared here so the lookup for the page actually picked has
            # somewhere to land.
            if result["url"] != self.url_edit.text():
                self.chapter_spin.setValue(0)
            self.url_edit.setText(result["url"])
            idx = self.site_box.findData(result["site_id"])
            if idx >= 0:
                self.site_box.setCurrentIndex(idx)
            if result.get("latest_chapter"):
                # No status line for this any more. It was appended after
                # whatever the site lookup had said, so the message ran on
                # past its own end; the field is now labelled Last Released
                # Chapter and read-only, which says the same thing where
                # it matters.
                self.chapter_spin.setValue(result["latest_chapter"])
            if not self.selected_cover_url or not result.get("latest_chapter"):
                # This engine's search results are missing the cover
                # and/or chapter count (e.g. Madara's AJAX search returns
                # titles/links only) - fetch them from the matched page/
                # detail endpoint instead of leaving them blank.
                # The title goes with it: without one the catalogue
                # fallback inside fetch_manga_details matches on the URL
                # slug, which misses for anything not named in English.
                threading.Thread(
                    target=self._resolve_manga_details,
                    args=(result["url"], result.get("title") or ""),
                    daemon=True).start()
        elif result.get("stremio_url"):
            # Saved regardless of which Video Website ends up selected
            # below, so progress-syncing keeps working even for entries
            # that save no url at all (see _entry_imdb_id).
            self.selected_imdb_id = result.get("id")
            # An entry already pinned to a Video Website never gets the
            # stremio:// link saved: open_tracker_entry tries url first,
            # so a leftover deep link would silently win over the site.
            # What goes there instead is that site's own page for this
            # title, resolved in the background - explicitly cleared first
            # so the field is empty (and the entry falls back to the
            # site's search page) if the resolution finds nothing. The
            # site is the one already on the entry now, not a picked one:
            # nothing here chooses a site any more.
            video_site_id = self._saved_site_id
            if self.type_box.currentText() in VIDEO_TYPES and video_site_id is not None:
                self.url_edit.setText("")
                self._start_video_site_resolution(video_site_id, result["title"])
            else:
                self._pending_video_identity = None
                self._set_status_part("site", "")
                self.url_edit.setText(result["stremio_url"])
            # Cinemeta's catalog search has no episode data at all (title/
            # poster/year only) - a background lookup fills in the Last
            # Season/Last Episode fields with the latest aired episode
            # (how far the release currently goes), *not* your own watch
            # progress - that's a separate concept, only ever set via
            # Sync Progress (a connected Stremio account) after the
            # entry's saved, same as
            # Manga's "Last Chapter" (site's latest) never doubles as
            # "Last Watched Chapter" (your own progress) either. Tracked
            # via its own identity (not url_edit's text) since site-
            # provider Anime doesn't get a url saved at all.
            #
            # Skipped for a film: there is no episode to look up, and the
            # boxes it would fill aren't on screen (see tracks_progress).
            if tracks_progress(self.type_box.currentText()):
                self._pending_episode_identity = result["stremio_url"]
                catalog = STREMIO_CATALOG_BY_TYPE.get(self.type_box.currentText(), "series")
                threading.Thread(target=self._resolve_episode_progress,
                                  args=(result["id"], catalog, result["stremio_url"]),
                                  daemon=True).start()
            else:
                self._pending_episode_identity = None

        self._refresh_preview()
        if self.selected_cover_url:
            self._set_status_part("cover", "Loading cover...")
            threading.Thread(target=self._download_cover, args=(self.selected_cover_url,), daemon=True).start()
        else:
            self._set_status_part("cover", "")

    def _download_cover(self, url):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently and leave the UI stuck on
        # "Loading cover..." forever.
        try:
            path = images.download(url)
        except Exception:
            path = None
        self._signals.cover_ready.emit(url, path)

    def _on_cover_downloaded(self, url, path):
        if url != self.selected_cover_url:
            return
        self.selected_cover_path = str(path) if path else None
        self._refresh_preview()
        self._set_status_part("cover", "" if path else "Couldn't download the cover image")

    def _resolve_manga_details(self, page_url, title=""):
        details = {}
        try:
            details = manga_sites.fetch_manga_details(page_url,
                                                      title=title)
        except Exception:
            pass
        self._signals.manga_details_resolved.emit(
            page_url, details.get("cover_url") or "", details.get("latest_chapter") or 0.0)

    def _on_manga_details_resolved(self, page_url, cover_url, latest_chapter):
        if page_url != self.url_edit.text():
            return  # the user picked a different suggestion meanwhile
        # Never overwrite a value the user already has (either from the
        # search result itself or from editing it by hand while this was
        # still loading in the background).
        if latest_chapter and not self.chapter_spin.value():
            self.chapter_spin.setValue(latest_chapter)
        if cover_url and not self.selected_cover_url:
            self.selected_cover_url = cover_url
            self._set_status_part("cover", "Loading cover...")
            threading.Thread(target=self._download_cover, args=(cover_url,), daemon=True).start()
        elif not self.selected_cover_url:
            self._set_status_part("cover", "")

    # ---- Video Website page-url resolution ---------------------------
    # Only for an entry already pinned to one. Cinemeta (the only public
    # anime search API) knows nothing about that site, so the site's own
    # page for the title is resolved separately, right here, against that
    # one site. No dropdown feeds this any more - the entry's saved
    # site_id does - but it still has to run, or editing such an entry's
    # title would leave it pointing at the old title's page.

    def _start_video_site_resolution(self, site_id, title):
        site = anime_sites.get_site(site_id)
        if not site:
            self._pending_video_identity = None
            self._set_status_part("site", "")
            return
        # Identity, not url_edit's text, tracks staleness here - the
        # field is empty for the whole duration of the lookup, so it
        # can't tell one pending lookup from another.
        identity = f"{site_id}\n{title}"
        self._pending_video_identity = identity
        # Nothing on screen while it runs. The line used to say
        # "Searching...", which added a second thing moving under a field
        # the user isn't waiting on - the page url fills itself in, and
        # only its *failure* is worth a word (see _on_video_url_resolved).
        self._set_status_part("site", "")
        # Not a bare thread: this fires per debounced keystroke, so
        # typing a title used to start one unbounded resolution after
        # another, each opening its own connections while its result was
        # already stale. submit_latest keeps only the newest.
        lookup_pool.submit_latest("video-site", self._resolve_video_site_url,
                                  site, title, identity)

    def _resolve_video_site_url(self, site, title, identity):
        # Must never raise: an uncaught exception would kill the thread
        # silently and leave the dialog stuck on "Searching...".
        try:
            url = anime_sites.resolve_page_url(site, title)
        except Exception:
            url = None
        self._signals.video_url_resolved.emit(identity, site.get("name", ""), url or "")

    def _video_lookup_in_flight(self):
        """True while a Video Website page-url lookup is still running.
        A stale result reporting in leaves this True, correctly - the
        newer lookup it was superseded by is the one still out."""
        return (self._pending_video_identity is not None
                and self._pending_video_identity != self._resolved_video_identity)

    def _on_video_url_resolved(self, identity, site_name, url):
        if identity != self._pending_video_identity:
            return  # the user picked a different title or site meanwhile
        self._resolved_video_identity = identity
        self.url_edit.setText(url)
        self._set_status_part(
            "site", "" if url else f"No page found on {site_name} - it'll open that site's search instead")
        if self._save_waiting_on_video:
            self._save_waiting_on_video = False
            self._save()

    def _save_after_video_wait(self):
        """VIDEO_URL_SAVE_WAIT_MS expired with the lookup still out - save
        without a page url rather than leaving the dialog stuck."""
        if self._save_waiting_on_video:
            self._save_waiting_on_video = False
            self._save()

    def _resolve_episode_progress(self, imdb_id, catalog, identity):
        try:
            total = stremio.fetch_latest_episode(imdb_id, catalog)
        except Exception:
            total = None
        total_season, total_episode = total or (0, 0)
        self._signals.latest_episode_resolved.emit(identity, total_season, total_episode)

    def _on_latest_episode_resolved(self, identity, total_season, total_episode):
        if identity != self._pending_episode_identity:
            return  # the user picked a different suggestion meanwhile
        # Only `latest_available` - how far the release currently goes,
        # for the tooltip. This used to seed the last-watched spinners as
        # well when they were empty, which is how the latest aired
        # episode could be saved as if it were watched; there is nothing
        # to seed now, and watch progress is only ever what was played.
        self._latest_available = format_episode_progress(total_season, total_episode)

    # ------------------------------------------------------------------
    def _progress_text(self):
        """What to store in `progress`.

        For reading that is the *site's* latest released chapter, which
        this form still shows (read-only) and a picked suggestion still
        fills in. For video it is watch progress, which nothing here sets
        any more - so it is handed straight back unchanged, legacy
        freeform text included."""
        if self.type_box.currentText() in MANGA_TYPES:
            value = self.chapter_spin.value()
            if not value and self._original_progress and not parse_chapter_progress(self._original_progress):
                return self._original_progress  # unparsed legacy text, spinner untouched - keep it
            return format_chapter_progress(value)
        return self._original_progress

    def _save(self):
        title = self.title_combo.currentText().strip()
        if not title:
            inform(self, "Tracker", "Title can't be empty.")
            return

        # The Video Website's page for this title is resolved in a
        # background thread - ~1.5s for Crunchyroll, measured - and Save
        # is reachable long before it lands. Saving in that window stored
        # an empty url, and *nothing re-resolved one*, so the entry
        # opened that site's search page permanently: the reported
        # "One Piece opens Crunchyroll search" bug, reproduced by picking
        # the suggestion and saving inside the lookup's own duration. So
        # hold the save until the lookup reports - it always does, hit or
        # miss - and let _on_video_url_resolved re-enter here.
        if (self.type_box.currentText() in VIDEO_TYPES and not self.url_edit.text().strip()
                and self._video_lookup_in_flight() and not self._video_wait_used):
            self._video_wait_used = True
            self._save_waiting_on_video = True
            self.save_btn.setEnabled(False)
            self.save_btn.setText("Finding page...")
            QTimer.singleShot(VIDEO_URL_SAVE_WAIT_MS, self._save_after_video_wait)
            return

        if self.is_new:
            self.entry = {"id": str(uuid.uuid4())}
        current_type = self.type_box.currentText()
        is_manga = current_type in MANGA_TYPES
        is_video = current_type in VIDEO_TYPES
        # Reading picks its site here; video keeps whatever it was already
        # pinned to (see _saved_site_id). Never None-ed on save: that
        # would quietly unpin every Netflix/Crunchyroll entry the first
        # time it was edited, and anime_sites.streaming_provider is what
        # tells the player those are DRM and unplayable.
        site_id = self.site_box.currentData() if is_manga else self._saved_site_id
        saved_url = self.url_edit.text().strip()
        # An Anime entry set to a configured Video Website (not the
        # built-in "Stremio" option) must never carry a stray stremio://
        # link left over from an earlier pick or from switching the
        # dropdown by hand after one - open_tracker_entry tries url
        # first, so a stale one there would silently override the site.
        # An http(s) url here is the opposite case: it's the page
        # anime_sites resolved *on that site*, and it's exactly what
        # should open, instead of the site's search results.
        if is_video and site_id is not None and saved_url.startswith("stremio://"):
            saved_url = ""
        self.entry.update(
            title=title,
            type=current_type,
            status=self.status_box.currentText(),
            progress=self._progress_text(),
            progress_verified=self._progress_verified,
            latest_available=self._latest_available,
            # Carried through untouched - the reader writes it, and an
            # edit of the title or status must not reset it to whatever a
            # spinner happened to hold.
            last_watched_chapter=self._saved_watched_chapter if is_manga else None,
            show_last_watched=(self.show_watched_check.isChecked()
                               if tracks_progress(current_type) else False),
            url=saved_url,
            cover_url=self.selected_cover_url,
            cover_path=self.selected_cover_path,
            site_id=site_id,
            imdb_id=self.selected_imdb_id if is_video else None,
        )
        self.on_save(self.entry, self.is_new)
        self.accept()
