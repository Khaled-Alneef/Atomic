"""The details page: what clicking the body of a tracker card opens.

A full-window surface over the app - the same overlay mechanism the
reader and player use (parented to the main window's central widget, so
the sidebar is covered, not hidden) - showing one entry the way a
streaming service's title page does: the artwork full-bleed behind a
scrim, the title's logo, the facts (runtime, years, rating, genres,
cast, summary) down the left, and the thing you actually came for on the
right - every episode or chapter, with its date and whether it has been
watched, one click from playing.

Where the data comes from, and why it costs almost nothing:

  * **Video** (Anime/Series/Movie): one keyless Cinemeta request
    (stremio.fetch_meta) carries every fact on the page *and* the whole
    episode list - name, date and thumbnail per episode. Measured on
    Bleach TYBW: 50 episodes, every field present.
  * **Reading** (Manga/Manhwa/Manhua): the chapter list comes from
    chapter_source, which is cached on disk for six hours - a details
    page for a series that was just read opens instantly.
  * The backdrop and logo are TMDB via helpers/artwork, cached on disk
    by IMDb id, and both fail soft to nothing: no backdrop means the
    page keeps its own dark ground, no logo means the title is text.

The split between the two ways into an entry is settled here once:
**the card's body opens this page; only the round continue button on
the cover resumes.** Before this page existed the two did nearly the
same thing for video and the owner rightly called that broken.
"""

import datetime

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from helpers import artwork, images, logs, lookup_pool, net, theme
from helpers.widgets import Card, GlassPage, scroll_area, show_toast, use_hover_cursor

try:
    from helpers import stremio
except Exception:                                   # pragma: no cover
    stremio = None
try:
    from helpers import chapter_source
except Exception:                                   # pragma: no cover
    chapter_source = None

MANGA_TYPES = ("Manga", "Manhwa", "Manhua")

# The right-hand list panel. Wide enough for an episode name, a date and
# a badge on one row; the rest of the window belongs to the artwork.
# Widened with the rows: the owner asked for larger text throughout this
# list, and 12pt names in a 440px panel elided half of everything.
PANEL_WIDTH = 490
ROW_HEIGHT = 72

# The scrim over the backdrop, top to bottom - heavier than the player's
# loading frame because real text sits on this one.
SCRIM = ((0.0, 7, 10, 20, 200), (0.45, 7, 10, 20, 170), (1.0, 7, 10, 20, 232))

CHAPTER_LIST_TIMEOUT = 45.0

# U+200E LEFT-TO-RIGHT MARK, built with chr() so no invisible character
# sits in this file waiting for a re-encoding tool to mangle it (the
# same reason the Fluent glyphs are escapes). Prefixed onto every list
# row title: an Arabic name otherwise flips the whole paragraph RTL and
# the clipped end is the *start* - the chapter number.
_LTR_MARK = chr(0x200E)

# Fluent glyphs, as escapes on purpose (see reader.py - a re-encoding
# tool turns the bare characters into mojibake, and it has happened).
ICON_BACK = "\ue72b"                  # Back arrow
ICON_FULLSCREEN = "\ue740"
ICON_EXIT_FULLSCREEN = "\ue73f"
ICON_SEARCH = "\ue721"
ICON_PLAY_GLYPH = "\ue768"


class _Signals(QObject):
    meta = Signal(int, object)          # run, Cinemeta meta dict or None
    art = Signal(int, str, str)         # run, "logo"/"backdrop", local path
    chapters = Signal(int, object)      # run, chapter list or None
    sources = Signal(int, object, object)  # run, stream list, (season, ep)


def _meta_worker(signals, run, imdb_id, content_type):
    """Never raises - lookup_pool workers die silently (see that module)."""
    try:
        meta = stremio.fetch_meta(imdb_id, content_type) if stremio else None
    except Exception:
        logs.exception("details meta lookup failed")
        meta = None
    signals.meta.emit(run, meta)


def _art_worker(signals, run, entry):
    """Backdrop first - it is the whole page's ground.

    The small w780 copy goes up the moment it lands (a few hundred KB)
    and the full-resolution original replaces it when it arrives - the
    owner reported the ground "taking a while", and most of that while
    was a multi-MB original on a first visit. Runs on its own thread,
    not lookup_pool: that pool is four wide and shared with the
    tracker's page-load backfill, so this page's own artwork could sit
    in a queue behind fifty schedule lookups."""
    for kind, fetch in (("backdrop", artwork.backdrop_fast_path),
                        ("backdrop", artwork.backdrop_path),
                        ("logo", artwork.logo_path)):
        try:
            path = fetch(entry)
        except Exception:
            path = None
        if path:
            signals.art.emit(run, kind, path)


def _sources_worker(signals, run, entry, season, episode):
    """Everything playable for one episode - what the source picker
    lists. Its own thread (the user is watching this one), never raises."""
    try:
        from helpers import streams as streams_module
        found = streams_module.find_streams(
            entry, season=season, episode=episode,
            deadline=net.deadline_in(24))
    except Exception:
        logs.exception("details source lookup failed")
        found = []
    signals.sources.emit(run, list(found or []), (season, episode))


def _reading_art_worker(signals, run, entry):
    """The reading page's ground: AniList's banner (or cover) for the
    title, downloaded into the app's one image cache.

    Reading entries carry no IMDb id, so TMDB - the video pages' source -
    can never answer for them; before this, every manga details page sat
    on the flat dark ground. AniList is keyless, already a dependency,
    and its banner is landscape art cut for exactly this use. Fails soft
    like every art lookup: no match or no network just keeps the dark
    ground."""
    try:
        from helpers import anilist
        url = anilist.fetch_manga_artwork(entry.get("title") or "")
        path = images.download(url) if url else None
    except Exception:
        logs.exception("details manga artwork failed")
        path = None
    if path:
        signals.art.emit(run, "backdrop", str(path))


def _chapters_worker(signals, run, entry):
    try:
        chapters = chapter_source.list_chapters(
            entry, deadline=net.deadline_in(CHAPTER_LIST_TIMEOUT))
    except Exception:
        logs.exception("details chapter listing failed")
        chapters = None
    signals.chapters.emit(run, list(chapters or []))


def _pretty_date(value) -> str:
    """`Jul 25, 2026` out of an ISO stamp, or the raw text if it will not
    parse - a wrong-looking date is more honest than a hidden one."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp.strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return text[:10]


def _aired(value):
    try:
        return datetime.datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _chip(text, accent=False) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {theme.ON_ACCENT if accent else theme.TEXT};"
        f" background: {theme.ACCENT_GRADIENT if accent else theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.ACCENT if accent else theme.BORDER};"
        f" border-radius: {theme.RADIUS}px; padding: 5px 14px;"
        f" font-weight: 600; font-size: 10.5pt;")
    return label


def _badge(text, kind) -> QLabel:
    """WATCHED / READ in the accent, UPCOMING in the success green - the
    two states the owner's reference picture colours differently."""
    colours = {"watched": (theme.ON_ACCENT, theme.ACCENT_GRADIENT, theme.ACCENT),
               "upcoming": ("#04140c", theme.SUCCESS, theme.SUCCESS)}
    fg, bg, border = colours[kind]
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {fg}; background: {bg}; border: 1px solid {border};"
        f" border-radius: {theme.RADIUS_SM}px; padding: 2px 10px;"
        f" font-weight: 700; font-size: 8.5pt;")
    return label


class DetailsPage(GlassPage):
    """One entry, full screen: facts on the left, the episode or chapter
    list on the right."""

    closed = Signal()

    def __init__(self, entry, parent=None):
        super().__init__(parent=parent)
        self.entry = dict(entry or {})
        self._run = 0
        self._closed = False
        self._backdrop = None
        self._backdrop_scaled = None      # see paintEvent's size cache
        self._backdrop_size = None
        self._meta = None
        self._videos = []
        self._chapters = []
        self._season = 0
        self._is_reading = self.entry.get("type") in MANGA_TYPES

        # The source-picker step: filled in when an episode row is
        # clicked, cleared by its back row or by picking a source.
        # {"season", "episode", "streams" (None while looking)}.
        self._source_pick = None

        self._signals = _Signals()
        self._signals.meta.connect(self._on_meta)
        self._signals.art.connect(self._on_art)
        self._signals.chapters.connect(self._on_chapters)
        self._signals.sources.connect(self._on_sources)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        # Debounced: a chapter list runs to 500 rows and rebuilding it on
        # every keystroke stutters under a fast typist.
        self._search_timer.timeout.connect(self._fill_rows)

        self._build()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._start_lookups()

    # ---- construction ---------------------------------------------------
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(36, 24, 24, 28)
        root.setSpacing(24)

        # Left: identity. Kept on plain transparent widgets so the
        # backdrop shows through - the scrim in paintEvent is what keeps
        # the text readable over it.
        left = QVBoxLayout()
        left.setSpacing(10)

        top_row = QHBoxLayout()
        self._back_btn = self._round_button(ICON_BACK, "Back (Esc)")
        self._back_btn.clicked.connect(self.leave)
        top_row.addWidget(self._back_btn)
        top_row.addStretch(1)
        left.addLayout(top_row)
        left.addStretch(1)

        self._logo = QLabel()
        self._logo.setVisible(False)
        left.addWidget(self._logo)
        self._title_label = QLabel(self.entry.get("title") or "",
                                   objectName="PanelTitle")
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(
            "font-size: 26pt; font-weight: 800; background: transparent;")
        left.addWidget(self._title_label)

        self._facts = QLabel("")
        self._facts.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 12pt; font-weight: 600;"
            f" background: transparent;")
        left.addWidget(self._facts)

        self._genres_head = self._section_label("GENRES")
        left.addWidget(self._genres_head)
        self._genres_row = QHBoxLayout()
        self._genres_row.setSpacing(8)
        self._genres_row.addStretch(1)
        left.addLayout(self._genres_row)

        self._cast_head = self._section_label("CAST")
        left.addWidget(self._cast_head)
        self._cast_row = QHBoxLayout()
        self._cast_row.setSpacing(8)
        self._cast_row.addStretch(1)
        left.addLayout(self._cast_row)

        self._summary_head = self._section_label("SUMMARY")
        left.addWidget(self._summary_head)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setMaximumWidth(720)
        self._summary.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; background: transparent;")
        left.addWidget(self._summary)
        for widget in (self._genres_head, self._cast_head, self._summary_head,
                       self._summary):
            widget.setVisible(False)

        left.addSpacing(14)
        verb = "Reading" if self._is_reading else "Watching"
        # Much larger than the app's stock accent button, at the owner's
        # ask - this is the page's one primary action and it was sized
        # like a form's Save.
        self._continue_btn = QPushButton(f"  Continue {verb}", objectName="Accent")
        self._continue_btn.setFixedHeight(58)
        self._continue_btn.setMinimumWidth(280)
        self._continue_btn.setStyleSheet(
            "QPushButton { font-size: 14pt; padding: 10px 30px; }")
        use_hover_cursor(self._continue_btn)
        self._continue_btn.clicked.connect(self._continue)
        continue_row = QHBoxLayout()
        continue_row.addWidget(self._continue_btn)
        continue_row.addStretch(1)
        left.addLayout(continue_row)

        # Directly under Continue: one download button per medium, and it
        # always asks - which episodes (one, a range, or the season) or
        # which chapters, plus the audio wanted for video. No second
        # season button and no assumed target (the owner's ask).
        if self._is_reading:
            label = "  Download Chapters"
        elif self.entry.get("type") == "Movie":
            label = "  Download Film"
        else:
            label = "  Download Episodes"
        self._download_btn = QPushButton(label)
        self._download_btn.setFixedHeight(44)
        self._download_btn.setMinimumWidth(280)
        self._download_btn.setStyleSheet(
            f"QPushButton {{ font-size: 11.5pt; padding: 8px 24px;"
            f" background: {theme.SURFACE_HOVER}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT}; }}")
        use_hover_cursor(self._download_btn)
        self._download_btn.clicked.connect(self._download_current)
        download_row = QHBoxLayout()
        download_row.addWidget(self._download_btn)
        download_row.addStretch(1)
        left.addSpacing(8)
        left.addLayout(download_row)
        left.addStretch(2)

        root.addLayout(left, stretch=1)
        root.addWidget(self._build_panel())

    def _section_label(self, text) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt; font-weight: 700;"
            f" letter-spacing: 1px; background: transparent; padding-top: 8px;")
        return label

    def _round_button(self, glyph, tooltip):
        button = QPushButton(glyph)
        button.setToolTip(tooltip)
        button.setFixedSize(40, 40)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # padding: 0 or the app-wide button padding clips the glyph to a
        # sliver (the measured trap reader._glyph_button records).
        button.setStyleSheet(
            f"QPushButton {{ background: {theme.PANEL_FILL}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; padding: 0px;"
            f" font-family: {theme.FONT_STACK_ICONS}; font-size: 13pt;"
            f" border-radius: 20px; }}"
            f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
            f" background: {theme.SURFACE_HOVER}; }}")
        use_hover_cursor(button)
        return button

    def _build_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFixedWidth(PANEL_WIDTH)
        panel.setStyleSheet(
            f"QFrame {{ background: rgba(10, 15, 28, 210);"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_LG}px; }}")
        column = QVBoxLayout(panel)
        column.setContentsMargins(14, 14, 14, 14)
        column.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(6)
        # Larger than the stock text buttons, with the rest of this panel
        # (the owner's ask - at the old 4px padding these read as labels).
        self._prev_btn = QPushButton("‹  Prev")
        self._next_btn = QPushButton("Next  ›")
        for button, step in ((self._prev_btn, -1), (self._next_btn, 1)):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {theme.TEXT};"
                f" border: none; padding: 8px 12px; font-weight: 700;"
                f" font-size: 12.5pt; }}"
                f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
            use_hover_cursor(button)
            button.clicked.connect(lambda checked=False, s=step: self._step_season(s))
        self._season_box = QComboBox()
        self._season_box.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS}px;"
            f" padding: 8px 16px; font-weight: 700; font-size: 12pt; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}")
        use_hover_cursor(self._season_box)
        self._season_box.activated.connect(self._pick_season)
        header.addWidget(self._prev_btn)
        header.addStretch(1)
        header.addWidget(self._season_box)
        header.addStretch(1)
        header.addWidget(self._next_btn)
        column.addLayout(header)
        if self._is_reading:
            # One flat chapter list - seasons are a video concept.
            self._prev_btn.setVisible(False)
            self._next_btn.setVisible(False)
            self._season_box.setVisible(False)
            title = QLabel("Chapters")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
                f" background: transparent; border: none;")
            header.insertWidget(2, title, stretch=1)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "search chapters" if self._is_reading else "search videos")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._search_timer.start(220))
        column.addWidget(self._search)

        self._rows_host = QWidget(objectName="Bare")
        self._rows_host.setStyleSheet("background: transparent; border: none;")
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 6, 0)
        self._rows.setSpacing(6)
        self._rows.addStretch(1)
        area = scroll_area(self._rows_host)
        area.setStyleSheet("background: transparent; border: none;")
        area.viewport().setStyleSheet("background: transparent;")
        column.addWidget(area, stretch=1)

        self._panel_note = QLabel("Loading...")
        self._panel_note.setWordWrap(True)
        self._panel_note.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
        column.addWidget(self._panel_note)
        return panel

    # ---- lookups ----------------------------------------------------------
    def _start_lookups(self):
        import threading
        self._run += 1
        if self.entry.get("imdb_id"):
            # A dedicated thread, not lookup_pool - see _art_worker.
            threading.Thread(target=_art_worker,
                             args=(self._signals, self._run, dict(self.entry)),
                             daemon=True).start()
        if self._is_reading:
            # No IMDb id to hang TMDB art off - AniList's banner is the
            # reading ground (see _reading_art_worker).
            threading.Thread(target=_reading_art_worker,
                             args=(self._signals, self._run, dict(self.entry)),
                             daemon=True).start()
            if chapter_source is not None:
                lookup_pool.submit(_chapters_worker, self._signals, self._run,
                                   dict(self.entry))
            else:
                self._panel_note.setText("No chapter source in this build.")
        elif self.entry.get("imdb_id") and stremio is not None:
            kind = "movie" if self.entry.get("type") == "Movie" else "series"
            lookup_pool.submit(_meta_worker, self._signals, self._run,
                               self.entry.get("imdb_id"), kind)
        else:
            self._panel_note.setText(
                "This entry has no matched title, so there is no episode "
                "list to show. Continue below still opens it.")

    def _on_meta(self, run, meta):
        if run != self._run or self._closed:
            return
        if not meta:
            self._panel_note.setText(
                "The episode list couldn't be loaded. Check the connection "
                "and reopen this page.")
            return
        self._meta = meta
        self._videos = [v for v in (meta.get("videos") or [])
                        if isinstance(v, dict)]
        self._fill_facts()
        self._fill_seasons()
        self._fill_rows()

    def _on_art(self, run, kind, path):
        if run != self._run or self._closed:
            return
        if kind == "backdrop":
            pixmap = QPixmap(path)
            self._backdrop = None if pixmap.isNull() else pixmap
            self._backdrop_scaled = None
            self._backdrop_size = None
            self.update()
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        # Scaled by devicePixelRatio and tagged, or it blurs on any
        # non-100% display (.claude/rules/ui.md).
        ratio = self.devicePixelRatioF() or 1.0
        scaled = pixmap.scaled(int(420 * ratio), int(130 * ratio),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        self._logo.setPixmap(scaled)
        self._logo.setVisible(True)
        self._title_label.setVisible(False)

    def _on_chapters(self, run, chapters):
        if run != self._run or self._closed:
            return
        self._chapters = list(chapters or [])
        if not self._chapters:
            self._panel_note.setText("No chapters were found for this title.")
            return
        read = self._last_read()
        total = len({c.get("number") for c in self._chapters})
        self._facts.setText(
            f"{total} chapters"
            + (f"  ·  read up to {read:g}" if read else ""))
        self._fill_rows()

    # ---- the facts column ---------------------------------------------
    def _fill_facts(self):
        meta = self._meta or {}
        parts = [str(meta.get("runtime") or "").strip(),
                 str(meta.get("releaseInfo") or meta.get("year") or "").strip()]
        rating = str(meta.get("imdbRating") or "").strip()
        if rating:
            parts.append(f"★ {rating} IMDb")
        self._facts.setText("   ·   ".join(p for p in parts if p))

        def fill(row, values):
            for value in values:
                row.insertWidget(row.count() - 1, _chip(value))

        genres = [g for g in (meta.get("genres") or []) if g][:5]
        if genres:
            self._genres_head.setVisible(True)
            fill(self._genres_row, genres)
        cast = [c for c in (meta.get("cast") or []) if c][:4]
        if cast:
            self._cast_head.setVisible(True)
            fill(self._cast_row, cast)
        description = str(meta.get("description") or "").strip()
        if description:
            self._summary_head.setVisible(True)
            self._summary.setVisible(True)
            self._summary.setText(description)

    # ---- the list panel -------------------------------------------------
    def _seasons(self) -> list:
        seasons = sorted({int(v.get("season") or 0) for v in self._videos})
        # Specials (season 0) only when they are all there is.
        numbered = [s for s in seasons if s > 0]
        return numbered or seasons

    def _fill_seasons(self):
        seasons = self._seasons()
        self._season_box.blockSignals(True)
        self._season_box.clear()
        for season in seasons:
            self._season_box.addItem(f"Season {season}" if season else "Specials",
                                     season)
        # Open on the season being watched, else the newest with anything
        # aired - the one a returning viewer is actually looking for.
        watched_season, _ = self._progress()
        pick = watched_season if watched_season in seasons else (
            seasons[-1] if seasons else 0)
        index = max(0, self._season_box.findData(pick))
        self._season_box.setCurrentIndex(index)
        self._season = self._season_box.currentData() or 0
        self._season_box.blockSignals(False)
        self._sync_season_arrows()

    def _sync_season_arrows(self):
        seasons = self._seasons()
        self._prev_btn.setEnabled(bool(seasons) and self._season > seasons[0])
        self._next_btn.setEnabled(bool(seasons) and self._season < seasons[-1])

    def _step_season(self, step):
        seasons = self._seasons()
        if not seasons or self._season not in seasons:
            return
        index = seasons.index(self._season) + step
        if 0 <= index < len(seasons):
            self._season = seasons[index]
            self._season_box.setCurrentIndex(
                self._season_box.findData(self._season))
            self._sync_season_arrows()
            self._fill_rows()

    def _pick_season(self, _index):
        self._season = self._season_box.currentData() or 0
        self._sync_season_arrows()
        self._fill_rows()

    def _progress(self):
        """(season, episode) the entry's progress has reached - only when
        confirmed real, the same rule the cards follow (an unverified
        number is the tracker's guess at what is *out*, not at what has
        been watched, and a guess must not paint WATCHED badges)."""
        if not self.entry.get("progress_verified"):
            return 0, 0
        try:
            from windows.tracker import parse_episode_progress
        except ImportError:                             # pragma: no cover
            return 0, 0
        season, episode = parse_episode_progress(self.entry.get("progress"))
        return int(season or 0), int(episode or 0)

    def _last_read(self) -> float:
        try:
            return float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _clear_rows(self):
        while self._rows.count() > 1:
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) as well as deleteLater, in that order -
                # taken out of the layout, a card stays a visible child
                # at its old geometry until the event loop gets to the
                # delete, and a refill painted over half-dead rows
                # (player._fill_episode_bar hit exactly this).
                widget.setParent(None)
                widget.deleteLater()

    def _fill_rows(self):
        if self._source_pick is not None:
            self._fill_source_rows()
        elif self._is_reading:
            self._fill_chapter_rows()
        else:
            self._fill_episode_rows()

    def _fill_episode_rows(self):
        self._clear_rows()
        wanted = self._search.text().strip().lower()
        season = int(self._season or 0)
        now = datetime.datetime.now(datetime.timezone.utc)
        watched_season, watched_episode = self._progress()
        rows = [v for v in self._videos if int(v.get("season") or 0) == season]
        rows.sort(key=lambda v: int(v.get("number") or v.get("episode") or 0))
        if not rows and self.entry.get("type") == "Movie":
            self._rows.insertWidget(0, self._row_card(
                "Play Film", _pretty_date((self._meta or {}).get("released")),
                None, lambda: self._open_source_picker(None, None)))
            self._panel_note.setVisible(False)
            return
        shown = 0
        for video in rows:
            number = int(video.get("number") or video.get("episode") or 0)
            name = str(video.get("name") or video.get("title") or "").strip()
            title = f"{number}. {name}" if name else f"{number}. Episode {number}"
            if wanted and wanted not in title.lower():
                continue
            aired = _aired(video.get("firstAired") or video.get("released"))
            upcoming = bool(aired and aired > now)
            watched = (not upcoming and watched_episode
                       and (season < watched_season
                            or (season == watched_season
                                and number <= watched_episode)))
            badge = ("upcoming", "UPCOMING") if upcoming else (
                ("watched", "WATCHED") if watched else None)
            self._rows.insertWidget(shown, self._row_card(
                title, _pretty_date(video.get("firstAired") or video.get("released")),
                badge,
                # An episode chosen from this list goes through the
                # source picker - the owner's ask: no assumed default
                # here. Continue/Next in the player keep the automatic
                # smallest-preferred pick.
                (None if upcoming
                 else lambda s=season, e=number: self._open_source_picker(s, e)),
                on_menu=(None if upcoming
                         else lambda ev, s=season, e=number:
                         self._episode_menu(ev, s, e))))
            shown += 1
        self._panel_note.setVisible(shown == 0)
        if shown == 0:
            self._panel_note.setText("Nothing matches that search."
                                     if wanted else "No episodes in this season.")

    def _fill_chapter_rows(self):
        from windows.reader import (chapter_name, chapter_number,
                                    chapter_published, is_arabic)
        self._clear_rows()
        wanted = self._search.text().strip().lower()
        read_up_to = self._last_read()
        shown = 0
        for chapter in self._chapters:
            number = chapter_number(chapter)
            name = chapter_name(chapter)
            head = f"{number:g}" if number is not None else "-"
            title = f"{head}. {name}" if name else f"Chapter {head}"
            if is_arabic(chapter):
                title = f"{title}  · عربي"
            if wanted and wanted not in title.lower():
                continue
            read = number is not None and read_up_to and number <= read_up_to
            self._rows.insertWidget(shown, self._row_card(
                title, chapter_published(chapter),
                ("watched", "READ") if read else None,
                lambda n=number: self._read(n),
                on_menu=lambda ev, n=number: self._chapter_menu(ev, n)))
            shown += 1
        self._panel_note.setVisible(shown == 0)
        if shown == 0 and self._chapters:
            self._panel_note.setText("Nothing matches that search.")

    def _row_card(self, title, date_text, badge, on_click, on_menu=None):
        from windows.reader import _ElidedLabel
        card = Card(matte=True, hoverable=on_click is not None)
        card.setFixedHeight(ROW_HEIGHT)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        glyph = QLabel(ICON_PLAY_GLYPH)
        glyph.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-family: {theme.FONT_STACK_ICONS};"
            f" font-size: 13pt; background: transparent; border: none;")
        row.addWidget(glyph)

        column = QVBoxLayout()
        column.setSpacing(2)
        # Elided, and forced to a left-to-right base direction with a
        # leading LRM mark: an Arabic chapter name makes Qt lay the whole
        # paragraph out right-to-left, and a long one then clipped its
        # *start* - which is where the chapter number sits, so rows
        # showed a name trailing off with no number anywhere (the
        # owner's screenshot). With the base pinned LTR the number stays
        # hard left and the overflow becomes an ellipsis at the right.
        head = _ElidedLabel(_LTR_MARK + str(title or ""))
        head.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 600; font-size: 12pt;"
            f" background: transparent; border: none;")
        column.addWidget(head)
        if date_text:
            date = QLabel(date_text)
            date.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 10pt;"
                f" background: transparent; border: none;")
            column.addWidget(date)
        row.addLayout(column, stretch=1)

        if badge:
            kind, text = badge
            row.addWidget(_badge(text, kind))
        if on_click is not None:
            card.clicked.connect(on_click)
        if on_menu is not None:
            card.rightClicked.connect(on_menu)
        return card

    # ---- the source picker ------------------------------------------------
    def _open_source_picker(self, season, episode):
        """Swap the list panel to this episode's sources, grouped by
        resolution - nothing plays until one is chosen."""
        import threading
        self._source_pick = {"season": season, "episode": episode,
                             "streams": None}
        self._run += 1
        threading.Thread(
            target=_sources_worker,
            args=(self._signals, self._run, dict(self.entry), season, episode),
            daemon=True).start()
        self._search.clear()
        self._fill_rows()

    def _close_source_picker(self):
        self._source_pick = None
        self._search.clear()
        self._fill_rows()

    def _on_sources(self, run, found, key):
        if run != self._run or self._closed:
            return
        pick = self._source_pick
        if not pick or (pick.get("season"), pick.get("episode")) != tuple(key):
            return
        pick["streams"] = [s for s in (found or [])
                           if s.get("kind") != "drm"]
        self._fill_rows()

    def _fill_source_rows(self):
        """The sources for the picked episode: a back row, then one
        heading per resolution with its files under it - source name,
        seeders, size and the release line, exactly what choosing needs."""
        self._clear_rows()
        pick = self._source_pick or {}
        season, episode = pick.get("season"), pick.get("episode")
        name = (f"S{int(season or 1):02d}E{int(episode):02d}"
                if episode else "this film")
        self._rows.insertWidget(0, self._row_card(
            "‹  Back to episodes", "", None, self._close_source_picker))
        shown = 1

        streams_found = pick.get("streams")
        if streams_found is None:
            self._panel_note.setVisible(True)
            self._panel_note.setText(f"Finding sources for {name}...")
            return
        if not streams_found:
            self._panel_note.setVisible(True)
            self._panel_note.setText(
                f"No playable source was found for {name}. Try again in "
                "a moment.")
            return

        try:
            from helpers import streams as streams_helper
        except Exception:                               # pragma: no cover
            streams_helper = None
        wanted = self._search.text().strip().lower()
        groups = (streams_helper.qualities(streams_found)
                  if streams_helper else [])
        if any(not (s.get("quality") or "") for s in streams_found):
            groups = groups + [""]
        for quality in groups:
            members = [s for s in streams_found
                       if (("2160p" if (s.get("quality") or "").lower() == "4k"
                            else (s.get("quality") or "").lower()) == quality)]
            if not members:
                continue
            visible = []
            for stream in members:
                text = f"{stream.get('source') or ''} {stream.get('title') or ''}".lower()
                if wanted and wanted not in text:
                    continue
                visible.append(stream)
            if not visible:
                continue
            heading = QLabel("4K (2160p)" if quality == "2160p"
                             else (quality or "Other").upper())
            heading.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 11pt; font-weight: 700;"
                f" letter-spacing: 1px; background: transparent; border: none;"
                f" padding: 6px 2px 0px 2px;")
            self._rows.insertWidget(shown, heading)
            shown += 1
            for stream in visible:
                seeders = int(stream.get("seeders") or 0)
                size = ""
                if streams_helper:
                    size = streams_helper.format_size(stream.get("size_bytes"))
                parts = [p for p in
                         (f"{seeders} seeders" if seeders else "",
                          size, (stream.get("title") or "").strip()[:70]) if p]
                self._rows.insertWidget(shown, self._row_card(
                    stream.get("source") or "Source", " · ".join(parts), None,
                    lambda s=stream: self._play_stream_choice(s)))
                shown += 1
        self._panel_note.setVisible(shown <= 1)
        if shown <= 1:
            self._panel_note.setText("Nothing matches that search.")

    def _play_stream_choice(self, stream):
        """Play exactly the chosen source. The rest of the list rides
        along behind it so a dead pick can still fall through, but the
        chosen one leads - nothing is re-ranked over the user's head."""
        pick = self._source_pick or {}
        season, episode = pick.get("season"), pick.get("episode")
        ordered = [stream] + [s for s in (pick.get("streams") or [])
                              if s is not stream]
        self._source_pick = None
        self._fill_rows()
        try:
            from windows import player
            page = player.open_player(self.window(), self.entry,
                                      season=season, episode=episode,
                                      streams=ordered)
            if page is not None:
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the chosen source")

    # ---- actions ---------------------------------------------------------
    def _play(self, season, episode):
        try:
            from windows import player
            page = player.open_player(self.window(), self.entry,
                                      season=season, episode=episode)
            if page is not None:
                # Re-badge the rows the moment the player closes: an
                # episode just watched must read WATCHED without leaving
                # and reopening this page.
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the player")

    def _read(self, number):
        try:
            from windows import reader
            page = reader.open_reader(self.window(), self.entry,
                                      chapter_number=number)
            if page is not None:
                page.closed.connect(self._on_overlay_closed)
        except Exception:
            logs.exception("details page could not open the reader")

    def _on_overlay_closed(self):
        """A player or reader opened from a row has closed - re-read this
        entry from disk (that is where they wrote progress) and redraw
        the badges."""
        if self._closed:
            return
        try:
            from helpers import storage
            from windows.tracker import AnimePage, SeriesPage
            data_file = (SeriesPage.DATA_FILE
                         if self.entry.get("type") in ("Series", "Movie")
                         else AnimePage.DATA_FILE)
            fresh = next((e for e in storage.load(data_file, [])
                          if e.get("id") == self.entry.get("id")), None)
            if fresh:
                self.entry.update(fresh)
        except Exception:
            logs.exception("details page could not re-read the entry")
        if self._is_reading:
            read = self._last_read()
            total = len({c.get("number") for c in self._chapters})
            if self._chapters:
                self._facts.setText(
                    f"{total} chapters"
                    + (f"  ·  read up to {read:g}" if read else ""))
        self._fill_rows()

    # ---- marking watched / read ---------------------------------------
    def _aired_last_episode(self, season) -> int:
        """The last already-aired episode number of `season`, from the
        Cinemeta list this page is already holding."""
        now = datetime.datetime.now(datetime.timezone.utc)
        last = 0
        for video in self._videos:
            if int(video.get("season") or 0) != int(season or 0):
                continue
            aired = _aired(video.get("firstAired") or video.get("released"))
            if aired is not None and aired > now:
                continue
            last = max(last, int(video.get("number")
                                 or video.get("episode") or 0))
        return last

    def _episode_menu(self, event, season, episode):
        """Right-click on an episode row: move the watched mark - one
        episode, or the whole season - in either direction, through
        tracker.correct_progress (the one writer allowed to move a
        number down)."""
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return
        watched_season, watched_episode = self._progress()
        already = (watched_season, watched_episode) >= (season, episode)
        menu = QMenu(self)
        mark = menu.addAction("Mark as Unwatched" if already
                              else "Mark as Watched")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Watched")
        clear_all = menu.addAction("Mark All as Unwatched")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is mark:
            if already:
                if episode <= 1:
                    target = ((season - 1, self._aired_last_episode(season - 1))
                              if season > 1 else (0, 0))
                else:
                    target = (season, episode - 1)
            else:
                target = (season, episode)
        elif chosen is mark_all:
            target = (season, max(1, self._aired_last_episode(season)))
        elif chosen is clear_all:
            target = ((season - 1, self._aired_last_episode(season - 1))
                      if season > 1 else (0, 0))
        else:
            return
        if target[1] <= 0:
            # Nothing watched at all: cleared directly, because
            # correct_progress refuses a zero episode.
            self._clear_video_progress()
        elif not correct_progress(self.entry, season=target[0],
                                  episode=target[1]):
            show_toast(self, "Could Not Save That")
            return
        self._fill_rows()

    def _clear_video_progress(self):
        from helpers import storage
        from windows.tracker import _progress_data_file
        fields = {"progress": "", "progress_verified": False,
                  "updated_at": storage.now_iso()}
        self.entry.update(fields)
        try:
            storage.update_entry(_progress_data_file(self.entry),
                                 self.entry.get("id"), fields)
        except Exception:
            logs.exception("details page could not clear progress")

    def _chapter_menu(self, event, number):
        """Right-click on a chapter row - the reading mirror of
        _episode_menu, against the read-up-to chapter number."""
        try:
            from windows.tracker import correct_progress
        except ImportError:                             # pragma: no cover
            return
        if number is None:
            return
        from windows.reader import chapter_number
        already = bool(self._last_read() and number <= self._last_read())
        menu = QMenu(self)
        mark = menu.addAction("Mark as Unread" if already else "Mark as Read")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Read")
        clear_all = menu.addAction("Mark All as Unread")
        chosen = menu.exec(event.globalPosition().toPoint())
        numbers = [c_num for c_num in
                   (chapter_number(c) for c in self._chapters)
                   if c_num is not None]
        if chosen is mark:
            if already:
                earlier = [n for n in numbers if n < number]
                target = max(earlier) if earlier else 0.0
            else:
                target = number
        elif chosen is mark_all:
            target = max(numbers) if numbers else 0.0
        elif chosen is clear_all:
            target = 0.0
        else:
            return
        if not correct_progress(self.entry, chapter=target):
            show_toast(self, "Could Not Save That")
            return
        read = self._last_read()
        total = len({c.get("number") for c in self._chapters})
        self._facts.setText(
            f"{total} chapters"
            + (f"  ·  read up to {read:g}" if read else ""))
        self._fill_rows()

    # ---- downloading ----------------------------------------------------
    def _download_current(self):
        if self._is_reading:
            self._open_chapter_download_dialog()
        else:
            self._open_episode_download_dialog()

    def _dialog_shell(self, title):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(520)
        theme.apply_dark_titlebar(dialog)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        return dialog, column

    @staticmethod
    def _dialog_buttons(dialog, column, on_start):
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        use_hover_cursor(cancel_btn)
        cancel_btn.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_btn)
        start_btn = QPushButton("Download", objectName="Accent")
        use_hover_cursor(start_btn)
        start_btn.clicked.connect(on_start)
        buttons.addWidget(start_btn)
        column.addLayout(buttons)
        return start_btn

    def _folder_row(self, column, dialog):
        """The shared saving-to row; returns a callable yielding the
        currently chosen folder."""
        from windows import downloads_page
        state = {"folder": downloads_page.saved_folder()}
        row = QHBoxLayout()
        row.addWidget(QLabel("Saving to:", objectName="Muted"))
        label = QLabel(state["folder"])
        label.setToolTip(state["folder"])
        row.addWidget(label, stretch=1)
        change = QPushButton("Change...")
        use_hover_cursor(change)

        def pick():
            chosen = downloads_page.choose_folder(dialog, state["folder"])
            if chosen:
                state["folder"] = chosen
                label.setText(chosen)
                label.setToolTip(chosen)
        change.clicked.connect(pick)
        row.addWidget(change)
        column.addLayout(row)
        return lambda: state["folder"]

    def _open_episode_download_dialog(self):
        """Which episodes, which audio, where - then queue.

        One dialog instead of the two buttons the owner rightly called
        redundant: scope covers this episode, a chosen range, or the
        whole season."""
        from helpers import downloads
        is_movie = self.entry.get("type") == "Movie"
        season = int(self._season or 1)
        last = self._aired_last_episode(season)
        if not is_movie and last <= 0:
            show_toast(self, "No Aired Episodes to Download in This Season")
            return

        dialog, column = self._dialog_shell(
            "Download Film" if is_movie else "Download Episodes")

        scope = QComboBox()
        first_box, last_box = QComboBox(), QComboBox()
        if not is_movie:
            scope.addItems(["One Episode", "A Range of Episodes",
                            "The Whole Season"])
            use_hover_cursor(scope)
            scope_row = QHBoxLayout()
            scope_row.addWidget(QLabel(f"Season {season}:"))
            scope_row.addWidget(scope, stretch=1)
            column.addLayout(scope_row)

            # Default to what Continue would play, clamped to aired.
            watched_season, watched_episode = self._progress()
            current = (min(watched_episode + 1, last)
                       if watched_season == season and watched_episode else 1)
            for box in (first_box, last_box):
                box.addItems([f"Episode {n}" for n in range(1, last + 1)])
                box.setCurrentIndex(current - 1)
                use_hover_cursor(box)
            range_row = QHBoxLayout()
            range_row.addWidget(QLabel("From:"))
            range_row.addWidget(first_box, stretch=1)
            range_row.addWidget(QLabel("To:"))
            range_row.addWidget(last_box, stretch=1)
            column.addLayout(range_row)

        # Which audio the picked release should carry. A preference over
        # release names (dual-audio/dub tags), not a track switch - see
        # downloads._order_by_audio for what it can and cannot promise.
        audio_box = QComboBox()
        audio_box.addItem("Japanese (original)", "jp")
        audio_box.addItem("English dub (when a release has one)", "en")
        use_hover_cursor(audio_box)
        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("Audio:"))
        audio_row.addWidget(audio_box, stretch=1)
        column.addLayout(audio_row)

        count_label = QLabel("", objectName="Muted")
        count_label.setWordWrap(True)
        column.addWidget(count_label)
        folder_of = self._folder_row(column, dialog)

        def picked_numbers():
            if is_movie:
                return []
            mode = scope.currentIndex()
            if mode == 0:
                return [first_box.currentIndex() + 1]
            if mode == 2:
                return list(range(1, last + 1))
            low = min(first_box.currentIndex(), last_box.currentIndex()) + 1
            high = max(first_box.currentIndex(), last_box.currentIndex()) + 1
            return list(range(low, high + 1))

        def sync(*_args):
            if is_movie:
                count_label.setText("The film will be queued for download.")
                return
            ranged = scope.currentIndex() == 1
            first_box.setEnabled(scope.currentIndex() != 2)
            last_box.setEnabled(ranged)
            count = len(picked_numbers())
            count_label.setText(
                f"{count} episode{'s' if count != 1 else ''} of season "
                f"{season} will be queued.")
        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)
        sync()

        def start():
            numbers = picked_numbers()
            audio = audio_box.currentData()
            try:
                if is_movie:
                    downloads.queue_episode(self.entry, audio=audio,
                                            folder=folder_of())
                elif len(numbers) == 1:
                    downloads.queue_episode(self.entry, season=season,
                                            episode=numbers[0], audio=audio,
                                            folder=folder_of())
                else:
                    if QMessageBox.question(
                            dialog, "Download Episodes",
                            f"Queue {len(numbers)} episodes of season "
                            f"{season} for download?"
                    ) != QMessageBox.StandardButton.Yes:
                        return
                    downloads.queue_season(self.entry, season=season,
                                           episodes=numbers, audio=audio,
                                           folder=folder_of())
            except Exception:
                logs.exception("details page could not queue a download")
                show_toast(self, "Could Not Queue That Download")
                dialog.reject()
                return
            dialog.accept()
            show_toast(self, "Queued - See the Downloads Page")

        self._dialog_buttons(dialog, column, start)
        dialog.exec()

    def _open_chapter_download_dialog(self):
        """Which chapters - one, or a chosen range - then queue as .cbz.
        The same shape the reader's own download dialog has."""
        from helpers import downloads
        from windows.reader import chapter_number, chapter_title, is_arabic
        if not self._chapters:
            show_toast(self, "The Chapter List Is Still Loading")
            return
        # One language, like the reader's dialog: the list arrives
        # grouped by language, and a numeric range across the join holds
        # the same chapter twice - the second file overwrites the first.
        anchor_lang = str(self._chapters[0].get("lang") or "").lower()
        arabic = [c for c in self._chapters if is_arabic(c)]
        candidates = arabic or [
            c for c in self._chapters
            if str(c.get("lang") or "").lower() == anchor_lang]
        if not candidates:
            candidates = list(self._chapters)

        dialog, column = self._dialog_shell("Download Chapters")
        scope = QComboBox()
        scope.addItems(["One Chapter", "A Range of Chapters"])
        use_hover_cursor(scope)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Download:"))
        scope_row.addWidget(scope, stretch=1)
        column.addLayout(scope_row)

        # Default to the chapter being read, when it is in the list.
        read = self._last_read()
        current = next((i for i, c in enumerate(candidates)
                        if chapter_number(c) == read), 0)
        labels = [chapter_title(c) for c in candidates]
        first_box, last_box = QComboBox(), QComboBox()
        for box in (first_box, last_box):
            box.addItems(labels)
            box.setCurrentIndex(current)
            use_hover_cursor(box)
            box.view().setMinimumWidth(420)
            box.view().setFont(box.font())
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("From:"))
        range_row.addWidget(first_box, stretch=1)
        range_row.addWidget(QLabel("To:"))
        range_row.addWidget(last_box, stretch=1)
        column.addLayout(range_row)

        count_label = QLabel("", objectName="Muted")
        count_label.setWordWrap(True)
        column.addWidget(count_label)
        folder_of = self._folder_row(column, dialog)

        def picked():
            if scope.currentIndex() == 0:
                return [candidates[first_box.currentIndex()]]
            low = min(first_box.currentIndex(), last_box.currentIndex())
            high = max(first_box.currentIndex(), last_box.currentIndex())
            # Reversed: the list runs newest first, and a range should
            # land on disk oldest first so the queue reads in order.
            return list(reversed(candidates[low:high + 1]))

        def sync(*_args):
            last_box.setEnabled(scope.currentIndex() == 1)
            count = len(picked())
            count_label.setText(
                f"{count} chapter{'s' if count != 1 else ''} will be saved "
                f"as .cbz file{'s' if count != 1 else ''}.")
        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)
        sync()

        def start():
            chapters = picked()
            if len(chapters) > 1 and QMessageBox.question(
                    dialog, "Download Chapters",
                    f"Queue {len(chapters)} chapters for download?"
            ) != QMessageBox.StandardButton.Yes:
                return
            try:
                downloads.queue_chapters(self.entry, chapters,
                                         folder=folder_of())
            except Exception:
                logs.exception("details page could not queue chapters")
                show_toast(self, "Could Not Queue Those Chapters")
                dialog.reject()
                return
            dialog.accept()
            show_toast(self, "Queued - See the Downloads Page")

        self._dialog_buttons(dialog, column, start)
        dialog.exec()

    def _continue(self):
        """Resume where watching/reading stopped - the page's primary
        action, and with the cards' round button gone, the only one.
        Through open_tracker_entry so an entry the in-app player cannot
        serve still falls back to its browser route."""
        try:
            from windows import tracker
            tracker.open_tracker_entry(self, self.entry, resume=True)
        except Exception:
            logs.exception("details page could not continue the entry")

    def _on_inapp_closed(self, _entry_id):
        """tracker._wire_overlay_refresh's hook - a player or reader
        opened by Continue has closed; same refresh as a row's own."""
        self._on_overlay_closed()

    # ---- painting ----------------------------------------------------------
    def paintEvent(self, event):
        super().paintEvent(event)
        if self._backdrop is None:
            return
        painter = QPainter(self)
        rect = self.rect()
        # Scaled once per size, not per paint: backdrops are the
        # full-resolution originals now (artwork.BACKDROP_SIZE), and
        # smooth-scaling several megapixels on every repaint would make
        # hovering the list stutter.
        if self._backdrop_scaled is None or self._backdrop_size != rect.size():
            self._backdrop_scaled = self._backdrop.scaled(
                rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            self._backdrop_size = rect.size()
        scaled = self._backdrop_scaled
        painter.drawPixmap(int((rect.width() - scaled.width()) / 2),
                           int((rect.height() - scaled.height()) / 2), scaled)
        gradient = QLinearGradient(0.0, 0.0, 0.0, float(rect.height()))
        for stop, red, green, blue, alpha in SCRIM:
            gradient.setColorAt(stop, QColor(red, green, blue, alpha))
        painter.fillRect(rect, gradient)
        painter.end()

    # ---- lifetime ----------------------------------------------------------
    def follow(self, host):
        """Track the host's size - the same overlay contract the reader
        documents (there is no layout to join over hand-positioned
        pages)."""
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_host", None) and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.leave()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.BackButton:
            self.leave()
            event.accept()
            return
        super().mousePressEvent(event)

    def leave(self):
        if self._closed:
            return
        self._closed = True
        self._run += 1
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        self.deleteLater()


def open_details(window, entry):
    """Put the details page over `window`. The one entry point - the same
    central-widget host trick reader.open_reader documents, so the
    sidebar is covered rather than hidden."""
    host = window.centralWidget() if hasattr(window, "centralWidget") else window
    host = host if host is not None else getattr(window, "container", window)
    page = DetailsPage(entry, host)
    page.follow(host)
    page.show()
    page.raise_()
    page.setFocus()
    return page
