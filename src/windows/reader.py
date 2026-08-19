"""In-app reader for Manga / Manhwa / Manhua, in Arabic.

A full-window page that sits *over* the app's page stack rather than in
it: it is opened from an entry and closed back to whatever page was
underneath, so it has no sidebar row and no place in main.PAGES (see
`open_reader` at the bottom - one hook, `reader.open_reader(window, entry)`).

Four things decide almost everything in here:

  * **One reading surface.** Continuous vertical scroll, always. A
    page-turning mode existed beside it and is gone: the owner reads
    manhwa and Arabic manga scans, both of which are read as a strip,
    and a second surface only ever meant a second set of bugs. What is
    left of direction is which arrow key means "next chapter" - Arabic
    and manga are read right to left, so that is the **left** one, and
    the Next Chapter button sits on the left edge for the same reason.
  * **Reading order is not list order.** chapter_source hands chapters
    back newest first, so the next chapter to read is at index **- 1**.
    step_chapter is the one place that inversion lives; it shipped
    without it once and Previous/Next were each other.
  * **Two floating bars, revealed differently.** The top bar comes back
    on a scroll up or on the pointer nearing the top. The chapter
    controls on the floor - next, the jump list, previous - are hover
    only, and never move for a scroll: re-reading a panel is not a
    request to change chapter.
  * **Headers.** Most of these hosts answer an image request with 403
    unless it carries the Referer the source hands back, so every image
    fetch goes through _fetch_page_file with `headers` applied. A page
    that 403s says so and offers the browser, rather than showing a
    broken frame forever.
  * **Original size, centred.** There is deliberately no fit mode: a
    page is decoded at its own pixel size, drawn one image pixel per
    *device* pixel, and centred between the viewport's edges. Fit-width
    and fit-height used to exist and were removed - they re-decoded
    every page on every window drag and still softened scans that were
    already the right size. The +/- controls remain, but they now scale
    *from* that original size rather than choosing between fits, so the
    baseline is a constant and zoom is a multiplier on it.
  * **Memory.** A 200-page webtoon held as full-size QPixmaps is
    gigabytes: one long strip page is ~20MB decoded. So only the slots
    near the viewport hold pixels at all, and everything decoded goes
    through _PixmapCache, which is bounded in *bytes* rather than in
    entries (entries here differ in size by two orders of magnitude, so
    a count would bound nothing).

Decoding happens on lookup_pool's shared workers as a QImage - which is
safe off the UI thread, where QPixmap is not - and only the cheap
QImage->QPixmap conversion happens on the UI thread. Nothing here ever
spawns a thread per page: unbounded per-entry threads have already
saturated this user's whole home network once (see helpers/lookup_pool).
"""

import os
import urllib.error
import urllib.request
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from helpers import (downloads, images, logs, lookup_pool, net, storage,
                     theme)
from helpers.widgets import (Card, GlassPage, finish_toast, show_toast,
                             use_hover_cursor)
from windows.tracker import format_chapter_progress

# Imported defensively: this page is useful (and testable) with a stub
# source injected over it, and a missing module must read as "the reader
# has no source wired up" in front of the user rather than an ImportError
# that takes the whole app down at startup.
try:
    from helpers import chapter_source
except Exception:                                   # pragma: no cover
    chapter_source = None


# ---- budgets -------------------------------------------------------------
# One image request, and the whole chapter-listing chain. The listing is
# allowed longer because it is a search across a site, not one file.
PAGE_TIMEOUT = 12.0
CHAPTER_LIST_TIMEOUT = 25.0
CHAPTER_PAGES_TIMEOUT = 20.0

# Bigger than net.MAX_RESPONSE_BYTES on purpose: that ceiling is sized for
# API responses and covers, and a single webtoon strip image routinely
# runs past it.
MAX_PAGE_BYTES = 16_000_000

# Decoded pixmaps held at once. A long strip page is ~20MB, so this is
# roughly a dozen of the worst case and a few hundred of an ordinary
# manga page.
PIXMAP_BUDGET_BYTES = 192 * 1024 * 1024

# Never decode more than this many pixels from one image, whatever the
# zoom asks for. Scaling a page up is legitimate; scaling it up to
# something that costs hundreds of megabytes is not.
MAX_DECODED_PIXELS = 40_000_000

# Zoom, as a multiplier on the page's own size. 100% is the original,
# which is also where every chapter opens.
ZOOM_STEP = 0.15
ZOOM_MIN, ZOOM_MAX = 0.25, 4.0

# How far outside the viewport the strip keeps pixels. This margin is
# also what does the preloading - a slot one screen down is fetched
# before it is scrolled to.
STRIP_LOAD_MARGIN = 1600     # px of strip loaded past each viewport edge
STRIP_KEEP_MARGIN = 3600     # ...and past which pixels are dropped again

# A slot's guessed size before its image has decoded. Width is replaced
# by the first real page's width as soon as one arrives (pages within a
# chapter are all cut to the same width), so the guess only ever governs
# the very first screenful.
PLACEHOLDER_RATIO = 1.45     # height = width * this
DEFAULT_PAGE_WIDTH = 800     # logical px

# The page position is written back debounced, so a scroll doesn't write
# tracker.json on every frame.
SAVE_POSITION_MS = 1200

# Both bars float *over* the reading surface instead of sitting in the
# layout. Hiding a bar that is in the layout gives the strip the bar's
# height back, which yanks the artwork ~48px up the screen mid-read; an
# overlay costs one setGeometry per resize and moves nothing when it
# comes and goes.
BAR_HEIGHT = 48
BOTTOM_HEIGHT = 60      # the chapter controls, pinned to the window's floor
BAR_REVEAL_PX = 72      # pointer this close to the top brings it back
BOTTOM_REVEAL_PX = 110  # ...and this close to the bottom, for that one
SCROLL_HIDE_DELTA = 12  # px of travel before a scroll counts as intent

# The three floating bottom controls: previous/next pinned to the window's
# two edges with the chapter list centred between them. Sized here rather
# than left to sizeHint so _place_bottom can do its arithmetic before any
# of them has been shown.
BOTTOM_CONTROL_HEIGHT = 34
BOTTOM_STEP_WIDTH = 170
CHAPTER_BOX_WIDTH = 460     # wide on purpose - chapter names are long
BOTTOM_GAP = 16

# One wheel notch, in px of strip. Qt's default here is singleStep (20)
# times the OS's wheel lines (3) = 60px, measured - which on a webtoon
# chapter 30,000px tall is 500 notches from top to bottom. 300 was the
# first try and the owner still called it slow on a long webtoon: at a
# ~900px viewport that is a third of a screen per notch, so a 30,000px
# chapter is 100 notches. 640 is a bit over two thirds of a screenful,
# which puts the same chapter at ~47 notches without overshooting a
# panel - a full screenful per notch reads past the join between two.
WHEEL_STEP_PX = 640

# Down/Up arrow, same reasoning as the wheel: 120 was a tenth of a
# screen and unusable as a way to move down a strip.
ARROW_SCROLL_PX = 260

# Segoe Fluent Icons codepoints, the same family the sidebar and the
# player use (theme.FONT_STACK_ICONS) - monochrome, so they take the
# button's own colour instead of an emoji's.
#
# Written as \u escapes rather than as the characters themselves, and
# that is not style: these are private-use codepoints, and any tool that
# re-encodes this file turns each one into two or three mojibake boxes
# in the middle of the bar (CLAUDE.md records exactly that happening
# twice). An escape survives being read in the wrong codepage.
ICON_REFRESH = "\ue72c"               # Refresh
ICON_FULLSCREEN = "\ue740"            # FullScreen
ICON_EXIT_FULLSCREEN = "\ue73f"       # BackToWindow
ICON_LEAVE = "\uf3b1"                 # SignOut - a door with a way out of it
ICON_BROWSER = "\ue774"               # Globe
ICON_CHEVRON_DOWN = "\ue70d"          # ChevronDown - "this opens"
ICON_DOWNLOAD = "\ue896"              # Download

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Atomic/1.0"


# ---- fetching ------------------------------------------------------------
def _request_headers(headers) -> dict:
    """The headers one page image is fetched with.

    `headers` comes from chapter_source and is not decoration: most of
    these hosts check Referer and answer 403 without it. A User-Agent is
    added underneath rather than over the top, so a source that wants a
    specific one keeps it."""
    merged = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
    merged.update({str(k): str(v) for k, v in (headers or {}).items()})
    return merged


def _fetch_page_file(url: str, headers: dict):
    """`url` on disk, as (Path, None) - or (None, message) saying what
    went wrong in words meant for the user.

    Cached in images.CACHE_DIR under images.cache_path_for_url, which is
    the app's one image cache; this is deliberately not a second cache
    next to it. It is not images.download() only because that helper
    cannot carry headers, and headers are the whole difference between a
    page and a 403 here.

    Written to a temp name and renamed into place: a download cut off
    half way through must not be left behind as a cached file, because
    nothing would ever re-fetch it and the page would be permanently
    broken."""
    path = images.cache_path_for_url(url)
    try:
        if path.exists() and path.stat().st_size > 0:
            return path, None
    except OSError:
        pass
    request = urllib.request.Request(url, headers=_request_headers(headers))
    try:
        # Computed here rather than at submit time: a job can sit in the
        # pool's queue behind others, and a deadline started back then
        # would already be spent before the first byte is asked for.
        deadline = net.deadline_in(PAGE_TIMEOUT)
        with urllib.request.urlopen(request, timeout=PAGE_TIMEOUT) as response:
            data = net.read_bytes(response, deadline, MAX_PAGE_BYTES)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return None, (f"The site refused this page (HTTP {error.code}). "
                          "That usually means it only serves images to its "
                          "own pages.")
        return None, f"The site answered HTTP {error.code} for this page."
    except Exception as error:
        return None, f"This page couldn't be downloaded ({type(error).__name__})."
    if not data:
        return None, "The site returned an empty page image."
    try:
        temp_path = path.with_name(path.name + ".part")
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    except OSError as error:
        return None, f"This page couldn't be saved to the cache ({error})."
    return path, None


def _drop_cached_page(url: str):
    """Forget the on-disk copy of one page image.

    Only Refresh calls this. "The images failed" and "the site replaced
    them" are the two reasons that button exists, and re-listing the
    chapter fixes neither on its own: a page that downloaded as a
    truncated or placeholder image is cached, and every later read would
    be served that same file forever."""
    try:
        images.cache_path_for_url(url).unlink(missing_ok=True)
    except OSError:
        pass


def zoom_key(zoom) -> int:
    """A zoom as the hashable thing everything here caches by: an int
    percentage, not the float, so two arithmetically-equal zooms can't
    produce two cache entries."""
    return int(round(float(zoom) * 100))


def _decode_size(natural: QSize, key: int) -> QSize:
    """The size an image is decoded at: its own, times the zoom.

    At 100% - where every chapter opens - this is the image untouched,
    so nothing is resampled at all in the ordinary case. The
    MAX_DECODED_PIXELS ceiling is the one thing that can override the
    answer, because the alternative to a slightly soft page is the app
    dying on a 400MB decode."""
    if natural.isEmpty():
        return natural
    scale = max(0.01, key / 100.0)
    pixels = natural.width() * natural.height() * scale * scale
    if pixels > MAX_DECODED_PIXELS:
        scale *= (MAX_DECODED_PIXELS / pixels) ** 0.5
    if abs(scale - 1.0) < 1e-6:
        return natural
    return QSize(max(1, round(natural.width() * scale)),
                 max(1, round(natural.height() * scale)))


def _centre_scrollbar(bar, maximum):
    """Put a scroll area's horizontal scroll in the middle of its range.

    Centring the *widget* inside the layout is only half of "the chapter
    sits in the middle": as soon as one page is wider than the window -
    a double-page spread in One Piece is 2630px against a 1488px
    viewport, measured - the content is wider than the viewport, the
    horizontal scroll starts at 0, and every ordinary page is drawn
    off to one side and clipped. Screenshotted before this existed.

    Wired to `rangeChanged` rather than called by hand from six places,
    which is also what makes it leave the reader's own panning alone: a
    drag sideways doesn't change the range, so nothing fights it. Only a
    rebuild, a resize, a zoom, or a wider page arriving does."""
    if maximum > 0:
        bar.setValue(maximum // 2)


class _PageSignals(QObject):
    """The worker -> UI boundary for page images. A widget is never
    touched from a pool thread; this is the only thing that crosses."""

    decoded = Signal(int, int, object, int)  # run, index, QImage, zoom key
    failed = Signal(int, int, str)           # run, index, message


class _ChapterSignals(QObject):
    listed = Signal(int, object, str)     # run, chapters or None, error
    opened = Signal(int, object, str)     # run, pages dict or None, error


class _PixmapCache:
    """Decoded pages, bounded by total bytes.

    Bytes rather than a count (which is what images.py can get away with,
    since every thumbnail there is the same size): one entry here can be
    20MB and another 200KB, so a count would bound nothing at all."""

    def __init__(self, budget=PIXMAP_BUDGET_BYTES):
        self._items = OrderedDict()
        self._bytes = 0
        self._budget = budget

    @staticmethod
    def _cost(pixmap: QPixmap) -> int:
        return max(1, pixmap.width() * pixmap.height() * 4)

    def get(self, key):
        pixmap = self._items.get(key)
        if pixmap is not None:
            self._items.move_to_end(key)
        return pixmap

    def put(self, key, pixmap, keep=()):
        if key in self._items:
            self._bytes -= self._cost(self._items.pop(key))
        self._items[key] = pixmap
        self._bytes += self._cost(pixmap)
        self._evict(keep)

    def _evict(self, keep=()):
        protected = set(keep)
        for key in list(self._items):
            if self._bytes <= self._budget:
                return
            if key in protected:
                continue
            self._bytes -= self._cost(self._items.pop(key))

    def clear(self):
        self._items.clear()
        self._bytes = 0


class _PageStore(QObject):
    """Fetch + decode + cache for the pages of one chapter.

    Keyed by (page index, zoom). It used to be keyed by a whole fit box;
    with one rendering mode the only thing that can still change a
    page's decoded size is the zoom, so that is all the key carries."""

    ready = Signal(int)          # page index, now in the cache
    failed = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _PageSignals()
        self._signals.decoded.connect(self._on_decoded)
        self._signals.failed.connect(self._on_failed)
        self._cache = _PixmapCache()
        self._urls = []
        self._headers = {}
        self._pending = set()      # (index, zoom key) already queued
        self._errors = {}
        self._keep = set()
        # Carried into every job and checked on the way back: a chapter
        # opened after this one must never have its slots filled by the
        # previous chapter's images, which is the same run-id rule the
        # tracker's lookups follow.
        self._run = 0

    def configure(self, urls, headers):
        self._run += 1
        self._urls = list(urls or [])
        self._headers = dict(headers or {})
        self._pending.clear()
        self._errors.clear()
        self._keep.clear()
        self._cache.clear()

    def count(self):
        return len(self._urls)

    def urls(self):
        return list(self._urls)

    def url(self, index):
        if 0 <= index < len(self._urls):
            return self._urls[index]
        return ""

    def error(self, index):
        return self._errors.get(index)

    def set_keep(self, keys):
        """Cache keys the views are currently *showing*, which eviction
        must not take out from under them - dropping the page on screen
        to make room for the one being preloaded is how a reader ends up
        blinking."""
        self._keep = set(keys)

    def cached(self, index, key):
        return self._cache.get((index, key))

    def request(self, index, key):
        """Queue page `index` at zoom `key` unless it is already cached,
        already queued, or already known to have failed."""
        if not (0 <= index < len(self._urls)):
            return None
        cache_key = (index, key)
        pixmap = self._cache.get(cache_key)
        if pixmap is not None:
            return pixmap
        if cache_key in self._pending or index in self._errors:
            return None
        self._pending.add(cache_key)
        lookup_pool.submit(_decode_page_job, self._signals, self._run, index,
                           self._urls[index], self._headers, key)
        return None

    def _on_decoded(self, run, index, image, key):
        if run != self._run:
            return
        cache_key = (index, key)
        self._pending.discard(cache_key)
        # QImage -> QPixmap has to happen here, on the UI thread; it is
        # also the cheap half (~0.1ms against several ms of decode), which
        # is exactly why the expensive half is done in the worker.
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._errors[index] = "This page couldn't be decoded as an image."
            self.failed.emit(index, self._errors[index])
            return
        self._cache.put(cache_key, pixmap, keep=self._keep)
        self.ready.emit(index)

    def _on_failed(self, run, index, message):
        if run != self._run:
            return
        for key in [k for k in list(self._pending) if k[0] == index]:
            self._pending.discard(key)
        self._errors[index] = message
        self.failed.emit(index, message)


def _decode_page_job(signals, run, index, url, headers, key):
    """One page: download and decode. Runs on a lookup_pool worker.

    Must never raise - an uncaught exception here would be swallowed by
    the pool and the slot would wait forever with nothing to show and
    nothing to say. Every path emits either `decoded` or `failed`.

    QImage rather than QPixmap on purpose: QPixmap may only be touched on
    the GUI thread, QImage may not be."""
    try:
        path, error = _fetch_page_file(url, headers)
        if path is None:
            signals.failed.emit(run, index, error or "This page couldn't be loaded.")
            return
        image = QImage(str(path))
        if image.isNull():
            signals.failed.emit(run, index,
                                "This page isn't a readable image - the site "
                                "may have sent an error page instead.")
            return
        target = _decode_size(image.size(), key)
        if target != image.size():
            image = image.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        signals.decoded.emit(run, index, image, key)
    except Exception as error:                       # pragma: no cover
        logs.exception("reader page decode failed")
        signals.failed.emit(run, index,
                            f"This page couldn't be loaded ({type(error).__name__}).")


def _screen_ratio(widget) -> float:
    """The display's scale factor.

    Off the *screen*, not the widget: a widget's own devicePixelRatioF is
    1.0 until it has been shown, and a pixmap sized from that is upscaled
    by the display afterwards and looks soft - the same trap
    widgets.magnifier_icon records."""
    screen = None
    window = widget.window() if widget is not None else None
    if window is not None:
        screen = window.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0


def _tagged(pixmap: QPixmap, ratio: float) -> QPixmap:
    """A pixmap decoded in *device* pixels, told what ratio it is in, so
    Qt draws it at the right logical size instead of stretching it. Every
    image in this reader goes through here - without the tag, every page
    is blurry on any non-100% display."""
    if pixmap is not None and not pixmap.isNull():
        pixmap.setDevicePixelRatio(ratio)
    return pixmap


# ---- chapter list --------------------------------------------------------
def is_arabic(chapter) -> bool:
    return str(chapter.get("lang") or "").lower().startswith("ar")


def chapter_number(chapter):
    """A chapter's number as a float, or None when it hasn't got one.
    Fractional on purpose - scanlators split chapters ("24.5"), the same
    reason tracker.parse_chapter_progress is a float."""
    raw = chapter.get("number")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def chapter_title(chapter) -> str:
    number = chapter_number(chapter)
    head = f"Chapter {format_chapter_progress(number)}" if number is not None else "Chapter"
    title = str(chapter.get("title") or "").strip()
    return f"{head} - {title}" if title else head


class _ChapterListView(QWidget):
    """The chapter picker: one Card per chapter, Arabic ones marked."""

    picked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._notice = QLabel("", objectName="Muted")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        layout.addWidget(self._notice)

        self._body = QWidget(objectName="Bare")
        self._rows = QVBoxLayout(self._body)
        self._rows.setContentsMargins(0, 0, 8, 0)
        self._rows.setSpacing(6)
        self._rows.addStretch()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self._body)
        layout.addWidget(area, stretch=1)

    def set_notice(self, text):
        self._notice.setText(text or "")
        self._notice.setVisible(bool(text))

    def set_chapters(self, chapters, current=-1):
        # Torn down and rebuilt rather than updated in place - a page
        # here rebuilds from scratch, the same rule the rest of the app
        # follows (.claude/rules/ui.md).
        while self._rows.count() > 1:
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, chapter in enumerate(chapters):
            self._rows.insertWidget(
                index, self._build_row(index, chapter, index == current))

    def _build_row(self, index, chapter, reading=False):
        card = Card(matte=True)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)

        title = QLabel(chapter_title(chapter), objectName="CardTitle")
        title.setWordWrap(True)
        if reading:
            # The chapter being read is marked on the row itself rather
            # than by selecting it: this list is rebuilt on every visit
            # and has no selection to keep, and after scrolling 500 rows
            # "which one am I on" is not answerable from anything else
            # on screen.
            title.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 800;")
        row.addWidget(title, stretch=1)

        if reading:
            mark = QLabel("Reading")
            mark.setStyleSheet(
                f"color: {theme.TEXT}; background: {theme.ACCENT}; "
                f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
                f"font-weight: 700;")
            row.addWidget(mark)

        group = str(chapter.get("group") or "").strip()
        if group:
            group_label = QLabel(group, objectName="CardMeta")
            row.addWidget(group_label)

        # The language is the point of this reader, so it is a badge and
        # not a suffix on the title: the owner is scanning for the Arabic
        # ones, and a word at the end of a variable-length line is not
        # something you can scan down a column.
        badge = QLabel("عربي" if is_arabic(chapter) else
                       (str(chapter.get("lang") or "?").upper()))
        arabic = is_arabic(chapter)
        badge.setStyleSheet(
            f"color: {theme.TEXT if arabic else theme.TEXT_MUTED}; "
            f"background: {theme.ACCENT if arabic else theme.SURFACE_HOVER}; "
            f"border: 1px solid {theme.ACCENT if arabic else theme.BORDER}; "
            f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
            f"font-weight: 700;")
        row.addWidget(badge)

        card.clicked.connect(lambda i=index: self.picked.emit(i))
        return card


class _ChapterCombo(QComboBox):
    """The chapter jump list.

    Two things a plain QComboBox does not do here.

    *It draws its own down-chevron.* Painted rather than styled through
    `QComboBox::down-arrow`, which takes an `image:` url and nothing
    else - there is no icon file in this app to point one at, every
    arrow it draws is a Segoe Fluent glyph (theme.FONT_STACK_ICONS).
    Qt's own arrow is switched off in the same stylesheet so there is
    exactly one, and the box carries right-hand padding so a long
    chapter name does not run under it.

    *It marks the chapter being read.* Qt highlights the row the pointer
    is over and the row that is current, and after scrolling a
    500-chapter list neither of those is still visibly the one open."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_chapter = -1
        # Background and border repeated from the app's own QComboBox
        # rule rather than inherited from it. Measured: with only
        # `padding-right` set here the box came out **transparent** over
        # the artwork and the chapter name was unreadable against a
        # webtoon's white panels - a widget-level stylesheet re-resolves
        # the whole rule for this widget, it does not merge property by
        # property with the application sheet.
        self.setStyleSheet(
            f"QComboBox {{ background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px;"
            f" padding: 4px 26px 4px 10px; }}"
            f"QComboBox:hover {{ border: 1px solid {theme.ACCENT}; }}"
            f"QComboBox::drop-down {{ border: none; width: 26px; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}")

    def clear(self):
        # The mark is an item role, so it goes with the items. Without
        # this a re-list (Refresh) leaves _current_chapter pointing at a
        # row that no longer carries the styling, and the next
        # set_current_chapter for the same index returns early and marks
        # nothing at all.
        self._current_chapter = -1
        super().clear()

    def set_current_chapter(self, index):
        if index == self._current_chapter:
            return
        for row in (self._current_chapter, index):
            if not (0 <= row < self.count()):
                continue
            marked = row == index >= 0
            font = QFont(self.font())
            font.setBold(marked)
            self.setItemData(row, font, Qt.ItemDataRole.FontRole)
            self.setItemData(row, QBrush(QColor(theme.ACCENT if marked
                                                else theme.TEXT)),
                             Qt.ItemDataRole.ForegroundRole)
        self._current_chapter = index

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        font = QFont(painter.font())
        font.setFamilies(list(theme.FONT_FAMILY_ICON_FALLBACKS))
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_MUTED))
        painter.drawText(self.rect().adjusted(0, 0, -9, 0),
                         int(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter),
                         ICON_CHEVRON_DOWN)
        painter.end()


# ---- continuous vertical (webtoon) reader -------------------------------
class _StripSlot(QLabel):
    """One page in the vertical strip.

    It keeps its height whether or not it is holding pixels, so dropping
    a far-away page's pixmap doesn't collapse the strip under the reader
    - which would move everything below it and throw the scroll position
    away."""

    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        self.setWordWrap(True)
        self.natural = None    # QSize of the decoded image, once known
        self.loaded = False


class _StripView(QScrollArea):
    """Continuous vertical scroll - manhwa and manhua, which are drawn as
    one strip and have no pages to turn. Also the default for everything
    else: it is what the owner reads in."""

    positionChanged = Signal()
    ranOff = Signal(int)

    def __init__(self, store: _PageStore, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Always on: a strip is taller than any window by definition, so
        # the bar is coming either way, and reserving its width up front
        # keeps the centred column from shifting sideways the moment the
        # first page arrives and the bar appears.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._store = store
        self._slots = []
        self._zoom = zoom_key(1.0)
        # The width the last decoded page came out at, in logical px.
        # Pages inside one chapter are cut to the same width, so the
        # first one that lands makes every later placeholder right.
        self._typical_width = DEFAULT_PAGE_WIDTH

        self._body = QWidget(objectName="Bare")
        self._column = QVBoxLayout(self._body)
        self._column.setContentsMargins(0, 0, 0, 0)
        # No gap at all: a webtoon's panels are cut mid-image, and any
        # spacing between slots draws a visible seam through the artwork.
        self._column.setSpacing(0)
        self._column.addStretch()
        self.setWidget(self._body)

        store.ready.connect(self._on_page_ready)
        store.failed.connect(self._on_page_failed)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        h_bar = self.horizontalScrollBar()
        h_bar.rangeChanged.connect(
            lambda _minimum, maximum: _centre_scrollbar(h_bar, maximum))

    # ---- state -------------------------------------------------------
    def set_zoom(self, key):
        """Change the zoom without losing the reader's place.

        Every slot's pixels belong to the *old* zoom, so they are dropped
        rather than left on screen at the wrong size - the store still
        holds them, so zooming back is a cache hit. The scroll position
        is re-anchored on the page that was under the top of the
        viewport: heights are about to change by the zoom's ratio, and
        keeping the raw scrollbar value would land somewhere unrelated,
        several pages off at 4x."""
        if int(key) == self._zoom:
            return
        anchor = self.index()
        previous_zoom, self._zoom = self._zoom, int(key)
        # Carried across rather than reset to the default: the guess is
        # only wrong by the ratio between the two zooms, and the real
        # widths are what it was measured from.
        self._typical_width = max(1, round(
            self._typical_width * (self._zoom / max(1, previous_zoom))))
        for slot in self._slots:
            slot.setPixmap(QPixmap())
            slot.loaded = False
            slot.natural = None
        for slot in self._slots:
            self._resize_slot(slot)
        if 0 <= anchor < len(self._slots):
            self.verticalScrollBar().setValue(self._slots[anchor].y())
        self._sync_visible()

    def rebuild(self):
        while self._column.count() > 1:
            item = self._column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._slots = []
        self._typical_width = max(1, round(DEFAULT_PAGE_WIDTH * self._zoom / 100.0))
        for index in range(self._store.count()):
            slot = _StripSlot(index, self._body)
            slot.setText("")
            # AlignHCenter is the whole of "the chapter sits in the
            # middle". Without it a fixed-size widget in a QVBoxLayout is
            # placed at its cell's top-*left*, so every page hugged the
            # left edge of the window however wide the window got.
            self._column.insertWidget(index, slot, 0,
                                      Qt.AlignmentFlag.AlignHCenter)
            self._slots.append(slot)
            self._resize_slot(slot)
        self.verticalScrollBar().setValue(0)
        self._sync_visible()

    def index(self):
        """Which page the reader is actually on - the slot under the top
        of the viewport, since in a strip "the page you are on" is the
        one you are looking at, not one you turned to."""
        top = self.verticalScrollBar().value()
        for slot in self._slots:
            if slot.y() + slot.height() > top:
                return slot.index
        return max(0, len(self._slots) - 1)

    def set_index(self, index):
        if 0 <= index < len(self._slots):
            self.verticalScrollBar().setValue(self._slots[index].y())
            self._sync_visible()
            self.positionChanged.emit()

    def step(self, forward):
        """A screenful, not a page: this is a strip. Running off either
        end is what moves to the next/previous chapter."""
        bar = self.verticalScrollBar()
        amount = int(self.viewport().height() * 0.9)
        if forward and bar.value() >= bar.maximum() - 2:
            self.ranOff.emit(1)
            return
        if not forward and bar.value() <= bar.minimum() + 2:
            self.ranOff.emit(-1)
            return
        bar.setValue(bar.value() + (amount if forward else -amount))

    def scroll_step(self, dy):
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + dy)

    def wheelEvent(self, event):
        """A wheel notch moves WHEEL_STEP_PX, not Qt's 60.

        Done here rather than by raising the scrollbar's singleStep
        because QScrollArea recomputes that itself whenever the widget or
        the viewport is resized - and this strip resizes on every page
        that decodes, so a step set once would not survive the first
        image. A trackpad's pixel-delta scroll is left alone: it is
        already 1:1 with the finger and multiplying it would make the
        strip fly."""
        if not event.pixelDelta().isNull():
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - round(steps * WHEEL_STEP_PX))
        event.accept()

    # ---- sizing ------------------------------------------------------
    def _placeholder_size(self):
        # Deliberately *not* clamped to the viewport: the guess exists to
        # make the strip's total height roughly right before anything has
        # decoded, and a width clamped to a narrow window would
        # under-guess every height and make the scrollbar lurch as the
        # real pages arrive.
        width = max(1, self._typical_width)
        return width, int(width * PLACEHOLDER_RATIO)

    def _resize_slot(self, slot):
        """The slot's size: the decoded image's own, in logical pixels,
        once it is known - and a guess before that.

        Changing the height of a slot *above* the viewport moves
        everything below it, so the scrollbar is compensated by the same
        delta. Without that, a page settling into its real height while
        another is being read scrolls the reader somewhere else
        mid-panel - and on a webtoon, where the guess is out by a factor
        of three, it does so violently."""
        ratio = _screen_ratio(self)
        if slot.natural is not None and not slot.natural.isEmpty():
            width = max(1, round(slot.natural.width() / ratio))
            height = max(1, round(slot.natural.height() / ratio))
        else:
            width, height = self._placeholder_size()
        previous = slot.height()
        if width == slot.width() and height == previous:
            return
        bar = self.verticalScrollBar()
        above = slot.y() + slot.height() <= bar.value()
        slot.setFixedSize(width, height)
        if above:
            bar.setValue(bar.value() + (height - previous))

    # ---- loading -----------------------------------------------------
    def _sync_visible(self):
        """Give pixels to the slots near the viewport, take them back
        from the ones far outside it. This is also what preloads: a slot
        one screen below has already been asked for by the time it is
        scrolled to."""
        if not self._slots:
            return
        bar = self.verticalScrollBar()
        top = bar.value()
        bottom = top + self.viewport().height()
        keep = []
        ratio = _screen_ratio(self)
        for slot in self._slots:
            slot_top, slot_bottom = slot.y(), slot.y() + slot.height()
            near = (slot_bottom > top - STRIP_LOAD_MARGIN
                    and slot_top < bottom + STRIP_LOAD_MARGIN)
            far = (slot_bottom < top - STRIP_KEEP_MARGIN
                   or slot_top > bottom + STRIP_KEEP_MARGIN)
            if near:
                keep.append((slot.index, self._zoom))
                pixmap = self._store.request(slot.index, self._zoom)
                if pixmap is not None and not slot.loaded:
                    self._show(slot, pixmap, ratio)
                elif pixmap is None and not slot.loaded:
                    slot.setText(self._store.error(slot.index) or "")
            elif far and slot.loaded:
                # Height is kept (see _StripSlot) - only the pixels go.
                slot.setPixmap(QPixmap())
                slot.loaded = False
        self._store.set_keep(keep)

    def _show(self, slot, pixmap, ratio):
        slot.natural = pixmap.size()
        slot.setText("")
        slot.setPixmap(_tagged(QPixmap(pixmap), ratio))
        slot.loaded = True
        self._typical_width = max(1, round(pixmap.width() / ratio))
        self._resize_slot(slot)

    def _on_page_ready(self, index):
        if 0 <= index < len(self._slots):
            pixmap = self._store.cached(index, self._zoom)
            if pixmap is not None:
                self._show(self._slots[index], pixmap, _screen_ratio(self))
        self._sync_visible()

    def _on_page_failed(self, index, message):
        if 0 <= index < len(self._slots):
            self._slots[index].setText(message)

    def _on_scrolled(self, _value):
        self._sync_visible()
        self.positionChanged.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only the not-yet-decoded slots can change size on a resize now
        # that pages are never re-fitted to the window; _resize_slot is
        # a no-op for the rest.
        for slot in self._slots:
            self._resize_slot(slot)
        self._sync_visible()


# ---- the page ------------------------------------------------------------
class ReaderPage(GlassPage):
    """The reader itself: a chapter list, a reading surface, and the one
    place progress is written back to the entry.

    It covers the *whole* window, sidebar included - it is parented to
    the main window's central widget rather than to main.py's page
    container (see `open_reader`). That is the entire mechanism behind
    "no sidebar while reading", and it is the same one player.py already
    uses; main.py needs no hide/restore code and knows nothing about
    it."""

    closed = Signal()

    def __init__(self, entry, data_file="tracker.json", parent=None,
                 origin_page=None):
        super().__init__(parent=parent)
        self.entry = dict(entry or {})
        self.data_file = data_file
        # Which page of the app the reader was opened from, so the door
        # button puts him back there rather than on whatever happens to
        # be underneath. None when the caller couldn't say - see
        # `open_reader`, which is the only thing that fills this in.
        self.origin_page = origin_page
        self.chapters = []
        self.chapter_index = -1
        self.direction = "rtl"
        self._page_headers = {}
        self._run = 0
        # Every chapter opens at the page's own size; +/- scales from
        # there. Not remembered on the entry - the fixed baseline is the
        # point, and a zoom left at 250% from a fortnight ago is not.
        self._zoom = 1.0
        self._closed = False
        self._last_scroll = 0
        # The chapter controls are hover-only, so this is the one thing
        # that decides whether they are on screen - never the scroll.
        self._bottom_shown = False
        self._pending_start_page = 0
        self._pending_resume = None
        self._refresh_toast = None
        self._bottom_widgets = None     # see resizeEvent's guard
        # Filled in the first time the download dialog opens, from the
        # folder last used anywhere in the app.
        self._download_folder = None

        self._signals = _ChapterSignals()
        self._signals.listed.connect(self._on_chapters_listed)
        self._signals.opened.connect(self._on_chapter_opened)

        self._store = _PageStore(self)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._write_position)

        self._build()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._load_chapters()

    # ---- construction -------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack_host = QWidget(objectName="Bare")
        self._stack = QVBoxLayout(self._stack_host)
        self._stack.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack_host, stretch=1)

        self._list_view = _ChapterListView()
        self._list_view.picked.connect(self.open_chapter)
        self._strip_view = _StripView(self._store)
        self._strip_view.positionChanged.connect(self._on_position_changed)
        self._strip_view.ranOff.connect(self._on_ran_off)

        self._message = QWidget(objectName="Bare")
        message_column = QVBoxLayout(self._message)
        message_column.addStretch()
        self._message_label = QLabel("", objectName="PanelSubtitle")
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_column.addWidget(self._message_label)
        self._message_button = QPushButton("Open in Browser", objectName="Accent")
        use_hover_cursor(self._message_button)
        self._message_button.clicked.connect(self._open_in_browser)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(self._message_button)
        button_row.addStretch()
        message_column.addLayout(button_row)
        message_column.addStretch()

        for widget in (self._list_view, self._strip_view, self._message):
            self._stack.addWidget(widget)
            widget.setVisible(False)

        # Built last and parented straight to the page, deliberately not
        # added to `root`: they float over the reading surface so that
        # hiding either one moves nothing underneath (see BAR_HEIGHT).
        self._bar = self._build_bar()
        self._bar.setParent(self)
        self._bar.setFixedHeight(BAR_HEIGHT)
        self._build_bottom()

        # Watched for three things: the pointer reaching the top of the
        # window (which brings the top bar back), the pointer reaching
        # the bottom (which is the *only* thing that brings the chapter
        # controls back), and the scroll direction.
        view = self._strip_view
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.verticalScrollBar().valueChanged.connect(self._on_scroll_value)

        self._show_only(self._message)
        self._set_message("Loading chapters...", browser=False)

    def _build_bar(self):
        bar = QWidget(objectName="ReaderBar")
        # Scoped to the object name rather than set bare: a plain
        # `background:` on a parent propagates into every child, and the
        # buttons in here have their own QSS to keep.
        bar.setStyleSheet(
            f"#ReaderBar {{ background: {theme.SURFACE}; "
            f"border-bottom: 1px solid {theme.BORDER}; }}")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(8)

        # Left: which chapter this is - the chapter's *own* number, not
        # its position in the list. "Chapter 4 of 41" was here and is
        # gone: on a 507-chapter One Piece listing the index says
        # nothing, and the number is what the rest of the app tracks by.
        self._chapter_label = QLabel("", objectName="SectionTitle")
        row.addWidget(self._chapter_label)

        self._browser_btn = self._glyph_button(
            ICON_BROWSER, "Open this chapter on its site")
        row.addWidget(self._browser_btn)
        self._browser_btn.clicked.connect(self._open_in_browser)
        row.addStretch()

        # The title is centred on the *window*, so it is not in `row` at
        # all: a label between two stretches sits in the middle of what
        # the two clusters leave over, and those clusters are different
        # widths, so it would land off centre by half their difference.
        # A free child positioned across the whole bar is the only way
        # the centre is the real centre - see _place_title, which also
        # elides it before it can reach either cluster.
        self._title_label = QLabel(self.entry.get("title") or "Reader",
                                   bar, objectName="SectionTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title_label.lower()

        # Right cluster, and the order is read from the right edge
        # inwards: exit, refresh, then the size buttons. Full screen sits
        # outside those three because it is the one control here that
        # changes the window rather than the chapter.
        self._full_btn = self._glyph_button(ICON_FULLSCREEN, "Full screen (F)")
        self._full_btn.clicked.connect(self._toggle_fullscreen)
        row.addWidget(self._full_btn)

        # Sizing, scaling the page's own size up or down. There is no fit
        # menu beside these any more: 100% is always the original, and
        # these are the only thing that moves off it (0 resets).
        self._zoom_out_btn = self._button("−", "Smaller (−)")
        self._zoom_out_btn.clicked.connect(lambda: self._change_zoom(-ZOOM_STEP))
        self._zoom_label = QLabel("100%", objectName="Muted")
        self._zoom_label.setToolTip("Page size - 100% is the image's own size (0 resets)")
        self._zoom_in_btn = self._button("+", "Bigger (+)")
        self._zoom_in_btn.clicked.connect(lambda: self._change_zoom(ZOOM_STEP))
        # Squared to the glyph buttons' footprint: left at the app's
        # default padding these came out 89px wide against the 34px
        # icons beside them (measured), which made the right-hand
        # cluster read as two unrelated groups.
        for button in (self._zoom_out_btn, self._zoom_in_btn):
            button.setFixedSize(34, 30)
        row.addWidget(self._zoom_out_btn)
        row.addWidget(self._zoom_label)
        row.addWidget(self._zoom_in_btn)

        self._download_btn = self._glyph_button(
            ICON_DOWNLOAD, "Save chapters as .cbz files")
        self._download_btn.clicked.connect(self._open_download_dialog)
        row.addWidget(self._download_btn)

        self._refresh_btn = self._glyph_button(
            ICON_REFRESH, "Re-fetch this chapter from the site (R)")
        self._refresh_btn.clicked.connect(self.refresh_chapter)
        row.addWidget(self._refresh_btn)

        self._leave_btn = self._glyph_button(
            ICON_LEAVE, "Leave the reader (Esc)")
        self._leave_btn.clicked.connect(self.leave)
        row.addWidget(self._leave_btn)
        return bar

    def _build_bottom(self):
        """The chapter controls: previous and next pinned hard to the
        window's two edges with the jump list centred between them,
        floating over the artwork.

        Three separate children rather than one full-width bar, and that
        is the point of them: a bar would cover the bottom 60px of every
        page and swallow the wheel and any click there even where it was
        showing nothing but background. Positioned by hand in
        _place_bottom - there is no layout here to join (see the top bar).

        Which edge is which follows the reading direction, not Western
        UI habit: this is right-to-left content, so *next* is to the left
        and *previous* to the right, the same way Ctrl+Left is already
        the next chapter. Both carry the word as well as the chevron, so
        the position never has to be interpreted."""
        self._next_btn = self._button("‹  Next Chapter",
                                      "Next chapter (Ctrl+Left)")
        self._next_btn.clicked.connect(lambda: self.step_chapter(1))
        self._prev_btn = self._button("Previous Chapter  ›",
                                      "Previous chapter (Ctrl+Right)")
        self._prev_btn.clicked.connect(lambda: self.step_chapter(-1))

        self._chapter_box = _ChapterCombo(self)
        self._chapter_box.setToolTip("Jump to a chapter")
        # The popup is a separate view with its own width, and it does
        # *not* inherit the box's: chapter names are long ("Chapter 214 -
        # ...") and were being elided to nothing useful. Widened here,
        # and the font is set on the view as well because a QListView
        # paints its items with its own font() rather than the QSS
        # ::item font-family (.claude/rules/ui.md).
        popup = self._chapter_box.view()
        popup.setMinimumWidth(560)
        popup.setFont(self._chapter_box.font())
        popup.setTextElideMode(Qt.TextElideMode.ElideRight)
        use_hover_cursor(self._chapter_box)
        self._chapter_box.activated.connect(self.open_chapter)

        self._bottom_widgets = (self._next_btn, self._chapter_box,
                                self._prev_btn)
        for widget in self._bottom_widgets:
            widget.setParent(self)
            widget.setVisible(False)
        # Solid rather than transparent: these sit on top of artwork that
        # is white as often as it is black, and a control that borrows
        # the page's colours is unreadable on half of them.
        edge = (f"QPushButton {{ background: {theme.SURFACE};"
                f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS_SM}px; font-weight: 600; }}"
                f"QPushButton:hover {{ background: {theme.SURFACE_HOVER};"
                f" border: 1px solid {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: {theme.TEXT_DIM};"
                f" border: 1px solid {theme.BORDER}; }}")
        self._next_btn.setStyleSheet(edge)
        self._prev_btn.setStyleSheet(edge)

    def _button(self, text, tooltip):
        button = QPushButton(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        use_hover_cursor(button)
        return button

    def _glyph_button(self, glyph, tooltip):
        """A bar button carrying a Segoe Fluent glyph instead of a word.

        The font family has to be named on the button itself: the glyph
        is a private-use codepoint, and in the app's default family it
        is a blank box rather than an icon (theme.FONT_STACK_ICONS is
        the same stack the sidebar and the player use)."""
        button = self._button(glyph, tooltip)
        button.setFixedSize(34, 30)
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {theme.TEXT}; font-family: {theme.FONT_STACK_ICONS};"
            f" font-size: 12pt; border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
        return button

    def _in_chapter(self):
        return self.chapter_index >= 0 and self._strip_view.isVisible()

    def _show_only(self, widget):
        for child in (self._list_view, self._strip_view, self._message):
            child.setVisible(child is widget)
        reading = widget is self._strip_view
        # The reading surface runs edge to edge under the floating bars;
        # the list and the message keep the top bar's height clear so
        # their first row isn't hidden behind it.
        self._stack.setContentsMargins(
            *((0, 0, 0, 0) if reading else (16, BAR_HEIGHT + 10, 16, 12)))
        self._reveal_bar()
        # The chapter controls belong to a chapter. On the list and on an
        # error there is nothing for them to step through, and the list
        # has its own rows to click.
        if reading:
            self._place_bottom()
        else:
            self._hide_bottom()

    def _set_message(self, text, browser=True):
        self._message_label.setText(text)
        self._message_button.setVisible(browser)
        self._show_only(self._message)

    # ---- top bar visibility --------------------------------------------
    def _reveal_bar(self):
        self._bar.show()
        self._bar.raise_()

    def _hide_bar(self):
        # Only ever inside a chapter: the list and the error messages
        # have no scroll gesture behind them and would lose their only
        # way back out.
        if not self._in_chapter():
            return
        if self._chapter_box.view().isVisible():
            return          # the jump list is open over it - don't yank it
        self._bar.hide()

    def _on_scroll_value(self, value):
        """Hide on the way down, come back on the way up.

        Thresholded rather than sign-only, and `_last_scroll` is only
        moved when the threshold is crossed, so a slow drag accumulates
        into one decision instead of flickering the bar on the jitter of
        a trackpad."""
        delta = value - self._last_scroll
        if abs(delta) < SCROLL_HIDE_DELTA:
            return
        self._last_scroll = value
        if delta > 0:
            self._hide_bar()
        else:
            self._reveal_bar()
        # Deliberately not touched here. The chapter controls are hover-
        # only: scrolling up mid-chapter to re-read a panel is not a
        # request to jump chapters, and having them appear over the
        # artwork every time the strip went backwards is what made them
        # worth pinning to the floor in the first place.

    # ---- chapter controls (hover only) ---------------------------------
    def _place_bottom(self):
        """Put the three floating controls on the window's floor.

        Widths are fixed rather than taken from sizeHint so this can run
        before any of them has been shown - _show_only calls it on the
        way into a chapter, which is before the first paint."""
        y = self.height() - BOTTOM_HEIGHT + (
            BOTTOM_HEIGHT - BOTTOM_CONTROL_HEIGHT) // 2
        width = self.width()
        # x=0 and flush against the right edge, not inset: "pinned hard
        # to the far left and far right edges" is the ask, and an inset
        # of even 8px reads as floating rather than pinned.
        self._next_btn.setGeometry(0, y, BOTTOM_STEP_WIDTH,
                                   BOTTOM_CONTROL_HEIGHT)
        self._prev_btn.setGeometry(width - BOTTOM_STEP_WIDTH, y,
                                   BOTTOM_STEP_WIDTH, BOTTOM_CONTROL_HEIGHT)
        # Centred on the window, which is the same centre the artwork is
        # drawn on - and capped, so it stays a control rather than
        # growing into a bar as the window widens.
        gap = max(0, width - 2 * (BOTTOM_STEP_WIDTH + BOTTOM_GAP))
        box_width = max(200, min(CHAPTER_BOX_WIDTH, gap))
        self._chapter_box.setGeometry((width - box_width) // 2, y,
                                      box_width, BOTTOM_CONTROL_HEIGHT)
        # Positioning never reveals them - they are hover-only, so a
        # resize or a chapter opening leaves them exactly as hidden or
        # as shown as they already were.
        if self._bottom_shown:
            self._reveal_bottom()

    def _reveal_bottom(self):
        self._bottom_shown = True
        for widget in self._bottom_widgets:
            widget.show()
            widget.raise_()

    def _hide_bottom(self):
        if self._chapter_box.view().isVisible():
            return          # the jump list is open - don't yank it shut
        self._bottom_shown = False
        for widget in self._bottom_widgets:
            widget.hide()

    # ---- chapter loading ----------------------------------------------
    def _load_chapters(self, refresh=False):
        if chapter_source is None:
            self._set_message(
                "The chapter source isn't available in this build, so there "
                "is nothing to read from here. The entry's own page still "
                "opens in your browser.")
            return
        self._run += 1
        lookup_pool.submit(_list_chapters_job, self._signals, self._run,
                           self.entry, refresh)

    def refresh_chapter(self):
        """Re-fetch what is on screen from the site.

        Both halves, because "the images are broken" and "the site put up
        a fixed version" are the two reasons to press this and neither is
        fixed by the other: the chapter list is re-listed past
        chapter_source's cache, *and* the cached image files for the
        pages currently open are dropped so they are downloaded again
        rather than re-read from disk."""
        if chapter_source is None:
            return
        for url in self._store.urls():
            _drop_cached_page(url)
        if 0 <= self.chapter_index < len(self.chapters):
            self._pending_resume = {
                "chapter_id": self.chapters[self.chapter_index].get("id"),
                "index": self.chapter_index,
                "page": int(self._current_view().index()) if self._store.count() else 0,
            }
        else:
            self._pending_resume = None
        self._refresh_toast = show_toast(self, "Refreshing...", duration_ms=None)
        self._load_chapters(refresh=True)

    def _on_chapters_listed(self, run, chapters, error):
        if run != self._run or self._closed:
            return
        if chapters is None:
            self._finish_refresh("Refresh Failed")
            self._set_message(
                f"The chapter list couldn't be loaded. {error}".strip())
            return
        self.chapters = list(chapters)
        if not self.chapters:
            self._finish_refresh("No Chapters Found")
            self._set_message(
                "No chapters were found for this title on any configured "
                "source - not in Arabic and not in any other language.")
            return
        self._chapter_box.clear()
        for chapter in self.chapters:
            label = chapter_title(chapter)
            if is_arabic(chapter):
                label = f"[عربي] {label}"
            self._chapter_box.addItem(label)
        arabic = [c for c in self.chapters if is_arabic(c)]
        self._list_view.set_notice(
            "" if arabic else
            "No Arabic chapters were found for this title - the chapters "
            "below are what the source has in other languages.")
        self._list_view.set_chapters(self.chapters, self._reading_index())
        self._show_only(self._list_view)
        self._sync_controls()
        self._finish_refresh("Refreshed")
        if self._pending_resume is not None:
            self._reopen_pending()
            return
        self._offer_resume()

    def _reading_index(self):
        """Which row of the chapter list is "the one being read".

        The open chapter while there is one - but the list is built
        *before* the first chapter opens (and a resume opens one only
        after), so on the way in there is no chapter_index yet and the
        list would land with nothing marked at all. The saved position
        answers it there, and the entry's last-read chapter number
        answers it for a title opened for the first time on this
        machine. -1 only when the entry has never been read."""
        if 0 <= self.chapter_index < len(self.chapters):
            return self.chapter_index
        saved = self.entry.get("reader_position")
        chapter_id = saved.get("chapter_id") if isinstance(saved, dict) else None
        if chapter_id is not None:
            for index, chapter in enumerate(self.chapters):
                if chapter.get("id") == chapter_id:
                    return index
        try:
            last = float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last:
            for index, chapter in enumerate(self.chapters):
                if chapter_number(chapter) == last:
                    return index
        return -1

    def _finish_refresh(self, text):
        if self._refresh_toast is None:
            return
        finish_toast(self._refresh_toast, self, text)
        self._refresh_toast = None

    def _reopen_pending(self):
        """Go back to the chapter Refresh was pressed on.

        By id first: a re-list can have picked up newer chapters at the
        top, so the index it had is not the index it has. The index is
        only a fallback for a source that hands back no ids."""
        resume, self._pending_resume = self._pending_resume, None
        chapter_id = resume.get("chapter_id")
        page = int(resume.get("page") or 0)
        for index, chapter in enumerate(self.chapters):
            if chapter_id is not None and chapter.get("id") == chapter_id:
                self.open_chapter(index, start_page=page)
                return
        index = int(resume.get("index") or 0)
        if 0 <= index < len(self.chapters):
            self.open_chapter(index, start_page=page)

    def _offer_resume(self):
        """Reopen where reading stopped, when the saved position still
        points at a chapter that exists. Silently ignored otherwise - a
        source can renumber or drop a chapter between sessions, and
        opening the wrong one is worse than opening the list."""
        saved = self.entry.get("reader_position")
        if not isinstance(saved, dict):
            return
        chapter_id = saved.get("chapter_id")
        for index, chapter in enumerate(self.chapters):
            if chapter.get("id") == chapter_id:
                self.open_chapter(index, start_page=int(saved.get("page") or 0))
                return

    def open_chapter(self, index, start_page=0):
        if not (0 <= index < len(self.chapters)):
            return
        self.chapter_index = index
        self._pending_start_page = max(0, start_page)
        self._run += 1
        self._set_message("Loading chapter...", browser=True)
        self._sync_controls()
        # Opening is what records progress - see _mark_chapter_read. It
        # happens here rather than on reaching the last page because the
        # rest of the app now tracks reading by the last chapter opened,
        # and a chapter half-read is still the one being read.
        self._mark_chapter_read()
        lookup_pool.submit(_chapter_pages_job, self._signals, self._run,
                           self.chapters[index])

    def _on_chapter_opened(self, run, payload, error):
        if run != self._run or self._closed:
            return
        if payload is None:
            self._set_message(f"This chapter couldn't be opened. {error}".strip())
            return
        pages = list(payload.get("pages") or [])
        if not pages:
            self._set_message(
                "The source returned no pages for this chapter. It may have "
                "been removed, or the site changed its reader.")
            return
        self.direction = (payload.get("direction") or "rtl").lower()
        self._page_headers = dict(payload.get("headers") or {})
        self._store.configure(pages, self._page_headers)
        self._show_chapter(start_page=self._pending_start_page)

    def _show_chapter(self, start_page=0):
        self._last_scroll = 0
        self._show_only(self._strip_view)
        self._strip_view.rebuild()
        self._strip_view.set_index(start_page)
        # Re-anchored *after* the jump to the resume page, not before it:
        # that jump is a scroll of hundreds of px and would otherwise
        # read as "scrolling down", hiding the bar the instant a chapter
        # opened.
        self._last_scroll = self._strip_view.verticalScrollBar().value()
        self._reveal_bar()
        self._sync_controls()

    # ---- navigation ----------------------------------------------------
    def step_chapter(self, step):
        """Move a chapter in *reading* order: +1 is the next chapter to
        read, -1 the one before it.

        **The sign is inverted against the index on purpose.**
        chapter_source.list_chapters returns chapters newest first
        (`_sort_key`, `reverse=True`), so the next chapter to read is at
        index **- 1**, not index + 1. This shipped backwards - Previous
        opened the chapter after the one being read and Next opened the
        one before it, and running off the bottom of a strip went
        backwards too, because every caller passed a raw index delta.
        The inversion happens here, once, so the two buttons, Ctrl+arrow
        and ranOff can all talk in reading order.

        Language is the second half of it: chapters arrive Arabic-first
        rather than in one flat order, so the adjacent index can be the
        English fallback of the chapter just read. Same-language first,
        and only then whatever is adjacent."""
        if not self.chapters:
            return
        if self.chapter_index < 0:
            self.open_chapter(0)
            return
        delta = -1 if step > 0 else 1
        current_lang = str(self.chapters[self.chapter_index].get("lang") or "").lower()
        candidates = range(self.chapter_index + delta,
                           len(self.chapters) if delta > 0 else -1,
                           delta)
        for index in candidates:
            if str(self.chapters[index].get("lang") or "").lower() == current_lang:
                self.open_chapter(index)
                return
        nearby = self.chapter_index + delta
        if 0 <= nearby < len(self.chapters):
            self.open_chapter(nearby)
            return
        show_toast(self, "No More Chapters" if step > 0 else "This Is the First Chapter")

    def _on_ran_off(self, direction):
        self.step_chapter(1 if direction > 0 else -1)

    def _current_view(self):
        # One reading surface. Kept as a method because the persistence
        # and refresh paths ask it for the page index, and a second
        # surface is exactly what this reader is not getting again.
        return self._strip_view

    def _on_position_changed(self):
        self._sync_controls()
        self._save_timer.start(SAVE_POSITION_MS)

    # ---- view controls -------------------------------------------------
    def _sync_controls(self):
        total = len(self.chapters)
        reading = 0 <= self.chapter_index < total
        if reading:
            number = chapter_number(self.chapters[self.chapter_index])
            self._chapter_label.setText(
                f"Chapter {format_chapter_progress(number)}"
                if number is not None else "Chapter")
            self._chapter_box.blockSignals(True)
            self._chapter_box.setCurrentIndex(self.chapter_index)
            self._chapter_box.blockSignals(False)
        else:
            self._chapter_label.setText(f"{total} chapters" if total else "")
        # Marked on the box itself as well as selected - see
        # _ChapterCombo.set_current_chapter for why selection alone is
        # not visible after scrolling a 500-row popup.
        self._chapter_box.set_current_chapter(self.chapter_index if reading else -1)
        # Newest first, so the *next* chapter is the lower index: at
        # index 0 there is nothing further to read, at the last index
        # nothing earlier.
        self._next_btn.setEnabled(reading and self.chapter_index > 0)
        self._prev_btn.setEnabled(reading and self.chapter_index < total - 1)
        self._refresh_btn.setEnabled(chapter_source is not None
                                     and self._refresh_toast is None)
        self._zoom_label.setText(f"{round(self._zoom * 100)}%")
        self._place_title()

    def _place_title(self):
        """The entry's name, centred on the window and elided before it
        can reach either cluster of buttons.

        The reserve is twice the wider cluster because the label spans
        the whole bar: taking only the wider side off one end would move
        the text's centre off the window's."""
        label = getattr(self, "_title_label", None)
        if label is None:
            return
        width = self._bar.width()
        label.setGeometry(0, 0, width, BAR_HEIGHT)
        left = self._browser_btn.geometry().right()
        right = width - self._leave_btn.geometry().left()
        reserve = 2 * max(left, right, 0) + 24
        text = self.entry.get("title") or "Reader"
        metrics = QFontMetrics(label.font())
        label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                         max(40, width - reserve)))
        label.setToolTip(text)

    def _change_zoom(self, delta):
        zoom = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom + delta))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self._apply_zoom()

    def _apply_zoom(self):
        self._strip_view.set_zoom(zoom_key(self._zoom))
        self._sync_controls()

    def _toggle_fullscreen(self):
        window = self.window()
        toggle = getattr(window, "toggle_fullscreen", None)
        if callable(toggle):
            toggle()
        # Asked *after* the toggle, and off the window rather than
        # remembered here: F and the title bar can both change this, so
        # a flag of our own goes stale the first time either is used.
        self._sync_fullscreen_glyph()

    def _sync_fullscreen_glyph(self):
        full = bool(self.window().isFullScreen())
        self._full_btn.setText(ICON_EXIT_FULLSCREEN if full else ICON_FULLSCREEN)
        self._full_btn.setToolTip("Leave full screen (F)" if full
                                  else "Full screen (F)")

    def _open_in_browser(self):
        url = ""
        if 0 <= self.chapter_index < len(self.chapters):
            url = self.chapters[self.chapter_index].get("url") or ""
        url = url or self.entry.get("url") or ""
        if not url:
            show_toast(self, "This Entry Has No Page to Open")
            return
        webbrowser.open(url)

    # ---- downloading ---------------------------------------------------
    def _download_candidates(self):
        """The chapters a range may be taken from: everything in one
        language, newest first.

        One language, not the whole list, and this is not tidiness. The
        list arrives grouped - Arabic first, then whatever other
        languages the source has - so an index span across the join holds
        the same chapter twice. Both copies save as
        `<title> - <number>.cbz` (helpers.downloads._run_chapter), so the
        second silently overwrites the first and a "42 chapters" download
        leaves 30 files. The language of the chapter being read decides
        it; on the chapter list, the first chapter's does."""
        if not self.chapters:
            return []
        anchor = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters) else 0)
        wanted = str(self.chapters[anchor].get("lang") or "").lower()
        return [index for index, chapter in enumerate(self.chapters)
                if str(chapter.get("lang") or "").lower() == wanted]

    def _open_download_dialog(self):
        """This chapter, or a range of them, saved as .cbz.

        A dialog rather than the player's overlay panel: this page is an
        ordinary widget in the main window, and every other "fill this in
        and confirm" in the app (adding a game, editing an entry) is a
        QDialog. The reader's own bars are hover-revealed and float over
        the artwork, which is the wrong place to put a form."""
        if not self.chapters:
            show_toast(self, "No Chapters to Download")
            return
        from windows import downloads_page
        if self._download_folder is None:
            self._download_folder = downloads_page.saved_folder()

        candidates = self._download_candidates()
        reading = self.chapter_index if self.chapter_index in candidates else -1
        current = candidates.index(reading) if reading >= 0 else 0

        dialog = QDialog(self)
        dialog.setWindowTitle("Download Chapters")
        dialog.setMinimumWidth(560)
        theme.apply_dark_titlebar(dialog)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)

        scope = QComboBox()
        scope.addItems(["This Chapter", "A Range of Chapters"])
        use_hover_cursor(scope)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Download:"))
        scope_row.addWidget(scope, stretch=1)
        column.addLayout(scope_row)

        labels = [chapter_title(self.chapters[index]) for index in candidates]
        first_box, last_box = QComboBox(), QComboBox()
        for box in (first_box, last_box):
            box.addItems(labels)
            box.setCurrentIndex(current)
            use_hover_cursor(box)
            # A QComboBox popup paints its items with the view's own
            # font, not the QSS ::item family (.claude/rules/ui.md), and
            # chapter names are long enough to need the width.
            box.view().setMinimumWidth(460)
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

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Saving to:", objectName="Muted"))
        folder_label = QLabel(self._download_folder)
        folder_label.setToolTip(self._download_folder)
        folder_row.addWidget(folder_label, stretch=1)
        change_btn = QPushButton("Change...")
        use_hover_cursor(change_btn)
        folder_row.addWidget(change_btn)
        column.addLayout(folder_row)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("Cancel")
        use_hover_cursor(cancel_btn)
        cancel_btn.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_btn)
        start_btn = QPushButton("Download", objectName="Accent")
        use_hover_cursor(start_btn)
        buttons.addWidget(start_btn)
        column.addLayout(buttons)

        def picked_indexes():
            if scope.currentIndex() == 0:
                return [candidates[first_box.currentIndex()]]
            low = min(first_box.currentIndex(), last_box.currentIndex())
            high = max(first_box.currentIndex(), last_box.currentIndex())
            # Reversed: the list runs newest first, and a range should
            # arrive on disk oldest first so the queue reads in order.
            return list(reversed(candidates[low:high + 1]))

        def sync(*_args):
            ranged = scope.currentIndex() == 1
            last_box.setEnabled(ranged)
            count = len(picked_indexes())
            count_label.setText(
                f"{count} chapter{'s' if count != 1 else ''} will be saved as "
                f".cbz file{'s' if count != 1 else ''}.")
            start_btn.setText("Download" if count == 1 else f"Download {count}")

        scope.currentIndexChanged.connect(sync)
        first_box.currentIndexChanged.connect(sync)
        last_box.currentIndexChanged.connect(sync)

        def change_folder():
            picked = downloads_page.choose_folder(dialog, self._download_folder)
            if not picked:
                return
            self._download_folder = picked
            folder_label.setText(picked)
            folder_label.setToolTip(picked)

        change_btn.clicked.connect(change_folder)
        start_btn.clicked.connect(
            lambda: self._start_chapter_download(dialog, picked_indexes()))
        sync()
        dialog.exec()

    def _start_chapter_download(self, dialog, indexes):
        chapters = [self.chapters[index] for index in indexes
                    if 0 <= index < len(self.chapters)]
        if not chapters:
            show_toast(self, "Nothing to Download")
            return
        # Asked once, with the count, and only for a range: this list can
        # run to several hundred chapters, and queueing all of One Piece
        # must not be a single click on the wrong row.
        if len(chapters) > 1 and QMessageBox.question(
                dialog, "Download Chapters",
                f"Queue {len(chapters)} chapters for download?"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            downloads.queue_chapters(self.entry, chapters,
                                     folder=self._download_folder)
        except Exception:
            logs.exception("Could not queue a chapter download")
            show_toast(self, "Could Not Queue Those Chapters")
            return
        dialog.accept()
        show_toast(self, "Queued for Download" if len(chapters) == 1
                   else f"Queued {len(chapters)} Chapters")

    # There is no "back to the chapter list" any more, and no button for
    # it: the jump list pinned to the bottom centre does that job from
    # inside the chapter, without leaving the page being read. The list
    # view itself stays - it is what a title opens on when there is no
    # saved position to resume.

    # ---- persistence ---------------------------------------------------
    def _write_position(self):
        if not self.chapters or self.chapter_index < 0 or not self._store.count():
            return
        chapter = self.chapters[self.chapter_index]
        self._update_entry({"reader_position": {
            "chapter_id": chapter.get("id"),
            "chapter_number": chapter.get("number"),
            "page": int(self._current_view().index()),
            "saved_at": storage.now_iso(),
        }})

    def _mark_chapter_read(self):
        """Raise the entry's own last-read chapter to the one just
        opened.

        The tracker's existing `last_watched_chapter` (a float - see
        tracker.parse_chapter_progress), never a second field of this
        page's own: the card, Home and the schedule all read that one.
        Only ever raised - reopening chapter 3 of a series you are 40
        chapters into must not throw that away.

        Silent: this page has no manual progress control any more, and a
        toast on every chapter opened is noise, not news."""
        if not (0 <= self.chapter_index < len(self.chapters)):
            return
        number = chapter_number(self.chapters[self.chapter_index])
        if number is None:
            return
        current = self.entry.get("last_watched_chapter") or 0.0
        if number <= current:
            return
        self.entry["last_watched_chapter"] = number
        self._update_entry({"last_watched_chapter": number,
                            "updated_at": storage.now_iso()})

    def _update_entry(self, fields) -> bool:
        """One entry, never a whole list back from this page - several
        pages hold their own copy of tracker.json and a whole-list save
        restores a snapshot minutes stale (.claude/rules/ui.md, and the
        reordering defect it comes from)."""
        entry_id = self.entry.get("id")
        if not entry_id:
            return False
        self.entry.update(fields)
        try:
            return storage.update_entry(self.data_file, entry_id, fields)
        except Exception:
            logs.exception("reader could not save progress")
            return False

    # ---- input ----------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        control = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if key == Qt.Key.Key_Escape:
            self.leave()
            return
        if key == Qt.Key.Key_F and not control:
            self._toggle_fullscreen()
            return
        if key == Qt.Key.Key_R and not control:
            self.refresh_chapter()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._change_zoom(ZOOM_STEP)
            return
        if key == Qt.Key.Key_Minus:
            self._change_zoom(-ZOOM_STEP)
            return
        if key == Qt.Key.Key_0:
            self._zoom = 1.0
            self._apply_zoom()
            return
        if self.chapter_index < 0 or not self._store.count():
            super().keyPressEvent(event)
            return
        view = self._strip_view
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            # Right-to-left content, so the *left* arrow is forward -
            # the same direction the Next Chapter button sits in at the
            # bottom-left. There is one surface now, so this no longer
            # depends on a mode.
            forward = key == Qt.Key.Key_Left
            if control:
                self.step_chapter(1 if forward else -1)
            else:
                view.step(forward)
            return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_PageDown):
            view.step(True)
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_PageUp):
            view.step(False)
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            down = key == Qt.Key.Key_Down
            view.scroll_step(ARROW_SCROLL_PX if down else -ARROW_SCROLL_PX)
            return
        if key == Qt.Key.Key_Home:
            view.set_index(0)
            return
        if key == Qt.Key.Key_End:
            view.set_index(self._store.count() - 1)
            return
        super().keyPressEvent(event)

    # ---- lifetime --------------------------------------------------------
    def follow(self, host):
        """Track `host`'s size, so the reader stays exactly over what it
        covers - which is the whole central widget, sidebar included.

        An event filter rather than a layout: the pages under it are
        positioned by hand and slide over each other (main._show_page),
        so there is no layout here to join - and this keeps the hook in
        main.py to nothing at all."""
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_host", None) and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            return super().eventFilter(obj, event)
        if obj is self._strip_view.viewport():
            if event.type() == QEvent.Type.MouseMove:
                # The viewport's own top is the page's top while a
                # chapter is open (no layout margin there), so this
                # needs no coordinate mapping - and mapToGlobal is
                # wrong on mixed-DPI displays anyway.
                y = event.position().y()
                if y <= BAR_REVEAL_PX:
                    self._reveal_bar()
                # Hover is the *only* way the chapter controls come back
                # (the owner's ask: scrolling must never summon them),
                # so this is also the only place that shows them. The
                # pointer moving onto the controls themselves generates
                # no viewport move at all, which is what keeps them up
                # while they are being aimed at.
                if y >= self._strip_view.viewport().height() - BOTTOM_REVEAL_PX:
                    self._reveal_bottom()
                else:
                    self._hide_bottom()
            elif event.type() == QEvent.Type.Leave:
                # Pointer off the reading surface entirely - including
                # out of the window. Without this they stay up wherever
                # the last move left them.
                self._hide_bottom()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Guarded because adding this page's own layout in _build can
        # resize it before the floating children exist - they are
        # deliberately built last (they are not in any layout).
        if getattr(self, "_bottom_widgets", None) is None:
            return
        self._bar.setGeometry(0, 0, self.width(), BAR_HEIGHT)
        self._place_title()
        self._place_bottom()
        # A resize is also how full screen arrives, and it can be
        # triggered from the title bar rather than from this page.
        self._sync_fullscreen_glyph()

    def leave(self):
        """Close the reader and go back to wherever the manga was opened
        from - Home, or the Reading page.

        The sidebar needs nothing put back: it was never modified, only
        covered, so it reappears the moment this widget is hidden. The
        *page* underneath is the part that can be wrong. It is normally
        still the one the entry was opened from, and navigate_to no-ops
        when it is; it is only not when something navigated behind the
        reader while it was open (the updater's dialog, a global search),
        and landing on that page instead of the shelf he came from is
        what the door button is meant to rule out."""
        if self._closed:
            return
        self._closed = True
        self._save_timer.stop()
        self._write_position()
        self._run += 1          # nothing still in flight may touch this
        host = getattr(self, "_host", None)
        if host is not None:
            host.removeEventFilter(self)
        self.closed.emit()
        self.hide()
        window = self.window()
        navigate = getattr(window, "navigate_to", None)
        if self.origin_page and callable(navigate):
            try:
                navigate(self.origin_page, animate=False)
            except Exception:
                logs.exception("reader could not return to its origin page")
        page = getattr(window, "_current_page", None)
        if page is not None:
            page.setFocus()
        self.deleteLater()


# ---- background jobs -----------------------------------------------------
def _list_chapters_job(signals, run, entry, refresh=False):
    """The chapter list for one entry. Runs on a lookup_pool worker and
    must never raise - a dead worker takes every queued lookup with it."""
    try:
        chapters = chapter_source.list_chapters(
            entry, deadline=net.deadline_in(CHAPTER_LIST_TIMEOUT),
            refresh=bool(refresh))
        signals.listed.emit(run, list(chapters or []), "")
    except Exception as error:
        logs.exception("reader chapter listing failed")
        signals.listed.emit(run, None, _reason(error))


def _chapter_pages_job(signals, run, chapter):
    try:
        payload = chapter_source.chapter_pages(
            chapter, deadline=net.deadline_in(CHAPTER_PAGES_TIMEOUT))
        if not payload:
            signals.opened.emit(run, None, "The source returned nothing for it.")
            return
        signals.opened.emit(run, dict(payload), "")
    except Exception as error:
        logs.exception("reader chapter open failed")
        signals.opened.emit(run, None, _reason(error))


def _reason(error) -> str:
    """A failure in words, ending in a full stop - these are pasted
    straight into a sentence in front of the user."""
    text = str(error).strip()
    if not text:
        return f"The source failed with {type(error).__name__}."
    return text if text.endswith(".") else text + "."


def _origin_page_name(window):
    """Which of main.PAGES the reader is being opened over.

    Read off the window's own navigation history rather than passed in,
    because the callers are entry cards on two different pages and
    neither knows its own name - and importing main here to look up
    `_current_page`'s class would be a circular import at load time.
    None whenever the window is not the real one (a test harness, a
    stub), which the door button treats as "nowhere to go back to"."""
    try:
        history = getattr(window, "_history", None)
        index = getattr(window, "_history_index", None)
        if history and isinstance(index, int) and 0 <= index < len(history):
            return history[index]
    except Exception:
        pass
    return None


def open_reader(window, entry, data_file="tracker.json"):
    """Open the reader over `window`, covering the sidebar as well.

    The one entry point, and the one thing main.py needs to call. It is
    deliberately not a member of main.PAGES: this is not a section of the
    app with a sidebar row, it is a surface opened on top of one and
    closed back out of.

    The host is the window's **central widget**, not main.py's page
    container - that one line is the whole of "the sidebar disappears
    while reading". player.py already does exactly this for the same
    reason; nothing in main.py hides or restores anything, so there is
    no state to get stuck in the hidden half."""
    host = window.centralWidget() if hasattr(window, "centralWidget") else window
    host = host if host is not None else getattr(window, "container", window)
    page = ReaderPage(entry, data_file, host,
                      origin_page=_origin_page_name(window))
    page.follow(host)
    page.show()
    page.raise_()
    page.setFocus()
    return page
