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
    QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation, QTimer, Qt,
)
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from helpers import images, nav_config, storage, theme
from helpers.widgets import Card, GlassPage, scroll_area
from windows.link_grid import open_link_entry
from windows.tracker import IN_PROGRESS_STATUSES, MANGA_TYPES, format_chapter_progress, open_tracker_entry

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

HERO_CONTENT_WIDTH = 620
HERO_SLIDE_LIMIT = 4
HERO_SLIDE_INTERVAL_MS = 5000
HERO_SLIDE_ANIM_MS = 320

PAGE_FOR_TYPE = {"Anime": "anime", "Series": "series", **{t: "manga" for t in MANGA_TYPES}}


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
        self.games = storage.load(GAMES_FILE, [])
        self.websites = storage.load(WEBSITES_FILE, [])
        self.apps = storage.load(APPS_FILE, [])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
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

        header = QVBoxLayout()
        header.setSpacing(2)
        self.greeting_label = QLabel(f"{_greeting()} \U0001F44B", objectName="PanelTitle")
        header.addWidget(self.greeting_label)
        header.addWidget(QLabel("Here's what's going on", objectName="PanelSubtitle"))
        body_layout.addLayout(header)

        self._greeting_timer = QTimer(self)
        self._greeting_timer.timeout.connect(self._refresh_greeting)
        self._greeting_timer.start(GREETING_REFRESH_MS)

        body_layout.addWidget(self._build_hero())

        # Preview sections are ordered to match the sidebar's (draggable,
        # user-customizable) nav order - a section spanning two nav
        # entries (Anime & Manga, or the Apps/Websites row) sorts by
        # whichever of the two sits earlier.
        sections = []

        anime_manga_recent = self._recent_entries(self.tracker_entries)
        if anime_manga_recent:
            pos = min(nav_config.nav_position("anime"), nav_config.nav_position("manga"))
            sections.append((pos, self._build_section(
                "Anime & Reading", self._build_poster_grid(anime_manga_recent))))

        series_recent = self._recent_entries(self.series_entries)
        if series_recent:
            pos = nav_config.nav_position("series")
            sections.append((pos, self._build_section(
                "Series", self._build_poster_grid(series_recent))))

        recent_games = self._recent_games()
        if recent_games:
            pos = nav_config.nav_position("games")
            sections.append((pos, self._build_section(
                "Games", self._build_games_grid(recent_games))))

        lists_row = QHBoxLayout()
        lists_row.setSpacing(16)
        if self.apps:
            lists_row.addWidget(self._build_quick_list("Quick Apps", self.apps, APPS_FILE))
        if self.websites:
            lists_row.addWidget(self._build_quick_list("Websites", self.websites, WEBSITES_FILE))
        if self.apps or self.websites:
            row_wrap = QWidget(objectName="Bare")
            row_wrap.setLayout(lists_row)
            pos = min(nav_config.nav_position("apps"), nav_config.nav_position("websites"))
            sections.append((pos, row_wrap))

        for _, widget in sorted(sections, key=lambda pair: pair[0]):
            body_layout.addWidget(widget)

        body_layout.addStretch()

    def _refresh_greeting(self):
        self.greeting_label.setText(f"{_greeting()} \U0001F44B")

    # ------------------------------------------------------------------
    def _all_trackable_entries(self):
        return self.tracker_entries + self.series_entries

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

        # A fixed-size clipping viewport, not a layout - _advance_hero
        # slides the current/next slide widgets across it by animating
        # raw geometry, which needs stable pixel bounds to slide within
        # rather than a layout that would just reflow around them.
        # Height matches HERO_COVER_SIZE the same way the unanimated
        # single-entry version implicitly did (the cover was always the
        # tallest thing in the row).
        self._hero_viewport = QWidget(objectName="Bare")
        self._hero_viewport.setFixedSize(HERO_CONTENT_WIDTH, HERO_COVER_SIZE[1])
        self._hero_index = 0
        self._hero_slide = self._build_hero_slide(self._hero_entries[0])
        self._hero_slide.setParent(self._hero_viewport)
        self._hero_slide.setGeometry(0, 0, HERO_CONTENT_WIDTH, HERO_COVER_SIZE[1])
        self._hero_slide.show()

        if len(self._hero_entries) > 1:
            self._hero_timer = QTimer(self)
            self._hero_timer.timeout.connect(self._advance_hero)
            self._hero_timer.start(HERO_SLIDE_INTERVAL_MS)

        outer_layout.addStretch()
        # See the comment further down (theme.SCROLLBAR_WIDTH nudge) -
        # unchanged from the single-entry version, just now positioning
        # the viewport instead of the content block directly.
        outer_layout.addSpacing(9 + 45)
        outer_layout.addWidget(self._hero_viewport)
        outer_layout.addStretch()
        return hero

    def _build_hero_slide(self, entry):
        """One carousel page: cover+title+progress+Continue button for a
        single in-progress entry. Built fresh per entry (initial slide
        and every _advance_hero swap) rather than kept alive - simpler
        than diffing/updating one persistent widget's contents."""
        # The cover+text block is centered as a group within the hero
        # card (stretches on both sides below) rather than pinned flush
        # left with a big empty gap on wide windows. A *fixed* width -
        # not just a cap - matters here: a word-wrapped QLabel's sizeHint
        # is a conservative guess unless something forces it wider, so
        # without this the block would shrink to that guess instead of
        # actually using the room a fixed width guarantees it.
        content = QWidget(objectName="Bare")
        content.setFixedWidth(HERO_CONTENT_WIDTH)
        layout = QHBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

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
        if entry["type"] in MANGA_TYPES:
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

        continue_btn = QPushButton("▶ Continue", objectName="Accent")
        continue_btn.setFixedSize(170, 46)
        continue_btn.clicked.connect(lambda: self._continue_entry(entry))
        # Nudged right on its own - everything else in this column
        # (label, title, progress) stays flush with the column's edge.
        # The trailing addStretch() matters: without something claiming
        # the row's leftover width, Qt centers a no-stretch row instead
        # of anchoring it to the fixed leading spacer, overshooting the
        # nudge far past what was asked for.
        continue_row = QHBoxLayout()
        continue_row.setContentsMargins(0, 0, 0, 0)
        continue_row.addSpacing(35)
        continue_row.addWidget(continue_btn)
        continue_row.addStretch()
        text_col.addLayout(continue_row)

        layout.addWidget(text_widget, stretch=1)
        return content

    def _advance_hero(self):
        """Slide from the current carousel entry to the next one (wraps
        back to the first after the last) - old slide exits to the
        left, new one enters from the right. Always the same direction
        since this is a timed loop, not a user-driven back/forward."""
        self._hero_index = (self._hero_index + 1) % len(self._hero_entries)
        old_slide = self._hero_slide
        new_slide = self._build_hero_slide(self._hero_entries[self._hero_index])
        new_slide.setParent(self._hero_viewport)
        width, height = HERO_CONTENT_WIDTH, HERO_COVER_SIZE[1]
        new_slide.setGeometry(width, 0, width, height)
        new_slide.show()
        new_slide.raise_()

        group = QParallelAnimationGroup(self)
        anim_new = QPropertyAnimation(new_slide, b"pos", self)
        anim_new.setDuration(HERO_SLIDE_ANIM_MS)
        anim_new.setStartValue(QPoint(width, 0))
        anim_new.setEndValue(QPoint(0, 0))
        anim_new.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_new)

        anim_old = QPropertyAnimation(old_slide, b"pos", self)
        anim_old.setDuration(HERO_SLIDE_ANIM_MS)
        anim_old.setStartValue(QPoint(0, 0))
        anim_old.setEndValue(QPoint(-width, 0))
        anim_old.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_old)

        group.finished.connect(old_slide.deleteLater)
        self._hero_anim_group = group  # keep a reference so it isn't gc'd mid-animation
        self._hero_slide = new_slide
        group.start()

    def _continue_entry(self, entry):
        if not open_tracker_entry(self, entry):
            self.app.navigate_to(PAGE_FOR_TYPE.get(entry["type"], "anime"))

    # ------------------------------------------------------------------
    def _build_poster_grid(self, entries):
        box = QWidget(objectName="SectionBox")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(grid)

        for index, entry in enumerate(entries):
            card = Card(hoverable=True)
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

            if entry["type"] in MANGA_TYPES:
                watched = entry.get("last_watched_chapter")
                meta_text = f"Ch {format_chapter_progress(watched)}" if watched else entry["type"]
            elif entry.get("progress_verified") and entry.get("progress"):
                meta_text = entry["progress"]
            else:
                meta_text = ""
            if meta_text:
                meta = QLabel(meta_text, objectName="CardMeta")
                meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                card_layout.addWidget(meta)

            card.clicked.connect(lambda en=entry: self._continue_entry(en))
            grid.addWidget(card, 0, index)
        return box

    def _build_games_grid(self, games):
        box = QWidget(objectName="SectionBox")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(16, 16, 16, 16)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addLayout(grid)

        for index, game in enumerate(games):
            card = Card(hoverable=True)
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
            subprocess.Popen([game["path"]], shell=True, cwd=str(Path(game["path"]).parent))
            game["last_played"] = storage.now_iso()
            storage.save(GAMES_FILE, self.games)
        except OSError as exc:
            QMessageBox.critical(self, "Games", f"Couldn't launch this game:\n{exc}")

    # ------------------------------------------------------------------
    def _build_quick_list(self, title, entries, data_file):
        frame = QWidget(objectName="SectionBox")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(QLabel(title, objectName="CardTitle"))

        for entry in self._recent_links(entries)[:QUICK_LIST_LIMIT]:
            row = Card(hoverable=True)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setSpacing(10)

            icon = QLabel()
            icon.setFixedSize(*ROW_ICON_SIZE)
            icon.setPixmap(images.thumbnail_or_avatar(entry.get("image"), entry["name"], ROW_ICON_SIZE))
            row_layout.addWidget(icon)
            row_layout.addWidget(QLabel(entry["name"]))
            row_layout.addStretch()

            row.clicked.connect(lambda en=entry, df=data_file, es=entries: self._open_quick_link(en, title, df, es))
            layout.addWidget(row)

        return frame

    def _open_quick_link(self, entry, title, data_file, entries):
        open_link_entry(self, entry, title)
        entry["last_used"] = storage.now_iso()
        storage.save(data_file, entries)
