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

import datetime
import os
import re
import urllib.error
import urllib.request
import webbrowser
from collections import OrderedDict

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QFontMetrics, QImage,
                         QPainter, QPixmap)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from helpers import (app_settings, downloads, history, images, logs,
                     lookup_pool, net, storage, theme)
from helpers.widgets import (Card, GlassPage, GlyphButton, confirm,
                             finish_toast, frameless_dialog, show_toast,
                             use_hover_cursor)
from windows.tracker import correct_progress, format_chapter_progress


def _mark_history(entry, numbers, read: bool):
    """Tick (or untick) these chapters in History - the store that also
    holds titles with no saved entry, and the only one that can record
    single chapters rather than one read-up-to number. Never raises: a
    history write must not cost a mark."""
    try:
        marks = [history.chapter_key(n) for n in numbers if n is not None]
        if marks:
            history.set_watched(entry, marks, read)
    except Exception:
        logs.exception("could not write the reading history")

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
# Raised from 25s when chapter_source began following a paginated
# chapter list. olympustaff prints 40 chapters per page and hangs the
# other 200 off `?page=2..7`; the full list for The Beginning After the
# End measures 21.7s cold (249 chapters, no gaps) where the first page
# alone was 13s for 41. It is paid once per series per six hours - the
# listing is cached - and the alternative is the list the owner
# reported: only the newest forty chapters, ending at the one just read.
CHAPTER_LIST_TIMEOUT = 45.0
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

# Zoom, as a multiplier on the *medium's* baseline size. 100% is where
# every chapter opens.
ZOOM_STEP = 0.15
ZOOM_MIN, ZOOM_MAX = 0.25, 4.0

# What 100% means, per medium, as a multiple of the image's own pixels.
#
# It used to be 1.0 for everything - one image pixel per device pixel -
# and that is only right for one of the three. Manga scans are cut for
# print and come off these sites far wider than a page needs to be read
# at, so 100% was a page you had to zoom *out* of every time; manhwa and
# manhua are cut as narrow phone strips and arrived slightly too small.
# The numbers are the owner's own, measured against what he was setting
# by hand every chapter: manga 75% of the old baseline, the two vertical
# strips 115% - then raised again to 145% *of that* (1.15 x 1.45 =
# 1.67), the owner's ask, since 115% still opened smaller than the size
# every strip chapter was being zoomed to by hand.
#
# This is the baseline, not the zoom: the +/- controls and the label
# still read 100% here, so "100%" means "the size this medium opens at"
# rather than a number that has to be re-learned per series.
MEDIUM_BASE_SCALE = {"manga": 0.75, "manhwa": 1.67, "manhua": 1.67}
DEFAULT_BASE_SCALE = 1.0

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
# Within this far of the strip's end counts as 'finished the
# chapter', which is when the next/previous controls appear on
# their own. Generous rather than exact: the last page of a
# webtoon is often a tall credits panel, and the controls being
# there slightly early is better than having to hunt for them.
END_OF_CHAPTER_PX = 260
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
# first try and the owner still called it slow on a long webtoon; 640
# overshot a panel; 460 was called still slightly too fast; 380 was the
# next step down and the owner asked for slower again, so 320; and the
# owner asked for slower once more, so 260 - a bit over a quarter of a
# ~900px viewport, ~121 notches on the 31,365px Kingdom chapter
# measured earlier. Raised from Qt's 60 in the first place so a long
# webtoon does not need five hundred.
WHEEL_STEP_PX = 260

# The gap drawn between pages, per medium. Zero for the vertical strips
# - a webtoon's panels are cut mid-image and any spacing draws a seam
# through the artwork - and a sliver for manga only, whose pages are
# discrete printed pages: butted hard together they read as one long
# unbroken scan, which is what the owner asked to have visibly broken.
MANGA_PAGE_GAP_PX = 6

# Down/Up arrow, same reasoning as the wheel: 120 was a tenth of a
# screen and unusable as a way to move down a strip.
ARROW_SCROLL_PX = 200

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
ICON_LEAVE = "\ue76b"                 # ChevronLeft - the sidebar's own
                                      # fold glyph (main.FOLD_CLOSE_ICON),
                                      # which the player's exit and the
                                      # details page's back now carry too,
                                      # so one shape means "back" across
                                      # the whole app (the owner's ask)
ICON_CHAPTER_LIST = "\ue8fd"          # List - the same glyph the player's
                                      # episode list uses, so "the list of
                                      # things to open" is one icon in both
ICON_BROWSER = "\ue774"               # Globe
ICON_CHEVRON_DOWN = "\ue70d"          # ChevronDown - "this opens"
ICON_DOWNLOAD = "\ue896"              # Download
ICON_READ = "\ue73e"                  # CheckMark - a chapter already read

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


# A leading repetition of the chapter's own number, with whatever
# separator the site used between it and the name.
_LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-:–—.)]*\s*")


def chapter_name(chapter) -> str:
    """The chapter's own name for the list's centre column - without the
    number, which has a column of its own now.

    3asq writes the number into the name as well ("885 - <name>", all
    380 rows of Kingdom, measured), so leaving it there would print the
    same number twice on every row. Only a *leading* repetition of this
    chapter's own number is cut: olympustaff buries it mid-sentence
    instead, and pulling numbers out of the middle of somebody's title
    is guesswork. Falls back to the number when a source gives no name,
    so the column is never simply blank."""
    title = str(chapter.get("title") or "").strip()
    number = chapter_number(chapter)
    if title and number is not None:
        match = _LEADING_NUMBER_RE.match(title)
        if match and _as_number(match.group(1)) == number:
            title = title[match.end():].strip()
    if title:
        return title
    return (f"Chapter {format_chapter_progress(number)}"
            if number is not None else "Chapter")


def _as_number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def chapter_published(chapter) -> str:
    """A chapter's release date, readable - or "" when there isn't one.

    Empty rather than guessed, and that is the whole point of it.
    Measured across the owner's own entries: the scan sites put no date
    in their markup at all, so chapter_source hands back `published: ""`
    for every row of 3asq (380/380 blank), olympustaff and lavascans,
    and only the meshmanga v2 API carries a real timestamp (200/200
    filled, ISO with a Z). A plausible-looking made-up date would be
    indistinguishable from the real ones on the same list, which is
    worse than a blank column."""
    raw = str(chapter.get("published") or "").strip()
    if not raw:
        return ""
    try:
        moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A plain date, or a timestamp with a space where the T should
        # be - neither is worth its own parser, and anything else is a
        # shape nothing here produces.
        for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                moment = datetime.datetime.strptime(raw[:len(shape) + 2], shape)
                break
            except ValueError:
                continue
        else:
            return ""
    return f"{moment.day} {moment.strftime('%b %Y')}"


# The chapter row's two flanks, and they are the *same* fixed width on
# purpose: the name between them is centred on the row only if what sits
# on either side of it is equally wide. Unequal clusters would put its
# centre off the row's by half their difference - the same arithmetic
# _place_title does for the title in the top bar.
ROW_SIDE_WIDTH = 178
ROW_NUMBER_WIDTH = 66


class _ElidedLabel(QLabel):
    """A one-line label that takes whatever width the layout gives it and
    ends in an ellipsis instead of being clipped mid-glyph.

    A plain QLabel cannot do this. With word wrap off its minimum size
    hint is the whole string, so one long chapter name would widen every
    row in a 500-row list past the viewport; with word wrap on it becomes
    two lines and that row is taller than the ones around it, which is
    exactly what a list you scan down a column must not do. `Ignored` as
    the horizontal policy is what lets the layout hand it a width rather
    than ask it for one."""

    def __init__(self, text="", parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self._full = str(text or "")
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        self.setToolTip(self._full)
        self._elide()

    def _elide(self):
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full,
                                           Qt.TextElideMode.ElideRight,
                                           max(0, self.width())))

    def setText(self, text):
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class _ChapterListView(QWidget):
    """The chapter picker before reading starts: the series name, then one
    Card per chapter laid out number / name / release date, Arabic ones
    marked.

    Right-clicking a row marks that chapter read or unread - the only
    place on this page progress can be moved *down*, which is why it goes
    out as a signal for the page to run through tracker.correct_progress
    rather than being written from here."""

    picked = Signal(int)
    markRequested = Signal(int, bool)     # chapter index, finished?
    markAllRequested = Signal(bool)       # every chapter, finished?

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # The series name. It was nowhere on this page - the only copy
        # was the top bar's, which was being elided to a single ellipsis
        # (see _place_title), so a list of 380 Arabic chapter names
        # carried nothing at all saying which series they belonged to.
        self._title = QLabel("", objectName="PanelTitle")
        self._title.setWordWrap(True)
        self._title.setVisible(False)
        layout.addWidget(self._title)

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

    def set_title(self, text):
        self._title.setText(text or "")
        self._title.setVisible(bool(text))

    def set_notice(self, text):
        self._notice.setText(text or "")
        self._notice.setVisible(bool(text))

    def set_chapters(self, chapters, current=-1, read_up_to=0.0):
        # Torn down and rebuilt rather than updated in place - a page
        # here rebuilds from scratch, the same rule the rest of the app
        # follows (.claude/rules/ui.md).
        while self._rows.count() > 1:
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, chapter in enumerate(chapters):
            number = chapter_number(chapter)
            read = bool(read_up_to and number is not None
                        and number <= read_up_to)
            self._rows.insertWidget(
                index, self._build_row(index, chapter, index == current, read))

    def _build_row(self, index, chapter, reading=False, read=False):
        """One chapter: number hard left, name centred, date hard right.

        Three cells rather than a run of widgets in one row, because
        "centred" here means centred on the *row* - see ROW_SIDE_WIDTH.
        The `group` field that used to sit in here is gone: every source
        chapter_source builds sets it to "" (all four site shapes and the
        MangaDex path), so it was a widget that could never have text."""
        card = Card(matte=True)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(12)
        arabic = is_arabic(chapter)

        left = QWidget(objectName="Bare")
        left.setFixedWidth(ROW_SIDE_WIDTH)
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(8)
        number = chapter_number(chapter)
        number_label = QLabel(format_chapter_progress(number) if number is not None
                              else "-", objectName="CardTitle")
        number_label.setFixedWidth(ROW_NUMBER_WIDTH)
        # The chapter being read is marked on the row itself rather than
        # by selecting it: this list is rebuilt on every visit and has no
        # selection to keep, and after scrolling 500 rows "which one am I
        # on" is not answerable from anything else on screen.
        if reading:
            number_label.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 800;")
        left_row.addWidget(number_label)

        # The language is the point of this reader, so it is a badge and
        # not a suffix on the name: the owner is scanning for the Arabic
        # ones, and a word at the end of a variable-length line is not
        # something you can scan down a column. It sits in the left cell
        # for the same reason - a column, not a ragged edge.
        badge = QLabel("عربي" if arabic else
                       (str(chapter.get("lang") or "?").upper()))
        badge.setStyleSheet(
            f"color: {theme.ON_ACCENT if arabic else theme.TEXT_MUTED}; "
            f"background: {theme.ACCENT_GRADIENT if arabic else theme.SURFACE_HOVER}; "
            f"border: 1px solid {theme.ACCENT if arabic else theme.BORDER}; "
            f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
            f"font-weight: 700;")
        left_row.addWidget(badge)
        left_row.addStretch()
        row.addWidget(left)

        # Elided, not wrapped - see _ElidedLabel: a name that wraps makes
        # its row taller than the 500 around it, and one that does not
        # elide widens every row to the longest name in the list.
        name = _ElidedLabel(chapter_name(chapter), objectName="CardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if reading:
            name.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 800;")
        row.addWidget(name, stretch=1)

        right = QWidget(objectName="Bare")
        right.setFixedWidth(ROW_SIDE_WIDTH)
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(8)
        right_row.addStretch()
        if reading:
            mark = QLabel("Reading")
            mark.setStyleSheet(
                f"color: {theme.ON_ACCENT}; background: {theme.ACCENT_GRADIENT}; "
                f"border-radius: {theme.RADIUS_SM}px; padding: 2px 10px; "
                f"font-weight: 700;")
            right_row.addWidget(mark)
        elif read:
            # Every chapter reading has reached carries the tick, not
            # only the one being read - the owner's ask: the list should
            # answer "which of these have I been through" at a glance.
            # A glyph in the SUCCESS green, the same vocabulary the
            # player's episode list uses for the same fact.
            mark = QLabel(ICON_READ)
            mark.setToolTip("Read")
            mark.setStyleSheet(
                f"color: {theme.SUCCESS}; font-family: {theme.FONT_STACK_ICONS}; "
                f"font-size: 10pt; background: transparent;")
            right_row.addWidget(mark)
        published = chapter_published(chapter)
        date_label = QLabel(published, objectName="CardMeta")
        date_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        # Kept in the layout even with nothing in it, so the flank stays
        # its fixed width and the names down the list stay on one centre
        # line whether or not a source dates its chapters.
        right_row.addWidget(date_label)
        row.addWidget(right)

        card.clicked.connect(lambda i=index: self.picked.emit(i))
        card.rightClicked.connect(
            lambda event, i=index: self._show_mark_menu(event, i))
        return card

    def _show_mark_menu(self, event, index):
        """Mark a chapter read or unread from the list.

        Both wordings are always offered rather than one being worked out
        from the stored number: "finished" and "unfinished" are the two
        things the owner can want here, and hiding whichever one looks
        redundant means the menu changes shape row to row on a list he is
        scanning by position."""
        menu = QMenu(self)
        menu.addAction("Finished reading",
                       lambda: self.markRequested.emit(index, True))
        menu.addAction("Unfinished reading",
                       lambda: self.markRequested.emit(index, False))
        menu.addSeparator()
        menu.addAction("Mark all as read",
                       lambda: self.markAllRequested.emit(True))
        menu.addAction("Mark all as unread",
                       lambda: self.markAllRequested.emit(False))
        menu.exec(event.globalPosition().toPoint())


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
    500-chapter list neither of those is still visibly the one open.

    ...and a third that is not about drawing: right-clicking a row of the
    open list marks that chapter read or unread, so the mark is reachable
    from inside a chapter and not only from the list page."""

    markRequested = Signal(int, bool)     # chapter index, finished?
    markAllRequested = Signal(bool)       # every chapter, finished?

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_chapter = -1
        # Filtered on the popup's *viewport*, not on the box: the popup is
        # a separate top-level view with its own event stream, so a
        # contextMenuEvent on this widget never sees a press inside it.
        self.view().viewport().installEventFilter(self)
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
            f" border-radius: {theme.RADIUS}px;"
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

    def eventFilter(self, obj, event):
        if (obj is self.view().viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.RightButton):
            row = self.view().indexAt(event.position().toPoint()).row()
            if row >= 0:
                menu = QMenu(self)
                menu.addAction("Finished reading",
                               lambda r=row: self.markRequested.emit(r, True))
                menu.addAction("Unfinished reading",
                               lambda r=row: self.markRequested.emit(r, False))
                menu.addSeparator()
                menu.addAction("Mark all as read",
                               lambda: self.markAllRequested.emit(True))
                menu.addAction("Mark all as unread",
                               lambda: self.markAllRequested.emit(False))
                menu.exec(event.globalPosition().toPoint())
            # Swallowed either way: a right press the view sees becomes a
            # change of current row, which on this box is a jump to that
            # chapter - marking one must not also open it.
            return True
        return super().eventFilter(obj, event)

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
    zoomRequested = Signal(int)     # +1 / -1 zoom steps (Ctrl+wheel)

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
        # The reading scrollbar, restyled to actually be visible: the
        # app-wide handle is SURFACE_HOVER, which over near-black artwork
        # margins is a bar the owner reported as unreadable. Wider, with
        # a lit handle and an accent hover, scoped to this view only so
        # the rest of the app's bars stay as they are.
        self.verticalScrollBar().setStyleSheet(
            f"QScrollBar:vertical {{ background: {theme.BG_ALT};"
            f" width: 14px; margin: 0px; border-left: 1px solid {theme.BORDER}; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.TEXT_DIM};"
            f" min-height: 36px; border-radius: 6px; margin: 2px 2px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.ACCENT}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f" {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            f" {{ background: none; }}")
        self._store = store
        self._slots = []
        self._zoom = zoom_key(1.0)
        # See eventFilter: re-entrancy guard for the scrollbar wheel
        # forwarding, not state anything else reads.
        self._forwarding_wheel = False
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
        # **The wheel died in the last 11px of the window and this is
        # why.** The vertical bar is AlwaysOn, so it owns the rightmost
        # SCROLLBAR_WIDTH px of the reading surface - and a QScrollBar
        # handles the wheel *itself* and accepts it, so wheelEvent below
        # never ran there. Measured on a 1400px window over a 31,365px
        # chapter: x <= 1388 moved 460px a notch, x >= 1389 moved 60
        # (Qt's wheelScrollLines x singleStep), which on a strip that
        # long is 0.19% of it and reads as nothing happening at all.
        # Filtered rather than fixed by widening the margin: the bar has
        # to stay where it is and stay draggable.
        for bar in (self.verticalScrollBar(), self.horizontalScrollBar()):
            bar.installEventFilter(self)

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
        strip fly.

        Ctrl+wheel is zoom, not scroll - the shortcut every browser and
        image viewer has taught. Emitted as a request rather than acted
        on: the zoom (and the medium baseline folded into it) belongs to
        the page, not the view."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                self.zoomRequested.emit(1 if steps > 0 else -1)
            event.accept()
            return
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

    def set_page_gap(self, gap):
        """The space drawn between page slots - see MANGA_PAGE_GAP_PX for
        which media get one and why the strips must not."""
        self._column.setSpacing(max(0, int(gap)))

    def eventFilter(self, obj, event):
        """Wheel over either scrollbar scrolls the strip, not the bar.

        See the installEventFilter note in __init__: without this the
        rightmost 11px of the window scrolled at Qt's default 60px a
        notch while the rest of it moved WHEEL_STEP_PX, which the owner
        reported as the wheel not working over there. Returning True
        consumes it so the bar's own handler cannot also run and add its
        60 on top."""
        if (event.type() == QEvent.Type.Wheel
                and not self._forwarding_wheel
                and obj in (self.verticalScrollBar(), self.horizontalScrollBar())):
            # Guarded because wheelEvent's own fall-through calls
            # QScrollArea's handler, which hands the event straight back
            # to the scrollbar - and without the flag that arrives here
            # again and recurses until the stack ends. A trackpad's
            # pixel-delta scroll takes that path on every event.
            self._forwarding_wheel = True
            try:
                self.wheelEvent(event)
            finally:
                self._forwarding_wheel = False
            return True
        return super().eventFilter(obj, event)

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
        # A landscape page is a double-page spread (manga pages are cut
        # portrait; only a spread comes out wider than tall), and at the
        # chapter's zoom it lands about twice the viewport wide - the
        # owner was zooming out for page 3 of every such chapter and
        # back in for page 4. Fitted to the window's width instead,
        # display-only: the cache keeps the decode untouched, so nothing
        # is re-fetched when the window or the zoom changes, and
        # _typical_width is not fed a spread's width (it sizes the
        # placeholders for the ordinary pages).
        shown = QPixmap(pixmap)
        is_spread = pixmap.width() > pixmap.height()
        if is_spread:
            fit = self.viewport().width() * ratio
            if 0 < fit < shown.width():
                shown = shown.scaledToWidth(
                    int(fit), Qt.TransformationMode.SmoothTransformation)
        slot.natural = shown.size()
        slot.setText("")
        slot.setPixmap(_tagged(shown, ratio))
        slot.loaded = True
        if not is_spread:
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
                 origin_page=None, resume=True, chapter_number=None):
        super().__init__(parent=parent)
        self.entry = dict(entry or {})
        self.data_file = data_file
        # Whether opening this entry jumps straight back to where reading
        # stopped, or lands on the chapter list. The card now has two
        # targets - the round continue button on the cover resumes, the
        # rest of the card browses - and this is which one was pressed.
        self.resume_on_open = bool(resume)
        # A specific chapter to open the moment the list arrives - the
        # details page's chapter rows pass this. It outranks resume: a
        # row named a chapter, and opening anything else is a wrong
        # answer, not a convenience.
        self._open_number = chapter_number
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
        # Every chapter opens at 100%; +/- scales from there. Not
        # remembered on the entry - the fixed baseline is the point, and
        # a zoom left at 250% from a fortnight ago is not.
        self._zoom = 1.0
        # ...and what 100% is worth for this medium (see
        # MEDIUM_BASE_SCALE). Read off the entry's own type, so a manga
        # and a manhwa opened one after the other each land where they
        # are meant to without either being touched.
        self._base_scale = MEDIUM_BASE_SCALE.get(
            str(self.entry.get("type") or "").strip().lower(),
            DEFAULT_BASE_SCALE)
        self._closed = False
        self._last_scroll = 0
        # The chapter controls are hover-only, so this is the one thing
        # that decides whether they are on screen - never the scroll.
        self._bottom_shown = False
        # Why the chapter controls are showing: True when the strip
        # reached the end rather than the pointer arriving.
        self._bottom_at_end = False
        self._pending_start_page = 0
        self._pending_resume = None
        self._refresh_toast = None
        self._bottom_widgets = None     # see resizeEvent's guard
        # How many times _place_title has bailed out on a bar whose
        # layout had not run yet. Bounded there, not state anything reads.
        self._title_retries = 0
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
        self._list_view.markRequested.connect(self._mark_chapter)
        self._list_view.markAllRequested.connect(self._mark_all_chapters)
        self._list_view.set_title(self.entry.get("title") or "")
        self._strip_view = _StripView(self._store)
        self._strip_view.positionChanged.connect(self._on_position_changed)
        self._strip_view.ranOff.connect(self._on_ran_off)
        self._strip_view.zoomRequested.connect(
            lambda step: self._change_zoom(ZOOM_STEP * step))
        # Manga only - see MANGA_PAGE_GAP_PX. Read once here: the type
        # cannot change while the reader is open.
        if str(self.entry.get("type") or "").strip().lower() == "manga":
            self._strip_view.set_page_gap(MANGA_PAGE_GAP_PX)

        self._message = QWidget(objectName="Bare")
        message_column = QVBoxLayout(self._message)
        message_column.addStretch()
        # The series name over every message this page shows, and the
        # reason it is here rather than left to the top bar: "Loading
        # chapter..." on its own says nothing about *which* series is
        # loading, and the bar's own copy was being elided away to an
        # ellipsis (see _place_title, fixed in the same change).
        self._message_title = QLabel(self.entry.get("title") or "",
                                     objectName="PanelTitle")
        self._message_title.setWordWrap(True)
        self._message_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_title.setVisible(bool(self.entry.get("title")))
        message_column.addWidget(self._message_title)
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

        # Before the first chapter, not after it: the strip is born at
        # 1.0 and rebuild() reads that value, so a manga would decode its
        # first screenful at the old baseline and re-decode it the
        # moment anything touched the zoom.
        self._apply_zoom()

        self._show_only(self._message)
        self._set_message("Loading chapters...", browser=False)

    def _build_bar(self):
        bar = QWidget(objectName="ReaderBar")
        # Scoped to the object name rather than set bare: a plain
        # `background:` on a parent propagates into every child, and the
        # buttons in here have their own QSS to keep.
        # Rounded along the edge that faces the artwork; the two corners
        # sitting on the window's own top edge stay square, so the bar
        # reads as a panel laid over the page rather than a notched
        # window frame. This one is an ordinary child widget, not a
        # native window like the player's bars, so the corners it does
        # not paint simply show the page behind - no mask needed.
        bar.setStyleSheet(
            f"#ReaderBar {{ background: {theme.PANEL_FILL}; "
            f"border-bottom: 1px solid {theme.BORDER}; "
            f"border-bottom-left-radius: {theme.RADIUS_LG}px; "
            f"border-bottom-right-radius: {theme.RADIUS_LG}px; }}")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(8)

        # Far left: the way out, then which chapter this is. The player's
        # top bar opens the same way (arrow, then the episode list), so
        # leaving is in the same corner whichever one you are in. A large
        # left arrow now, not the SignOut door (the owner's ask - the
        # same glyph the player's prev/next carry), painted rather than
        # set as text so the clipping that read as a scratched icon
        # stays gone (see widgets.GlyphButton).
        self._leave_btn = GlyphButton(
            ICON_LEAVE, "Leave the reader (Esc)", size=(34, 30), font_pt=15)
        self._leave_btn.clicked.connect(self.leave)
        row.addWidget(self._leave_btn)

        # Beside the door, and the pair is deliberate: one leaves the
        # reader, the other goes up one level to the chapter list. The
        # jump list on the floor changes chapter without showing the
        # list, which is a different thing and not a way back to it.
        self._list_btn = self._glyph_button(
            ICON_CHAPTER_LIST, "Back to the chapter list")
        self._list_btn.clicked.connect(self.show_chapter_list)
        row.addWidget(self._list_btn)

        # The chapter's *own* number, not its position in the list.
        # "Chapter 4 of 41" was here and is gone: on a 507-chapter One
        # Piece listing the index says nothing, and the number is what
        # the rest of the app tracks by.
        self._chapter_label = QLabel("", objectName="SectionTitle")
        row.addWidget(self._chapter_label)
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
        #
        # ...and the padding itself has to go with the width, or the
        # button keeps the app-wide 8px 16px inside a 34px box and clips
        # its own "-"/"+" away to nothing - which is exactly what these
        # two were doing (measured: an empty frame, no glyph). Same trap
        # as _glyph_button below; the size is unchanged, only the inset.
        for button in (self._zoom_out_btn, self._zoom_in_btn):
            button.setFixedSize(34, 30)
            # Padding only. Everything else - fill, border, font size -
            # still comes from the app-wide rule, so these two look
            # exactly as they were meant to, minus the clipping.
            button.setStyleSheet("QPushButton { padding: 0px; }")
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

        # Far right: the site this chapter came from. It sits at the
        # opposite edge from the door on purpose - the two controls that
        # take you *out* of the reader bracket the bar rather than
        # sitting next to each other where one gets pressed for the
        # other.
        self._browser_btn = self._glyph_button(
            ICON_BROWSER, "Open this chapter on its site")
        self._browser_btn.clicked.connect(self._open_in_browser)
        row.addWidget(self._browser_btn)
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
        # Right-clicking a row of the open jump list marks it read or
        # unread - the same menu the list page's rows carry, so the mark
        # is reachable from inside a chapter too.
        self._chapter_box.markRequested.connect(self._mark_chapter)
        self._chapter_box.markAllRequested.connect(self._mark_all_chapters)

        self._bottom_widgets = (self._next_btn, self._chapter_box,
                                self._prev_btn)
        for widget in self._bottom_widgets:
            widget.setParent(self)
            widget.setVisible(False)
            # The pointer can leave a control without ever re-entering
            # the strip's viewport - off the side of the window, or up
            # into the top bar - and the viewport's own Leave is the
            # only other thing watching. Deferred a turn so the Enter
            # onto a neighbouring control lands first.
            widget.installEventFilter(self)
        # Solid rather than transparent: these sit on top of artwork that
        # is white as often as it is black, and a control that borrows
        # the page's colours is unreadable on half of them.
        edge = (f"QPushButton {{ background: {theme.PANEL_FILL};"
                f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
                f" border-radius: {theme.RADIUS}px; font-weight: 600; }}"
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
        the same stack the sidebar and the player use).

        `padding: 0px` is what makes the glyph appear at all, and it is
        not cosmetic. A widget stylesheet is merged with the application
        one property by property, so a rule that names no padding
        inherits the app-wide QPushButton's `8px 16px` - which on a 34px
        button leaves 34 - 32 = 2px of content width, and Qt renders the
        glyph as a 2px sliver. Measured off a grab of this bar: refresh
        painted 4 non-background pixels, the globe 22, where the +/-
        buttons beside them (also clipped, also at the app's padding)
        painted only their own frame. player.py already carries the same
        note on PlayPauseButton and _season_arrow, from the same cause."""
        button = self._button(glyph, tooltip)
        button.setFixedSize(34, 30)
        button.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" color: {theme.TEXT}; font-family: {theme.FONT_STACK_ICONS};"
            f" padding: 0px;"
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
        # The end of the chapter is the one place the chapter controls
        # are wanted without being asked for: finishing a chapter *is*
        # the request to go to the next one. Checked before the movement
        # threshold below, because the last scroll into the end can be a
        # small one and would otherwise be swallowed.
        self._update_end_of_chapter()

        delta = value - self._last_scroll
        if abs(delta) < SCROLL_HIDE_DELTA:
            return
        self._last_scroll = value
        if delta > 0:
            self._hide_bar()
        else:
            self._reveal_bar()
        # The chapter controls are otherwise hover-only: scrolling up
        # mid-chapter to re-read a panel is not a request to jump
        # chapters, and having them appear over the artwork every time
        # the strip went backwards is what made them worth pinning to
        # the floor in the first place.

    def _update_end_of_chapter(self):
        """Show the chapter controls once the strip reaches its end, and
        take them away again on scrolling back up.

        `_bottom_at_end` records that *this* is why they are showing, so
        scrolling away hides them again without also yanking them out
        from under a pointer that is genuinely hovering there."""
        view = self._strip_view
        bar = view.verticalScrollBar() if view is not None else None
        if bar is None or bar.maximum() <= 0:
            return
        at_end = bar.value() >= bar.maximum() - END_OF_CHAPTER_PX
        if at_end and not self._bottom_shown:
            self._bottom_at_end = True
            self._reveal_bottom()
        elif not at_end and self._bottom_at_end:
            self._bottom_at_end = False
            self._hide_bottom()

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
        # Guarded rather than run on every mouse move through the strip:
        # show()+raise_() on three native-composited children, sixty
        # times a second, is the flicker the owner saw as the bar
        # "glitching" as much as the hide below was.
        if self._bottom_shown:
            return
        self._bottom_shown = True
        for widget in self._bottom_widgets:
            widget.show()
            widget.raise_()

    def _hide_bottom(self):
        if not self._bottom_shown:
            return
        if self._chapter_box.view().isVisible():
            return          # the jump list is open - don't yank it shut
        self._bottom_shown = False
        for widget in self._bottom_widgets:
            widget.hide()

    def _pointer_wants_bottom(self) -> bool:
        """Whether the pointer is on one of the three floating chapter
        controls, or still in the strip of window they live in.

        **This is the whole reason the bar looked broken.** The controls
        are children of this page raised over the strip, so the pointer
        arriving on one *leaves* the strip's viewport - and the Leave
        handler hid them. Reaching for the Next Chapter button therefore
        made it vanish under the cursor every time, which is what "the
        lower bar does not appear" was.

        Asked through QApplication.widgetAt, which hit-tests the widget
        tree rather than comparing coordinates, so no scale factor comes
        into it - mapToGlobal is wrong across two monitors at different
        scales and this has to be right on both (.claude/rules/ui.md)."""
        widgets = self._bottom_widgets or ()
        cursor = QCursor.pos()
        under = QApplication.widgetAt(cursor)
        while under is not None:
            if under in widgets:
                return True
            under = under.parentWidget()
        # ...and the band itself, so sliding off the end of one control
        # sideways - which the viewport never sees, because the pointer
        # was never in it - does not shut the row that is still being
        # used. Measured against the window's own geometry, which is
        # already global, never through mapFromGlobal - that maps through
        # the wrong screen's scale factor on this machine's mixed-DPI
        # monitors (.claude/rules/ui.md).
        frame = self.window().geometry()
        return (frame.contains(cursor)
                and cursor.y() >= frame.bottom() - BOTTOM_REVEAL_PX)

    def _hide_bottom_unless_aimed_at(self):
        # Wrapped because this is also reached from a zero-timer, and
        # the reader can be closed and deleted between the Leave and the
        # timer firing. An exception escaping a Qt slot is qFatal in
        # PyQt6 - an immediate abort, not a traceback (the same trap
        # widgets.release_hover_cursor carries).
        try:
            if self._closed or self._pointer_wants_bottom():
                return
            self._hide_bottom()
        except RuntimeError:
            pass

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
        self._list_view.set_chapters(self.chapters, self._reading_index(),
                                     self._last_read_number())
        self._show_only(self._list_view)
        self._sync_controls()
        self._finish_refresh("Refreshed")
        if self._pending_resume is not None:
            self._reopen_pending()
            return
        # A chapter asked for by number (a details-page row) wins over
        # everything: that click named exactly one chapter.
        if self._open_number is not None:
            wanted, self._open_number = self._open_number, None
            index = self._row_for_number(wanted)
            if index >= 0:
                self.open_chapter(index)
                return
        # Only when the reader was opened *to* resume. Clicking the body
        # of a card asks for the list; jumping straight into a chapter
        # would be the one thing that click is not for.
        if self.resume_on_open:
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

    def _drop_refresh_toast(self):
        """Take the sticky "Refreshing..." off screen without a verdict.

        Leaving the chapter (or the reader) is when the owner stopped
        caring about the answer, and the toast otherwise sat there until
        the 45s listing finished or the sticky ceiling closed it - the
        reported "the refreshing alert was still there for a long time".
        The refresh itself keeps running; only its announcement goes."""
        toast, self._refresh_toast = self._refresh_toast, None
        if toast is None:
            return
        try:
            toast.close()
        except RuntimeError:
            pass        # already closed itself

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
        """Reopen where reading stopped.

        Two ways of knowing where that is, and it used to accept only
        the first. **A saved page position exists only for a chapter
        read inside this reader**; a series whose progress came from the
        tracker - typed in, or marked read from the list - has a
        last-read chapter number and no `reader_position` at all, and
        for those the continue button landed on the chapter list, which
        is exactly what it is not for. Measured on the owner's own file:
        of thirteen entries, six carry a reader position and the rest
        carry only a number.

        So the position answers it when there is one, and the entry's
        own last-read chapter answers it when there is not
        (`_reading_index`, which matches the number exactly). Still
        silently ignored when neither resolves to a chapter that exists -
        a source can renumber or drop chapters between sessions, and
        opening the wrong one is worse than opening the list."""
        saved = self.entry.get("reader_position")
        saved = saved if isinstance(saved, dict) else {}
        chapter_id = saved.get("chapter_id")
        # The freshest of the two records wins. The position is written
        # by scrolling; the chapter number by *opening* a chapter and by
        # the list's mark-read menu - so a reader who moved on to chapter
        # 1190 can still be holding a page position inside 1189. Opening
        # the stale page then reads as "continue did nothing" (measured
        # on the owner's own One Piece entry: position at 1189, number at
        # 1190). When the number is ahead, the number is the truth.
        try:
            last_watched = float(self.entry.get("last_watched_chapter") or 0)
        except (TypeError, ValueError):
            last_watched = 0.0
        saved_index = -1
        if chapter_id is not None:
            for index, chapter in enumerate(self.chapters):
                if chapter.get("id") == chapter_id:
                    saved_index = index
                    break
        try:
            saved_number = float(saved.get("chapter_number") or 0)
        except (TypeError, ValueError):
            saved_number = 0.0
        if not saved_number and saved_index >= 0:
            # An older record without the number field - the chapter it
            # points at knows its own.
            saved_number = float(chapter_number(self.chapters[saved_index]) or 0)
        if saved_index >= 0 and saved_number >= last_watched:
            self.open_chapter(saved_index, start_page=int(saved.get("page") or 0))
            return
        # No stored position, a stale one, or one this source no longer
        # lists. The last-watched *number* answers instead. Looked up
        # directly rather than through _reading_index, which prefers the
        # saved position's chapter id - the very record that just lost
        # the freshness comparison above.
        if last_watched:
            index = self._row_for_number(last_watched)
            if index >= 0:
                self.open_chapter(index)
                return
        index = self._reading_index()
        if index >= 0:
            self.open_chapter(index)

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
        # Every chapter that actually opens is read history - stepping
        # from 12 to 13 inside the reader is as much "I read that" as
        # opening 13 from the list was.
        if 0 <= self.chapter_index < len(self.chapters):
            number = chapter_number(self.chapters[self.chapter_index])
            if number is not None:
                _mark_history(self.entry, [number], True)
                try:
                    history.touch(self.entry, progress=f"Ch {number:g}")
                except Exception:
                    pass
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
        # Off while the list is what is already on screen - a button that
        # goes where you are reads as broken, not as a no-op.
        self._list_btn.setEnabled(bool(self.chapters)
                                  and not self._list_view.isVisible())
        self._refresh_btn.setEnabled(chapter_source is not None
                                     and self._refresh_toast is None)
        self._zoom_label.setText(f"{round(self._zoom * 100)}%")
        self._place_title()

    def _place_title(self):
        """The entry's name, centred on the window and elided before it
        can reach either cluster of buttons.

        The reserve is twice the wider cluster because the label spans
        the whole bar: taking only the wider side off one end would move
        the text's centre off the window's.

        The series name went missing from this bar for **two** reasons,
        and each on its own was enough to reduce every title to the 40px
        floor - which is what the owner saw as "Th..." beside "Chapter
        247". Both are fixed here.

        *The two edges were measured off the wrong buttons.* The door is
        the leftmost control and the globe the rightmost, so
        `_browser_btn.right()` was ~width-12 and `width -
        _leave_btn.left()` ~width-12 as well; the reserve came out at
        about 2 x width. The left cluster actually ends at the chapter
        label and the right one starts at the full-screen button, so
        those are the two clearances.

        *And they were measured before the bar's layout had run.* This is
        reached from _sync_controls, which can fire while every child of
        the bar is still at the default (0, 0) - `_full_btn.left()` is
        then 0, the right clearance is the whole bar width, and the
        reserve is twice it again. So the layout is activated first
        (synchronous, and a no-op once it is current), and if the
        clusters still read as unpositioned after that the label is left
        exactly as it is and re-placed on the next turn of the event
        loop, rather than having an ellipsis written over it against a
        measurement that means nothing."""
        label = getattr(self, "_title_label", None)
        if label is None:
            return
        width = self._bar.width()
        if width <= 0:
            return
        layout = self._bar.layout()
        if layout is not None:
            layout.activate()
        label.setGeometry(0, 0, width, BAR_HEIGHT)
        left = self._chapter_label.geometry().right()
        right_start = self._full_btn.geometry().left()
        if left <= 0 or right_start <= 0:
            # Bounded, because a retry that can never succeed would spin
            # a zero-timer for the life of the page.
            if self._title_retries < 5:
                self._title_retries += 1
                QTimer.singleShot(0, self._place_title)
            return
        self._title_retries = 0
        reserve = 2 * max(left, width - right_start, 0) + 24
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
        # The strip is told the *decode* scale - the label and the
        # controls stay in the user's 100%, the medium's baseline is
        # folded in here, once.
        self._strip_view.set_zoom(zoom_key(self._zoom * self._base_scale))
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
        dialog.setMinimumWidth(560)
        column = QVBoxLayout(dialog)
        column.setContentsMargins(20, 18, 20, 18)
        column.setSpacing(12)
        # Called with the layout already in place so the heading lands at
        # its top; the rows below then append after it.
        frameless_dialog(dialog, title="Download Chapters")

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
        if len(chapters) > 1 and not confirm(
                dialog, "Download Chapters",
                f"Queue {len(chapters)} chapters for download?"):
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

    # ---- back to the list, and marking a chapter ------------------------
    def show_chapter_list(self):
        """Up one level: the chapter list, without leaving the reader.

        The jump list on the floor is not this. That one changes chapter
        without ever showing the list, which is a different thing - it
        answers "take me to 214", not "let me look at what there is". The
        button sits beside the door because the two are the pair of ways
        *out* of a chapter, one of them a level and one of them all the
        way.

        The position is written here rather than left to the debounce
        timer: leaving a chapter is exactly when a reader stops
        scrolling, and losing the last page to a 1.2s timer that had not
        fired yet is only ever noticed a session later. chapter_index is
        deliberately kept, so the list still marks the chapter that was
        open and Next/Previous still answer for it."""
        if not self.chapters:
            return
        self._save_timer.stop()
        self._write_position()
        self._drop_refresh_toast()
        self._list_view.set_chapters(self.chapters, self._reading_index(),
                                     self._last_read_number())
        self._show_only(self._list_view)
        self._sync_controls()

    # ---- back / forward -------------------------------------------------
    # The two extra buttons on the side of the mouse, which every browser
    # and file manager on this machine already treats as "up one level"
    # and "back into where I was". Wired to the same two steps the reader
    # already has - the chapter list is one level above a chapter, and
    # the reader itself is one above the list - rather than to
    # previous/next chapter: a fourth-button click that silently changed
    # chapter would be a very expensive way to be wrong, and Ctrl+arrow
    # and the floor buttons already do that.
    def go_back(self):
        if self._in_chapter():
            self.show_chapter_list()
            return
        self.leave()

    def go_forward(self):
        """Back into the chapter the list was opened from.

        Only when its pages are still loaded - chapter_index survives
        going up to the list (see show_chapter_list), so this costs
        nothing and needs no refetch. With no chapter behind it there is
        nothing forward of here, and it does nothing rather than opening
        something arbitrary."""
        if self._in_chapter() or not self._store.count():
            return
        if not (0 <= self.chapter_index < len(self.chapters)):
            return
        self._show_only(self._strip_view)
        self._sync_controls()

    def _handle_side_button(self, button) -> bool:
        """True when `button` was one of the mouse's side buttons and has
        been acted on. Shared by the page's own press handler and the
        filter over the strip's viewport - a press inside the viewport
        never reaches the page."""
        if button == Qt.MouseButton.BackButton:
            self.go_back()
            return True
        if button == Qt.MouseButton.ForwardButton:
            self.go_forward()
            return True
        return False

    def mousePressEvent(self, event):
        # Reached by anything that ignored the press on its way up -
        # the chapter list's rows and its scroll area all do, since
        # Card only ever handles the left and right buttons.
        if self._handle_side_button(event.button()):
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        # The backstop for the window's absolute right edge: a wheel that
        # lands on the page itself (past the scrollbar's own pixels, or
        # on a floating control that ignored it) still scrolls the strip
        # rather than dying. The strip's own handler also owns Ctrl+wheel
        # zoom, so forwarding gets both behaviours for free.
        if self._in_chapter():
            self._strip_view.wheelEvent(event)
            event.accept()
            return
        super().wheelEvent(event)

    def _row_for_number(self, number):
        for index, chapter in enumerate(self.chapters):
            if chapter_number(chapter) == number:
                return index
        return -1

    def _mark_chapter(self, index, finished):
        """Mark chapter `index` read, or not read.

        `correct_progress` and not `record_progress`: this is the one
        writer allowed to move the stored number *down*, which is exactly
        what "unfinished" is, and record_progress would refuse it in
        silence (tracker._write_progress is forward-only by design).

        "Finished" is the chapter's own number. "Unfinished" is the
        highest number *this source actually lists* below it, not
        `number - 1`: scanlators split chapters, so the one before 25 can
        be 24.5 - and 0 when there is nothing earlier, which reads as
        nothing read."""
        if not (0 <= index < len(self.chapters)):
            return
        number = chapter_number(self.chapters[index])
        if number is None:
            show_toast(self, "This Chapter Has No Number to Record")
            return
        if finished:
            target = number
        else:
            earlier = [n for n in (chapter_number(c) for c in self.chapters)
                       if n is not None and n < number]
            target = max(earlier) if earlier else 0.0
        # The History tick, explicitly and in both directions: the write
        # that rides along inside correct_progress only ever *adds* one,
        # for the chapter it is given - so unmarking 25 (which stores
        # 24.5) would leave 25's own tick standing and the details page
        # would go on calling it read.
        _mark_history(self.entry, [number], finished)
        # An unsaved title has no entry to write a number onto, and
        # correct_progress says so by returning False - but the mark was
        # still recorded, in History. Only a *saved* title failing here
        # is a real failure worth a toast.
        if not correct_progress(self.entry, chapter=target) and self.entry.get("id"):
            show_toast(self, "Could Not Save That Chapter")
            return
        # Rebuilt rather than left alone: the list marks where reading has
        # reached, and the row it was marking is not the answer any more.
        # While a chapter is open that mark belongs to the open one; on
        # the list it belongs to whatever was just marked.
        marked = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters)
                  else self._row_for_number(target))
        self._list_view.set_chapters(self.chapters, marked,
                                     self._last_read_number())
        self._sync_controls()
        show_toast(self, f"Read Up to Chapter {format_chapter_progress(target)}"
                   if target else "Marked as Not Started")

    def _last_read_number(self) -> float:
        try:
            return float(self.entry.get("last_watched_chapter") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _mark_all_chapters(self, finished):
        """Every chapter read, or none of them - the menu's "mark all".

        "All read" is the highest numbered chapter this source lists,
        which is exactly what "I have read all of this" means to the
        tracker's read-up-to model; "all unread" is zero. Both through
        correct_progress, the one writer allowed to move the number in
        either direction."""
        numbers = [n for n in (chapter_number(c) for c in self.chapters)
                   if n is not None]
        if not numbers:
            show_toast(self, "These Chapters Carry No Numbers to Record")
            return
        target = max(numbers) if finished else 0.0
        # Every chapter's tick, not just the highest - see _mark_chapter.
        _mark_history(self.entry, numbers, finished)
        # See _mark_chapter: an unsaved title records into History only,
        # and that is not a failure.
        if not correct_progress(self.entry, chapter=target) and self.entry.get("id"):
            show_toast(self, "Could Not Save That")
            return
        marked = (self.chapter_index
                  if 0 <= self.chapter_index < len(self.chapters) else -1)
        self._list_view.set_chapters(self.chapters, marked,
                                     self._last_read_number())
        self._sync_controls()
        show_toast(self, f"Marked All Read Up to Chapter "
                         f"{format_chapter_progress(target)}"
                   if finished else "Marked All as Unread")

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
            if event.type() == QEvent.Type.MouseButtonPress:
                # Swallowed here rather than left to propagate: a press
                # inside a QAbstractScrollArea's viewport does not reach
                # this page, so mousePressEvent above never sees the one
                # gesture the reading surface most needs it for.
                if self._handle_side_button(event.button()):
                    return True
            elif event.type() == QEvent.Type.MouseMove:
                # The viewport's own top is the page's top while a
                # chapter is open (no layout margin there), so this
                # needs no coordinate mapping - and mapToGlobal is
                # wrong on mixed-DPI displays anyway.
                y = event.position().y()
                if y <= BAR_REVEAL_PX:
                    self._reveal_bar()
                # Hover is the *only* way the chapter controls come back
                # (the owner's ask: scrolling must never summon them),
                # so this is also the only place that shows them.
                if y >= self._strip_view.viewport().height() - BOTTOM_REVEAL_PX:
                    self._reveal_bottom()
                else:
                    self._hide_bottom()
            elif event.type() == QEvent.Type.Leave:
                # Pointer off the reading surface - which is also what
                # the pointer arriving *on* the controls looks like from
                # here, so where it actually went has to be asked. See
                # _pointer_wants_bottom: hiding unconditionally is what
                # made the bar impossible to click.
                self._hide_bottom_unless_aimed_at()
        elif (obj in (self._bottom_widgets or ())
                and event.type() == QEvent.Type.Leave):
            QTimer.singleShot(0, self._hide_bottom_unless_aimed_at)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        super().showEvent(event)
        # The bar's buttons only take real positions once the page is up,
        # and _place_title measures the gap between two of them - placing
        # it again here is what stops the title being elided against a
        # bar whose layout had not run when _sync_controls first asked.
        self._place_title()

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
        self._drop_refresh_toast()
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


# When the music URL was last opened, and how long before another reader
# open is allowed to open it again (see _open_music_quietly's throttle).
_MUSIC_OPENED_AT = float("-inf")
_MUSIC_REOPEN_S = 10 * 60


def _open_music_quietly(window):
    """Open the configured reading-music URL (Settings) behind the app.

    The setting promises music *alongside* reading, but a plain
    webbrowser.open fronts the browser, which buried the reader the
    moment it opened (the owner's report) - the music is to be heard,
    not looked at. ShellExecuteW with SW_SHOWMINNOACTIVE (7) asks
    Windows for a minimized, un-activated window; browsers routinely
    ignore that show hint when the URL lands in an already-running
    instance, so the hint alone is not the fix. The part that must
    actually work is the re-foreground: shortly after, Atomic
    re-activates itself and pulls the reader back over whatever the
    browser did. Twice, because a cold browser can take more than half
    a second to steal focus - a re-front that fires before the theft
    fixes nothing. Never raises; no music URL means nothing happens.

    Throttled: the details page opens a reader per chapter row clicked,
    and the browser route this mirrors was only ever hit once per
    sitting - un-throttled, working through a series would stack a new
    music tab (and two focus fights) on every single chapter."""
    global _MUSIC_OPENED_AT
    try:
        url = app_settings.get_manga_music_url()
    except Exception:
        url = ""
    if not url:
        return
    import time
    if time.monotonic() - _MUSIC_OPENED_AT < _MUSIC_REOPEN_S:
        return
    _MUSIC_OPENED_AT = time.monotonic()
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(None, "open", url, None,
                                                None, 7)  # SW_SHOWMINNOACTIVE
        else:
            webbrowser.open(url)
    except Exception:
        logs.exception("could not open the reading-music URL")
        return

    def refront():
        try:
            if window.isMinimized():
                window.showNormal()
            window.raise_()
            window.activateWindow()
            if os.name == "nt":
                import ctypes
                # activateWindow is a request Windows is free to ignore
                # for a process that no longer holds the foreground;
                # asking user32 directly is the stronger form, and its
                # worst case is a taskbar flash rather than a buried app.
                ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
        except Exception:
            pass        # a closed window mid-timer is not worth a log

    QTimer.singleShot(500, refront)
    QTimer.singleShot(1500, refront)


def open_reader(window, entry, data_file="tracker.json", resume=True,
                chapter_number=None):
    """Open the reader over `window`, covering the sidebar as well.

    `resume=False` lands on the chapter list instead of jumping back to
    where reading stopped. That is the difference between the two targets
    a reading card now has: the round continue button on its cover
    resumes, the rest of the card browses.

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
    # Opening is what Read History records - the owner opened chapters
    # and found the section empty, because the only writer was the
    # mark-as-read path. The chapter tick rides along when one was asked
    # for by number; resuming records the title and lets the reader's
    # own marking tick the chapter it lands on.
    try:
        shown = (f"Ch {float(chapter_number):g}"
                 if chapter_number is not None else None)
        history.touch(entry, progress=shown)
        if chapter_number is not None:
            history.set_watched(entry, history.chapter_key(chapter_number), True)
    except Exception:
        logs.exception("could not record the reading history")
    # The music site opens with the reader, exactly as the browser route
    # opens it beside the reading tab (tracker._open_manga_entry) - but
    # quietly, behind the app, since the reading surface is *this* page.
    _open_music_quietly(window)
    page = ReaderPage(entry, data_file, host,
                      origin_page=_origin_page_name(window), resume=resume,
                      chapter_number=chapter_number)
    page.follow(host)
    page.show()
    page.raise_()
    page.setFocus()
    return page
