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
import uuid

from PyQt6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QVBoxLayout, QWidget,
)

from helpers import (app_settings, artwork, history, images, logs, lookup_pool,
                     net, storage, theme)
from helpers.widgets import (Card, GlassPage, GlyphButton, confirm,
                             frameless_dialog, scroll_area, show_toast,
                             use_hover_cursor)

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
SCRIM = ((0.0, 14, 12, 9, 200), (0.45, 14, 12, 9, 170), (1.0, 14, 12, 9, 232))

CHAPTER_LIST_TIMEOUT = 45.0

# U+200E LEFT-TO-RIGHT MARK, built with chr() so no invisible character
# sits in this file waiting for a re-encoding tool to mangle it (the
# same reason the Fluent glyphs are escapes). Prefixed onto every list
# row title: an Arabic name otherwise flips the whole paragraph RTL and
# the clipped end is the *start* - the chapter number.
_LTR_MARK = chr(0x200E)

# Fluent glyphs, as escapes on purpose (see reader.py - a re-encoding
# tool turns the bare characters into mojibake, and it has happened).
ICON_BACK = "\ue76b"                  # ChevronLeft - the sidebar's own
                                      # fold glyph, which the player's
                                      # exit and the reader's leave now
                                      # carry too, so one shape means
                                      # "back" everywhere (owner's ask)
ICON_FULLSCREEN = "\ue740"
ICON_EXIT_FULLSCREEN = "\ue73f"
ICON_SEARCH = "\ue721"
# The list panel's re-ask button - the same Refresh glyph the reader's
# top bar carries, so "look again" is one shape across the app.
ICON_REFRESH = "\ue72c"
ICON_PLAY_GLYPH = "\ue768"


class _Signals(QObject):
    meta = Signal(int, object)          # run, Cinemeta meta dict or None
    art = Signal(int, str, str)         # run, "logo"/"backdrop", local path
    chapters = Signal(int, object)      # run, chapter list or None
    sources = Signal(int, object, object)  # run, stream list, (season, ep)
    # A reading title's MangaDex genre tags - run, [names]. Video pages
    # get genres free with the Cinemeta meta; this is the reading pair.
    reading_genres = Signal(int, object)
    # Resolving a Discover reading title on a picked site - run, the
    # fields to store ({url, site_id}) or None, the site's name.
    site_resolved = Signal(int, object, str)
    # A just-saved entry's downloaded cover: entry id, data file, the
    # cover URL it came from ("" when the entry already knew it), local
    # path. Crossed back so the storage write happens on the UI thread
    # with every other writer, not from the pool.
    saved_cover = Signal(str, str, str, str)


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


def _reading_genres_worker(signals, run, title):
    """MangaDex's genre tags for this title, for the genre buttons.
    Never raises - the pool's worker thread dies silently otherwise."""
    try:
        from helpers import discover
        names = discover.reading_genres(title)
    except Exception:
        names = []
    signals.reading_genres.emit(run, list(names or []))


def _site_resolve_worker(signals, run, site, title):
    """Find `title` on one picked reading site and hand back the fields
    that bind the entry to it. Best title-matched result wins; a site
    that answers nothing usable reports None so the page can say so and
    re-offer the list. Never raises."""
    fields, site_name = None, str(site.get("name") or "the site")
    try:
        from helpers import manga_sites, title_match
        results = manga_sites.search_site(site, title,
                                          deadline=net.deadline_in(12))
        best, best_score = None, 0.0
        for row in results or []:
            score = title_match.similarity(title, row.get("title") or "")
            if score > best_score:
                best, best_score = row, score
        # 0.5, not the schedule lookups' 0.85: the user picked this site
        # by hand and is about to see the chapter list it produces, so a
        # looser match is checkable in a way a silent background lookup
        # never is - and these sites localize titles enough that 0.85
        # would refuse real hits.
        if best is not None and best_score >= 0.5 and best.get("url"):
            fields = {"url": best["url"], "site_id": site.get("id")}
            if best.get("cover_url"):
                fields["cover_url"] = best["cover_url"]
    except Exception:
        fields = None
    signals.site_resolved.emit(run, fields, site_name)


def _chapters_worker(signals, run, entry, refresh=False):
    """`refresh` skips chapter_source's six-hour disk cache - what the
    panel's refresh button passes, and the only way a title that
    published an hour ago shows its new chapter today.

    The first page is drawn as soon as it parses rather than after the
    whole paginated list is in: a full list is up to twenty-four more
    fetches (21.7s measured on olympustaff), and the newest chapters -
    the ones on page one - are what someone opening a series is nearly
    always after. Every emission is a superset of the last, so the panel
    just refills."""
    def partial(found):
        signals.chapters.emit(run, list(found or []))

    try:
        chapters = chapter_source.list_chapters(
            entry, deadline=net.deadline_in(CHAPTER_LIST_TIMEOUT),
            refresh=refresh, on_partial=partial)
    except Exception:
        logs.exception("details chapter listing failed")
        # None, not []: an exception here is "the lookup failed", and
        # collapsing it into an empty list made a dead connection read
        # as "this title has no chapters" - with no retry, since the
        # page believed it had its answer. _on_chapters retries a None
        # once and only then says so.
        signals.chapters.emit(run, None)
        return
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


def _chip_button(text) -> QPushButton:
    """A chip that can be pressed - the genre buttons (the owner's ask:
    the genres open everything else filed under them). Deliberately the
    _chip look at rest, so the facts row stays one design; hover borrows
    the accent border every other clickable thing here answers with."""
    button = QPushButton(text)
    button.setStyleSheet(
        f"QPushButton {{ color: {theme.TEXT}; background: {theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.BORDER};"
        f" border-radius: {theme.RADIUS}px; padding: 5px 14px;"
        f" font-weight: 600; font-size: 10.5pt; }}"
        f"QPushButton:hover {{ border: 1px solid {theme.ACCENT};"
        f" color: {theme.ACCENT}; }}")
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    use_hover_cursor(button)
    return button


def _badge(text, kind) -> QLabel:
    """DONE in the accent, UPCOMING in the success green - the two
    states the owner's reference picture colours differently. One word
    for finished across both media (the owner's ask), where this used to
    say WATCHED on episodes and READ on chapters."""
    colours = {"watched": (theme.ON_ACCENT, theme.ACCENT_GRADIENT, theme.ACCENT),
               "upcoming": ("#0d1206", theme.SUCCESS, theme.SUCCESS)}
    fg, bg, border = colours[kind]
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {fg}; background: {bg}; border: 1px solid {border};"
        f" border-radius: {theme.RADIUS_SM}px; padding: 2px 10px;"
        f" font-weight: 700; font-size: 8.5pt;")
    return label


class _PanelNote(QLabel):
    """The status note centred over the list panel's viewport.

    It is a viewport child placed by hand (a layout row would be
    destroyed by every refill - see _build_panel), and the by-hand
    placement used to hold two bugs the owner saw as one clipped,
    off-centre message: the geometry was computed in the page's
    resizeEvent from the viewport's size *at that moment*, which is
    stale (the splitter's layout settles after the page's own resize),
    and nothing re-ran the layout when the text changed, so a long
    error kept a short predecessor's box. Now the note watches the
    viewport's own Resize - never stale - and re-lays itself on every
    setText."""

    def __init__(self, text, viewport):
        super().__init__(text, viewport)
        self._viewport = viewport
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The size is set programmatically rather than in the
        # stylesheet: the wrapped height below comes from fontMetrics(),
        # which does not see a QSS font-size.
        font = self.font()
        font.setPointSizeF(11.5)
        self.setFont(font)
        self.setStyleSheet(f"color: {theme.TEXT_MUTED};"
                           f" background: transparent; border: none;")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        viewport.installEventFilter(self)
        self.relayout()

    def eventFilter(self, obj, event):
        if obj is self._viewport and event.type() in (QEvent.Type.Resize,
                                                      QEvent.Type.Show):
            self.relayout()
        return False

    def setText(self, text):
        super().setText(text)
        self.relayout()

    def relayout(self):
        try:
            width = max(80, self._viewport.width() - 40)
            # Ints, not the enums: TextFlag and AlignmentFlag are
            # different enum types in PyQt6 and cannot be |'d directly.
            flags = (int(Qt.TextFlag.TextWordWrap.value)
                     | int(Qt.AlignmentFlag.AlignHCenter.value))
            wrapped = self.fontMetrics().boundingRect(
                QRect(0, 0, width, 0), flags, self.text())
            height = max(wrapped.height(), self.fontMetrics().height())
            self.setGeometry(20,
                             max(0, (self._viewport.height() - height) // 2),
                             width, height)
            self.raise_()
        except RuntimeError:
            pass        # the panel is being torn down


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
        # One-shot latches for the silent retry each list lookup gets
        # (see _on_meta/_on_chapters); re-armed by every fresh start.
        self._meta_retried = False
        self._chapters_retried = False

        # The source-picker step: filled in when an episode row is
        # clicked, cleared by its back row or by picking a source.
        # {"season", "episode", "streams" (None while looking)}.
        self._source_pick = None
        # A Discover reading title with no site yet: the list panel
        # offers the configured reading sites first (the owner's ask),
        # and this stays True until one is picked and answers.
        self._site_choice_pending = False
        # Per-episode/chapter ticks from helpers/history - what lets an
        # *unsaved* title be marked watched at all, and what lets a
        # saved one be ticked out of order (its progress is one number,
        # which cannot say "5 and 7 but not 6"). Read once here and kept
        # in step by _mark_history.
        self._history_marks = history.watched_keys(self.entry)

        self._signals = _Signals()
        self._signals.meta.connect(self._on_meta)
        self._signals.art.connect(self._on_art)
        self._signals.chapters.connect(self._on_chapters)
        self._signals.sources.connect(self._on_sources)
        self._signals.saved_cover.connect(self._on_saved_cover)
        self._signals.reading_genres.connect(self._on_reading_genres)
        self._signals.site_resolved.connect(self._on_site_resolved)

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
            f"QFrame {{ background: rgba(17, 14, 10, 210);"
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
        # Wide enough for a two-digit season plus the drop arrow. Without
        # this the box is sized for "Season 2" and "Season 22" is cut off
        # mid-word (the owner's screenshot): QComboBox sizes to its
        # *current* item, and the padding and arrow are not counted by
        # the length hint, hence a floor as well.
        self._season_box.setMinimumContentsLength(11)
        self._season_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._season_box.setMinimumWidth(150)
        self._season_box.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS}px;"
            f" padding: 8px 16px; font-weight: 700; font-size: 12pt; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}"
            # The popup list, too: it inherits none of the above, and at
            # its default width the same two-digit names clipped.
            f"QComboBox QAbstractItemView {{ background: {theme.SURFACE};"
            f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
            f" selection-background-color: {theme.SURFACE_ACTIVE};"
            f" outline: none; padding: 4px; }}")
        use_hover_cursor(self._season_box)
        self._season_box.activated.connect(self._pick_season)
        header.addWidget(self._prev_btn)
        header.addStretch(1)
        header.addWidget(self._season_box)
        header.addStretch(1)
        header.addWidget(self._next_btn)
        # Re-ask the source for this list (the owner's ask). Both lists
        # are cached - chapters on disk for six hours, the episode meta
        # by the session - so a title that has just published something
        # otherwise shows a stale list with no way to say "look again".
        # widgets.GlyphButton, not a QPushButton carrying the glyph as
        # text: this rendered as an empty box on the owner's screen, and
        # reader._glyph_button records exactly why. A widget stylesheet
        # merges with the app-wide one property by property, so a rule
        # that names no padding inherits QPushButton's `8px 16px` - on a
        # 38px button that leaves ~6px of content width and Qt draws a
        # clipped box instead of the icon. GlyphButton paints the glyph
        # onto its own rect, with no padding in the arithmetic at all.
        self._refresh_btn = GlyphButton(
            ICON_REFRESH,
            "Look for new " + ("chapters" if self._is_reading else "episodes"))
        self._refresh_btn.clicked.connect(self._refresh_list)
        header.addWidget(self._refresh_btn)
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

        # A Discover title arrives here unsaved - no id (see
        # tracker._discover_entry) - and this list is where deciding to
        # keep it happens, so Save lives at the top of it (the owner's
        # ask). An entry opened from Saved never shows it.
        self._save_btn = QPushButton("+  Save to My List", objectName="Accent")
        self._save_btn.setFixedHeight(44)
        use_hover_cursor(self._save_btn)
        self._save_btn.clicked.connect(self._save_entry)
        self._save_btn.setVisible(not self.entry.get("id"))
        column.addWidget(self._save_btn)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "search chapters" if self._is_reading else "search videos")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._search_timer.start(220))
        column.addWidget(self._search)

        # No inline "background: transparent" on the host/area here: a
        # declaration-only stylesheet on a widget cascades to every
        # descendant and outranks the app stylesheet, which was silently
        # killing the row Cards' matte fill and their hover ring
        # (measured: with the sheet on the area, a hovered #Card painted
        # nothing; without it, the ACCENT ring and ACCENT_SOFT fill).
        # theme.py's QScrollArea and #Bare rules already make the area
        # and host transparent without touching their children.
        self._rows_host = QWidget(objectName="Bare")
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 6, 0)
        self._rows.setSpacing(6)
        self._rows.addStretch(1)
        area = scroll_area(self._rows_host)
        column.addWidget(area, stretch=1)

        # Centred over the list rather than tucked under it. "Finding
        # sources for S01E07..." used to sit in the bottom-left corner,
        # where it read as a footnote and was easy to miss entirely (the
        # owner's ask).
        #
        # A child of the scroll area's viewport, not a row in the column:
        # rows come and go with every refill (_clear_rows empties that
        # layout), and a note living in it would be destroyed by the
        # first list it was meant to describe. _PanelNote lays itself
        # out - see its docstring for the two bugs the by-hand placement
        # used to have.
        self._panel_note = _PanelNote("Loading...", area.viewport())
        return panel

    # ---- lookups ----------------------------------------------------------
    def _start_lookups(self):
        import threading
        self._run += 1
        self._meta_retried = False
        self._chapters_retried = False
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
            lookup_pool.submit(_reading_genres_worker, self._signals,
                               self._run, self.entry.get("title") or "")
            if chapter_source is None:
                self._panel_note.setText("No chapter source in this build.")
            elif (not self.entry.get("url") and not self.entry.get("site_id")
                    and self._reading_site_choices()):
                # A Discover title has no site yet - offer the configured
                # reading sites first (the owner's ask), and fetch the
                # chapters from whichever one is picked.
                self._fill_site_rows()
            else:
                lookup_pool.submit(_chapters_worker, self._signals, self._run,
                                   dict(self.entry))
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
            # One silent retry before admitting defeat: the owner
            # reported the list "sometimes fetches nothing" on titles
            # that load fine a moment later - a single flaky Cinemeta
            # answer, and the page treated it as final. The note keeps
            # its loading text while the retry runs; the refresh button
            # stays disarmed for the same reason.
            if not self._meta_retried and self.entry.get("imdb_id"):
                self._meta_retried = True
                kind = ("movie" if self.entry.get("type") == "Movie"
                        else "series")
                lookup_pool.submit(_meta_worker, self._signals, self._run,
                                   self.entry.get("imdb_id"), kind)
                return
            self._finish_refresh()
            self._panel_note.setText(
                "The episode list couldn't be loaded. Check the connection "
                "and reopen this page.")
            return
        self._finish_refresh()
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
        if chapters is None:
            # The lookup *failed* (see _chapters_worker) - distinct from
            # a title that genuinely has no chapters. One silent retry,
            # without refresh=True even if a refresh asked: letting the
            # disk cache answer the second attempt turns "error" into a
            # slightly stale list, which is the better failure.
            if not self._chapters_retried:
                self._chapters_retried = True
                lookup_pool.submit(_chapters_worker, self._signals,
                                   self._run, dict(self.entry))
                return
            self._finish_refresh()
            self._panel_note.setText(
                "The chapter list couldn't be loaded. Check the "
                "connection and press the refresh button to try again.")
            return
        self._finish_refresh()
        self._chapters = list(chapters)
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
            self._fill_genre_buttons(genres)
        cast = [c for c in (meta.get("cast") or []) if c][:4]
        if cast:
            self._cast_head.setVisible(True)
            fill(self._cast_row, cast)
        description = str(meta.get("description") or "").strip()
        if description:
            self._summary_head.setVisible(True)
            self._summary.setVisible(True)
            self._summary.setText(description)

    # ---- genres as doors --------------------------------------------
    def _fill_genre_buttons(self, genres):
        """The genre chips, pressable: each one opens everything else
        filed under that genre (the owner's ask). Replaces whatever the
        row held - the reading lookup can land after a rebuild."""
        while self._genres_row.count() > 1:
            item = self._genres_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for genre in genres:
            button = _chip_button(genre)
            button.clicked.connect(
                lambda _checked=False, g=genre: self._open_genre_browse(g))
            self._genres_row.insertWidget(self._genres_row.count() - 1, button)

    def _on_reading_genres(self, run, names):
        if run != self._run or self._closed or not names:
            return
        self._genres_head.setVisible(True)
        self._fill_genre_buttons([str(n) for n in names][:5])

    def _open_genre_browse(self, genre):
        """Open the genre as its own full page over the window - not a
        dialog (the owner's ask). Hosted on the central widget like this
        page is, so it covers the sidebar the same way and Back/Escape
        lands here again."""
        window = self.window()
        host = (window.centralWidget() if hasattr(window, "centralWidget")
                else window)
        host = host if host is not None else window
        page = GenreBrowsePage(genre, self._is_reading, host)
        page.open_title = self._open_browsed_title
        page.follow(host)
        page.show()
        page.raise_()
        page.setFocus()
        return page

    def _open_browsed_title(self, item, entry_type):
        """A pick from the genre browse opens its own details page over
        this one - the same transient-entry road a Discover card takes
        (tracker._discover_entry states why it is id-less)."""
        try:
            from windows.tracker import discover_entry
            open_details(self.window(), discover_entry(item, entry_type))
        except Exception:
            logs.exception("genre browse could not open the details page")

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
        elif self._site_choice_pending:
            self._fill_site_rows()
        elif self._is_reading:
            self._fill_chapter_rows()
        else:
            self._fill_episode_rows()

    def _refresh_list(self):
        """Ask the source for this list again (the panel's refresh
        button). Reading skips chapter_source's six-hour disk cache;
        video simply re-fetches the Cinemeta meta, which is not cached.
        The button disables itself until the answer lands, so a series
        of impatient presses cannot stack lookups."""
        if self._closed or self._site_choice_pending:
            return
        self._run += 1
        self._meta_retried = False
        self._chapters_retried = False
        self._refresh_btn.setEnabled(False)
        self._clear_rows()
        self._panel_note.setVisible(True)
        self._panel_note.setText("Looking for new "
                                 + ("chapters..." if self._is_reading
                                    else "episodes..."))
        if self._is_reading:
            if chapter_source is None:
                self._finish_refresh("No chapter source in this build.")
                return
            lookup_pool.submit(_chapters_worker, self._signals, self._run,
                               dict(self.entry), True)
        elif self.entry.get("imdb_id") and stremio is not None:
            kind = "movie" if self.entry.get("type") == "Movie" else "series"
            lookup_pool.submit(_meta_worker, self._signals, self._run,
                               self.entry.get("imdb_id"), kind)
        else:
            self._finish_refresh("This entry has no matched title, so there "
                                 "is nothing to refresh.")

    def _finish_refresh(self, message=""):
        """Re-arm the refresh button once an answer (or a refusal) has
        landed. Guarded: the panel can be torn down under a lookup."""
        try:
            self._refresh_btn.setEnabled(True)
        except RuntimeError:
            return
        if message:
            self._panel_note.setVisible(True)
            self._panel_note.setText(message)

    # ---- picking a reading site (Discover titles) ---------------------
    def _reading_site_choices(self) -> list:
        try:
            from helpers import manga_sites
            return manga_sites.list_sites()
        except Exception:
            return []

    def _fill_site_rows(self):
        """The configured reading sites as the list panel's rows - what
        a Discover manga shows before it has a site (the owner's ask).
        MangaDex closes the list as the built-in fallback, so there is
        always a road to a chapter list even with every site down."""
        self._site_choice_pending = True
        self._clear_rows()
        shown = 0
        for site in self._reading_site_choices():
            self._rows.insertWidget(shown, self._row_card(
                site.get("name") or "Site", site.get("base_url") or "",
                None, lambda s=site: self._pick_reading_site(s),
                play_glyph=False))
            shown += 1
        self._rows.insertWidget(shown, self._row_card(
            "MangaDex", "The built-in chapter source", None,
            self._pick_mangadex, play_glyph=False))
        self._panel_note.setVisible(True)
        self._panel_note.setText("Where should this be read from?")

    def _pick_reading_site(self, site):
        self._run += 1
        self._clear_rows()
        self._panel_note.setVisible(True)
        self._panel_note.setText(
            f"Searching {site.get('name') or 'the site'} for "
            f"'{self.entry.get('title')}'...")
        lookup_pool.submit(_site_resolve_worker, self._signals, self._run,
                           dict(site), self.entry.get("title") or "")

    def _pick_mangadex(self):
        self._site_choice_pending = False
        self._run += 1
        self._chapters_retried = False
        self._panel_note.setVisible(True)
        self._panel_note.setText("Loading...")
        lookup_pool.submit(_chapters_worker, self._signals, self._run,
                           dict(self.entry))

    def _on_site_resolved(self, run, fields, site_name):
        if run != self._run or self._closed:
            return
        if not fields:
            # Back to the list, with the verdict written where the
            # question was - a silent return would read as a dead click.
            self._fill_site_rows()
            self._panel_note.setText(
                f"{site_name} has nothing under this title. "
                "Pick another website.")
            return
        # Written in place on the shared dict: if the title is saved
        # later (the Save button), the binding rides along; if it is
        # already saved, record it now so the pick survives the page.
        self.entry.update(fields)
        if self.entry.get("id"):
            try:
                from windows.tracker import _progress_data_file
                storage.update_entry(_progress_data_file(self.entry),
                                     self.entry["id"], fields)
            except Exception:
                logs.exception("details page could not record the site")
        self._site_choice_pending = False
        self._panel_note.setText(f"Loading chapters from {site_name}...")
        self._run += 1
        self._chapters_retried = False
        lookup_pool.submit(_chapters_worker, self._signals, self._run,
                           dict(self.entry))

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
                None, lambda: self._start_episode(None, None)))
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
            # Either store may say watched: the entry's progress number
            # (saved titles) or an explicit History tick (which is all
            # an unsaved title has, and what an out-of-order tick on a
            # saved one writes).
            watched = not upcoming and (
                (watched_episode
                 and (season < watched_season
                      or (season == watched_season
                          and number <= watched_episode)))
                or history.episode_key(season, number) in self._history_marks)
            # "DONE" for both media (the owner's ask) - one word for
            # "you have finished this", rather than WATCHED here and
            # READ on the chapter rows.
            badge = ("upcoming", "UPCOMING") if upcoming else (
                ("watched", "DONE") if watched else None)
            self._rows.insertWidget(shown, self._row_card(
                title, _pretty_date(video.get("firstAired") or video.get("released")),
                badge,
                (None if upcoming
                 else lambda s=season, e=number: self._start_episode(s, e)),
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
            # Same two stores as the episode rows - see there.
            read = number is not None and (
                (read_up_to and number <= read_up_to)
                or history.chapter_key(number) in self._history_marks)
            self._rows.insertWidget(shown, self._row_card(
                title, chapter_published(chapter),
                ("watched", "DONE") if read else None,
                lambda n=number: self._read(n),
                on_menu=lambda ev, n=number: self._chapter_menu(ev, n)))
            shown += 1
        self._panel_note.setVisible(shown == 0)
        if shown == 0 and self._chapters:
            self._panel_note.setText("Nothing matches that search.")

    def _row_card(self, title, date_text, badge, on_click, on_menu=None,
                  play_glyph=True):
        from windows.reader import _ElidedLabel
        card = Card(matte=True, hoverable=on_click is not None)
        card.setFixedHeight(ROW_HEIGHT)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        # play_glyph=False is the source picker's back row: it navigates
        # rather than plays, and a play triangle on "Back to episodes"
        # read as a resume control (the owner's screenshot).
        if play_glyph:
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
    def _start_episode(self, season, episode):
        """What pressing an episode does. With "Auto choose source to
        play" on (Settings > Playback, the default - the picking step
        was the slow part of starting anything, the owner's ask), the
        player opens straight away and races the best sources itself;
        off restores the source picker. The player's own source and
        resolution panels are the manual override either way."""
        if app_settings.get_auto_pick_source():
            self._play(season, episode)
        else:
            self._open_source_picker(season, episode)

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
        self._sync_season_controls()
        self._fill_rows()

    def _close_source_picker(self):
        self._source_pick = None
        self._search.clear()
        self._sync_season_controls()
        self._fill_rows()

    def _sync_season_controls(self):
        """Hide the season picker and its Prev/Next while the source
        list is up (the owner's ask).

        They steer the *episode* list, and stepping a season under a
        list of sources for one episode of the season you just left is
        an offer that cannot mean anything. The Back row inside the
        source list is the way out of it."""
        picking = self._source_pick is not None
        reading = self._is_reading
        for widget in (self._season_box, self._prev_btn, self._next_btn):
            try:
                widget.setVisible(not picking and not reading)
            except RuntimeError:
                pass        # the panel is being torn down

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
            "‹  Back to episodes", "", None, self._close_source_picker,
            play_glyph=False))
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
            from windows.tracker import _progress_data_file
            fresh = next((e for e in storage.load(_progress_data_file(self.entry), [])
                          if e.get("id") == self.entry.get("id")), None)
            if fresh:
                self.entry.update(fresh)
        except Exception:
            logs.exception("details page could not re-read the entry")
        # The ticks too: playing an episode writes one whether or not
        # this title is saved, and an unsaved title has nothing else to
        # re-read (see helpers/history).
        try:
            self._history_marks = history.watched_keys(self.entry)
        except Exception:
            pass
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
        # Either store counts, exactly as the row badge reads them.
        already = ((watched_episode
                    and (watched_season, watched_episode) >= (season, episode))
                   or history.episode_key(season, episode) in self._history_marks)
        menu = QMenu(self)
        # The explicit way into the source list while auto-pick is on -
        # the left click plays right away then (see _start_episode).
        pick_source = menu.addAction("Choose Source...")
        menu.addSeparator()
        mark = menu.addAction("Mark as Unwatched" if already
                              else "Mark as Watched")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Watched")
        clear_all = menu.addAction("Mark All as Unwatched")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is pick_source:
            self._open_source_picker(season, episode)
            return
        if chosen is mark:
            watched = not already
            episodes = [episode]
            if already:
                if episode <= 1:
                    target = ((season - 1, self._aired_last_episode(season - 1))
                              if season > 1 else (0, 0))
                else:
                    target = (season, episode - 1)
            else:
                target = (season, episode)
        elif chosen is mark_all:
            watched, episodes = True, self._season_episodes(season)
            target = (season, max(1, self._aired_last_episode(season)))
        elif chosen is clear_all:
            watched, episodes = False, self._season_episodes(season)
            target = ((season - 1, self._aired_last_episode(season - 1))
                      if season > 1 else (0, 0))
        else:
            return

        # History first, and unconditionally: it is the only store an
        # unsaved title has, and it is what makes a per-episode tick
        # possible at all - the entry's single progress number cannot
        # say "5 and 7 but not 6".
        self._mark_history([history.episode_key(season, number)
                            for number in episodes], watched)
        if self.entry.get("id"):
            if target[1] <= 0:
                # Nothing watched at all: cleared directly, because
                # correct_progress refuses a zero episode.
                self._clear_video_progress()
            elif not correct_progress(self.entry, season=target[0],
                                      episode=target[1]):
                show_toast(self, "Could Not Save That")
                return
        self._fill_rows()

    def _season_episodes(self, season) -> list:
        """Every episode number this page knows of in `season` - what
        "mark all" ticks. Read off the loaded meta rather than counted,
        so a season with gaps ticks exactly what exists."""
        numbers = []
        for video in self._videos:
            if int(video.get("season") or 0) != int(season or 0):
                continue
            number = int(video.get("number") or video.get("episode") or 0)
            if number:
                numbers.append(number)
        return numbers

    def _mark_history(self, marks, watched: bool):
        """Write ticks and keep the page's cached set in step. The title
        is touched either way, so marking something watched is itself
        enough to put it in History - which is the point for a title
        that was never saved."""
        marks = [m for m in marks if m]
        if not marks:
            return
        try:
            history.set_watched(self.entry, marks, watched)
            if watched:
                self._history_marks.update(marks)
            else:
                self._history_marks.difference_update(marks)
        except Exception:
            logs.exception("details page could not write history")

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
        already = bool((self._last_read() and number <= self._last_read())
                       or history.chapter_key(number) in self._history_marks)
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
            read, marked = not already, [number]
            if already:
                earlier = [n for n in numbers if n < number]
                target = max(earlier) if earlier else 0.0
            else:
                target = number
        elif chosen is mark_all:
            read, marked = True, numbers
            target = max(numbers) if numbers else 0.0
        elif chosen is clear_all:
            read, marked = False, numbers
            target = 0.0
        else:
            return

        # See _episode_menu: History is written whether or not this
        # title is saved, and is the only store when it is not.
        self._mark_history([history.chapter_key(n) for n in marked], read)
        if self.entry.get("id") and not correct_progress(self.entry,
                                                         chapter=target):
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
        dialog.setMinimumWidth(520)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        # With the layout already in place, so the heading lands at its
        # top and the caller's rows append after it.
        frameless_dialog(dialog, title=title)
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
                    if not confirm(
                            dialog, "Download Episodes",
                            f"Queue {len(numbers)} episodes of season "
                            f"{season} for download?"):
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
            if len(chapters) > 1 and not confirm(
                    dialog, "Download Chapters",
                    f"Queue {len(chapters)} chapters for download?"):
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

    # ---- saving an unsaved (Discover-opened) title --------------------
    def _save_entry(self):
        """Write this title into its tracker file.

        The id stamped here is what flips every "is it saved" check in
        the app, this button's own visibility rule included; the page
        that opened this one adopts the new row when the overlay closes
        (tracker._on_inapp_closed reads the id off the shared entry
        dict, which is why it is written in place rather than onto a
        copy)."""
        if self.entry.get("id"):
            return
        try:
            from windows.tracker import _progress_data_file
            data_file = _progress_data_file(self.entry)
            stamp = storage.now_iso()
            self.entry["id"] = str(uuid.uuid4())
            self.entry.setdefault("status", "Reading" if self._is_reading
                                  else "Watching")
            self.entry["added_at"] = stamp
            self.entry["updated_at"] = stamp
            # A plain append of a fresh read: update_entry cannot create,
            # and any tracker page open behind this overlay re-reads the
            # file the moment the overlay closes.
            entries = storage.load(data_file, [])
            entries.append(dict(self.entry))
            storage.save(data_file, entries)
        except Exception:
            logs.exception("details page could not save the entry")
            show_toast(self, "Could Not Save That")
            self.entry.pop("id", None)
            return
        # History and the tracker are one story about the same title -
        # a row already in History gains the new entry's id rather than
        # sitting there looking unsaved forever.
        history.link_entry(self.entry)
        self._save_btn.setText("Saved to My List")
        self._save_btn.setEnabled(False)
        show_toast(self, f"'{self.entry.get('title')}' Added to Saved")
        # A reading title picked off Discover can arrive with no
        # cover_url at all (the Madara search shape names none) - its
        # own card resolved the art from the series page, so the save
        # does the same through page_url rather than storing an entry
        # that stays coverless on Saved and Home forever (the owner's
        # Kingdom (WAN) report).
        page_url = (self.entry.get("url") or "") if self._is_reading else ""
        if self.entry.get("cover_url") or page_url:
            lookup_pool.submit(self._saved_cover_worker, self._signals,
                               self.entry["id"], data_file,
                               self.entry.get("cover_url") or "", page_url)

    @staticmethod
    def _saved_cover_worker(signals, entry_id, data_file, url, page_url=""):
        """Download the poster the Discover row carried - or, for a
        reading title that never carried one, the cover its series page
        names - off the UI thread; the write happens back on it. Never
        raises."""
        resolved = ""
        try:
            if not url and page_url:
                from helpers import manga_sites
                found = manga_sites.fetch_manga_details(page_url) or {}
                url = resolved = found.get("cover_url") or ""
            path = images.download(url) if url else None
        except Exception:
            path = None
        if path:
            signals.saved_cover.emit(entry_id, data_file, resolved, str(path))

    def _on_saved_cover(self, entry_id, data_file, url, path):
        fields = {"cover_path": path}
        if url:
            # The worker had to resolve the URL itself - record it, so
            # the tracker's sharper-cover backfill has something to
            # re-derive from later.
            fields["cover_url"] = url
        try:
            storage.update_entry(data_file, entry_id, fields)
        except Exception:
            logs.exception("details page could not record the cover")
        if self.entry.get("id") == entry_id:
            self.entry.update(fields)

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
        #
        # Cut at devicePixelRatio and tagged with it, or the ground is
        # rendered at logical size and stretched by Qt on any non-100%
        # display - which is what made the chapter list's background
        # look soft (the owner's report; the same trap HeroBanner had,
        # and .claude/rules/ui.md states it plainly). The ratio is part
        # of the cache key, so dragging the window to a differently
        # scaled monitor re-cuts rather than reusing the other one's.
        ratio = self.devicePixelRatioF() or 1.0
        if (self._backdrop_scaled is None
                or self._backdrop_size != (rect.size(), ratio)):
            target = QSize(max(1, int(rect.width() * ratio)),
                           max(1, int(rect.height() * ratio)))
            self._backdrop_scaled = self._backdrop.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            self._backdrop_scaled.setDevicePixelRatio(ratio)
            self._backdrop_size = (rect.size(), ratio)
        scaled = self._backdrop_scaled
        # Centred on the pixmap's *logical* size - width()/height() are
        # device pixels once it carries a ratio.
        width = scaled.width() / ratio
        height = scaled.height() / ratio
        painter.drawPixmap(int((rect.width() - width) / 2),
                           int((rect.height() - height) / 2), scaled)
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


class _GenreBrowseSignals(QObject):
    rows = Signal(str, object)      # section label, catalog rows
    poster = Signal(int, str)       # row key, local poster path
    done = Signal()


# The genre page's grid: the tracker's own poster size, so a genre
# browse and Discover are visibly the same surface. Written out rather
# than imported from windows.tracker - that module imports this one at
# call time and a top-level import each way is a cycle.
_BROWSE_POSTER_SIZE = (160, 216)
# Nine across (the owner's ask): at six the grid left a wide empty gutter
# on the right of a maximised window, so the rows read as half-filled.
_BROWSE_COLUMNS = 9
# Enough to fill those rows rather than leave the last one ragged.
_BROWSE_LIMIT = 36


class GenreBrowsePage(GlassPage):
    """Everything else filed under one genre - a full page, the shape
    Discover has (the owner's ask: "a page like discovery, not a small
    window"). Opened over the window like the details page and the
    reader, with the same Back/Escape way out.

    Video genres come from Cinemeta's own genre catalogs (series and
    movies, a row each); reading genres from MangaDex's tag browse. A
    poster opens that title's details page over this one, the same
    transient-entry road a Discover card takes."""

    closed = Signal()

    def __init__(self, genre, is_reading, host):
        super().__init__(parent=host)
        self._genre = str(genre)
        self._is_reading = bool(is_reading)
        self._covers = {}         # poster key -> (label, title)
        self._next_key = 0
        self._closed = False
        self._sections = {}       # row label -> the grid to fill
        self.open_title = None    # set by the caller

        column = QVBoxLayout(self)
        column.setContentsMargins(28, 20, 28, 20)
        column.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        # GlyphButton for the same reason the refresh control is one -
        # see _build_panel: a text glyph on a QSS button inherits the
        # app-wide padding and clips to a box.
        back = GlyphButton(ICON_BACK, "Back", size=(40, 40))
        back.clicked.connect(self.leave)
        header.addWidget(back)
        heading = QLabel(self._genre, objectName="PanelTitle")
        header.addWidget(heading)
        header.addStretch(1)
        column.addLayout(header)

        # Deliberately no inline stylesheet on the host or the area: the
        # declaration-only sheet that used to sit here cascaded into
        # every descendant and outranked the app stylesheet, so these
        # poster Cards never painted their #Card hover ring - the one
        # visual where Discover's identical cards did (the owner's
        # report). theme.py's QScrollArea/#Bare rules keep the ground
        # transparent without reaching the children.
        self._body_host = QWidget(objectName="Bare")
        self._body = QVBoxLayout(self._body_host)
        self._body.setContentsMargins(0, 0, 8, 0)
        self._body.setSpacing(16)
        self._body.addStretch(1)
        area = scroll_area(self._body_host)
        column.addWidget(area, stretch=1)

        self._note = QLabel(f"Looking for {self._genre} titles...")
        self._note.setWordWrap(True)
        self._note.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent; border: none;")
        column.addWidget(self._note)

        self._signals = _GenreBrowseSignals()
        self._signals.rows.connect(self._on_rows)
        self._signals.poster.connect(self._on_poster)
        self._signals.done.connect(self._on_done)
        self._got_rows = False
        import threading
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    # ---- the overlay contract, same as DetailsPage --------------------
    def follow(self, host):
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
            return
        super().mousePressEvent(event)

    def leave(self):
        if self._closed:
            return
        self._closed = True
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        self.deleteLater()

    # ---- filling it ---------------------------------------------------
    def _fetch_worker(self):
        # Never raises; every row reports what it found, and `done`
        # clears the looking-note whatever happened.
        from helpers import discover
        try:
            if self._is_reading:
                rows = discover.discover_reading_genre(self._genre,
                                                       limit=_BROWSE_LIMIT)
                self._signals.rows.emit("", list(rows or []))
            else:
                for kind, label in (("series", "Series"), ("movie", "Movies")):
                    rows = discover.discover_video(kind, genre=self._genre,
                                                   limit=_BROWSE_LIMIT)
                    self._signals.rows.emit(label, list(rows or []))
        except Exception:
            logs.exception("genre browse lookup failed")
        self._signals.done.emit()

    def _on_rows(self, label, rows):
        if self._closed or not rows:
            return
        self._got_rows = True
        insert_at = self._body.count() - 1
        if label:
            heading = QLabel(label, objectName="SectionTitle")
            self._body.insertWidget(insert_at, heading)
            insert_at += 1
        grid_host = QWidget(objectName="Bare")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        for index, item in enumerate(rows):
            grid.addWidget(self._build_card(item),
                           index // _BROWSE_COLUMNS, index % _BROWSE_COLUMNS)
        self._body.insertWidget(insert_at, grid_host)

    def _build_card(self, item):
        """One poster tile - the Discover grid's own card, so the two
        pages read as the same surface."""
        title = (item.get("title") or "").strip()
        card = Card(hoverable=True)
        card.setFixedWidth(_BROWSE_POSTER_SIZE[0] + 20)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(6)

        cover = QLabel()
        cover.setFixedSize(*_BROWSE_POSTER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(None, title,
                                                   _BROWSE_POSTER_SIZE))
        layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)

        name = QLabel(title, objectName="CardTitle")
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)

        bits = [str(item.get("year") or "").strip()]
        rating = str(item.get("imdbRating") or "").strip()
        if rating:
            bits.append(f"★ {rating}")
        meta_text = "  ·  ".join(b for b in bits if b)
        if meta_text:
            meta = QLabel(meta_text, objectName="CardMeta")
            meta.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(meta)

        poster_url = item.get("poster") or ""
        if poster_url:
            key = self._next_key
            self._next_key += 1
            self._covers[key] = (cover, title)
            lookup_pool.submit(self._poster_worker, self._signals, key,
                               poster_url)

        card.clicked.connect(lambda it=dict(item): self._pick(it))
        return card

    @staticmethod
    def _poster_worker(signals, key, url):
        # Never raises - the pool's worker thread dies silently otherwise.
        try:
            path = images.download(url)
        except Exception:
            path = None
        if path:
            signals.poster.emit(key, str(path))

    def _on_poster(self, key, path):
        pair = self._covers.get(key)
        if pair is None:
            return
        cover, title = pair
        try:
            cover.setPixmap(images.thumbnail_or_avatar(path, title,
                                                       _BROWSE_POSTER_SIZE))
        except RuntimeError:
            pass          # the page closed under the download

    def _on_done(self):
        if self._closed:
            return
        if self._got_rows:
            self._note.setVisible(False)
        else:
            self._note.setText(f"Nothing was found under {self._genre}. "
                               "Check the connection and try again.")

    def _pick(self, item):
        callback = self.open_title
        if callable(callback):
            callback(item, item.get("type")
                     or ("Manga" if self._is_reading else "Series"))


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
