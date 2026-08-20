"""Home dashboard: a "Continue Watching" hero for whatever anime/manga/
series is in progress, plus preview rows for Anime & Manga, Series, Games,
Quick Apps, and Websites. Everything here is derived from the same data
files the other pages read/write (tracker.json's/series.json's
`updated_at`/`status`, games.json's `last_played`) - there's no separate
"recents" state to maintain.
"""

import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from helpers import (game_launch, global_search, images, launchers,
                     lookup_pool, nav_config, storage, theme)
from helpers.widgets import (
    Card, GlassPage, HeroBanner, SideScroller, inform, scroll_area,
    search_field, use_hover_cursor,
)
from windows.link_grid import missing_app_targets, open_link_entry
from windows.tracker import (
    IN_PROGRESS_STATUSES, MANGA_TYPES, POSTER_SIZE, VIDEO_TYPES,
    ContinueCover, attach_continue_cover, format_chapter_progress,
    open_tracker_entry, shows_last_watched,
)

GREETING_REFRESH_MS = 60_000

TRACKER_FILE = "tracker.json"
SERIES_FILE = "series.json"
GAMES_FILE = "games.json"
WEBSITES_FILE = "websites.json"
APPS_FILE = "apps.json"

# No local POSTER_SIZE any more: Home's rows show the same-size cards
# the Anime/Reading/Series pages do (the owner's ask), so the size is
# imported from windows.tracker with the rest of its pieces - one number
# in one place, and main.py's prewarm follows it automatically.
ICON_SIZE = (40, 40)
ROW_ICON_SIZE = (28, 28)

# How long after something is opened from Home its section re-sorts it
# to the front. Not immediate: the card would slide out from under the
# pointer that just clicked it, which reads as a misclick. Games had this
# from the start; Quick Apps and Quick Websites re-sorted on the next
# tick of the event loop instead, so the row under the cursor rearranged
# itself while the click was still being let go of.
RESORT_DELAY_MS = 2500

# No preview limits for the tracker rows any more - see _recent_entries.
GAMES_PREVIEW_LIMIT = 6
QUICK_LIST_LIMIT = 5

# The search field's width here. Wider than a page's own 220px filter box
# because it searches everything rather than one list, and capped so it
# stays a field rather than a banner across a 2048px display.
SEARCH_BAR_WIDTH = 520
# What it may shrink to before the page would rather clip it - narrow
# enough that a 1000px window still shows a usable field.
SEARCH_BAR_MIN_WIDTH = 240

HERO_SLIDE_LIMIT = 4
HERO_SLIDE_INTERVAL_MS = 6000

# Anime opens on the merged watch page now - there is no "anime" page
# key left (see nav_config).
PAGE_FOR_TYPE = {"Anime": "series", "Series": "series",
                 **{t: "manga" for t in MANGA_TYPES}}


class _HeroSignals(QObject):
    # entry id -> local backdrop path, crossing back from a fetch thread.
    backdrop = pyqtSignal(str, str)


class _GameSignals(QObject):
    # game id -> local Steam cover path, back from the lookup pool so
    # the storage write and the redraw stay on the UI thread.
    cover = pyqtSignal(str, str)


def _greeting():
    hour = datetime.now().hour
    if hour < 12:
        return "Good Morning"
    if hour < 18:
        return "Good Afternoon"
    return "Good Evening"


def _clock_text() -> str:
    """The time as "9:24 AM" - 12-hour, no leading zero on the hour,
    uppercase meridiem.

    Assembled rather than strftime'd: the hour without a leading zero is
    "%-I" on Linux and "%#I" on Windows, so either spelling makes this
    platform-specific for the sake of one digit."""
    now = datetime.now()
    return f"{now.hour % 12 or 12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


class HomePage(GlassPage):
    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        self.tracker_entries = storage.load(TRACKER_FILE, [])
        self.series_entries = storage.load(SERIES_FILE, [])
        # Re-extracts any game icon that was never captured or whose
        # cached file has gone missing, rather than silently showing a
        # letter avatar here until the Games page is next opened.
        self.games = launchers.backfill_missing_icons(storage.load(GAMES_FILE, []))
        self.games = launchers.backfill_launch_commands(self.games)
        self.websites = storage.load(WEBSITES_FILE, [])
        self.apps = storage.load(APPS_FILE, [])
        self._game_signals = _GameSignals()
        self._game_signals.cover.connect(self._on_game_cover)

        # Sections the user has hidden *and* asked to keep off Home (see
        # nav_config.home_hidden_sections). Applied to what gets drawn
        # rather than to the loaded lists above, which stay whole -
        # they're what gets written back when an entry is opened from
        # here, and saving a filtered copy would delete the rest.
        hidden = nav_config.home_hidden_sections()
        self._hidden_home_sections = hidden
        self._home_tracker_entries = [
            entry for entry in self.tracker_entries
            if not ("anime" in hidden and entry.get("type") == "Anime")
            and not ("manga" in hidden and entry.get("type") in MANGA_TYPES)
        ]
        self._home_series_entries = [] if "series" in hidden else self.series_entries

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        # No #Panel background here (unlike other pages) - Home's own
        # pieces (the hero's cards, each section's #SectionBox) already
        # carry their own framing, so a big panel behind all of them
        # just added an extra layer of background covering the whole
        # page underneath.
        panel = QFrame(objectName="Bare")
        outer.addWidget(panel)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 20, 24, 24)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        # Symmetric, not just a right-side clearance gap for the
        # scrollbar - an asymmetric margin here would shift every fixed-
        # width centered block on this page (the hero) slightly left of
        # true-center relative to the panel, even though it measures as
        # centered within its own now-narrower-on-one-side container.
        body_layout.setContentsMargins(8, 0, 8, 0)
        body_layout.setSpacing(22)
        # The hero's cover+text block is a fixed width centered against
        # this viewport (see _build_hero) - reserving the scrollbar's
        # width unconditionally keeps that centered regardless of
        # whether this page's content is currently tall enough to
        # actually need scrolling (see scroll_area's always_show_vbar).
        panel_layout.addWidget(scroll_area(body, always_show_vbar=True))

        # Greeting on the left, the app-wide search on the same line.
        # Only this page carries the field: it searches everything, and
        # Home is the page that is already about everything. Ctrl+K
        # reaches the same panel from anywhere else.
        header_row = QHBoxLayout()
        header = QVBoxLayout()
        header.setSpacing(2)
        self.greeting_label = QLabel(f"{_greeting()} \U0001F44B", objectName="PanelTitle")
        header.addWidget(self.greeting_label)
        header.addWidget(QLabel("Here's what's going on", objectName="PanelSubtitle"))
        greeting_box = QWidget(objectName="Bare")
        greeting_box.setLayout(header)
        header_row.addWidget(greeting_box)

        header_row.addStretch()
        # A range, not a fixed width. Fixed at 520 the row's minimum came
        # to 1733px - the greeting, the field and the greeting-width
        # spacer that balances it - so on a 1400px window the row
        # overflowed the viewport and the field was clipped off the right
        # edge rather than sitting centred.
        self.search_bar = search_field("Search everything...")
        self.search_bar.setMinimumWidth(SEARCH_BAR_MIN_WIDTH)
        self.search_bar.setMaximumWidth(SEARCH_BAR_WIDTH)
        self.search_bar.textEdited.connect(self._search_bar_typed)
        self.search_bar.installEventFilter(self)
        # The results list under the field, built on the first keystroke
        # and closed with the query.
        self._search_results = None
        # Top-aligned, which puts it on the greeting's own line: the
        # block under it is two lines (greeting plus subtitle), so
        # centring the field against the block dropped it into the gap
        # between them - measured 11px below the greeting's centre, and
        # visibly so. The field and the greeting line are within a pixel
        # of the same height, so aligning their tops aligns their middles.
        header_row.addWidget(self.search_bar, stretch=3, alignment=Qt.AlignmentFlag.AlignTop)
        header_row.addStretch()
        # Balances the greeting's width on the right, so the field lands
        # centred in the page rather than centred in what is left over
        # beside the greeting. Taken from the greeting's own hint at
        # build time; the stretches either side do the rest. It carries
        # the clock now - the balance was already the right shape and
        # width for it, and a second widget beside it would have thrown
        # the centring out by its own width.
        clock_box = QWidget(objectName="Bare")
        clock_box.setMaximumWidth(greeting_box.sizeHint().width())
        clock_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        clock_layout = QVBoxLayout(clock_box)
        # Default margins, deliberately not zeroed: the greeting sits in
        # a layout with the same defaults, and its 9px top margin is
        # exactly how far down the row its first line starts. Measured -
        # zeroing these put the clock 9px above the greeting's line.
        # Its own style rather than the greeting's #PanelTitle: same
        # weight and colour, a few points larger - see theme.py.
        self.clock_label = QLabel(_clock_text(), objectName="HomeClock")
        self.clock_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        clock_layout.addWidget(self.clock_label)
        # AlignTop, like the search field: the block on the left is two
        # lines, so centring against it would drop the clock into the gap
        # between the greeting and its subtitle.
        header_row.addWidget(clock_box, stretch=1, alignment=Qt.AlignmentFlag.AlignTop)
        body_layout.addLayout(header_row)

        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(self._refresh_greeting)
        self._greeting_timer.start(GREETING_REFRESH_MS)

        # The clock re-arms itself to the next minute boundary instead of
        # riding the greeting's fixed 60s interval: a timer started
        # mid-minute would show each new minute up to 59s late, which is
        # visible against any other clock on the screen.
        self._clock_timer = QTimer(self)
        self._clock_timer.setSingleShot(True)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._arm_clock()

        body_layout.addWidget(self._build_hero())

        # Preview sections are ordered to match the sidebar's (draggable,
        # user-customizable) nav order - a section spanning two nav
        # entries (Anime & Manga, or the Apps/Websites row) sorts by
        # whichever of the two sits earlier.
        sections = []

        # tracker.json holds only the reading types now - Anime moved to
        # series.json with the page merge (main._merge_anime_into_series)
        # - so this section is plainly Reading.
        reading_recent = self._recent_entries(self._home_tracker_entries)
        if reading_recent:
            pos = nav_config.nav_position("manga")
            sections.append((pos, self._build_section(
                "Reading", self._build_poster_grid(reading_recent))))

        series_recent = self._recent_entries(self._home_series_entries)
        if series_recent:
            pos = nav_config.nav_position("series")
            # "Watching", not the sidebar's "Watch": Home's rows keep the
            # longer headings (the owner's ask), pairing with "Reading".
            sections.append((pos, self._build_section(
                "Watching", self._build_poster_grid(series_recent))))

        # Kept so the row can be redrawn in place the moment a game is
        # launched from it (see _refresh_games_row) - the order here is
        # "most recently played first", and it used to be worked out only
        # when the page was built.
        self._games_section = None
        self._games_grid = None
        self._quick_lists = {}

        recent_games = [] if "games" in hidden else self._recent_games()
        if recent_games:
            pos = nav_config.nav_position("games")
            self._backfill_game_covers(recent_games)
            self._games_grid = self._build_games_grid(recent_games)
            self._games_section = self._build_section("Games", self._games_grid)
            sections.append((pos, self._games_section))

        show_apps = bool(self.apps) and "apps" not in hidden
        show_websites = bool(self.websites) and "websites" not in hidden
        lists_row = QHBoxLayout()
        lists_row.setSpacing(16)
        if show_apps:
            lists_row.addWidget(self._build_quick_list("Quick Apps", self.apps, APPS_FILE))
        if show_websites:
            lists_row.addWidget(self._build_quick_list("Websites", self.websites, WEBSITES_FILE))
        if show_apps or show_websites:
            row_wrap = QWidget(objectName="Bare")
            row_wrap.setLayout(lists_row)
            pos = min(nav_config.nav_position("apps"), nav_config.nav_position("websites"))
            sections.append((pos, row_wrap))

        for _, widget in sorted(sections, key=lambda pair: pair[0]):
            body_layout.addWidget(widget)

        body_layout.addStretch()

    def _search_bar_typed(self, text):
        """Results appear under the field as it is typed into.

        The panel is a list, not a second search box: it opens beneath
        this field, follows it, and closes when there is nothing to
        show."""
        if not text.strip():
            self._close_search_results()
            return
        if self._search_results is None:
            self._search_results = global_search.GlobalSearch(
                self.window(), anchor=self.search_bar)
            # The panel can close without this page asking it to - it
            # closes itself when a result is clicked - and it deletes
            # itself when it does. Dropping the reference then is what
            # keeps the next keystroke from talking to a deleted panel.
            self._search_results.closed.connect(self._forget_search_results)
            self._search_results.show()
        self._search_results.set_query(text)

    def _forget_search_results(self):
        self._search_results = None

    def _close_search_results(self):
        if self._search_results is not None:
            self._search_results.close()
            self._search_results = None

    def eventFilter(self, obj, event):
        """Up/Down/Enter/Escape in the field drive the list under it -
        the field keeps the focus while the list is being driven, which
        is why the panel is shown without activating. Escape is the one
        that ends it, focus included."""
        if obj is self.search_bar and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if self._search_results is not None:
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    self._search_results.move_selection(1 if key == Qt.Key.Key_Down else -1)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._search_results.open_current()
                    self.search_bar.clear()
                    self._close_search_results()
                    return True
            if key == Qt.Key.Key_Escape:
                # Escape leaves the field, it doesn't just empty it -
                # measured, clearing alone left the caret blinking in a
                # box the user had just said they were done with, while
                # the same key on a tracker page dropped focus onto the
                # page. Same ending on both now.
                self.search_bar.clear()
                self._close_search_results()
                self.search_bar.clearFocus()
                self.setFocus()
                return True
        return super().eventFilter(obj, event)

    def _refresh_greeting(self):
        self.greeting_label.setText(f"{_greeting()} \U0001F44B")

    def _tick_clock(self):
        self.clock_label.setText(_clock_text())
        self._arm_clock()

    def _arm_clock(self):
        """Fire again just after the next minute turns over. The 50ms is
        so the timer lands on the far side of the boundary rather than a
        hair before it, which would re-arm for another whole minute
        showing the minute that has just ended."""
        now = datetime.now()
        self._clock_timer.start(
            (60 - now.second) * 1000 - now.microsecond // 1000 + 50)

    # ------------------------------------------------------------------
    def _all_trackable_entries(self):
        """Everything Home may draw on - the hidden-from-Home sections
        already filtered out, so the "Continue" carousel can't headline
        a section the user has taken off this page."""
        return self._home_tracker_entries + self._home_series_entries

    def _in_progress_entries(self):
        entries = [e for e in self._all_trackable_entries() if e.get("status") in IN_PROGRESS_STATUSES]
        return sorted(entries, key=lambda e: e.get("updated_at") or "", reverse=True)

    def _recent_entries(self, entries):
        """Every entry, most recently touched first - no preview cap any
        more (the owner's ask: an added entry must always appear here).
        The row scrolls sideways instead of truncating, the same answer
        the tracker pages give a long section."""
        return sorted(entries, key=lambda e: e.get("updated_at") or "",
                      reverse=True)

    def _recent_games(self):
        played = [g for g in self.games if g.get("last_played")]
        played = sorted(played, key=lambda g: g["last_played"], reverse=True)
        rest = [g for g in self.games if not g.get("last_played")]
        return (played + rest)[:GAMES_PREVIEW_LIMIT]

    def _recent_links(self, entries):
        """Same "sort by latest used" rule as Anime/Manga/Series'
        updated_at (see _recent_entries) - Quick Apps/Websites previews
        aren't a static list anymore, they float whatever was opened
        most recently to the top."""
        return sorted(entries, key=lambda e: e.get("last_used") or "", reverse=True)

    def _progress_meta_text(self, entry):
        """Short progress caption for a tracker/series entry - "Ch N" for
        manga (your own last-*read* chapter, not the site's latest), or
        the verified watch progress for anime/series. Shared by the
        poster grid and the hero peeks, which both show this under a
        title rather than the hero's own fuller "Chapter N" phrasing."""
        # An entry whose "Show Last Watched" tick is off hides the number
        # here too - Home showing what the tracker page deliberately
        # doesn't would read as one of the two being broken.
        if not shows_last_watched(entry):
            return ""
        if entry["type"] in MANGA_TYPES:
            watched = entry.get("last_watched_chapter")
            return f"Ch {format_chapter_progress(watched)}" if watched else entry["type"]
        if entry.get("progress_verified") and entry.get("progress"):
            return entry["progress"]
        return ""

    # ------------------------------------------------------------------
    def _build_section(self, title, content_widget):
        section = QWidget(objectName="Bare")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(QLabel(title, objectName="SectionTitle"))
        layout.addWidget(content_widget)
        return section

    def _build_hero(self):
        """The continue hero, Harbor's shape (the owner's ask): one
        full-width banner - the current title's backdrop under a scrim,
        the title and its progress over it, Continue beside a details
        button, the pagination dashes underneath - rotating through the
        in-progress entries. Replaces the fixed-width peek carousel."""
        self._hero_entries = self._in_progress_entries()[:HERO_SLIDE_LIMIT]
        if not self._hero_entries:
            hero = QFrame(objectName="Hero")
            layout = QHBoxLayout(hero)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.addWidget(QLabel(
                "Nothing in progress yet - add an anime, manga, or series "
                "to start tracking.", objectName="Muted"))
            return hero

        self._hero_index = 0
        self._hero_backdrops = {}
        self._hero_signals = _HeroSignals()
        self._hero_signals.backdrop.connect(self._on_hero_backdrop)

        banner = HeroBanner()
        self._hero_banner = banner
        column = QVBoxLayout(banner)
        column.setContentsMargins(36, 22, 36, 16)
        column.setSpacing(10)

        chip_row = QHBoxLayout()
        self._hero_chip = QLabel("")
        self._hero_chip.setStyleSheet(
            f"color: {theme.ON_ACCENT}; background: {theme.ACCENT_GRADIENT};"
            f" border-radius: {theme.RADIUS_SM}px; padding: 3px 10px;"
            f" font-size: 8.5pt; font-weight: 700; letter-spacing: 1px;")
        chip_row.addWidget(self._hero_chip)
        chip_row.addStretch(1)
        column.addLayout(chip_row)
        column.addStretch(1)

        self._hero_title = QLabel("")
        self._hero_title.setWordWrap(True)
        self._hero_title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 27pt; font-weight: 800;"
            f" background: transparent;")
        column.addWidget(self._hero_title)

        self._hero_meta = QLabel("")
        self._hero_meta.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; font-weight: 600;"
            f" background: transparent;")
        column.addWidget(self._hero_meta)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.setContentsMargins(0, 8, 0, 0)
        self._hero_continue = QPushButton("▶  Continue", objectName="Accent")
        self._hero_continue.setFixedHeight(46)
        use_hover_cursor(self._hero_continue)
        self._hero_continue.clicked.connect(lambda: self._hero_open(resume=True))
        buttons.addWidget(self._hero_continue)
        # Harbor's second action: an outlined pill the backdrop reads
        # through, opening the episode/chapter list (the details page).
        self._hero_view = QPushButton("")
        self._hero_view.setFixedHeight(46)
        self._hero_view.setStyleSheet(
            f"QPushButton {{ background: {theme.rgba(theme.BG, 160)};"
            f" color: {theme.TEXT}; border: 1px solid {theme.TEXT_MUTED};"
            f" border-radius: {theme.RADIUS}px; padding: 8px 18px;"
            f" font-weight: 700; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
            f" color: {theme.ACCENT}; }}")
        use_hover_cursor(self._hero_view)
        self._hero_view.clicked.connect(lambda: self._hero_open(resume=False))
        buttons.addWidget(self._hero_view)
        buttons.addStretch(1)
        column.addLayout(buttons)

        # The pagination dashes, centred at the banner's foot - each one
        # jumps straight to its slide; the active one is longer and lit.
        dash_row = QHBoxLayout()
        dash_row.setContentsMargins(0, 10, 0, 0)
        dash_row.setSpacing(6)
        dash_row.addStretch(1)
        self._hero_dashes = []
        for index in range(len(self._hero_entries)):
            dash = QPushButton("")
            dash.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            use_hover_cursor(dash)
            dash.clicked.connect(lambda _checked=False, i=index: self._jump_hero(i))
            dash_row.addWidget(dash)
            self._hero_dashes.append(dash)
        dash_row.addStretch(1)
        column.addLayout(dash_row)

        # The banner's body is the details page, like a card's body.
        banner.clicked.connect(lambda: self._hero_open(resume=False))

        self._hero_timer = QTimer(self)
        self._hero_timer.timeout.connect(self._advance_hero)
        if len(self._hero_entries) > 1:
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)

        # Every slide's backdrop starts fetching now, its own thread each
        # (they are one cached file apiece after the first run), so
        # rotating never waits on the network.
        for entry in self._hero_entries:
            threading.Thread(target=self._hero_backdrop_worker,
                             args=(dict(entry),), daemon=True).start()
        self._show_hero_slide(0, fade=False)
        return banner

    def _hero_entry(self):
        return self._hero_entries[self._hero_index % len(self._hero_entries)]

    def _hero_open(self, resume):
        # Re-armed, not left running: the slide must not advance the
        # moment the player or details page closes over it.
        if self._hero_timer.isActive():
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)
        self._continue_entry(self._hero_entry(), resume=resume)

    def _advance_hero(self):
        self._show_hero_slide((self._hero_index + 1) % len(self._hero_entries))

    def _jump_hero(self, index):
        if self._hero_timer.isActive():
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)
        self._show_hero_slide(index)

    def _show_hero_slide(self, index, fade=True):
        self._hero_index = index
        entry = self._hero_entry()
        reading = entry["type"] in MANGA_TYPES
        self._hero_chip.setText("CONTINUE READING" if reading
                                else "CONTINUE WATCHING")
        self._hero_title.setText(entry["title"])
        meta_bits = [self._progress_meta_text(entry) or entry.get("status") or "",
                     str(entry.get("type") or "")]
        self._hero_meta.setText("   ·   ".join(bit for bit in meta_bits if bit))
        if reading:
            self._hero_view.setText("View Chapters")
        elif entry.get("type") == "Movie":
            self._hero_view.setText("View Details")
        else:
            self._hero_view.setText("View Episodes")
        self._hero_banner.set_backdrop(
            self._hero_backdrops.get(entry.get("id")), fade=fade)
        for i, dash in enumerate(self._hero_dashes):
            active = i == index
            dash.setFixedSize(30 if active else 14, 6)
            dash.setStyleSheet(
                f"QPushButton {{ background:"
                f" {theme.ACCENT if active else theme.SURFACE_ACTIVE};"
                f" border: none; border-radius: 3px; padding: 0px; }}"
                f"QPushButton:hover {{ background:"
                f" {theme.ACCENT if active else theme.TEXT_MUTED}; }}")

    def _hero_backdrop_worker(self, entry):
        """One slide's ground, off the UI thread: TMDB's backdrop by
        IMDb id for the video types, AniList's banner for reading - the
        same two sources the details page grounds itself with. Never
        raises; a title with no landscape art anywhere just keeps the
        banner's flat panel."""
        entry_id = str(entry.get("id") or "")
        try:
            if entry.get("type") in MANGA_TYPES:
                from helpers import anilist
                url = anilist.fetch_manga_artwork(entry.get("title") or "")
                found = images.download(url) if url else None
                if found:
                    self._hero_signals.backdrop.emit(entry_id, str(found))
                return
            if not entry.get("imdb_id"):
                return
            from helpers import artwork
            # Both sizes, in that order - the details page's pattern,
            # and the fix for a soft slider: the small w780 copy used to
            # be taken with `or`, so the full-resolution original was
            # never fetched at all and a ~1200px-wide banner was drawn
            # from a 780px image. Now the small one fills the banner
            # immediately and the original replaces it when it lands.
            quick = artwork.backdrop_fast_path(entry)
            if quick:
                self._hero_signals.backdrop.emit(entry_id, str(quick))
            full = artwork.backdrop_path(entry)
            if full:
                self._hero_signals.backdrop.emit(entry_id, str(full))
        except Exception:
            return          # no art anywhere just keeps the flat panel

    def _on_hero_backdrop(self, entry_id, path):
        # Whether this slide already had art: the sharp original landing
        # over the small copy is the *same picture*, so it swaps without
        # a cross-fade - dissolving a photo into itself reads as a
        # flicker. Only a genuinely new slide fades.
        upgrade = entry_id in self._hero_backdrops
        self._hero_backdrops[entry_id] = path
        if str(self._hero_entry().get("id") or "") != entry_id:
            return
        try:
            self._hero_banner.set_backdrop(path, fade=not upgrade)
        except RuntimeError:
            pass    # the page was torn down under the fetch

    def _continue_entry(self, entry, resume=True):
        """`resume=False` is the body of a reading card - it opens the
        chapter list instead of the chapter that was left open. The hero's
        own Continue button and the round button on a poster's cover both
        pass True, which is what every card here did before there were
        two targets on one."""
        if not open_tracker_entry(self, entry, resume=resume):
            self.app.navigate_to(PAGE_FOR_TYPE.get(entry["type"], "series"))

    # ------------------------------------------------------------------
    def _build_poster_grid(self, entries):
        # No #SectionBox frame here (unlike the Apps/Websites lists
        # below) - a wall of poster art doesn't need a background box
        # to read as a group the way rows of icon+text do.
        #
        # One sideways-scrolling strip, not a fixed grid: the rows carry
        # *every* entry now (see _recent_entries), so a full row scrolls
        # behind SideScroller's ‹ › arrows exactly like a tracker page's
        # section strips (tracker._build_section_strip is the model).
        box = QWidget(objectName="Bare")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        strip = QWidget(objectName="Bare")
        grid = QHBoxLayout(strip)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)

        for index, entry in enumerate(entries):
            # One shared #SectionBox frame for the whole grid (below)
            # rather than one behind every poster - #HomeItem drops the
            # per-item frame while keeping a hover highlight.
            card = Card(hoverable=True)
            card.setObjectName("HomeItem")
            card.setFixedWidth(POSTER_SIZE[0] + 20)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 8, 6, 8)

            pixmap = images.thumbnail_or_avatar(entry.get("cover_path"),
                                                entry["title"], POSTER_SIZE)
            # Every tracker medium carries the same two targets here now
            # (the owner's ask - anime/series/movies used to be a single
            # target): the round ring on the hovered cover resumes where
            # that entry stopped, the rest of the card opens the episode/
            # chapter list (the details page).
            continuable = entry["type"] in MANGA_TYPES + VIDEO_TYPES
            if continuable:
                cover = ContinueCover(
                    pixmap, POSTER_SIZE,
                    lambda en=entry: self._continue_entry(en, resume=True))
            else:
                cover = QLabel()
                cover.setFixedSize(*POSTER_SIZE)
                cover.setPixmap(pixmap)
            card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

            name = QLabel(entry["title"], objectName="CardTitle")
            name.setWordWrap(True)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            card_layout.addWidget(name)

            meta_text = self._progress_meta_text(entry)
            if meta_text:
                meta = QLabel(meta_text, objectName="CardMeta")
                meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                card_layout.addWidget(meta)

            card.clicked.connect(
                lambda en=entry, r=not continuable: self._continue_entry(en, resume=r))
            if continuable:
                # After every child exists - the relay watches each of
                # them, since each takes the hover off the card as the
                # pointer crosses onto it.
                attach_continue_cover(card, cover)
            grid.addWidget(card)
        grid.addStretch()

        # Same recipe as tracker._build_section_strip: the strip in its
        # own horizontal scroll area, fixed to the cards' height (without
        # that it claims the whole page), vertical wheel left for the
        # page, and SideScroller's arrows laid over the two ends.
        area = QScrollArea(objectName="Bare")
        area.setWidget(strip)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.viewport().setAutoFillBackground(False)
        strip.setAutoFillBackground(False)
        strip.adjustSize()
        area.setFixedHeight(strip.sizeHint().height()
                            + area.horizontalScrollBar().sizeHint().height())
        outer.addWidget(SideScroller(area))
        return box

    def _build_games_grid(self, games):
        # No #SectionBox frame here either - see _build_poster_grid.
        #
        # Poster tiles at the watch cards' own size (the owner's ask),
        # drawing the Steam cover helpers/game_art resolves rather than
        # the extracted .exe icon - a 32px shell icon stretched across a
        # 160px tile is mush. A game with no cover yet keeps the letter
        # avatar and gains its art the next time the Games page runs its
        # backfill.
        box = QWidget(objectName="Bare")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        strip = QWidget(objectName="Bare")
        grid = QHBoxLayout(strip)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)

        for game in games:
            card = Card(hoverable=True)
            card.setObjectName("HomeItem")
            card.setFixedWidth(POSTER_SIZE[0] + 20)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 8, 6, 8)

            cover = QLabel()
            cover.setFixedSize(*POSTER_SIZE)
            cover.setPixmap(images.thumbnail_or_avatar(
                game.get("cover"), game["name"], POSTER_SIZE))
            card_layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

            name = QLabel(game["name"], objectName="CardTitle")
            name.setWordWrap(True)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            card_layout.addWidget(name)

            card.clicked.connect(lambda g=game: self._launch_game(g))
            grid.addWidget(card)
        grid.addStretch()

        # The poster rows' own recipe (see _build_poster_grid): at this
        # width a full library no longer fits across the page, so the
        # row scrolls sideways behind SideScroller's arrows instead of
        # being cut off at the edge.
        area = QScrollArea(objectName="Bare")
        area.setWidget(strip)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.viewport().setAutoFillBackground(False)
        strip.setAutoFillBackground(False)
        strip.adjustSize()
        area.setFixedHeight(strip.sizeHint().height()
                            + area.horizontalScrollBar().sizeHint().height())
        outer.addWidget(SideScroller(area))
        return box

    def _launch_game(self, game):
        try:
            game_launch.run(game)
        except OSError as exc:
            inform(self, "Games", f"Couldn't launch this game:\n{exc}")
            return
        game["last_played"] = storage.now_iso()
        self._resort_after_delay(self._refresh_games_row)
        # Only this game's own field, not a wholesale save of the copy
        # Home loaded when it was built - that snapshot goes stale the
        # moment the Games page (or a Settings import) touches the list,
        # and writing it back would undo their changes.
        storage.update_entry(GAMES_FILE, game.get("id"), {"last_played": game["last_played"]})

    def _resort_after_delay(self, refresh):
        """Run `refresh` once, RESORT_DELAY_MS after whatever was just
        opened - back to the front of its section, but not under the
        pointer that clicked it: by then the app or game is coming up and
        the cursor has left the card.

        A timer parented to the page rather than QTimer.singleShot: a
        Home navigated away from before it fires takes the timer down
        with it, where a bare singleShot would fire into a page Qt has
        already deleted."""
        resort = QTimer(self)
        resort.setSingleShot(True)
        resort.timeout.connect(refresh)
        resort.start(RESORT_DELAY_MS)

    # ------------------------------------------------------------------
    def _swap_in(self, old, new):
        """Put `new` where `old` sits, in whatever layout is holding it.

        Home is built once in __init__, so anything that changes what a
        row should show (playing a game, opening an app) either redraws
        that row in place or waits until the page is next opened. This is
        the in-place half - one row rebuilt, rather than the whole page
        torn down and the scroll position with it."""
        parent = old.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is None:
            return old
        layout.replaceWidget(old, new)
        # hide() as well as deleteLater(): the delete is deferred to the
        # event loop, and until then the old widget goes on painting over
        # the new one (same trap as the grid pages' _refresh_grid).
        old.hide()
        old.deleteLater()
        return new

    def _refresh_games_row(self):
        if self._games_grid is None:
            return
        self._games_grid = self._swap_in(
            self._games_grid, self._build_games_grid(self._recent_games()))

    def _backfill_game_covers(self, games):
        """Resolve Steam covers for the games this row draws.

        The Games page does this for the whole library; Home does it for
        the handful it shows, because Home is very often the only page
        visited and a row of letter avatars is exactly what the poster
        tiles were meant to replace. game_art caches hits and
        authoritative misses on disk, so this costs a stat per game
        after the first run."""
        wanted = [g for g in games
                  if not (g.get("cover") and Path(g["cover"]).exists())]
        if not wanted:
            return
        for game in wanted:
            lookup_pool.submit(self._game_cover_worker, game.get("id"),
                               game.get("name") or "", game.get("path"))

    def _game_cover_worker(self, game_id, name, install_path):
        # Never raises - an exception here kills the pool's worker.
        try:
            from helpers import game_art
            path = game_art.fetch_cover(name, install_path=install_path)
        except Exception:
            path = None
        if path and game_id:
            self._game_signals.cover.emit(game_id, str(path))

    def _on_game_cover(self, game_id, path):
        game = next((g for g in self.games if g.get("id") == game_id), None)
        if game is None:
            return
        game["cover"] = path
        # One field on one entry - the Games page and Settings hold
        # their own copies of this file (see _launch_game).
        storage.update_entry(GAMES_FILE, game_id, {"cover": path})
        # Redrawn rather than one pixmap swapped: this row is rebuilt
        # wholesale anyway (_refresh_games_row) and holding label
        # references across that rebuild is what .claude/rules/ui.md
        # warns against.
        self._refresh_games_row()

    def _refresh_quick_list(self, data_file):
        """Redraw one Quick Apps/Websites list after something in it was
        opened, so it re-sorts to most-recently-used - on the same delay
        the Games row uses (see _resort_after_delay)."""
        found = self._quick_lists.get(data_file)
        if found is None:
            return
        frame, title, entries = found
        # _build_quick_list re-registers the new frame under this same
        # key, so the next refresh finds the widget that is on screen.
        self._swap_in(frame, self._build_quick_list(title, entries, data_file))

    def _build_quick_list(self, title, entries, data_file):
        frame = QWidget(objectName="SectionBox")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(QLabel(title, objectName="CardTitle"))

        for entry in self._recent_links(entries)[:QUICK_LIST_LIMIT]:
            row = Card(hoverable=True)
            row.setObjectName("HomeItem")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setSpacing(10)

            icon = QLabel()
            icon.setFixedSize(*ROW_ICON_SIZE)
            icon.setPixmap(images.thumbnail_or_avatar(entry.get("image"), entry["name"], ROW_ICON_SIZE))
            row_layout.addWidget(icon)
            row_layout.addWidget(QLabel(entry["name"]))
            row_layout.addStretch()

            # An app whose program has been uninstalled or moved reads as
            # broken on the Apps page and read as fine here, on the page
            # it is most likely to be clicked from. A row is a fraction
            # of a card's height, so it gets the short form of the same
            # answer: the word, in the same colour, with the paths in the
            # tooltip.
            missing = missing_app_targets(entry)
            if missing:
                flag = QLabel("Not found")
                flag.setStyleSheet(f"color: {theme.DANGER}; background: transparent;")
                row_layout.addWidget(flag)
                row.setToolTip("Not found:\n" + "\n".join(missing))

            row.clicked.connect(lambda en=entry, df=data_file, es=entries: self._open_quick_link(en, title, df, es))
            layout.addWidget(row)

        self._quick_lists[data_file] = (frame, title, entries)
        return frame

    def _open_quick_link(self, entry, title, data_file, entries):
        open_link_entry(self, entry, title)
        entry["last_used"] = storage.now_iso()
        # This entry's field only - the Apps/Websites pages hold their
        # own copy of the same file, so writing Home's whole list back
        # would undo anything they'd changed since Home was built (same
        # reason games.json edits go through update_entry).
        storage.update_entry(data_file, entry.get("id"), {"last_used": entry["last_used"]})
        # Same delayed live re-sort the Games row gets. The delay also
        # covers what defer_grid_rebuild's zero-delay timer was here for
        # - the row that was clicked is one of the widgets this rebuild
        # replaces, so it cannot be deleted from inside its own click
        # handler.
        self._resort_after_delay(lambda: self._refresh_quick_list(data_file))
