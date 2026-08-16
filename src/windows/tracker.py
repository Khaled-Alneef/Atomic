"""Anime & Manga & Series tracker: three pages sharing this list-widget
implementation. Anime/Manga share one data file split by the entry's
`type` field (so you can reclassify one into the other); Series has its
own file since it's a different domain entirely.

Typing a title searches a matching source in the background - Stremio's
Cinemeta catalog for Anime/Series, every reading site configured in
Settings for Manga - and picking a suggestion auto-fills the title/cover
and a direct link (a stremio:// deep link for Anime/Series, or the
matched manga's page on whichever site it came from) so double-click
jumps straight there. You can also just type your own title if it's not
listed there.
"""

import re
import threading
import time
import uuid
import webbrowser

from PyQt6.QtCore import QObject, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from helpers import (
    anilist, anime_sites, app_settings, images, lookup_pool, manga_sites,
    release_schedule, storage, stremio, theme,
)
from helpers.widgets import (
    Card, CardDragReorder, GlassPage, defer_grid_rebuild, finish_toast,
    scroll_area, show_toast,
)

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
    """Whether that number is yours to set by hand - the spinners in
    Add/Edit, and the +/- on the card.

    Stremio owns it on a Stremio entry and overwrites it on the next
    sync, so editing there fights the sync; that is why the +/- buttons
    were removed outright once, and the fix is per-entry rather than none
    at all. An entry pinned to Netflix or Crunchyroll has no source that
    can ever fill it in, and Manga has none whatsoever, so on those the
    number has to be yours."""
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
GRID_COLS = 9
PREVIEW_SIZE = (90, 120)
SEARCH_DEBOUNCE_MS = 450

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


def open_tracker_entry(parent, entry):
    """Open an entry's page: for Manga, its matched page on its reading
    site (or that site's search results for the title, if no specific
    page was matched) - the configured Manga Music site (if any) opens
    first, so it starts loading/playing before the reading tab takes
    focus. For Anime, a saved stremio:// deep link (opens Stremio) if the
    entry uses the built-in Stremio option, or its configured Video
    Website's search results otherwise. For Series, a saved stremio://
    link or plain URL as-is. Returns False if there's nothing to open."""
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
        storage.update_entry(AnimePage.DATA_FILE, entry.get("id"), fields)
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

# The one thing that can stop a sync and that the user can fix - no
# Stremio account connected. Everything else is a genuine no-match.
REASON_NO_STREMIO_ACCOUNT = "no_stremio_account"

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
    # Manga has no Stremio presence to sync progress against.
    SUPPORTS_PROGRESS_SYNC = True
    # Which release-schedule source the hover tooltip's "next episode/
    # chapter" lines come from for this page - see helpers/release_schedule.
    MEDIUM = release_schedule.MEDIUM_ANIME

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

        self._cover_signals = _CoverSignals()
        self._cover_signals.ready.connect(self._on_sharper_cover_ready)

        self._schedule_signals = _ScheduleSignals()
        self._schedule_signals.resolved.connect(self._on_schedule_resolved)

        self.entries = _migrate(storage.load(self.DATA_FILE, []), self.DATA_FILE)
        # What was on disk when this page loaded. _save_entries needs it
        # to tell "another page deleted this while I was open" from "I
        # have just added this and it isn't saved yet" - both look like
        # an entry of someone else's type that disk doesn't have.
        self._ids_at_load = {e.get("id") for e in self.entries if e.get("id")}

        # Filter selections last only as long as the page does. Pages
        # rebuild from scratch on every visit (.claude/rules/ui.md), and a
        # filter still narrowing the grid from a previous visit would read
        # as entries having gone missing. Empty means "everything".
        self._status_filter = set()
        self._type_filter = set()
        # (action, kind, value) for every item in the filter menu, so the
        # ticks can be re-read off the sets while the menu is still open.
        self._filter_actions = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(QLabel(self.TITLE, objectName="PanelTitle"))
        header.addStretch()
        # No refresh button: opening the page is the refresh (see
        # _auto_refresh). A button that had to be pressed, and then sat
        # on "Updating..." for as long as the slowest source took, was
        # asking the user to do the app's job and wait for it.
        add_btn = QPushButton("+", objectName="AccentIcon")
        add_btn.setFixedSize(40, 40)
        add_btn.setToolTip("Add Entry")
        add_btn.clicked.connect(self._open_form)
        header.addWidget(add_btn)
        layout.addLayout(header)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        top_row.addStretch()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search titles...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(220)
        # Debounced rather than filtering on every keystroke: each redraw
        # rebuilds every card from scratch (pages hold no state - see
        # .claude/rules/ui.md), so typing six characters would otherwise
        # rebuild the whole grid six times.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh_grid)
        self.search_box.textChanged.connect(lambda _text: self._search_timer.start())
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
        layout.addLayout(top_row)

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(14)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._drag_reorder = CardDragReorder(
            self.grid_body, self._begin_custom_order, self._drop_reorder)

        self._refresh_grid()
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
                self.MEDIUM, entry["title"], imdb_id=_entry_imdb_id(entry),
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
        _backfill_missing_latest_available."""
        for entry in self.entries:
            if entry["type"] not in MANGA_TYPES:
                continue
            upgraded = manga_sites.upgrade_cover_url(entry.get("cover_url"))
            if upgraded and upgraded != entry.get("cover_url"):
                # Bounded, like every other per-entry loop here: this one
                # downloads a full-size image per entry, so it is the
                # heaviest of them to fire all at once.
                lookup_pool.submit(self._fetch_sharper_cover, entry["id"], upgraded)

    def _fetch_sharper_cover(self, entry_id, new_url):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        try:
            path = images.download(new_url)
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
        for status in STATUSES_BY_TYPE.get(self.ENTRY_TYPES[0], _WATCHING_STATUSES):
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
        self._refresh_grid()

    def _clear_filters(self, *_args):
        # *_args because this is wired to triggered(bool) as well as being
        # called directly.
        self._status_filter.clear()
        self._type_filter.clear()
        self._sync_filter_menu_checks()
        self._refresh_grid()

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
        rows.extend(release_schedule.tooltip_lines(entry, self.MEDIUM))
        # Who said so. A number the user disagrees with ("I'm on episode
        # 2, why does this say 7?") is unarguable while it is anonymous -
        # naming the source is what turns it into something checkable.
        source = entry.get("progress_source")
        if source and entry.get("progress_verified"):
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
        return [(status, grouped[status])
                for status in [*known_statuses, *extra_statuses] if grouped.get(status)]

    def _refresh_grid(self, *_args):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        self._card_providers = anime_sites.streaming_provider_map()

        # Dragging is disabled while a search is narrowing the grid: a
        # drop writes the order that is on screen (see
        # _begin_custom_order), and the order on screen is not the whole
        # order while entries are hidden.
        narrowed = self._grid_is_narrowed()

        sections = self._sections()
        if not sections:
            if self._search_query():
                message = f"Nothing here matches '{self.search_box.text().strip()}'."
            elif narrowed:
                message = "Nothing matches the filter - clear it from the filter button."
            else:
                message = f"No {self.TITLE.lower()} yet - click '+' to create one."
            self.grid_layout.addWidget(QLabel(message, objectName="Muted"), 0, 0)
            return

        row = 0
        for status, group in sections:
            header = QLabel(f"{status} ({len(group)})", objectName="SectionTitle")
            self.grid_layout.addWidget(header, row, 0, 1, GRID_COLS)
            row += 1
            for index, entry in enumerate(group):
                card = self._build_card(entry)
                self.grid_layout.addWidget(card, row + index // GRID_COLS, index % GRID_COLS)
            row += (len(group) + GRID_COLS - 1) // GRID_COLS

    def _build_card(self, entry):
        card = Card(hoverable=True)
        card.setFixedWidth(POSTER_SIZE[0] + 20)
        provider = getattr(self, "_card_providers", {}).get(entry.get("site_id"))
        card.set_tooltip_provider(lambda en=entry, pv=provider: self._tooltip_html(en, pv))
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 10, 8, 10)
        card_layout.setSpacing(6)

        cover = QLabel()
        cover.setFixedSize(*POSTER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(entry.get("cover_path"), entry["title"], POSTER_SIZE))
        card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

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

        # Only where the number is actually yours to set - see
        # last_watched_is_editable. On a Stremio entry the next sync
        # overwrites whatever the button did, which is why these were
        # removed wholesale once; an entry pinned to Netflix, or any
        # Manga, has no such source and needs them.
        if last_watched_is_editable(entry):
            if entry["type"] in MANGA_TYPES:
                card_layout.addLayout(self._bump_controls(
                    entry, "Last Watched Chapter", self._bump_watched_chapter))
            else:
                card_layout.addLayout(self._bump_controls(
                    entry, "Last Watched Episode", self._bump_watched_episode))

        card.clicked.connect(lambda en=entry: self._open_entry(en))
        card.rightClicked.connect(lambda event, en=entry: self._show_context_menu(event, en))
        if not self._grid_is_narrowed():
            self._drag_reorder.attach(card, entry.get("id"))
        return card

    def _bump_controls(self, entry, label, handler):
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        for sign, delta in (("-", -1), ("+", 1)):
            button = QPushButton(sign, objectName="Icon")
            button.setFixedSize(46, 46)
            button.setToolTip(f"{label} {delta:+d}")
            button.clicked.connect(
                lambda checked=False, en=entry, d=delta: handler(en, d))
            if sign == "+":
                controls.addStretch()
            controls.addWidget(button)
        return controls

    def _bump_watched_chapter(self, entry, delta):
        current = entry.get("last_watched_chapter") or 0.0
        entry["last_watched_chapter"] = max(0.0, current + delta)
        entry["updated_at"] = storage.now_iso()
        self._save_entries()
        self._refresh_grid()

    def _bump_watched_episode(self, entry, delta):
        """Moves the episode and never the season: season lengths differ
        per show, so there is no arithmetic that finds a season boundary -
        guessing one would put the card on an episode that doesn't exist.
        The season is set in Edit, where it can be typed."""
        season, episode = parse_episode_progress(entry.get("progress"))
        entry["progress"] = format_episode_progress(season, max(0, episode + delta))
        # A number you pressed a button to set is confirmed by definition.
        # Without this the card would go on hiding it (_progress_display
        # shows nothing unverified) and the button would look broken.
        entry["progress_verified"] = True
        entry["progress_source"] = SOURCE_MANUAL
        entry["updated_at"] = storage.now_iso()
        self._save_entries()
        self._refresh_grid()

    def _progress_display(self, entry):
        # Hidden by the entry's own "Show Last Watched" tick, and always
        # for a film - the +/- above go with it, so the card doesn't keep
        # buttons for a number it no longer shows.
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

    def _open_entry(self, entry):
        if not open_tracker_entry(self, entry):
            self._open_form(edit=True, entry=entry)

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_form(edit=True, entry=entry))
        if entry.get("type") in ("Anime", "Series") and _entry_imdb_id(entry):
            menu.addAction("Sync Progress", lambda: self._sync_progress(entry))
        # No Move Up/Move Down: every page reorders by dragging a card,
        # and two menu items that only appeared under one sort mode were
        # a second way to do the same thing, worse.
        menu.addAction("Delete", lambda: self._delete_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    def _not_found_message(self, reason: str) -> str:
        """What to say when a sync found no progress.

        The one cause the user can act on is a Stremio account that was
        never connected - saying "not found" for that made the app look
        broken rather than unconfigured, which cost its owner a long
        time before anyone worked it out."""
        if REASON_NO_STREMIO_ACCOUNT in set((reason or "").split(",")):
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
            if found:
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

        if not silent:
            if not found:
                QMessageBox.information(self, "Sync Progress",
                                        self._not_found_message(reason))
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
            self._refresh_step_done(run, changed)

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
        if QMessageBox.question(self, "Delete Entry", f"Delete '{entry['title']}'?") == QMessageBox.StandardButton.Yes:
            self.entries.remove(entry)
            self._save_entries()
            self._refresh_grid()

    # ------------------------------------------------------------------
    def _open_form(self, edit=False, entry=None):
        if edit and entry is None:
            return
        EntryForm(self, entry if edit else None, default_type=self.ENTRY_TYPES[0], type_options=self.TYPE_OPTIONS,
                  on_save=self._on_form_save)

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


class AnimePage(TrackerPage):
    DATA_FILE = "tracker.json"
    ENTRY_TYPES = ("Anime",)
    TITLE = "Anime"
    TYPE_OPTIONS = ["Anime"]
    MEDIUM = release_schedule.MEDIUM_ANIME


class MangaPage(TrackerPage):
    DATA_FILE = "tracker.json"
    ENTRY_TYPES = MANGA_TYPES
    TITLE = "Reading"
    TYPE_OPTIONS = list(MANGA_TYPES)
    PROGRESS_COLUMNS = ["Last Released Chapter"]
    SUPPORTS_PROGRESS_SYNC = False
    MEDIUM = release_schedule.MEDIUM_MANGA


class SeriesPage(TrackerPage):
    DATA_FILE = "series.json"
    ENTRY_TYPES = ("Series", "Movie")
    TITLE = "Movies & Series"
    TYPE_OPTIONS = ["Series", "Movie"]
    MEDIUM = release_schedule.MEDIUM_SERIES


class _SearchSignals(QObject):
    results = Signal(str, list, int)  # provider ("stremio"/"manga_sites"), results, search sequence #
    cover_ready = Signal(str, object)
    manga_details_resolved = Signal(str, str, float)  # page url, cover url, latest chapter
    latest_episode_resolved = Signal(str, int, int)  # identity (stremio url), latest available season/episode
    video_url_resolved = Signal(str, str, str)  # identity (site id + title), site name, resolved page url ("" = none)


class EntryForm(QDialog):
    def __init__(self, parent, entry, default_type, type_options, on_save):
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
        # Whether the season/episode spinners hold *confirmed* progress
        # (fetched from a connected Stremio account, or typed in
        # by hand) rather than just the unconfirmed "latest episode out"
        # guess - only verified progress shows on the card. Manual edits
        # to the spinners flip this true (see the valueChanged connect
        # below, wired up only after the initial values are set so
        # loading an existing entry doesn't misread as a fresh edit).
        self._progress_verified = bool(entry and entry.get("progress_verified"))
        # Set only when this form fills the episode boxes itself from
        # the latest aired episode - see _on_latest_episode_resolved.
        self._autofilled_progress = False
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
        self.setFixedSize(460, 620)
        theme.apply_dark_titlebar(self)

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

        # The saved value stays a plain string (it's what the tree/home
        # dashboard display directly) - these spinners just give a
        # faster, typo-proof way to set it than freeform text, and a
        # one-click +1 for "I just finished this episode/chapter".
        self._original_progress = entry.get("progress", "") if entry else ""

        form.addSpacing(8)
        # Per-entry, because whether a last-watched number belongs on the
        # card depends on the entry and not the page: a film has none, a
        # Stremio entry keeps its own up to date, and one pinned to
        # Netflix only ever has whatever you put there. Ticking it off
        # hides the spinners here *and* the +/- on the card together - a
        # tick that left the buttons behind would be worse than no tick.
        # "&&" and not "&": QCheckBox reads a single one as a mnemonic
        # marker and swallows it, which is what drew "Movies  Series".
        self.show_watched_check = QCheckBox("Show Last Watched Season && Episode")
        # Off for a new entry, Stremio included: nothing is known about it
        # yet, and a number defaulting to visible would be showing E00
        # before anything has said otherwise. An existing entry keeps
        # whatever it had, and one saved before the tick existed defaults
        # on - see shows_last_watched.
        self.show_watched_check.setChecked(shows_last_watched(entry) if entry else False)
        self.show_watched_check.toggled.connect(self._update_progress_visibility)
        form.addWidget(self.show_watched_check)

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

        # Unlike Last Released Chapter above (auto-filled from the reading
        # site's latest release), this is yours to set - only you know what
        # you've actually read, same as Anime/Series' real Stremio
        # progress. Shown on the card and adjustable there via +/-.
        # Its own widget rather than a bare layout, so the tick can hide
        # this half while Last Released Chapter beside it stays: that one
        # is the site's number, and the tick is only about yours.
        self.watched_chapter_widget = QWidget()
        watched_col = QVBoxLayout(self.watched_chapter_widget)
        watched_col.setContentsMargins(0, 0, 0, 0)
        watched_col.setSpacing(2)
        watched_col.addWidget(QLabel("Last Watched Chapter"))
        self.watched_chapter_spin = QDoubleSpinBox()
        self.watched_chapter_spin.setRange(0, 99999)
        self.watched_chapter_spin.setDecimals(1)
        self.watched_chapter_spin.setValue((entry or {}).get("last_watched_chapter") or 0.0)
        watched_col.addWidget(self.watched_chapter_spin)
        chapter_layout.addWidget(self.watched_chapter_widget)

        form.addWidget(self.chapter_row)

        self.episode_row = QWidget()
        episode_layout = QHBoxLayout(self.episode_row)
        episode_layout.setContentsMargins(0, 0, 0, 0)
        episode_layout.setSpacing(14)
        season0, episode0 = parse_episode_progress(self._original_progress)
        season_col = QVBoxLayout()
        season_col.setSpacing(2)
        season_col.addWidget(QLabel("Last Watched Season"))
        self.season_spin = QSpinBox()
        self.season_spin.setRange(0, 99)
        self.season_spin.setSpecialValueText("—")
        self.season_spin.setValue(season0)
        season_col.addWidget(self.season_spin)
        episode_layout.addLayout(season_col)
        episode_col = QVBoxLayout()
        episode_col.setSpacing(2)
        episode_col.addWidget(QLabel("Last Watched Episode"))
        self.episode_spin = QSpinBox()
        self.episode_spin.setRange(0, 9999)
        self.episode_spin.setValue(episode0)
        episode_col.addWidget(self.episode_spin)
        episode_layout.addLayout(episode_col)
        episode_layout.addStretch()
        form.addWidget(self.episode_row)

        # Under the boxes it is about, rather than under the Video Website
        # dropdown where it used to sit: the number is what the sentence
        # explains, and it is meaningless next to a dropdown when the
        # boxes themselves are hidden.
        self.site_sync_hint = QLabel("Only Stremio tracks your progress automatically.",
                                     objectName="Muted")
        self.site_sync_hint.setWordWrap(True)
        form.addWidget(self.site_sync_hint)

        # Wired up only after both initial values are set above, so
        # loading an existing entry's saved progress isn't mistaken for a
        # fresh manual edit - only edits from here on mark it verified.
        self.season_spin.valueChanged.connect(self._on_progress_hand_edited)
        self.episode_spin.valueChanged.connect(self._on_progress_hand_edited)

        # Series only: the Stremio deep link, freely editable. Anime/Manga
        # have no equivalent field - their open target is fully derived
        # from the site dropdown below, never manually typed.
        self.url_row = QWidget()
        url_layout = QVBoxLayout(self.url_row)
        url_layout.setContentsMargins(0, 8, 0, 0)
        url_layout.setSpacing(4)
        self.url_label = QLabel()
        url_layout.addWidget(self.url_label)
        self.url_edit = QLineEdit(entry.get("url", "") if entry else "")
        url_layout.addWidget(self.url_edit)
        form.addWidget(self.url_row)

        # Manga/Anime only: which site (from Settings) this entry opens
        # to - "Reading Website" for Manga, "Video Website" for Anime
        # (where the built-in "Stremio" option means "use the deep link"
        # instead of a configured site). Picking a search suggestion below
        # sets both this and the matched link (kept on url_edit
        # internally, just not shown).
        self.site_row = QWidget()
        site_layout = QVBoxLayout(self.site_row)
        site_layout.setContentsMargins(0, 8, 0, 0)
        site_layout.setSpacing(4)
        self.site_label = QLabel()
        site_layout.addWidget(self.site_label)
        self.site_box = QComboBox()
        self._populate_site_options(entry.get("site_id") if entry else None)
        site_layout.addWidget(self.site_box)
        # Changing the Video Website by hand has to re-resolve the page
        # url - the one saved belongs to whichever site was picked
        # before, and opening it would land on the old site. Connected
        # after the initial populate (and _populate_site_options blocks
        # signals) so loading an existing entry doesn't fire this.
        self.site_box.currentIndexChanged.connect(self._on_site_changed)
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
            self.title_label.setText("Title (type to search Stremio)")
            self.url_label.setText("Stremio link (opens Stremio on double-click)")
        else:
            self.title_label.setText("Title (type to search your reading websites)")
        is_video = self.type_box.currentText() in VIDEO_TYPES
        self.site_label.setText(
            "Video Website (opens directly on double-click)" if is_video
            else "Reading Website (opens directly on double-click)")
        # The Stremio line's visibility is not set here any more. It used
        # to be, on `is_video` alone, and this runs *after*
        # _update_progress_visibility in __init__ - so it re-showed the
        # line under a hidden set of boxes on a new entry. One owner now:
        # _update_progress_visibility, which knows about the tick too.

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
        current_type = self.type_box.currentText()
        has_site = current_type in MANGA_TYPES or current_type in VIDEO_TYPES
        self.url_row.setVisible(not has_site)
        self.site_row.setVisible(has_site)

    def _update_progress_visibility(self, *_args):
        current_type = self.type_box.currentText()
        is_manga = current_type in MANGA_TYPES
        tracked = tracks_progress(current_type)
        show_watched = tracked and self.show_watched_check.isChecked()
        # The tick goes too for a film, rather than being left on screen
        # controlling nothing.
        self.show_watched_check.setVisible(tracked)
        self.show_watched_check.setText(
            "Show Last Watched Chapter" if is_manga
            else "Show Last Watched Season && Episode")
        # Manga's row holds Last Released Chapter as well, which is the
        # site's number and stays regardless - only its watched half
        # answers to the tick.
        self.chapter_row.setVisible(is_manga)
        self.watched_chapter_widget.setVisible(show_watched)
        self.episode_row.setVisible(not is_manga and show_watched)
        # Goes with the boxes it explains - and never for Manga, which has
        # no automatic source at all, so naming Stremio there would only
        # raise a question with no answer.
        self.site_sync_hint.setVisible(not is_manga and show_watched)
        self._update_watched_editability()

    def _update_watched_editability(self):
        """Stremio entries show the number they sync but don't hand it
        over to be edited - see last_watched_is_editable. Re-run on both
        Type and Video Website changes, since either can flip which case
        this entry is in while the dialog is open."""
        editable = (self.type_box.currentText() in MANGA_TYPES
                    or self.site_box.currentData() is not None)
        for spin in (self.season_spin, self.episode_spin):
            spin.setReadOnly(not editable)
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows if editable
                                  else QAbstractSpinBox.ButtonSymbols.NoButtons)

    def _refresh_preview(self):
        title = self.title_combo.currentText() or "?"
        self.preview_label.setPixmap(images.thumbnail_or_avatar(self.selected_cover_path, title, PREVIEW_SIZE))

    def _populate_site_options(self, current_site_id=None):
        self.site_box.blockSignals(True)
        self.site_box.clear()
        if self.type_box.currentText() in VIDEO_TYPES:
            self.site_box.addItem("Stremio", None)
            sites = anime_sites.list_sites()
        else:
            self.site_box.addItem("— None —", None)
            sites = manga_sites.list_sites()
        for site in sites:
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
        threading.Thread(target=self._search_worker,
                         args=(provider, text, seq, catalogs), daemon=True).start()
        # The Video Website is searched on its own, off what's typed,
        # rather than only when a Cinemeta suggestion is picked: the site
        # knows its own catalogue, and a title Cinemeta spells
        # differently (or doesn't carry at all) would otherwise fall
        # through to a search-results page. Also re-blanks a page url
        # resolved for a title that's since been edited away.
        site_id = self.site_box.currentData()
        if self.type_box.currentText() in VIDEO_TYPES and site_id is not None:
            self._start_video_site_resolution(site_id, text)

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
        return [(t, STREMIO_CATALOG_BY_TYPE.get(t, "series")) for t in types]

    def _search_worker(self, provider, text, seq, catalogs=()):
        if provider == "stremio":
            results = []
            for entry_type, catalog in catalogs:
                # Each result remembers which catalog answered, because
                # that is what the entry's Type has to become.
                results.extend({**r, "_entry_type": entry_type}
                               for r in stremio.search(text, catalog))
        else:
            results = manga_sites.search_all(text)
        self._signals.results.emit(provider, results, seq)

    def _video_site_options(self):
        """(site_id, site_name) pairs matching the Video Website dropdown -
        the built-in Stremio option (None) first, then every configured
        anime site, same order as _populate_site_options."""
        return [(None, "Stremio")] + [(s["id"], s["name"]) for s in anime_sites.list_sites()]

    def _expand_for_video_sites(self, results):
        """One Stremio match can open on any configured Video Website -
        fan each raw result out into one suggestion per site (mirroring
        manga's one-result-per-site search), tagged with which site it
        represents so picking it can set the dropdown below to match."""
        expanded = []
        for r in results:
            for site_id, site_name in self._video_site_options():
                expanded.append({**r, "_video_site_id": site_id, "_video_site_name": site_name})
        return expanded

    def _label_for_result(self, provider, r):
        if provider == "manga_sites":
            return f"{r['title']} ({r['site_name']})"
        if "_video_site_name" in r:
            # The type only earns its space when both were searched -
            # otherwise every suggestion would carry the same word.
            if self._searched_several_types and r.get("_entry_type"):
                return f"{r['title']} — {r['_entry_type']} ({r['_video_site_name']})"
            return f"{r['title']} ({r['_video_site_name']})"
        suffix = f" ({r['format']})" if r.get("format") else ""
        return f"{r['title']}{suffix}"

    def _apply_search_results(self, provider, results, seq):
        if seq != self._search_seq:
            return

        if provider == "stremio" and self.type_box.currentText() in VIDEO_TYPES:
            results = self._expand_for_video_sites(results)

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
            self.search_status_label.setText(f"{len(labels)} match(es) - pick one below")
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
                threading.Thread(target=self._resolve_manga_details, args=(result["url"],), daemon=True).start()
        elif result.get("stremio_url"):
            # Saved regardless of which Video Website ends up selected
            # below, so progress-syncing keeps working even for entries
            # that save no url at all (see _entry_imdb_id).
            self.selected_imdb_id = result.get("id")
            # An Anime suggestion is tagged with the Video Website it
            # represents (see _expand_for_video_sites) - picking it moves
            # the dropdown below to match, so the two always agree instead
            # of the dropdown silently staying on whatever it last was.
            video_site_id = result.get("_video_site_id")
            if self.type_box.currentText() in VIDEO_TYPES and "_video_site_id" in result:
                # Signals blocked because _on_site_changed would re-run
                # the resolution below off whatever is currently typed;
                # the explicit call does it with the suggestion's own
                # (canonical) title instead.
                self.site_box.blockSignals(True)
                idx = self.site_box.findData(video_site_id)
                if idx >= 0:
                    self.site_box.setCurrentIndex(idx)
                self.site_box.blockSignals(False)
            # A site other than the built-in "Stremio" option never gets
            # the stremio:// link saved: open_tracker_entry tries url
            # first, so a leftover deep link from an earlier pick on the
            # same title would silently win over the site. What goes
            # there instead is that site's own page for this title,
            # resolved in the background - explicitly cleared first so
            # the field is empty (and the entry falls back to the site's
            # search page) if the resolution finds nothing.
            uses_site = self.type_box.currentText() in VIDEO_TYPES and video_site_id is not None
            if uses_site:
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

    def _resolve_manga_details(self, page_url):
        details = {}
        try:
            details = manga_sites.fetch_manga_details(page_url)
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
    # The Anime counterpart of manga's "picking a suggestion stores the
    # real page url". Anime suggestions come from Cinemeta (the only
    # public anime search API), which knows nothing about the user's
    # Video Website - so the site's own page for the title is resolved
    # separately, right here, against that one site. Deliberately lazy:
    # one lookup for the title actually picked, not a fan-out across
    # every configured site on every keystroke.

    def _on_site_changed(self, _index):
        # Which site this is decides whether the last-watched spinners are
        # yours to edit at all (Stremio syncs its own), so that moves with
        # the dropdown - ahead of the early returns below, every one of
        # which still leaves the site changed.
        self._update_watched_editability()
        if self.type_box.currentText() not in VIDEO_TYPES:
            return
        site_id = self.site_box.currentData()
        title = self.title_combo.currentText().strip()
        if site_id is None:
            # Back to the built-in Stremio option - whatever site page
            # was resolved is wrong for it, and _on_suggestion_selected
            # is what puts the stremio:// link back.
            self._pending_video_identity = None
            self.url_edit.setText("")
            self._set_status_part("site", "")
            return
        self.url_edit.setText("")
        if title:
            self._start_video_site_resolution(site_id, title)

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
        # Doesn't name the site: which one is being asked is already in the
        # dropdown right there, and naming it made the line different for
        # every site while saying the same thing.
        self._set_status_part("site", "Searching...")
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
        self._latest_available = format_episode_progress(total_season, total_episode)
        if total_episode and not self.season_spin.value() and not self.episode_spin.value():
            self.season_spin.setValue(total_season)
            self.episode_spin.setValue(total_episode)
            # The setValue() calls above also trigger _on_progress_hand_
            # edited (which assumes any spinner change is a manual edit) -
            # this line runs after them and wins: this is never your real
            # progress, only ever the latest aired episode, so it should
            # never count as verified.
            self._progress_verified = False
            self._autofilled_progress = True

    def _on_progress_hand_edited(self, _value):
        self._progress_verified = True

    def _progress_is_yours(self) -> bool:
        """Whether the progress being saved should count as really yours,
        and so actually appear on the card (see _progress_display).

        Changing a spinner already says yes. This covers the case that
        does not: an entry whose stored progress is right there in the
        form, that you open and save without nudging anything. Nothing
        marked it verified, so the card stayed blank however many times
        you saved it - and the only way out was to change the number and
        change it back. Pressing Save on a value the form is showing you
        is an assertion that it is yours.

        The exception is a value this form filled in itself during this
        session, which is the latest aired episode rather than anything
        you watched - that keeps its hint and stays unverified."""
        if self._progress_verified:
            return True
        if self._autofilled_progress:
            return False
        return bool(self._progress_text())

    # ------------------------------------------------------------------
    def _progress_text(self):
        if self.type_box.currentText() in MANGA_TYPES:
            value = self.chapter_spin.value()
            if not value and self._original_progress and not parse_chapter_progress(self._original_progress):
                return self._original_progress  # unparsed legacy text, spinner untouched - keep it
            return format_chapter_progress(value)

        season, episode = self.season_spin.value(), self.episode_spin.value()
        if not season and not episode and self._original_progress and parse_episode_progress(self._original_progress) == (0, 0):
            return self._original_progress  # unparsed legacy text, spinners untouched - keep it
        return format_episode_progress(season, episode)

    def _save(self):
        title = self.title_combo.currentText().strip()
        if not title:
            QMessageBox.warning(self, "Tracker", "Title can't be empty.")
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
        has_site = is_manga or is_video
        site_id = self.site_box.currentData() if has_site else None
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
            progress_verified=self._progress_is_yours(),
            latest_available=self._latest_available,
            last_watched_chapter=self.watched_chapter_spin.value() if is_manga else None,
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
