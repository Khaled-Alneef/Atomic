"""Home dashboard: a "Continue Watching" hero for whatever anime/manga/
series is in progress, plus preview rows for Anime & Manga, Series, Games,
Quick Apps, and Websites. Everything here is derived from the same data
files the other pages read/write (tracker.json's/series.json's
`updated_at`/`status`, games.json's `last_played`) - there's no separate
"recents" state to maintain.
"""

import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QParallelAnimationGroup, QPropertyAnimation, QRect,
    QTimer, Qt, pyqtSignal,
)
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from helpers import (child_process, global_search, images, launchers,
                     nav_config, storage, theme)
from helpers.widgets import (
    Card, GlassPage, hold_hover_cursor, release_hover_cursor, scroll_area,
    search_field,
)
from windows.link_grid import missing_app_targets, open_link_entry
from windows.tracker import (
    IN_PROGRESS_STATUSES, MANGA_TYPES, format_chapter_progress,
    open_tracker_entry, shows_last_watched,
)

GREETING_REFRESH_MS = 60_000

TRACKER_FILE = "tracker.json"
SERIES_FILE = "series.json"
GAMES_FILE = "games.json"
WEBSITES_FILE = "websites.json"
APPS_FILE = "apps.json"

POSTER_SIZE = (78, 104)
HERO_COVER_SIZE = (156, 208)
ICON_SIZE = (40, 40)
ROW_ICON_SIZE = (28, 28)

TRACKER_PREVIEW_LIMIT = 6
SERIES_PREVIEW_LIMIT = 6
GAMES_PREVIEW_LIMIT = 6
QUICK_LIST_LIMIT = 5

# The search field's width here. Wider than a page's own 220px filter box
# because it searches everything rather than one list, and capped so it
# stays a field rather than a banner across a 2048px display.
SEARCH_BAR_WIDTH = 520
# What it may shrink to before the page would rather clip it - narrow
# enough that a 1000px window still shows a usable field.
SEARCH_BAR_MIN_WIDTH = 240

HERO_CONTENT_WIDTH = 620
HERO_SLIDE_LIMIT = 4
HERO_SLIDE_INTERVAL_MS = 5000
HERO_SLIDE_ANIM_MS = 320
# The active slide's cover+text+button block sits on its own framed
# card (same #Card look the peeks use below), padded out beyond the
# cover's own HERO_COVER_SIZE so the frame reads as a background for
# the whole block rather than just a border around the cover.
HERO_SLIDE_PADDING = 18
HERO_SLIDE_HEIGHT = HERO_COVER_SIZE[1] + HERO_SLIDE_PADDING * 2
HERO_COVER_TEXT_GAP = 20   # cover -> text column gap in the active slide
# Left/right breathing room for the Continue button, which otherwise
# spans its whole column edge to edge.
HERO_CONTINUE_INSET = 24
HERO_PEEK_GAP = 14
# The previous/next entries flanking the active slide are that same
# slide's exact layout at half scale (rendered scaled, not re-laid-out
# smaller) - the size difference alone reads as "further back", so the
# active slide doesn't need to compete with a same-size neighbor.
#
# Deriving the peek size by halving BOTH of the active slide's own
# dimensions - rather than composing it from its own cover/padding
# constants - is what keeps the two rects the same shape, and that
# matters: QPropertyAnimation interpolates a QRect's width and height
# independently, so tweening between two differently-shaped rects makes
# every frame in between some third shape. With a scaled pixmap that
# means a non-uniform stretch - a portrait cover visibly fattening
# toward square as it grows. Same aspect ratio at both ends keeps the
# whole grow/shrink a clean uniform zoom instead.
HERO_PEEK_FRAME_SIZE = (HERO_CONTENT_WIDTH // 2, HERO_SLIDE_HEIGHT // 2)
# Card pixmaps are rendered at this multiple of the *active slide's*
# full size, so they stay sharp all the way up to it rather than only
# at the smaller peek size they spend most of their time at.
HERO_CARD_SUPERSAMPLE = 2

# The carousel's 3 slots (peek/mid/peek) plus 2 off-stage holding spots
# (further out on each side) that an entering peek starts from / an
# exiting one animates into before being deleted - all laid out once on
# a fixed "stage" widget so _transition_hero can animate real widgets'
# geometry directly between them instead of juggling a viewport +
# separately-positioned peeks (see _transition_hero).
HERO_STAGE_WIDTH = HERO_PEEK_FRAME_SIZE[0] * 2 + HERO_PEEK_GAP * 2 + HERO_CONTENT_WIDTH
HERO_STAGE_HEIGHT = HERO_SLIDE_HEIGHT
_HERO_PEEK_TOP = (HERO_STAGE_HEIGHT - HERO_PEEK_FRAME_SIZE[1]) // 2
HERO_LEFT_RECT = QRect(0, _HERO_PEEK_TOP, *HERO_PEEK_FRAME_SIZE)
HERO_MID_RECT = QRect(HERO_PEEK_FRAME_SIZE[0] + HERO_PEEK_GAP, 0, HERO_CONTENT_WIDTH, HERO_SLIDE_HEIGHT)
HERO_RIGHT_RECT = QRect(
    HERO_PEEK_FRAME_SIZE[0] + HERO_PEEK_GAP + HERO_CONTENT_WIDTH + HERO_PEEK_GAP, _HERO_PEEK_TOP,
    *HERO_PEEK_FRAME_SIZE)
HERO_EXIT_LEFT_RECT = QRect(-(HERO_PEEK_FRAME_SIZE[0] + HERO_PEEK_GAP), _HERO_PEEK_TOP, *HERO_PEEK_FRAME_SIZE)
HERO_EXIT_RIGHT_RECT = QRect(HERO_STAGE_WIDTH + HERO_PEEK_GAP, _HERO_PEEK_TOP, *HERO_PEEK_FRAME_SIZE)

PAGE_FOR_TYPE = {"Anime": "anime", "Series": "series", **{t: "manga" for t in MANGA_TYPES}}


class _HeroCardLabel(QLabel):
    """A clickable, pixmap-backed carousel card - scaledContents makes it
    stretch the pixmap to fill whatever geometry it's given, so animating
    its `geometry` (see _transition_hero) smoothly shrinks/grows it while
    it moves. A plain widget with a laid-out cover+text row can't do this:
    its children have fixed pixel sizes and just get clipped/reflowed,
    not visually scaled, when the parent's geometry is animated - hence
    rendering the card to a pixmap first (see _build_hero_card_pixmap,
    which supersamples so this stays sharp when stretched up toward the
    much larger active-slide size, not just shrunk down).

    The scaling only stays undistorted because every rect this is
    animated between shares one aspect ratio - see HERO_PEEK_FRAME_SIZE."""

    clicked = pyqtSignal()

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        # A dedicated objectName rather than the shared #Card hoverable
        # rule (theme.py) - that rule highlights via *background*, which
        # a scaledContents pixmap filling the whole label would just
        # paint over invisibly. QLabel is a QFrame under the hood, so a
        # :hover *border* still shows up fine around the pixmap instead.
        self.setObjectName("HeroCardLabel")
        self.setScaledContents(True)
        self.setPixmap(pixmap)

    # Only while the pointer is genuinely inside, same as widgets.Card -
    # and these are the worst offenders for holding a cursor they
    # shouldn't, since the carousel builds fresh ones on a timer whether
    # or not anyone is looking at this page.
    def enterEvent(self, event):
        hold_hover_cursor(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        release_hover_cursor(self)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _greeting():
    hour = datetime.now().hour
    if hour < 12:
        return "Good Morning"
    if hour < 18:
        return "Good Afternoon"
    return "Good Evening"


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
        self.websites = storage.load(WEBSITES_FILE, [])
        self.apps = storage.load(APPS_FILE, [])

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
        # build time; the stretches either side do the rest.
        spacer = QWidget(objectName="Bare")
        spacer.setMaximumWidth(greeting_box.sizeHint().width())
        spacer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        header_row.addWidget(spacer, stretch=1)
        body_layout.addLayout(header_row)

        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(self._refresh_greeting)
        self._greeting_timer.start(GREETING_REFRESH_MS)

        body_layout.addWidget(self._build_hero())

        # Preview sections are ordered to match the sidebar's (draggable,
        # user-customizable) nav order - a section spanning two nav
        # entries (Anime & Manga, or the Apps/Websites row) sorts by
        # whichever of the two sits earlier.
        sections = []

        anime_manga_recent = self._recent_entries(self._home_tracker_entries)
        if anime_manga_recent:
            # One section covering two nav entries, so hiding just one of
            # them retitles it rather than dropping it - a row of only
            # manga still headed "Anime & Reading" reads like a bug.
            title = "Anime & Reading"
            if "anime" in hidden:
                title = "Reading"
            elif "manga" in hidden:
                title = "Anime"
            pos = min(nav_config.nav_position("anime"), nav_config.nav_position("manga"))
            sections.append((pos, self._build_section(
                title, self._build_poster_grid(anime_manga_recent))))

        series_recent = self._recent_entries(self._home_series_entries)
        if series_recent:
            pos = nav_config.nav_position("series")
            sections.append((pos, self._build_section(
                "Movies & Series", self._build_poster_grid(series_recent))))

        recent_games = [] if "games" in hidden else self._recent_games()
        if recent_games:
            pos = nav_config.nav_position("games")
            sections.append((pos, self._build_section(
                "Games", self._build_games_grid(recent_games))))

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
            self._search_results.show()
        self._search_results.set_query(text)

    def _close_search_results(self):
        if self._search_results is not None:
            self._search_results.close()
            self._search_results = None

    def eventFilter(self, obj, event):
        """Up/Down/Enter/Escape in the field drive the list under it -
        the field keeps the focus throughout, which is why the panel is
        shown without activating."""
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
                self.search_bar.clear()
                self._close_search_results()
                return True
        return super().eventFilter(obj, event)

    def _refresh_greeting(self):
        self.greeting_label.setText(f"{_greeting()} \U0001F44B")

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
        entries = sorted(entries, key=lambda e: e.get("updated_at") or "", reverse=True)
        return entries[:TRACKER_PREVIEW_LIMIT]

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
        hero = QFrame(objectName="Hero")
        outer_layout = QHBoxLayout(hero)
        outer_layout.setContentsMargins(20, 20, 20, 20)

        self._hero_entries = self._in_progress_entries()[:HERO_SLIDE_LIMIT]
        if not self._hero_entries:
            empty = QLabel("Nothing in progress yet - add an anime, manga, or series to start tracking.",
                            objectName="Muted")
            outer_layout.addWidget(empty)
            return hero

        # A fixed-size stage, not a layout - _transition_hero animates
        # the peek/mid widgets' raw geometry directly between the fixed
        # slot rects (HERO_LEFT_RECT/HERO_MID_RECT/HERO_RIGHT_RECT etc.,
        # in this widget's local coordinates), which needs stable pixel
        # bounds rather than a layout that would just reflow around them.
        self._hero_stage = QWidget(objectName="Bare")
        self._hero_stage.setFixedSize(HERO_STAGE_WIDTH, HERO_STAGE_HEIGHT)
        self._hero_index = 0
        self._hero_transitioning = False

        self._hero_mid_widget = self._build_hero_slide(self._hero_entries[0])
        self._hero_mid_widget.setParent(self._hero_stage)
        self._hero_mid_widget.setGeometry(HERO_MID_RECT)
        self._hero_mid_widget.show()

        has_neighbors = len(self._hero_entries) > 1
        self._hero_left_widget = None
        self._hero_right_widget = None
        if has_neighbors:
            self._hero_left_widget = self._build_hero_peek_label(self._neighbor_entry(-1))
            self._hero_left_widget.setParent(self._hero_stage)
            self._hero_left_widget.setGeometry(HERO_LEFT_RECT)
            self._hero_left_widget.clicked.connect(lambda: self._transition_hero(-1))
            self._hero_left_widget.show()

            self._hero_right_widget = self._build_hero_peek_label(self._neighbor_entry(1))
            self._hero_right_widget.setParent(self._hero_stage)
            self._hero_right_widget.setGeometry(HERO_RIGHT_RECT)
            self._hero_right_widget.clicked.connect(lambda: self._transition_hero(1))
            self._hero_right_widget.show()

            self._hero_timer = QTimer(self)
            self._hero_timer.timeout.connect(lambda: self._transition_hero(1))
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)

        outer_layout.addStretch()
        # See the comment further down (theme.SCROLLBAR_WIDTH nudge) -
        # unchanged from the single-entry version, just now positioning
        # the whole stage instead of the content block directly.
        outer_layout.addSpacing(9 + 45)
        outer_layout.addWidget(self._hero_stage)
        outer_layout.addStretch()
        return hero

    def _neighbor_entry(self, offset):
        n = len(self._hero_entries)
        return self._hero_entries[(self._hero_index + offset) % n]

    def _build_hero_card_pixmap(self, entry, with_button):
        """Renders one carousel entry's card (the same full-size layout
        the active slide uses - see _build_hero_slide) to a QPixmap
        rather than returning the live widget. _HeroCardLabel displays
        this and scales it as it animates; the widget itself is thrown
        away immediately since only its rendered appearance is needed.

        Always rendered at the *active slide's* size regardless of where
        it'll be shown - a peek is this exact pixmap displayed at half
        scale, which is what makes growing one into the active slide a
        clean uniform zoom rather than a re-layout. Supersampled on top
        of that so it stays sharp at full size too."""
        frame = self._build_hero_slide(entry, with_button=with_button)
        pixmap = QPixmap(frame.width() * HERO_CARD_SUPERSAMPLE, frame.height() * HERO_CARD_SUPERSAMPLE)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.scale(HERO_CARD_SUPERSAMPLE, HERO_CARD_SUPERSAMPLE)
        frame.render(painter)
        painter.end()
        frame.deleteLater()
        return pixmap

    def _build_hero_peek_label(self, entry):
        """A resting peek: the buttonless card, shown at half size."""
        return _HeroCardLabel(self._build_hero_card_pixmap(entry, with_button=False))

    def _build_hero_slide(self, entry, with_button=True):
        """One carousel page: cover+title+progress+Continue button for a
        single in-progress entry. Built fresh per entry (initial slide
        and every _transition_hero swap) rather than kept alive -
        simpler than diffing/updating one persistent widget's
        contents.

        with_button=False builds the identical card minus the Continue
        button, for the peeks - they're this same layout rendered at
        half scale (see _build_hero_card_pixmap), so they have to be
        built from the same code to stay perfectly in proportion."""
        # The cover+text block is centered as a group within the hero
        # card (stretches on both sides below) rather than pinned flush
        # left with a big empty gap on wide windows. A *fixed* width -
        # not just a cap - matters here: a word-wrapped QLabel's sizeHint
        # is a conservative guess unless something forces it wider, so
        # without this the block would shrink to that guess instead of
        # actually using the room a fixed width guarantees it.
        # A #Card frame (same look the peeks use) rather than a bare
        # widget, so the cover+text+button all sit on one shared
        # background instead of floating directly on the Hero's own.
        # Matte like the rest of Home - set as a property rather than
        # via Card(matte=True) since this is a plain frame, not a
        # clickable Card.
        content = QFrame(objectName="Card")
        content.setProperty("matte", True)
        content.setFixedSize(HERO_CONTENT_WIDTH, HERO_SLIDE_HEIGHT)
        layout = QHBoxLayout(content)
        layout.setContentsMargins(*([HERO_SLIDE_PADDING] * 4))
        layout.setSpacing(HERO_COVER_TEXT_GAP)

        cover = QLabel()
        cover.setFixedSize(*HERO_COVER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(entry.get("cover_path"), entry["title"], HERO_COVER_SIZE))
        layout.addWidget(cover)

        # A real QWidget (not a bare layout) so it gets actual geometry
        # from the HBoxLayout's stretch - the wrapped title label below
        # needs that to compute its wrap width against the row's real
        # available space instead of a too-narrow layout-item guess.
        text_widget = QWidget(objectName="Bare")
        text_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        text_col = QVBoxLayout(text_widget)
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(4)
        text_col.addWidget(QLabel("CONTINUE READING" if entry["type"] in MANGA_TYPES else "CONTINUE WATCHING",
                                   objectName="Muted"))
        title = QLabel(entry["title"], objectName="HeroTitle")
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        text_col.addWidget(title)
        # Manga shows your own last-*watched* chapter (manually entered on
        # the Reading page), not the site's latest-available one - "Chapter
        # 24.5" reads naturally. Anime/Series progress is only ever auto-
        # filled with a *guess* (the latest episode currently out, not
        # necessarily what you've watched) unless it's been verified (a
        # connected account, or typed in by hand) - shown only once
        # verified, same rule as the Anime/Reading pages themselves,
        # rather than stating a guess as fact.
        if not shows_last_watched(entry):
            progress_text = ""  # tick off, or a film - see shows_last_watched
        elif entry["type"] in MANGA_TYPES:
            watched = entry.get("last_watched_chapter")
            progress_text = f"Chapter {format_chapter_progress(watched)}" if watched else entry["status"]
        elif entry.get("progress_verified") and entry.get("progress"):
            progress_text = entry["progress"]
        else:
            progress_text = ""
        if progress_text:
            text_col.addWidget(QLabel(progress_text, objectName="CardMeta"))
        # A capped gap, not addStretch() - a full stretch would soak up
        # all the column's leftover height and pin the button to the
        # very bottom of the (taller) cover; this keeps it up near the
        # cover's middle instead.
        text_col.addSpacing(20)

        if with_button:
            continue_btn = QPushButton("▶ Continue", objectName="Accent")
            continue_btn.setFixedHeight(46)
            continue_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            continue_btn.clicked.connect(lambda: self._continue_entry(entry))
            # Stretched to fill rather than a fixed width nudged off to
            # one side, so it's as wide as the space allows and centred
            # in it. "The space" is the whole gap from the cover's right
            # edge to the card's inner edge - which is the text column
            # *plus* the column's leading gap, so the trailing inset has
            # to absorb that extra gap for the button to sit centred on
            # that span rather than on the narrower column alone.
            continue_row = QHBoxLayout()
            continue_row.setContentsMargins(
                HERO_CONTINUE_INSET, 0, HERO_CONTINUE_INSET + HERO_COVER_TEXT_GAP, 0)
            continue_row.addWidget(continue_btn)
            text_col.addLayout(continue_row)

        layout.addWidget(text_widget, stretch=1)
        return content

    def _transition_hero(self, direction):
        """Advance the carousel forward (direction=1, the auto-play
        timer's only mode) or backward (direction=-1, clicking the left
        peek) - the active slide shrinks and moves to become the peek
        on the departure side, the peek on the arrival side grows and
        moves to become the active slide, and a freshly-built peek
        slides in from off-stage to refill the arrival side. Clicking
        the right peek is direction=1, same as the timer.

        The active slide is a live interactive widget (it owns the real
        Continue button), but a live widget with fixed-size children
        can't be smoothly resized by animating its geometry - only
        moved. So it's ghosted to a _HeroCardLabel (a static pixmap
        that *can* be scaled smoothly) for the duration of the move,
        and only swapped back to a real widget once it lands - see
        _HeroCardLabel. The two peeks are already pixmap-backed, so
        they animate directly with no ghosting needed.

        The outgoing ghost is rendered as peek-style content (cover+
        title+meta, no button/eyebrow) from the very first frame, not
        grabbed from the live mid widget - it's *becoming* a peek, and
        a peek-shaped target doesn't have room for the button, so
        animating a full mid-with-button pixmap down to that size just
        squishes the button into a mangled sliver instead of shrinking
        cleanly."""
        if len(self._hero_entries) <= 1 or self._hero_transitioning:
            return
        self._hero_transitioning = True
        self._hero_timer.stop()

        n = len(self._hero_entries)
        old_index = self._hero_index
        new_index = (old_index + direction) % n
        old_mid_entry = self._hero_entries[old_index]
        entering_entry = self._hero_entries[(new_index + direction) % n]

        # Each card in motion is rendered as whatever it's *becoming*,
        # not what it was, so it already matches its neighbours when it
        # arrives: this one is on its way out to a peek slot, so it
        # drops the Continue button for the whole trip (the peeks it's
        # joining don't have one), while the peek on its way in to the
        # mid slot picks one up (see arriving_widget below). Each swap
        # is therefore invisible at the end of the move; the button
        # blinks only at the very first frame, where it's covered by
        # the motion starting.
        mid_ghost = _HeroCardLabel(
            self._build_hero_card_pixmap(old_mid_entry, with_button=False), self._hero_stage)
        mid_ghost.setGeometry(HERO_MID_RECT)
        mid_ghost.show()
        mid_ghost.raise_()
        self._hero_mid_widget.hide()
        self._hero_mid_widget.deleteLater()

        if direction == 1:
            exiting_widget, arriving_widget = self._hero_left_widget, self._hero_right_widget
            exit_rect, enter_rect = HERO_EXIT_LEFT_RECT, HERO_EXIT_RIGHT_RECT
            mid_ghost_target, entering_target = HERO_LEFT_RECT, HERO_RIGHT_RECT
        else:
            exiting_widget, arriving_widget = self._hero_right_widget, self._hero_left_widget
            exit_rect, enter_rect = HERO_EXIT_RIGHT_RECT, HERO_EXIT_LEFT_RECT
            mid_ghost_target, entering_target = HERO_RIGHT_RECT, HERO_LEFT_RECT

        # The mirror of mid_ghost above - this peek is becoming the
        # active slide, so it carries the button in on the way.
        arriving_widget.setPixmap(
            self._build_hero_card_pixmap(self._hero_entries[new_index], with_button=True))

        entering_widget = self._build_hero_peek_label(entering_entry)
        entering_widget.setParent(self._hero_stage)
        entering_widget.setGeometry(enter_rect)
        entering_widget.show()
        entering_widget.raise_()
        arriving_widget.raise_()

        group = QParallelAnimationGroup(self)

        def animate(widget, end_rect):
            anim = QPropertyAnimation(widget, b"geometry", self)
            anim.setDuration(HERO_SLIDE_ANIM_MS)
            anim.setStartValue(widget.geometry())
            anim.setEndValue(end_rect)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(anim)

        animate(exiting_widget, exit_rect)
        animate(mid_ghost, mid_ghost_target)
        animate(arriving_widget, HERO_MID_RECT)
        animate(entering_widget, entering_target)

        def finish():
            exiting_widget.deleteLater()
            mid_ghost.deleteLater()
            arriving_widget.deleteLater()

            self._hero_index = new_index
            self._hero_mid_widget = self._build_hero_slide(self._hero_entries[new_index])
            self._hero_mid_widget.setParent(self._hero_stage)
            self._hero_mid_widget.setGeometry(HERO_MID_RECT)
            self._hero_mid_widget.show()

            settled_side_widget = self._build_hero_peek_label(old_mid_entry)
            settled_side_widget.setParent(self._hero_stage)
            settled_side_widget.setGeometry(mid_ghost_target)
            settled_side_widget.show()

            if direction == 1:
                self._hero_left_widget = settled_side_widget
                self._hero_left_widget.clicked.connect(lambda: self._transition_hero(-1))
                self._hero_right_widget = entering_widget
                self._hero_right_widget.clicked.connect(lambda: self._transition_hero(1))
            else:
                self._hero_right_widget = settled_side_widget
                self._hero_right_widget.clicked.connect(lambda: self._transition_hero(1))
                self._hero_left_widget = entering_widget
                self._hero_left_widget.clicked.connect(lambda: self._transition_hero(-1))

            self._hero_transitioning = False
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)

        group.finished.connect(finish)
        self._hero_anim_group = group  # keep a reference so it isn't gc'd mid-animation
        group.start()

    def _continue_entry(self, entry):
        if not open_tracker_entry(self, entry):
            self.app.navigate_to(PAGE_FOR_TYPE.get(entry["type"], "anime"))

    # ------------------------------------------------------------------
    def _build_poster_grid(self, entries):
        # No #SectionBox frame here (unlike the Apps/Websites lists
        # below) - a wall of poster art doesn't need a background box
        # to read as a group the way rows of icon+text do.
        box = QWidget(objectName="Bare")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(grid)

        for index, entry in enumerate(entries):
            # One shared #SectionBox frame for the whole grid (below)
            # rather than one behind every poster - #HomeItem drops the
            # per-item frame while keeping a hover highlight.
            card = Card(hoverable=True)
            card.setObjectName("HomeItem")
            card.setFixedWidth(POSTER_SIZE[0] + 20)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 8, 6, 8)

            cover = QLabel()
            cover.setFixedSize(*POSTER_SIZE)
            cover.setPixmap(images.thumbnail_or_avatar(entry.get("cover_path"), entry["title"], POSTER_SIZE))
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

            card.clicked.connect(lambda en=entry: self._continue_entry(en))
            grid.addWidget(card, 0, index)
        return box

    def _build_games_grid(self, games):
        # No #SectionBox frame here either - see _build_poster_grid.
        box = QWidget(objectName="Bare")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(grid)

        for index, game in enumerate(games):
            card = Card(hoverable=True)
            card.setObjectName("HomeItem")
            card.setFixedWidth(96)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 10, 6, 10)

            icon = QLabel()
            icon.setFixedSize(*ICON_SIZE)
            icon.setPixmap(images.thumbnail_or_avatar(game.get("icon"), game["name"], ICON_SIZE))
            card_layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

            name = QLabel(game["name"], objectName="CardTitle")
            name.setWordWrap(True)
            name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            card_layout.addWidget(name)

            card.clicked.connect(lambda g=game: self._launch_game(g))
            grid.addWidget(card, 0, index)
        return box

    def _launch_game(self, game):
        try:
            subprocess.Popen([game["path"]], shell=True, cwd=str(Path(game["path"]).parent),
                             env=child_process.clean_env(),
                             creationflags=child_process.flags())
        except OSError as exc:
            QMessageBox.critical(self, "Games", f"Couldn't launch this game:\n{exc}")
            return
        game["last_played"] = storage.now_iso()
        # Only this game's own field, not a wholesale save of the copy
        # Home loaded when it was built - that snapshot goes stale the
        # moment the Games page (or a Settings import) touches the list,
        # and writing it back would undo their changes.
        storage.update_entry(GAMES_FILE, game.get("id"), {"last_played": game["last_played"]})

    # ------------------------------------------------------------------
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

        return frame

    def _open_quick_link(self, entry, title, data_file, entries):
        open_link_entry(self, entry, title)
        entry["last_used"] = storage.now_iso()
        # This entry's field only - the Apps/Websites pages hold their
        # own copy of the same file, so writing Home's whole list back
        # would undo anything they'd changed since Home was built (same
        # reason games.json edits go through update_entry).
        storage.update_entry(data_file, entry.get("id"), {"last_used": entry["last_used"]})
