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

from PyQt6.QtCore import QObject, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QLinearGradient, QPainter

Signal = pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

# hero_art at module level costs nothing new: its imports (PIL via
# helpers.images) are already pulled by this module's own `images`
# import - measured, not assumed, 22 August 2026.
from helpers import (app_art, cover_fetch, game_art, game_launch,
                     hero_art, history, images, launchers, lookup_pool,
                     nav_config, storage, theme)
from helpers.widgets import (
    Card, GlassPage, HERO_COVER_SIZE, HeroBanner, SideScroller, _OpaqueGround,
    hero_logo_label,
    hero_split, inform, scroll_area, set_hero_logo, SmoothTween, warm_backdrop,
    DriftButton,
    use_hover_cursor,
)
from windows.link_grid import missing_app_targets, open_link_entry
from windows.tracker import (
    IN_PROGRESS_STATUSES, MANGA_TYPES, POSTER_SIZE, VIDEO_TYPES,
    _cover_kind,
    ContinueCover, _progress_data_file, attach_continue_cover,
    discover_entry, format_chapter_progress, open_tracker_entry,
    shows_last_watched,
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

# No preview limits for the tracker rows any more - see _recent_entries,
# and none for Games either since 28 August 2026 (_recent_games). Quick
# Apps and Quick Websites keep theirs: they are vertical lists in a
# fixed box, not a strip that scrolls, so an uncapped one would push
# everything below it off the page.
QUICK_LIST_LIMIT = 5

# The search field's width here. Wider than a page's own 220px filter box
# because it searches everything rather than one list, and capped so it
# stays a field rather than a banner across a 2048px display.
# What it may shrink to before the page would rather clip it - narrow
# enough that a 1000px window still shows a usable field.

HERO_SLIDE_LIMIT = 4
# How long after the hero is built before its covers are warmed, and
# the gap between each - see _warm_next_hero_cover.
HERO_COVER_WARM_MS = 120
HERO_SLIDE_INTERVAL_MS = 6000

# The hero's logo treatment (widgets.hero_logo_label) went with the
# cover-left redesign of 23 August 2026 and is back by the owner's ask
# of 24 August: the TMDB logo stands where the name does, and the name
# is text when there is none. Only ever that swap - see _build_hero for
# the hide-the-name rule that is *not* coming back with it.

# Anime opens on the merged watch page now - there is no "anime" page
# key left (see nav_config).
PAGE_FOR_TYPE = {"Anime": "series", "Series": "series",
                 **{t: "manga" for t in MANGA_TYPES}}


class _HeroSignals(QObject):
    # entry id -> local backdrop path, crossing back from a fetch thread.
    backdrop = pyqtSignal(str, str)
    # entry id -> (logo path or "", hide the text title): the title
    # treatment the banner draws in place of its typed name, and whether
    # the name should go even without one (a real AniList reading banner
    # already carries it). See _hero_backdrop_worker.
    overlay = pyqtSignal(str, str, bool)


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


class _HeroDash(QWidget):
    """One pager pill under the hero, and the movement between them.

    The owner's ask, 26 August 2026: *"add an animation while
    transitioning of the bullets"*. They were QPushButtons carrying a
    stylesheet, resized and restyled on every slide change - which is a
    snap, and a style recomputation per pill per change on top of it.

    Painted instead, with one tween per pill carrying both the width and
    the colour, so the active pill *grows* into place while the outgoing
    one shrinks. Lit along its length rather than top-down for the
    reason theme.accent_gradient records: at 6px tall the vertical lip
    is well under a pixel and does not render at all."""

    clicked = Signal()

    HEIGHT = 6
    WIDTH_REST = 18
    WIDTH_ACTIVE = 30
    # 200ms: long enough to read as movement, short enough to be over
    # before the eye has finished travelling to the new slide.
    TWEEN_MS = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._t = 0.0
        self._hover = False
        self.setFixedHeight(self.HEIGHT)
        self.setFixedWidth(self.WIDTH_REST)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._tween = SmoothTween(self, self._on_tween, self.TWEEN_MS)
        use_hover_cursor(self)

    def _on_tween(self, value):
        self._t = float(value)
        self.setFixedWidth(int(round(
            self.WIDTH_REST + (self.WIDTH_ACTIVE - self.WIDTH_REST) * self._t)))
        self.update()

    def set_active(self, active, animate=True):
        target = 1.0 if active else 0.0
        if not animate:
            self._on_tween(target)
            return
        self._tween.start(self._t, target, self.TWEEN_MS)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        radius = self.height() / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        if self._t <= 0.001:
            resting = theme.TEXT_MUTED if self._hover else theme.SURFACE_ACTIVE
            painter.setBrush(QColor(resting))
            painter.drawRoundedRect(rect, radius, radius)
            painter.end()
            return
        # The resting tone underneath, then the accent ramp faded in over
        # it - so the colour crosses rather than switching, and a pill
        # halfway through the tween is halfway through the colour too.
        painter.setBrush(QColor(theme.SURFACE_ACTIVE))
        painter.drawRoundedRect(rect, radius, radius)
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for at, colour in theme.accent_stops(hover=self._hover):
            tint = QColor(colour)
            tint.setAlphaF(min(1.0, max(0.0, self._t)))
            gradient.setColorAt(at, tint)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()


class HomePage(GlassPage):
    # **This page folds live** - see main._toggle_sidebar. It is one
    # banner and a few scrolling rows rather than a grid of a hundred
    # cards, so it can be laid out on every step of the fold instead of
    # being photographed and blitted - which is what lets the hero
    # actually resize with the sidebar rather than snapping when the
    # animation lands.
    FOLD_LIVE = True

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
        # **0.7 - Home scrolls 30% slower than the rest of the app**,
        # the owner's ask, 24 August 2026 ("make the scrolling in the
        # main page 30% slower"), on top of the whole-app cut made the
        # same day. Home is a short page of big blocks where every other
        # page is a long list, so the same notch reads as a lurch here
        # and as travel there.
        panel_layout.addWidget(scroll_area(body, always_show_vbar=True,
                                           ground=theme.BG, notch_scale=0.7))

        # Greeting on the left, the clock on the right.
        header_row = QHBoxLayout()
        header = QVBoxLayout()
        header.setSpacing(2)
        self.greeting_label = QLabel(f"{_greeting()} \U0001F44B", objectName="PanelTitle")
        header.addWidget(self.greeting_label)
        header.addWidget(QLabel("Here's what's going on", objectName="PanelSubtitle"))
        greeting_box = QWidget(objectName="Bare")
        greeting_box.setLayout(header)
        header_row.addWidget(greeting_box)

        # **The search field that used to sit between these two
        # stretches is gone.** One bar in the window's title bar
        # searches everything from every page now (the owner's ask, 25
        # August 2026), and a second field on Home - directly under it -
        # was the same question asked twice.
        #
        # Both stretches stay. They were here to centre the field
        # between the greeting and the clock, and with it gone they are
        # what still pushes the clock to the right edge; collapsing them
        # to one would leave the clock floating beside the greeting.
        header_row.addStretch()
        # Capped at the greeting's width, which is what kept the field
        # centred in the page rather than in what was left over beside
        # the greeting. Kept with the field gone: it is still what stops
        # a long clock string dragging the row's balance around.
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
        if not series_recent:
            # **Nothing saved does not mean nothing watched** - the
            # owner, 31 August 2026: "I meant add it to the home page
            # like the reading". The row was already here and had simply
            # never had anything to draw: measured on his own data,
            # series.json holds **0** entries while History holds 41, of
            # which the watchable ones are what he has actually been
            # watching (Attack on Titan S01E02, Reacher S01E02, The Angel
            # Next Door S01E09). Reading looks populated beside it only
            # because its eight titles happen to be saved ones.
            series_recent = self._watching_from_history()
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
            # No separate backfill pass here any more - every card asks
            # for its own cover through cover_fetch as it is built (see
            # _ensure_game_cover). The old pass ran once, at page
            # construction, over the six games this row happens to show;
            # a card built by a later _refresh_games_row got nothing.
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

    def _hero_slide_entries(self):
        """Up to HERO_SLIDE_LIMIT slides, deliberately mixed across
        reading and watching rather than simply the newest four.

        Sorted by recency alone the hero showed only manga - the owner's
        report, 22 August 2026: he reads most days and watches in
        bursts, so on his real data every one of the four newest
        in-progress entries was a reading title and the carousel never
        held a watching one, though five were in progress. The two media
        now alternate, each lane newest-first, leading with whichever
        was touched last - so the first slide is still the most recently
        touched entry overall - and a lane running out hands its
        remaining slots to the other."""
        entries = self._in_progress_entries()
        reading = [e for e in entries if e.get("type") in MANGA_TYPES]
        watching = [e for e in entries if e.get("type") not in MANGA_TYPES]
        lanes = [reading, watching]
        if entries and entries[0].get("type") not in MANGA_TYPES:
            lanes.reverse()
        slides = []
        while len(slides) < HERO_SLIDE_LIMIT and any(lanes):
            for lane in lanes:
                if lane and len(slides) < HERO_SLIDE_LIMIT:
                    slides.append(lane.pop(0))
        return slides

    def _recent_entries(self, entries):
        """Every entry, most recently touched first - no preview cap any
        more (the owner's ask: an added entry must always appear here).
        The row scrolls sideways instead of truncating, the same answer
        the tracker pages give a long section."""
        return sorted(entries, key=lambda e: e.get("updated_at") or "",
                      reverse=True)

    def _watching_from_history(self):
        """The Watching row built out of History, for when series.json is
        empty - see the call site for the counts that made this needed.

        A saved entry is always preferred where one exists, so progress
        marks and Save state stay real; only the rest are transient
        records built from what History stored. That is the same rule
        tracker._history_entry follows for the History tab, and it is
        deliberately the same code shape - a second, subtly different
        conversion is how two sources start disagreeing about one title.

        Reading types are excluded: History holds those too (One Piece
        sits in it at Ch 1190), and letting them through would put the
        same titles in both Home rows.
        """
        try:
            rows = history.recent(VIDEO_TYPES)
        except Exception:
            return []                       # a lost row, never a lost page

        def key(title):
            return " ".join(str(title or "").strip().lower().split())

        saved = {key(entry.get("title")): entry
                 for entry in self.series_entries}
        out = []
        for row in rows:
            found = saved.get(key(row.get("title")))
            if found is not None:
                out.append(found)
                continue
            try:
                entry = discover_entry(
                    {"title": row.get("title"),
                     "poster": row.get("cover_url"),
                     "imdb_id": row.get("imdb_id")},
                    row.get("type") or VIDEO_TYPES[0])
            except Exception:
                continue
            # The site it was opened with, so resuming does not have to
            # go looking for it again.
            entry["url"] = row.get("url") or ""
            entry["site_id"] = row.get("site_id")
            entry["cover_path"] = row.get("cover_path")
            if row.get("progress"):
                entry["progress"] = row.get("progress")
            # _recent_entries sorts on this; History's own stamp is
            # last_opened, which is the same event by another name.
            entry["updated_at"] = row.get("last_opened") or ""
            out.append(entry)
        return out

    def _recent_games(self):
        """Every game, most recently played first, then the ones never
        played in saved order.

        **No preview cap**, 28 August 2026, the owner: "make the main
        page shows all games just like the readings and watchings in the
        main page!". It used to stop at six, which is the one thing that
        made this row different from Reading and Watching - those have
        carried every entry since `_recent_entries` dropped its own cap,
        and the row was already built to hold them: `_build_games_grid`
        is a sideways strip behind SideScroller's arrows, so a long
        library scrolls rather than being cut off. Each card asks for its
        own cover through `cover_fetch`, which queues on the bounded
        covers pool, so a large library is more queued lookups and not
        more simultaneous ones."""
        played = [g for g in self.games if g.get("last_played")]
        played = sorted(played, key=lambda g: g["last_played"], reverse=True)
        rest = [g for g in self.games if not g.get("last_played")]
        return played + rest

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
        self._hero_entries = self._hero_slide_entries()
        if not self._hero_entries:
            hero = QFrame(objectName="Hero")
            layout = QHBoxLayout(hero)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.addWidget(QLabel(
                "Nothing in progress yet - add an anime, manga, or series "
                "to start tracking.", objectName="Muted"))
            return hero

        self._hero_index = 0
        # Seeded from the entries themselves before anything is
        # fetched. The ground for a reading title costs an AniList query
        # *and* a banner download, with no small-copy fast path like the
        # video types have - so an unseeded Home paid the whole of it on
        # every single visit, which is the owner's "Kingdom WAN image
        # takes so long". Resolved once, remembered on the entry, and
        # from then on the slide is painted from disk on the first frame.
        self._hero_backdrops = {}
        # entry id -> logo path (a title-treatment PNG), and -> whether
        # the typed title should be hidden even without a logo. Both are
        # remembered on the entry the same way the backdrop is, so a
        # revisit draws the finished banner on the first frame with no
        # lookup - see _hero_backdrop_worker and _remember_hero_overlay.
        self._hero_logos = {}
        self._hero_hide_title = {}
        # Entries whose remembered ground was composed by an older
        # hero_art: the old JPEG still exists on disk, so exists() alone
        # would serve the superseded composition forever - the filename
        # version bump in hero_art cannot reach a path already written
        # onto an entry. Seeded anyway (old art on the first frame beats
        # a flat panel), but the worker below re-runs and cross-fades
        # the new composition in when it lands.
        stale_grounds = set()
        for hero in self._hero_entries:
            hid = hero.get("id")
            stored = hero.get("hero_backdrop")
            try:
                if stored and Path(stored).exists():
                    self._hero_backdrops[hid] = stored
                    if hero_art.stale_ground(stored):
                        stale_grounds.add(hid)
            except OSError:
                pass        # an unreadable path is simply not a cache hit
            logo = hero.get("hero_logo")
            try:
                if logo and Path(logo).exists():
                    self._hero_logos[hid] = logo
            except OSError:
                pass
            if hero.get("hero_hide_title") is not None:
                self._hero_hide_title[hid] = bool(hero.get("hero_hide_title"))
        self._hero_signals = _HeroSignals()
        self._hero_signals.backdrop.connect(self._on_hero_backdrop)
        self._hero_signals.overlay.connect(self._on_hero_overlay)

        # theme.BG: Home's body is the page ground itself, and that is
        # what the banner paints its corner outsides back to.
        banner = HeroBanner(theme.BG)
        self._hero_banner = banner
        # Cover on the left, details on the right - the owner's ask of
        # 23 August 2026, shared with the tracker's featured banner so
        # the two hero surfaces stay one design (widgets.hero_split).
        self._hero_cover, column = hero_split(banner)

        chip_row = QHBoxLayout()
        self._hero_chip = QLabel("")
        self._hero_chip.setStyleSheet(theme.EYEBROW_CHIP_QSS)
        chip_row.addWidget(self._hero_chip)
        chip_row.addStretch(1)
        column.addLayout(chip_row)
        column.addStretch(1)

        # The title treatment (a transparent TMDB logo PNG) sits where
        # the typed title does and replaces it when there is one - the
        # owner's ask, 24 August 2026: "instead of the name in the banner,
        # make it use the logo from TMDB, if it has no logo then use the
        # name normally". A reading title gets the logo of its anime
        # (Kingdom, One Piece, Hunter x Hunter - artwork.logo_path_by_title).
        #
        # **Only that swap.** The first version of this (22 August) also
        # hid the typed name for a reading title whose ground was real
        # AniList banner art, on the theory that the name was inside the
        # artwork - true of some banners, false of others - and the
        # owner's 23 August report was a banner with no name anywhere on
        # it ("where is the name on the banner????"). hero_hide_title is
        # still written onto entries by the worker but is never read:
        # the name hides only when a logo is actually drawn in its place.
        self._hero_logo = hero_logo_label()
        column.addWidget(self._hero_logo)
        self._hero_title = QLabel("")
        self._hero_title.setWordWrap(True)
        self._hero_title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 25pt; font-weight: 800;"
            f" background: transparent;")
        column.addWidget(self._hero_title)

        # The medium, in accent, directly under the name - image 2's
        # "Manga" line.
        self._hero_kind = QLabel("")
        self._hero_kind.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 12pt; font-weight: 700;"
            f" background: transparent;")
        column.addWidget(self._hero_kind)

        self._hero_meta = QLabel("")
        self._hero_meta.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; font-weight: 600;"
            f" background: transparent;")
        column.addWidget(self._hero_meta)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.setContentsMargins(0, 8, 0, 0)
        self._hero_continue = DriftButton("▶  Continue", kind="accent",
                                          objectName="DriftAccent")
        self._hero_continue.setFixedHeight(46)
        use_hover_cursor(self._hero_continue)
        self._hero_continue.clicked.connect(lambda: self._hero_open(resume=True))
        buttons.addWidget(self._hero_continue)
        # Harbor's second action: an outlined pill the backdrop reads
        # through, opening the episode/chapter list (the details page).
        self._hero_view = DriftButton("", kind="quiet", objectName="DriftQuiet")
        self._hero_view.setFixedHeight(46)
        # Fill, border and hover all come from DriftButton/#DriftQuiet now.
        use_hover_cursor(self._hero_view)
        self._hero_view.clicked.connect(lambda: self._hero_open(resume=False))
        buttons.addWidget(self._hero_view)
        buttons.addStretch(1)
        column.addLayout(buttons)

        # Balances the stretch above the chip so the title block sits in
        # the middle of the banner rather than against the dashes.
        column.addStretch(1)

        # The pagination dashes, centred at the banner's foot - each one
        # jumps straight to its slide; the active one is longer and lit.
        dash_row = QHBoxLayout()
        dash_row.setContentsMargins(0, 10, 0, 0)
        dash_row.setSpacing(6)
        dash_row.addStretch(1)
        self._hero_dashes = []
        for index in range(len(self._hero_entries)):
            dash = _HeroDash()
            dash.clicked.connect(lambda i=index: self._jump_hero(i))
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
        # rotating never waits on the network. A slide whose ground was
        # already remembered above is skipped outright - that is the
        # whole saving: no AniList round trip and no download for a
        # picture that is sitting on disk.
        for entry in self._hero_entries:
            hid = entry.get("id")
            # Start the worker unless the whole banner is already known -
            # its ground (composed by the *current* hero_art, see
            # stale_grounds above), its logo, and the hide-title
            # decision. A cached ground alone is not enough now: the
            # logo and that decision are what the overlay draws, and a
            # title resolved before this change has neither on its
            # entry yet.
            if (hid in self._hero_backdrops and hid not in stale_grounds
                    and hid in self._hero_logos
                    and hid in self._hero_hide_title):
                continue
            threading.Thread(target=self._hero_backdrop_worker,
                             args=(dict(entry),), daemon=True).start()
        self._show_hero_slide(0, fade=False)
        # **Every other slide's cover, decoded before a slide needs it.**
        # Measured 27 August 2026 on the real page, timing each step of a
        # slide change: `thumbnail_or_avatar` was **21-26ms** on the GUI
        # thread, and it is what the owner saw as the banner background
        # "glitching in a really fast way" - one 29-42ms stall at the
        # instant the transition starts, which is two dropped frames of
        # a 260ms fade. The same slide shown a second time costs 0.2ms,
        # so the cost is the first decode and nothing else.
        #
        # One per timer tick rather than a loop: four covers in a row is
        # the same stall moved somewhere else, where a tick apiece lands
        # each one in its own idle frame.
        self._hero_warm_queue = [dict(e) for e in self._hero_entries]
        QTimer.singleShot(HERO_COVER_WARM_MS, self._warm_next_hero_cover)
        return banner

    def _warm_next_hero_cover(self):
        """One hero cover into the image cache, then re-arm for the next.

        Guarded on the page still existing: this outlives a page rebuild
        by design (pages are rebuilt on every visit) and a dead widget
        must not take the timer's thread with it."""
        queue = getattr(self, "_hero_warm_queue", None)
        if not queue:
            return
        entry = queue.pop(0)
        try:
            images.thumbnail_or_avatar(entry.get("cover_path"),
                                       entry.get("title") or "",
                                       HERO_COVER_SIZE)
        except (RuntimeError, OSError, ValueError):
            pass                # a missing cover is the flat avatar, not a stall
        if queue:
            QTimer.singleShot(HERO_COVER_WARM_MS, self._warm_next_hero_cover)

    def _hero_entry(self):
        return self._hero_entries[self._hero_index % len(self._hero_entries)]

    def _hero_open(self, resume):
        # Re-armed, not left running: the slide must not advance the
        # moment the player or details page closes over it.
        if self._hero_timer.isActive():
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)
        self._continue_entry(self._hero_entry(), resume=resume)

    def _advance_hero(self):
        if self._hero_holds():
            return          # the timer keeps ticking; the next one may pass
        self._show_hero_slide((self._hero_index + 1) % len(self._hero_entries))

    def _hero_holds(self) -> bool:
        """Whether this rotation must not happen. Two reasons, in the
        order they cost:

        **Something is covering the page.** The reader, the player and
        the details page are hand-placed children of the *central
        widget* while a page lives in main.container, so Qt sees no
        overlap between them and goes on delivering this timer and
        painting the banner underneath. Measured 22 August 2026 with the
        details page open over Home: `page.visibleRegion()` is not empty,
        `_hero_timer.isActive()` is still True, and the banner painted
        **72 times in 20 seconds** - a burst holding the UI thread 73.6ms
        every six seconds, ten dropped frames at 144Hz, for a banner
        nobody can see. Worse on the first sight of each backdrop, which
        adds a full-resolution decode (48-107ms, see
        widgets._decoded_backdrop). That is the whole time a chapter
        list, a chapter or an episode is on screen - the owner's
        "clicked ... it showed me ch list and lagged heavily".
        `_top_overlay` is main.py's own answer to "what is on top", duck-
        typed there, so this covers the genre browse too and needs no
        open/close wiring on this page - a rotation skipped is simply
        retried six seconds later, and a browser-fallback open (which
        puts no overlay up) never freezes the carousel.

        **The pointer is on the banner.** A slide carries two buttons
        whose target changes with it - "View Chapters" becomes "View
        Episodes" - so rotating under an aiming pointer does not just
        move a control, it changes what pressing it does. This page
        already delays the games/apps re-sort by RESORT_DELAY_MS for the
        weaker version of the same problem.

        Asked of QApplication.widgetAt rather than the banner's
        Enter/Leave on purpose (.claude/rules/ui.md): crossing onto
        Continue fires the banner's *Leave*, so leave-means-gone would
        resume the carousel precisely while the pointer sits on a
        button."""
        banner = getattr(self, "_hero_banner", None)
        if banner is None:
            return False
        top_overlay = getattr(self.window(), "_top_overlay", None)
        try:
            if callable(top_overlay) and top_overlay() is not None:
                return True
        except RuntimeError:
            return True     # the window is going away; nothing to rotate for
        under = QApplication.widgetAt(QCursor.pos())
        return under is not None and (under is banner
                                      or banner.isAncestorOf(under))

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
        self._hero_kind.setText(str(entry.get("type") or ""))
        self._apply_hero_overlay()
        # The type moved up to its own accent line (_hero_kind), so it is
        # not repeated here - this row is the progress and the status.
        meta_bits = [self._progress_meta_text(entry) or entry.get("status") or ""]
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
            # animate=fade: the first draw is a state, not a change -
            # every pill tweening in on page build would read as the
            # pager loading rather than as the slide moving.
            dash.set_active(i == index, animate=fade)

    def _hero_backdrop_worker(self, entry):
        """One slide's ground, off the UI thread: TMDB's backdrop by
        IMDb id for the video types, hero_art's chain for reading. Never
        raises; a title with no landscape art anywhere just keeps the
        banner's flat panel.

        The reading side used to take AniList's *cover* when AniList had
        no banner and hand that straight to HeroBanner, which expands it -
        so a 460x624 portrait was drawn as the middle 17% of itself at
        2.75x. hero_art composes a real ground out of a cover instead;
        see that module for what was rendered and compared."""
        entry_id = str(entry.get("id") or "")
        from helpers import artwork
        try:
            if entry.get("type") in MANGA_TYPES:
                # cover_path/cover_url are whatever the entry's own
                # reading site already served - every tracked reading
                # entry here carries both - so a title AniList cannot
                # match still has a picture to build a ground from, and
                # the local copy costs no request at all.
                found, kind = hero_art.reading_ground(
                    entry.get("title") or "",
                    cover_path=entry.get("cover_path"),
                    cover_url=str(entry.get("cover_url") or ""))
                if found:
                    # Decoded here, on this thread, so the slide change does
                    # not pay a JPEG on the GUI thread - see
                    # widgets.warm_backdrop for the 31-43ms it cost.
                    warm_backdrop(found)
                    self._hero_signals.backdrop.emit(entry_id, str(found))
                # A reading title's logo: only when the same franchise is
                # an anime/series TMDB has a title treatment for - Kingdom,
                # Hunter x Hunter, One Piece all resolve (the owner's ask).
                # Cheap and disk-cached, so it runs even when the ground
                # above was already known.
                logo = artwork.logo_path_by_title(entry.get("title") or "")
                # Drop the typed name when a real AniList banner carries it
                # ("take the whole banner from Anilist ... remove the name")
                # or when a logo will stand in its place; keep it over a
                # composed cover ground (image 2).
                hide = bool(logo) or (kind == "banner")
                self._hero_signals.overlay.emit(entry_id, str(logo or ""), hide)
                return
            if not entry.get("imdb_id"):
                return
            # Both sizes, in that order - the details page's pattern,
            # and the fix for a soft slider: the small w780 copy used to
            # be taken with `or`, so the full-resolution original was
            # never fetched at all and a ~1200px-wide banner was drawn
            # from a 780px image. Now the small one fills the banner
            # immediately and the original replaces it when it lands.
            quick = artwork.backdrop_fast_path(entry)
            if quick:
                # Decoded here, on this thread, so the slide change does
                # not pay a JPEG on the GUI thread - see
                # widgets.warm_backdrop for the 31-43ms it cost.
                warm_backdrop(quick)
                self._hero_signals.backdrop.emit(entry_id, str(quick))
            full = artwork.backdrop_path(entry)
            if full:
                # Decoded here, on this thread, so the slide change does
                # not pay a JPEG on the GUI thread - see
                # widgets.warm_backdrop for the 31-43ms it cost.
                warm_backdrop(full)
                self._hero_signals.backdrop.emit(entry_id, str(full))
            # The video logo (TMDB title treatment): shown in place of the
            # typed title. Fails soft to text when the title has none.
            logo = artwork.logo_path(entry)
            self._hero_signals.overlay.emit(entry_id, str(logo or ""),
                                            bool(logo))
        except Exception:
            return          # no art anywhere just keeps the flat panel

    def _on_hero_backdrop(self, entry_id, path):
        # Whether this slide already had art: the sharp original landing
        # over the small copy is the *same picture*, so it swaps without
        # a cross-fade - dissolving a photo into itself reads as a
        # flicker. Only a genuinely new slide fades.
        upgrade = entry_id in self._hero_backdrops
        self._hero_backdrops[entry_id] = path
        self._remember_hero_backdrop(entry_id, path)
        if str(self._hero_entry().get("id") or "") != entry_id:
            return
        try:
            self._hero_banner.set_backdrop(path, fade=not upgrade)
        except RuntimeError:
            pass    # the page was torn down under the fetch

    def _on_hero_overlay(self, entry_id, logo_path, hide_title):
        """A slide's logo and hide-title decision, back from the fetch
        thread. Stored per id so a rotation redraws from it, remembered on
        the entry so a revisit needs no lookup, and applied at once if
        this is the slide on screen."""
        self._hero_logos[entry_id] = logo_path or None
        self._hero_hide_title[entry_id] = bool(hide_title)
        self._remember_hero_overlay(entry_id, logo_path or "",
                                    bool(hide_title))
        if str(self._hero_entry().get("id") or "") == entry_id:
            self._apply_hero_overlay()

    def _apply_hero_overlay(self):
        """Draw the current slide's title treatment, or fall back to the
        typed name. The logo replaces the text; a real AniList reading
        banner (hide_title, no logo) drops the text with nothing in its
        place, because the name is already in the banner art."""
        try:
            entry = self._hero_entry()
        except (IndexError, ZeroDivisionError):
            return
        # Logo in place of the name when this slide has one, the name
        # otherwise - never neither (see _build_hero).
        set_hero_logo(self._hero_logo, self._hero_title,
                      self._hero_logos.get(str(entry.get("id") or ""))
                      or self._hero_logos.get(entry.get("id")))
        # The portrait cover. `images.thumbnail_or_avatar` is the same
        # call every card on this page makes, so for a title already
        # drawn in the poster grid below this is a cache hit and not a
        # decode.
        try:
            self._hero_cover.setPixmap(images.thumbnail_or_avatar(
                entry.get("cover_path"), entry.get("title") or "",
                HERO_COVER_SIZE))
        except Exception:
            self._hero_cover.clear()
        # Same rule as the grid below: missing on disk means fetch, and
        # the swap only lands if this slide is still the one showing.
        _url = str(entry.get("cover_url") or "")

        def _set_hero(pixmap, en=entry):
            if self._hero_entry() is en:
                self._hero_cover.setPixmap(pixmap)
        cover_fetch.ensure(
            entry.get("id"), entry.get("cover_path"),
            (lambda u=_url, en=entry: cover_fetch.resolve(
                u, imdb_id=en.get("imdb_id") or "",
                title=en.get("title") or "",
                kind=_cover_kind(en.get("type")))) if _url else None,
            entry.get("title") or "", HERO_COVER_SIZE, _set_hero,
            persist=lambda path, en=entry: (
                en.__setitem__("cover_path", str(path)),
                storage.update_entry(_progress_data_file(en), en.get("id"),
                                     {"cover_path": str(path)})))

    def _on_inapp_closed(self, _entry_id):
        """The player or reader opened from this page has closed -
        rebuild Home fresh from disk, so the hero's "S01E07" / "Ch 551"
        and every card's progress caption show what was just watched or
        read. The owner, 24 August 2026: "make the Ep and Season / ch
        num in the main page change immediately when watched or read!".

        tracker._wire_overlay_refresh has delivered this hook to every
        tracker page since it was written and named Home as the one
        caller that "simply is not notified" - the player marks the
        episode watched at 85% (WATCHED_FRACTION) and the number was on
        disk all along; this page was showing the copy it loaded at
        build.

        A whole-page rebuild rather than surgical label updates, on
        purpose: the hero, its peeks, the Reading and Watching rows and
        History all carry the number somewhere, and pages here rebuild
        from scratch on every visit anyway - this is the same rebuild,
        one navigation earlier. Deferred a tick so the overlay's own
        close (which this page hosted) finishes tearing down first."""
        window = self.app
        refresh = getattr(window, "refresh_current_page", None)
        if not callable(refresh):
            return
        current = getattr(window, "_current_page", None)
        if current is not self:
            return          # the user already navigated; that rebuild won
        QTimer.singleShot(0, lambda: refresh() if current is
                          getattr(window, "_current_page", None) else None)

    def _ensure_game_cover(self, game, label):
        """Draw this game's poster now if it is on disk, and go and get
        it on the shared pool if it is not - see the note at the call
        site for why Home has to do this itself.

        `helpers/game_art` is the same resolver the Games page's
        backfill uses, so a cover fetched from either surface is one
        download and both find it afterwards."""
        game_id = game.get("id")
        if not game_id:
            return
        name = game.get("name") or ""
        install_path = game.get("path")

        def _set(pixmap, lbl=label):
            lbl.setPixmap(pixmap)

        cover_fetch.ensure(
            f"game:{game_id}", game.get("cover"),
            lambda n=name, p=install_path: game_art.fetch_cover(
                n, install_path=p),
            name, POSTER_SIZE, _set,
            persist=lambda path, g=game, gid=game_id: (
                g.__setitem__("cover", str(path)),
                storage.update_entry(GAMES_FILE, gid, {"cover": str(path)})))

    def _remember_hero_overlay(self, entry_id, logo_path, hide_title):
        """Persist a slide's logo and hide-title decision onto its entry,
        so the next visit draws the finished banner without a lookup.
        Through update_entry, never a whole-list write - the same rule
        _remember_hero_backdrop follows."""
        entry = next((e for e in self._hero_entries
                      if str(e.get("id") or "") == entry_id), None)
        if entry is None:
            return
        if (entry.get("hero_logo") == (logo_path or None)
                and entry.get("hero_hide_title") == hide_title):
            return
        entry["hero_logo"] = logo_path or None
        entry["hero_hide_title"] = hide_title
        try:
            storage.update_entry(_progress_data_file(entry), entry.get("id"),
                                 {"hero_logo": logo_path or None,
                                  "hero_hide_title": hide_title})
        except Exception:
            pass

    def _remember_hero_backdrop(self, entry_id, path):
        """Write a resolved backdrop onto its entry, so the next visit
        (and the next run) draws it without asking the network again.

        Through update_entry, never a whole-list write: Home holds
        copies of entries owned by two different files and several
        pages, and writing a list back from here is exactly the defect
        .claude/rules/ui.md records. Failing is fine - it only costs one
        re-resolve."""
        entry = next((e for e in self._hero_entries
                      if str(e.get("id") or "") == entry_id), None)
        if entry is None or entry.get("hero_backdrop") == path:
            return
        entry["hero_backdrop"] = path
        try:
            storage.update_entry(_progress_data_file(entry), entry.get("id"),
                                 {"hero_backdrop": path})
        except Exception:
            pass

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
            # Fetched when it is not on disk, and written back - the owner's
            # "the main page does not load the images by itself" (see
            # helpers/cover_fetch). Before this, Home drew cover_path and
            # nothing else, and only a visit to Discover ever re-created
            # the file it was pointing at.
            _url = str(entry.get("cover_url") or "")
            cover_fetch.ensure(
                entry.get("id"), entry.get("cover_path"),
                (lambda u=_url, en=entry: cover_fetch.resolve(
                    u, imdb_id=en.get("imdb_id") or "",
                    title=en.get("title") or "",
                    kind=_cover_kind(en.get("type")))) if _url else None,
                entry["title"], POSTER_SIZE,
                cover.set_cover if isinstance(cover, ContinueCover) else cover.setPixmap,
                persist=lambda path, en=entry: (
                    en.__setitem__("cover_path", str(path)),
                    storage.update_entry(_progress_data_file(en), en.get("id"),
                                         {"cover_path": str(path)})))

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
        # **Opaque, not transparent - and this reverses the two lines
        # that used to sit here.** theme.py's `#Bare` rule makes a scroll
        # body transparent, and a transparent body cannot be scrolled by
        # blitting: Qt repaints the page underneath and every widget over
        # it, every frame. scroll_area()'s docstring measures that at
        # 14.5ms/frame against 3.5ms, and these hand-built rows never had
        # the fix because they never went through that helper. Same
        # change, same reason, as the tracker's section strips.
        _OpaqueGround(strip, theme.BG)
        strip.adjustSize()
        area.setFixedHeight(strip.sizeHint().height()
                            + area.horizontalScrollBar().sizeHint().height())
        outer.addWidget(SideScroller(area, ground=theme.BG))
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
            # **And fetch it if there is none - the owner, 24 August
            # 2026: "the games images only load from the games page,
            # make them start loading from the main page also!".** This
            # row drew `game["cover"]` and nothing else, so a game whose
            # art had never been resolved stayed a blank slab here until
            # the Games page was opened and ran `_backfill_covers`. Same
            # shape as the defect cover_fetch was written for, and the
            # same fix: draw what is on disk, fetch what is not, write
            # the path back onto the entry.
            #
            # `storage.update_entry`, never a whole-list write - the
            # Games page holds its own copy of this file and a snapshot
            # written from here would undo whatever it has done since.
            self._ensure_game_cover(game, cover)
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
        # **Opaque, not transparent - and this reverses the two lines
        # that used to sit here.** theme.py's `#Bare` rule makes a scroll
        # body transparent, and a transparent body cannot be scrolled by
        # blitting: Qt repaints the page underneath and every widget over
        # it, every frame. scroll_area()'s docstring measures that at
        # 14.5ms/frame against 3.5ms, and these hand-built rows never had
        # the fix because they never went through that helper. Same
        # change, same reason, as the tracker's section strips.
        _OpaqueGround(strip, theme.BG)
        strip.adjustSize()
        area.setFixedHeight(strip.sizeHint().height()
                            + area.horizontalScrollBar().sizeHint().height())
        outer.addWidget(SideScroller(area, ground=theme.BG))
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
            # `art` before `image`, exactly as the Apps page draws its own
            # tiles (link_grid): `art` is the iTunes 512px artwork, `image`
            # the extracted shell icon, and Home preferring `image` is why
            # the same app wore two different icons on the two pages (the
            # owner's Wand report). Websites entries have no `art` and
            # keep their favicon through the fallback.
            icon.setPixmap(images.thumbnail_or_avatar(
                entry.get("art") or entry.get("image"), entry["name"],
                ROW_ICON_SIZE))
            if data_file == APPS_FILE:
                # The Apps page resolves artwork through app_art on its
                # own build; Home only ever drew what that page had left
                # behind - the owner's "the apps images do not load until
                # I go to the apps page". Same lookup, from here.
                cover_fetch.ensure(
                    entry.get("id"), entry.get("art") or entry.get("image"),
                    lambda n=entry.get("name") or "": app_art.fetch_art(n),
                    entry["name"], ROW_ICON_SIZE, icon.setPixmap,
                    persist=lambda path, en=entry: (
                        en.__setitem__("art", str(path)),
                        storage.update_entry(APPS_FILE, en.get("id"), {"art": str(path)})))
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
