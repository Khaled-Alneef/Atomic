"""The in-app video player: one full-window page over the whole app.

What is here and what is deliberately elsewhere:

  * `helpers/video_backend.py` owns libmpv - loading it, the option set,
    and tearing an instance down. This module never imports `mpv`.
  * `helpers/streams.py` finds something playable for an entry, and
    `helpers/subtitles.py` finds and decodes subtitle files. Both are
    imported softly (see below): a build without them still opens this
    page and still plays a url it was handed.

Three things about embedding mpv that are not obvious and each cost a
session's worth of debugging elsewhere:

  * **The video widget must be a real native window.** mpv is handed an
    HWND and renders into it directly. `WA_NativeWindow` has to be set
    (with `WA_DontCreateNativeAncestors`, or Qt promotes every ancestor
    up to the window and the page's own painting changes underneath us)
    and `winId()` read *after* that, or mpv gets a handle Qt later
    replaces and the video opens in a detached window of its own.
  * **A native child window is composited by Windows on top of
    everything Qt paints into the top-level.** Ordinary sibling widgets
    raised over the video are simply not visible - the overlay controls
    have to be native windows too, and their stacking then follows
    `raise_()`.
  * **Qt cannot alpha-blend those overlays, but Windows can.** This is
    what the bars being an opaque slab used to be about, and it was only
    half true: Qt's own translucency (WA_TranslucentBackground, an alpha
    in the QSS colour) is composited inside the top-level's surface and
    so blends against Qt's backdrop, not against mpv's separately
    composited swapchain. What *does* work is DWM's own per-window
    blend - `WS_EX_LAYERED` plus `SetLayeredWindowAttributes`, supported
    on child windows since Windows 8. DWM composites the layered child
    over whatever is already in the frame at that point, video included.
    See `_set_window_alpha`. Measured rather than assumed: with a frame
    on screen, sampling one screen pixel inside the controls bar with the
    layer on and again with it off gave two different colours, which is
    only possible if something behind the bar is reaching the pixel.
  * **mpv's callbacks arrive on mpv's own thread.** Every one of them
    does nothing but emit a signal on `_MpvBridge`; touching a widget
    from there is a hard crash, exactly as it is from the tracker's
    lookup threads.
  * **mpv also eats every mouse event over the video**, not just the
    moves the pointer poll already works around: its child window is not
    Qt's, so no press ever reaches a Qt widget. Click-to-pause is
    therefore polled from the same place and in the same way - see
    `_poll_mouse`.
"""

import ctypes
import os
import shutil
import tempfile
import threading
import uuid

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

from helpers import logs, net, storage, theme, video_backend
from helpers.widgets import (Card, GlassPage, scroll_area, show_toast,
                             use_hover_cursor)

# Soft imports. These two modules are written alongside this page and a
# tree without them must still import `windows.player` - main.py imports
# every page at startup, so an ImportError here would be a black app
# rather than a missing feature.
try:
    from helpers import streams as streams_module
except ImportError:                                     # pragma: no cover
    streams_module = None
try:
    from helpers import subtitles as subtitles_module
except ImportError:                                     # pragma: no cover
    subtitles_module = None


# Where a resume point lives. Its own file, not tracker.json: this is
# written every few seconds while something plays, and tracker.json is
# read-modify-written by four lookup workers at the same time.
RESUME_FILE = "player_state.json"

CONTROLS_HIDE_MS = 3200
# The pointer is polled rather than watched for MouseMove events: mpv
# creates its own child window inside ours and eats the moves that
# happen over the video, so the only reliable "the user is still there"
# signal is the global cursor position changing. Same approach as
# main.py's cursor watchdog, and no coordinate mapping is involved.
POINTER_POLL_MS = 180
# The mouse *button* is polled far faster than the pointer, for the same
# reason and by the same route: a click that goes down and up between two
# 180ms ticks would simply not exist. A real click measures 50-100ms, so
# 40ms sees the button held on at least one tick; the "pressed since the
# last call" bit of GetAsyncKeyState catches the ones shorter than that.
MOUSE_POLL_MS = 40
# A press and release this far apart is a drag (or a slipped hand), not a
# click, and must not toggle playback.
CLICK_MOVE_TOLERANCE_PX = 6
POSITION_SAVE_MS = 5000

SEEK_STEP_S = 5
VOLUME_STEP = 5
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)

# Below this there is nothing worth resuming from, and above it the
# episode is effectively finished - offering "resume at 99%" is worse
# than starting over.
RESUME_MIN_S = 30
RESUME_MAX_FRACTION = 0.97

# When an episode counts as watched. Credits and a next-episode teaser
# are routinely the last ~10%, so waiting for the file to actually end
# would mean most finished episodes never got marked.
WATCHED_FRACTION = 0.85

SUB_SIZE_MIN, SUB_SIZE_MAX, SUB_SIZE_STEP = 28, 90, 4
SUB_SIZE_DEFAULT = 55          # mpv's own default for sub-font-size
SUB_DELAY_STEP = 0.1

# Where mpv puts the subtitle line, as a percentage of frame height
# (100 = hard against the bottom, which is mpv's default). The controls
# bar is translucent now, not opaque, but a subtitle *behind* it is still
# unreadable - two layers of text over each other is worse than one over
# video - so the line still steps up out of its way while it is showing.
# Recomputed for the taller bar: 118px of an 800px-tall window is 14.75%,
# so 85 clears it with a little air, where the old 90 (sized for a 92px
# bar) would now clip.
SUB_POS_CLEAR = 100
SUB_POS_ABOVE_CONTROLS = 85

# 2x the per-request timeout the site modules use, for the same reason
# they bound a chain rather than each step: several sources tried in
# sequence is not "one timeout".
STREAM_BUDGET_S = 24
SUBTITLE_BUDGET_S = 24

# All four grew with the type (everything on this page is 2-3pt larger
# than it was). These are the measured heights the taller rows need, not
# a guess: at the old 92px the 42px play button plus a 15pt time label
# clipped its own descenders.
CONTROLS_HEIGHT = 118
TOPBAR_HEIGHT = 64
PANEL_WIDTH = 460
PANEL_MAX_HEIGHT = 520

# The episode list down the left. Wide enough for "Episode 12" plus the
# season heading at 11pt without eliding.
EPISODE_BAR_WIDTH = 250

# How see-through the bars are, 0-255. 205 is roughly 80%: enough that a
# bright frame is plainly visible through the bar, opaque enough that
# white text on it stays readable over a white frame - the failure this
# is balanced against, and the reason it is not the 60% a screenshot
# flatters.
BAR_ALPHA = 205

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002
_VK_LBUTTON = 0x01

# Segoe Fluent Icons codepoints, same family the sidebar uses (see
# theme.FONT_STACK_ICONS) - monochrome, so they take the button's colour
# instead of an emoji's own.
ICON_PLAY = ""
ICON_PAUSE = ""
ICON_PREV = ""
ICON_NEXT = ""
ICON_VOLUME = ""
ICON_MUTED = ""
ICON_FULLSCREEN = ""
ICON_EXIT_FULLSCREEN = ""
ICON_SUBTITLES = ""
ICON_TRACKS = ""
ICON_QUALITY = ""
ICON_BACK = ""
# Written as an escape rather than pasted like the ones above: it was
# added later, and a literal private-use character is invisible in a
# diff - which is how a wrong glyph gets reviewed as correct. E700 is
# GlobalNavButton, the same "list" affordance the app's own sidebar has.
ICON_EPISODES = ""

# The arrows on a stepper row. U+2039/U+203A, deliberately not the
# Fluent chevrons: these rows are drawn in the text font, and a Fluent
# glyph beside a text value sits on a visibly different baseline.
GLYPH_LEFT = "‹"
GLYPH_RIGHT = "›"


# ---------------------------------------------------------------------
# Small helpers


def _canonical_quality(quality) -> str:
    """"4k" and "2160p" name one resolution and must not read as two."""
    quality = str(quality or "").lower()
    return "2160p" if quality == "4k" else quality


def _format_time(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _resume_key(entry_id, season, episode) -> str:
    """One id per *episode*, not per entry - a series resumed at E4 must
    not hand its position to E5."""
    if episode:
        return f"{entry_id}|s{int(season or 0)}e{int(episode)}"
    return f"{entry_id}|film"


def load_resume(entry_id, season, episode):
    """The saved position for this episode, or None."""
    key = _resume_key(entry_id, season, episode)
    for record in storage.load(RESUME_FILE, []):
        if record.get("id") == key:
            return record
    return None


def save_resume(entry_id, season, episode, position, duration):
    """Remember where this episode got to.

    update_entry first, because it re-reads the file and touches exactly
    one record - writing a whole list back from a page is what erased a
    batch of imported games once (see storage.update_entry). The append
    below is the one case it cannot serve, there being nothing to update
    yet; it is safe here specifically because this file has a single
    writer - one player page, on the UI thread - unlike tracker.json."""
    key = _resume_key(entry_id, season, episode)
    fields = {"entry_id": entry_id, "season": season, "episode": episode,
              "position": float(position or 0.0),
              "duration": float(duration or 0.0),
              "updated_at": storage.now_iso()}
    if storage.update_entry(RESUME_FILE, key, fields):
        return
    records = storage.load(RESUME_FILE, [])
    records.append({"id": key, **fields})
    storage.save(RESUME_FILE, records)


def clear_resume(entry_id, season, episode):
    """Drop the resume point once the episode is finished, so the next
    visit starts clean instead of offering the credits."""
    key = _resume_key(entry_id, season, episode)
    records = storage.load(RESUME_FILE, [])
    kept = [r for r in records if r.get("id") != key]
    if len(kept) != len(records):
        storage.save(RESUME_FILE, kept)


def _mark_watched(entry, season, episode):
    """Write this episode onto the tracker entry.

    tracker is imported here rather than at module scope on purpose: the
    tracker page is what will open this player, so a top-level import
    each way is a cycle. This direction is the lazy one because it runs
    once per episode, where the tracker's call runs on a click.

    Everything that decides whether the number may move now lives in
    `tracker.record_progress` - forward-only ordering, which file the
    entry belongs to, and the types that carry no episode at all. This
    used to gate on `last_watched_is_editable` as well, and that gate is
    exactly why nothing was ever recorded: it is False for a
    Stremio-backed entry, and with the Video Website choice gone every
    new video entry has no site at all, so the old rule refused
    essentially every anime. Stremio no longer overwrites a higher
    number either, so there is nothing left to protect against."""
    try:
        from windows import tracker
    except ImportError:                                 # pragma: no cover
        return False
    return tracker.record_progress(entry, season=season, episode=episode)


def _make_native(widget):
    """Turn `widget` into a real native window so Windows composites it
    above the video's own native window.

    WA_DontCreateNativeAncestors matters as much as WA_NativeWindow:
    without it Qt promotes the page and everything above it too, and the
    page's radial-gradient backdrop stops being painted where the
    overlay sits."""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
    return widget


def _set_window_alpha(widget, alpha) -> bool:
    """Let DWM blend this native child window over whatever is under it.

    This is the one route to translucency that works here (see the module
    docstring). Qt's own translucency does not: the overlay is a native
    child window, mpv's video is a separately composited swapchain, and
    Qt never has both in one surface to blend. `WS_EX_LAYERED` moves the
    blend to DWM, which composites the children in z-order and so has the
    video already in the frame when it reaches the bar.

    Returns whether it took. Every caller treats False as "stay opaque"
    rather than as an error: a bar that cannot be seen through is a
    cosmetic loss, and a player that refuses to open over it is not."""
    if os.name != "nt":
        return False
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if not style & _WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_LAYERED)
        # The BOOL from this call, not the one above: SetWindowLongW
        # returns the *previous* style, and 0 is a legitimate previous
        # value, so it cannot be tested for success without clearing
        # GetLastError first.
        return bool(user32.SetLayeredWindowAttributes(
            hwnd, 0, int(max(0, min(255, alpha))), _LWA_ALPHA))
    except Exception:
        logs.exception("Could not make the player bar translucent")
        return False


def _icon_button(glyph, tooltip, size=44, font_pt=16):
    button = QPushButton(glyph)
    button.setObjectName("Flat")
    button.setToolTip(tooltip)
    button.setFixedSize(size, size)
    button.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none;"
        f" color: {theme.TEXT}; font-family: {theme.FONT_STACK_ICONS};"
        f" font-size: {font_pt}pt; border-radius: {theme.RADIUS_SM}px; }}"
        f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
        f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
    use_hover_cursor(button)
    return button


def _text_button(text, tooltip=""):
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setStyleSheet(
        f"QPushButton {{ background: transparent; border: 1px solid {theme.BORDER};"
        f" color: {theme.TEXT}; padding: 7px 13px; font-size: 11pt;"
        f" border-radius: {theme.RADIUS_SM}px; }}"
        f"QPushButton:hover {{ background: {theme.SURFACE_HOVER};"
        f" border: 1px solid {theme.ACCENT}; }}"
        f"QPushButton:disabled {{ color: {theme.TEXT_DIM}; }}")
    use_hover_cursor(button)
    return button


# ---------------------------------------------------------------------
# Thread boundaries


class _MpvBridge(QObject):
    """mpv's thread -> Qt. Nothing else crosses."""
    prop = Signal(str, object)      # property name, new value
    ended = Signal(str)             # end-file reason


class _WorkBridge(QObject):
    """Background lookups -> Qt. Every signal carries the run number it
    was fired for, so an answer from a superseded episode cannot be
    counted toward the current one - the same rule the tracker's lookups
    follow."""
    streams_ready = Signal(object, int)     # list | None
    subs_ready = Signal(object, int)        # list
    sub_file_ready = Signal(str, str, int)  # path, label, run
    # A torrent stream only becomes playable once the streaming server
    # has been handed its trackers, which takes a round trip (and may
    # start the server first). That cannot happen on the UI thread.
    stream_prepared = Signal(object, int, object, int)  # stream, index, resume, run
    failed = Signal(str, int)


# ---------------------------------------------------------------------
# Pieces of the page


class VideoSurface(QWidget):
    """The native window mpv renders into. Paints one flat fill so that
    the moment before the first frame is the app's own background rather
    than whatever was on screen underneath."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _make_native(self)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(theme.BG))
        self.setPalette(palette)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def native_handle(self) -> int:
        """The HWND to hand mpv. Read after the attributes above, never
        before - see this module's docstring."""
        return int(self.winId())


class SeekBar(QWidget):
    """Position, buffered-ahead and scrubbing in one strip.

    Buffered is drawn as a separate band rather than left implicit: on a
    stalling HLS source the difference between "the stream is dead" and
    "it is still filling" is the only thing that tells you whether
    switching source is worth doing, and that is precisely when someone
    is staring at this bar."""

    seeked = Signal(float)          # absolute seconds

    BAR_HEIGHT = 5
    KNOB_RADIUS = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.KNOB_RADIUS * 2 + 6)
        self.setMouseTracking(True)
        use_hover_cursor(self)
        self._duration = 0.0
        self._position = 0.0
        self._buffered = 0.0
        self._dragging = False

    def set_duration(self, value):
        self._duration = float(value or 0.0)
        self.update()

    def set_position(self, value):
        if self._dragging:
            return          # the thumb belongs to the pointer mid-drag
        self._position = float(value or 0.0)
        self.update()

    def set_buffered(self, value):
        self._buffered = float(value or 0.0)
        self.update()

    def _fraction_at(self, x):
        width = max(1, self.width())
        return min(1.0, max(0.0, x / width))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = (self.height() - self.BAR_HEIGHT) / 2
        rect = QRectF(0, top, self.width(), self.BAR_HEIGHT)
        radius = self.BAR_HEIGHT / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_HOVER))
        painter.drawRoundedRect(rect, radius, radius)

        if self._duration > 0:
            buffered = min(1.0, max(0.0, self._buffered / self._duration))
            played = min(1.0, max(0.0, self._position / self._duration))
            if buffered > played:
                painter.setBrush(QColor(theme.BORDER))
                painter.drawRoundedRect(
                    QRectF(0, top, self.width() * buffered, self.BAR_HEIGHT),
                    radius, radius)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(
                QRectF(0, top, self.width() * played, self.BAR_HEIGHT),
                radius, radius)
            centre = QPoint(int(self.width() * played), int(self.height() / 2))
            painter.setBrush(QColor(theme.TEXT))
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.drawEllipse(centre, self.KNOB_RADIUS, self.KNOB_RADIUS)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            return
        self._dragging = True
        self._position = self._fraction_at(event.position().x()) * self._duration
        self.update()

    def mouseMoveEvent(self, event):
        if self._dragging and self._duration > 0:
            self._position = self._fraction_at(event.position().x()) * self._duration
            self.update()
        if self._duration > 0:
            self.setToolTip(_format_time(
                self._fraction_at(event.position().x()) * self._duration))

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.seeked.emit(self._position)


class OverlayPanel(QFrame):
    """The popup the subtitle/track/quality buttons open.

    Native for the same reason the controls are (see module docstring),
    and therefore opaque: it sits over the video, and a native child
    window cannot be blended against another native child window."""

    closed = Signal()

    def __init__(self, parent, title):
        super().__init__(parent)
        _make_native(self)
        self.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px; }}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(6)
        label = QLabel(title)
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
            f" background: transparent; border: none;")
        header.addWidget(label)
        header.addStretch(1)
        close_btn = _icon_button("", "Close", size=30, font_pt=11)
        close_btn.clicked.connect(self.closed.emit)
        header.addWidget(close_btn)
        outer.addLayout(header)

        self.body = QWidget()
        self.body.setStyleSheet("background: transparent; border: none;")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        area = scroll_area(self.body)
        area.setStyleSheet("background: transparent; border: none;")
        area.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(area, stretch=1)

        # Vertical: the footer holds full-width stepper rows now, and two
        # of them side by side left the value column 40px wide.
        self.footer_layout = QVBoxLayout()
        self.footer_layout.setSpacing(6)
        outer.addLayout(self.footer_layout)

    def add_group(self, name):
        label = QLabel(name)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 10pt; font-weight: 700;"
            f" background: transparent; border: none; padding-top: 4px;")
        self.body_layout.addWidget(label)

    def add_row(self, title, subtitle, on_click, selected=False):
        card = Card(matte=True)
        card.setStyleSheet(
            f"QFrame#Card {{ background: {theme.SURFACE_HOVER};"
            f" border: 1px solid {theme.ACCENT if selected else theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(2)
        head = QLabel(title)
        head.setWordWrap(True)
        head.setStyleSheet(
            f"color: {theme.ACCENT if selected else theme.TEXT}; font-size: 11pt;"
            f" font-weight: 600; background: transparent; border: none;")
        layout.addWidget(head)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
                f" background: transparent; border: none;")
            layout.addWidget(sub)
        card.clicked.connect(on_click)
        self.body_layout.addWidget(card)
        return card

    def add_message(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt;"
            f" background: transparent; border: none;")
        self.body_layout.addWidget(label)
        return label

    def add_stepper(self, name, value_text, on_left, on_right):
        """A `‹ value ›` row: name on the left, then back-arrow, the
        current value centred between them, forward-arrow.

        A stepper rather than the pair of labelled buttons this used to
        be ("-0.1s" / "+0.1s" / "A-" / "A+"), because those never showed
        the size at all - the only way to find out what the subtitles
        were set to was to change it and look. The value is the point of
        the row; the arrows are what moves it.

        Returns a setter for the value text, so the caller nudges the
        number and hands the new one back without rebuilding the panel
        (which would scroll the subtitle list back to the top under the
        finger that is repeatedly pressing one arrow)."""
        row = QWidget()
        row.setStyleSheet("background: transparent; border: none;")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title = QLabel(name)
        title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt;"
            f" background: transparent; border: none;")
        layout.addWidget(title)
        layout.addStretch(1)

        left = _text_button(GLYPH_LEFT, f"Less {name.lower()}")
        left.setFixedWidth(38)
        left.clicked.connect(on_left)
        layout.addWidget(left)

        value = QLabel(value_text)
        value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Fixed, not sized to the text: without it the whole row shuffles
        # sideways every time the value gains or loses a character, and
        # the arrow the user is holding moves out from under the pointer.
        value.setFixedWidth(84)
        value.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11pt; font-weight: 600;"
            f" background: transparent; border: none;")
        layout.addWidget(value)

        right = _text_button(GLYPH_RIGHT, f"More {name.lower()}")
        right.setFixedWidth(38)
        right.clicked.connect(on_right)
        layout.addWidget(right)

        self.footer_layout.addWidget(row)
        return value.setText

    def finish(self):
        self.body_layout.addStretch(1)


# ---------------------------------------------------------------------


class PlayerPage(GlassPage):
    """The player, as a page covering the whole window.

    A page rather than a second top-level window: full screen, the dark
    title bar, the window's own geometry memory and Escape handling all
    already exist on the main window, and a separate window would need
    its own copy of each. It covers the sidebar as well as the content
    area - a 240px navigation column beside a video is not what anyone
    watching wants, and it makes the full-screen toggle a no-op on
    layout rather than a reparent (which would destroy and recreate the
    native window mpv is rendering into)."""

    def __init__(self, parent, entry, season=None, episode=None,
                 streams=None, on_close=None):
        super().__init__(parent=parent)
        self.entry = entry
        self.on_close = on_close
        self._window = parent.window() if parent is not None else None
        self._closing = False
        self._run = 0

        self.season, self.episode = self._starting_episode(season, episode)
        self._given_streams = list(streams) if streams else None

        self.handle = None
        self._streams = []
        self._stream_index = 0
        # Set here as well as in _load_into_mpv: mpv's property callbacks
        # start arriving the moment the instance exists, which is before
        # any file is loaded, and reading these from there would be an
        # AttributeError on mpv's own thread - where it kills the event
        # thread silently and the UI then waits forever.
        self._awaiting_first_frame = False
        self._buffering_percent = 0
        # Sources already proven dead this session, so _try_next_source
        # cannot loop back onto one it has just rejected.
        self._dead_sources = set()
        self._subtitles = []
        self._subtitle_label = "Off"
        self._sub_delay = 0.0
        self._sub_size = SUB_SIZE_DEFAULT
        self._tracks = []
        self._duration = 0.0
        self._position = 0.0
        self._paused = False
        self._volume = 100
        self._muted = False
        self._speed = 1.0
        self._marked_watched = False
        self._panel = None
        self._temp_dir = None
        self._pending_resume = None
        self._episode_bar = None
        self._episode_rows = {}
        # Click-to-pause state, filled in by _poll_mouse. The press
        # position is kept so a drag can be told from a click.
        self._mouse_down = False
        self._press_pos = None
        self._press_on_video = False

        self._bridge = _MpvBridge()
        self._bridge.prop.connect(self._on_property)
        self._bridge.ended.connect(self._on_ended)
        self._work = _WorkBridge()
        self._work.streams_ready.connect(self._on_streams)
        self._work.subs_ready.connect(self._on_subtitles)
        self._work.sub_file_ready.connect(self._on_subtitle_file)
        self._work.stream_prepared.connect(self._on_stream_prepared)
        self._work.failed.connect(self._on_failed)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self.surface = VideoSurface(self)
        self._build_top_bar()
        self._build_controls()
        self._build_episode_bar()
        self.status = QLabel("", self)
        _make_native(self.status)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS}px;"
            f" padding: 20px 26px; font-size: 13pt;")
        self.status.hide()

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._hide_controls)
        self._pointer_timer = QTimer(self)
        self._pointer_timer.timeout.connect(self._poll_pointer)
        self._last_pointer = QCursor.pos()
        self._mouse_timer = QTimer(self)
        self._mouse_timer.timeout.connect(self._poll_mouse)
        self._save_timer = QTimer(self)
        self._save_timer.timeout.connect(self._save_position)

        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())

        QTimer.singleShot(0, self._start)

    # ---- setup -------------------------------------------------------
    def _starting_episode(self, season, episode):
        """What to play when the caller did not say.

        Next episode rather than the stored one, but only when the
        stored number is confirmed: an unverified number is the tracker's
        *guess* at the latest episode out (see tracker._progress_display),
        and starting one past a guess would skip an episode nobody has
        watched."""
        if episode is not None:
            return season, episode
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return season, episode
        if not tracker.tracks_progress(self.entry.get("type")):
            return None, None
        stored_season, stored_episode = tracker.parse_episode_progress(
            self.entry.get("progress"))
        if self.entry.get("progress_verified") and stored_episode:
            return stored_season or 1, stored_episode + 1
        return stored_season or 1, 1

    def _build_top_bar(self):
        self.top_bar = QWidget(self)
        _make_native(self.top_bar)
        self.top_bar.setStyleSheet(f"background: {theme.SURFACE};")
        layout = QHBoxLayout(self.top_bar)
        layout.setContentsMargins(10, 6, 14, 6)
        layout.setSpacing(10)

        back = _icon_button(ICON_BACK, "Back (Esc)")
        back.clicked.connect(self.close_player)
        layout.addWidget(back)

        self.episodes_btn = _icon_button(ICON_EPISODES, "Episodes")
        self.episodes_btn.clicked.connect(self.toggle_episode_bar)
        self.episodes_btn.setVisible(bool(self.episode))
        layout.addWidget(self.episodes_btn)

        self.title_label = QLabel(self._title_text())
        self.title_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 14pt; font-weight: 700;"
            f" background: transparent;")
        layout.addWidget(self.title_label)

        self.source_label = QLabel("")
        self.source_label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 11pt; background: transparent;")
        layout.addWidget(self.source_label)
        layout.addStretch(1)
        self.top_bar.installEventFilter(self)

    def _title_text(self):
        title = self.entry.get("title") or "Playing"
        if self.episode:
            try:
                from windows import tracker
                return f"{title}  -  {tracker.format_episode_progress(self.season or 0, self.episode)}"
            except ImportError:                         # pragma: no cover
                pass
        return title

    def _build_controls(self):
        self.controls = QWidget(self)
        _make_native(self.controls)
        self.controls.setStyleSheet(f"background: {theme.SURFACE};")
        outer = QVBoxLayout(self.controls)
        outer.setContentsMargins(18, 10, 18, 12)
        outer.setSpacing(6)

        self.seek_bar = SeekBar()
        self.seek_bar.seeked.connect(self._seek_absolute)
        outer.addWidget(self.seek_bar)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.prev_btn = _icon_button(ICON_PREV, "Previous episode")
        self.prev_btn.clicked.connect(lambda: self._change_episode(-1))
        row.addWidget(self.prev_btn)

        self.play_btn = _icon_button(ICON_PAUSE, "Play / Pause (Space)", size=50, font_pt=19)
        self.play_btn.clicked.connect(self.toggle_pause)
        row.addWidget(self.play_btn)

        self.next_btn = _icon_button(ICON_NEXT, "Next episode")
        self.next_btn.clicked.connect(lambda: self._change_episode(1))
        row.addWidget(self.next_btn)

        has_episodes = bool(self.episode)
        self.prev_btn.setEnabled(has_episodes and self.episode > 1)
        self.next_btn.setEnabled(has_episodes)

        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11.5pt; background: transparent;")
        row.addWidget(self.time_label)
        row.addStretch(1)

        self.mute_btn = _icon_button(ICON_VOLUME, "Mute (M)")
        self.mute_btn.clicked.connect(self.toggle_mute)
        row.addWidget(self.mute_btn)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(130)
        self.volume_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 5px; background: {theme.SURFACE_HOVER};"
            f" border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {theme.TEXT}; width: 14px;"
            f" margin: -5px 0; border-radius: 7px; }}")
        use_hover_cursor(self.volume_slider)
        self.volume_slider.valueChanged.connect(self._set_volume)
        row.addWidget(self.volume_slider)

        self.speed_btn = _text_button("1x", "Playback speed")
        self.speed_btn.clicked.connect(self._cycle_speed)
        row.addWidget(self.speed_btn)

        self.subs_btn = _text_button(f"{ICON_SUBTITLES}  Subtitles", "Arabic subtitles")
        self.subs_btn.setStyleSheet(self.subs_btn.styleSheet() +
                                    f"QPushButton {{ font-family: {theme.FONT_STACK_ICONS}; }}")
        self.subs_btn.clicked.connect(self._open_subtitle_panel)
        row.addWidget(self.subs_btn)

        self.tracks_btn = _icon_button(ICON_TRACKS, "Audio and embedded subtitle tracks")
        self.tracks_btn.clicked.connect(self._open_tracks_panel)
        row.addWidget(self.tracks_btn)

        self.quality_btn = _icon_button(ICON_QUALITY, "Source and quality")
        self.quality_btn.clicked.connect(self._open_streams_panel)
        self.quality_btn.hide()          # shown only when there is a choice
        row.addWidget(self.quality_btn)

        self.fullscreen_btn = _icon_button(ICON_FULLSCREEN, "Full screen (F)")
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        row.addWidget(self.fullscreen_btn)

        outer.addLayout(row)
        self.controls.installEventFilter(self)

    # ---- episodes ----------------------------------------------------
    def _episode_count(self) -> int:
        """How many episodes to list for the season being watched.

        `latest_available` is how far the release currently goes (the
        tracker keeps it), so it is the honest upper bound. It is only
        believed when it is talking about *this* season - a series
        sitting on S04 whose latest_available reads "S01E25" would
        otherwise be given twenty-five rows that do not exist - and the
        episode being watched always counts, since being able to play it
        proves it is there whatever the stored number says."""
        current = int(self.episode or 0)
        try:
            from windows import tracker
        except ImportError:                             # pragma: no cover
            return max(current, 1)
        season, episode = tracker.parse_episode_progress(
            self.entry.get("latest_available"))
        if episode and (not season or not self.season or season == self.season):
            return max(episode, current, 1)
        return max(current, 1)

    def _build_episode_bar(self):
        """The episode list down the left, built once and refilled.

        Hidden until asked for: it covers a quarter of the frame, and a
        list nobody opened is not worth watching a film behind. Only ever
        built for something with episodes at all - a film has one row and
        it is the one already playing."""
        if not self.episode:
            return
        bar = QWidget(self)
        _make_native(bar)
        bar.setStyleSheet(f"background: {theme.SURFACE};")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 14, 10, 14)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.episode_bar_title = QLabel("Episodes")
        self.episode_bar_title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 13pt; font-weight: 700;"
            f" background: transparent;")
        header.addWidget(self.episode_bar_title)
        header.addStretch(1)
        outer.addLayout(header)

        self._episode_list = QWidget()
        self._episode_list.setStyleSheet("background: transparent;")
        self._episode_list_layout = QVBoxLayout(self._episode_list)
        self._episode_list_layout.setContentsMargins(0, 0, 0, 0)
        self._episode_list_layout.setSpacing(6)
        area = scroll_area(self._episode_list)
        area.setStyleSheet("background: transparent; border: none;")
        area.viewport().setStyleSheet("background: transparent;")
        outer.addWidget(area, stretch=1)

        bar.installEventFilter(self)
        bar.hide()
        self._episode_bar = bar
        self._fill_episode_bar()

    def _fill_episode_bar(self):
        """Rebuild the rows. Called on every episode change so the
        highlight follows what is actually playing - the list is small
        enough that rebuilding it costs less than tracking which row was
        selected last, and a page here rebuilds from scratch anyway
        (.claude/rules/ui.md)."""
        if self._episode_bar is None:
            return
        layout = self._episode_list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) as well as deleteLater(), and in that
                # order. deleteLater alone leaves the row a visible child
                # of the list with its old geometry until the event loop
                # gets round to it, and taking it out of the layout means
                # nothing moves it - measured: after one refill the list
                # painted a second "Episode 4" card over the rows below,
                # stretched down the panel. Unparenting removes it from
                # the screen on this line rather than eventually.
                widget.setParent(None)
                widget.deleteLater()
        self._episode_rows = {}

        if self.season:
            self.episode_bar_title.setText(f"Season {int(self.season)}")
        for number in range(1, self._episode_count() + 1):
            self._episode_rows[number] = self._episode_row(number)
            layout.addWidget(self._episode_rows[number])
        layout.addStretch(1)

    def _episode_row(self, number):
        current = number == int(self.episode or 0)
        card = Card(matte=True)
        card.setStyleSheet(
            f"QFrame#Card {{ background: "
            f"{theme.ACCENT_SOFT if current else theme.SURFACE_HOVER};"
            f" border: 1px solid {theme.ACCENT if current else theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px; }}")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(8)
        label = QLabel(f"Episode {number}")
        label.setStyleSheet(
            f"color: {theme.ACCENT if current else theme.TEXT}; font-size: 11pt;"
            f" font-weight: {700 if current else 500};"
            f" background: transparent; border: none;")
        layout.addWidget(label)
        layout.addStretch(1)
        if current:
            # A colour alone is not enough of a marker on a list where
            # every row already has a border - the glyph is what makes
            # "which one is playing" answerable at a glance.
            playing = QLabel(ICON_PLAY)
            playing.setStyleSheet(
                f"color: {theme.ACCENT}; font-family: {theme.FONT_STACK_ICONS};"
                f" font-size: 10pt; background: transparent; border: none;")
            layout.addWidget(playing)
        card.clicked.connect(lambda checked=False, n=number: self._pick_episode(n))
        return card

    def _pick_episode(self, number):
        if not self.episode or int(number) == int(self.episode):
            self.toggle_episode_bar()
            return
        self._save_position()
        self.episode = int(number)
        self.prev_btn.setEnabled(self.episode > 1)
        self._given_streams = None      # the handed-in list was for the old episode
        self._fill_episode_bar()
        self._begin_episode()

    def toggle_episode_bar(self):
        if self._episode_bar is None:
            return
        if self._episode_bar.isVisible():
            self._episode_bar.hide()
            self._wake_controls()
            return
        self._fill_episode_bar()
        self._layout_overlays()         # geometry first - see _show_status
        self._episode_bar.show()
        self._episode_bar.raise_()
        _set_window_alpha(self._episode_bar, BAR_ALPHA)
        self._wake_controls()

    # ---- start-up ----------------------------------------------------
    def _start(self):
        if not video_backend.available():
            self._show_status(
                f"{video_backend.unavailable_reason()}\n\n"
                "Run this from the project folder and reopen the player:\n"
                "python packaging/fetch_libmpv.py")
            self.controls.hide()
            return
        try:
            self.handle = video_backend.create(self.surface.native_handle())
        except video_backend.PlayerError as error:
            logs.exception("Could not create the mpv instance")
            self._show_status(f"The player could not start.\n\n{error}")
            self.controls.hide()
            return

        for name in ("time-pos", "duration", "pause", "demuxer-cache-time",
                     "track-list", "volume", "mute", "speed", "core-idle",
                     "paused-for-cache", "cache-buffering-state"):
            self.handle.observe_property(name, self._mpv_property)
        self.handle.register_event_callback(self._mpv_event)
        self.handle["sub-font-size"] = self._sub_size

        self._pointer_timer.start(POINTER_POLL_MS)
        self._mouse_timer.start(MOUSE_POLL_MS)
        self._save_timer.start(POSITION_SAVE_MS)
        self._wake_controls()
        self.setFocus()
        self._begin_episode()

    def _begin_episode(self):
        """Everything that has to happen for one episode: find something
        to play, and look for subtitles for it."""
        # Bring the torrent session and DHT up while the addon lookup is
        # still running. Bootstrapping costs several seconds and is pure
        # dead time if it only starts once a source has been chosen -
        # measured as most of the gap between the first play of a
        # session and every later one.
        try:
            from helpers import torrent_engine
            torrent_engine.prewarm()
        except Exception:
            pass
        self._run += 1
        self._marked_watched = False
        self._duration = 0.0
        self._position = 0.0
        self.seek_bar.set_duration(0)
        self.seek_bar.set_position(0)
        self.seek_bar.set_buffered(0)
        self.title_label.setText(self._title_text())
        self._fill_episode_bar()
        self._subtitles = []
        self._set_subtitle_count(None)

        if self._given_streams is not None:
            self._on_streams(self._given_streams, self._run)
        elif streams_module is None:
            self._show_status("No stream sources are available in this build.")
        else:
            self._show_status("Looking for a source...")
            self._spawn(self._find_streams_worker, self._run)
        self._search_subtitles()

    # ---- background work ---------------------------------------------
    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def _find_streams_worker(self, run):
        """Never raises: an exception here would kill the thread silently
        and the page would sit on "Looking for a source..." forever."""
        try:
            found = streams_module.find_streams(
                self.entry, season=self.season, episode=self.episode,
                deadline=net.deadline_in(STREAM_BUDGET_S))
            self._work.streams_ready.emit(list(found or []), run)
        except Exception:
            logs.exception("Stream lookup failed")
            self._work.streams_ready.emit([], run)

    def _search_subtitles(self):
        if subtitles_module is None:
            return
        self._spawn(self._subtitles_worker, self._run)

    def _subtitles_worker(self, run):
        try:
            from windows import tracker
            imdb_id = tracker._entry_imdb_id(self.entry)
            kind = "series" if self.episode else "movie"
        except Exception:
            imdb_id, kind = self.entry.get("imdb_id"), "movie"
        try:
            found = subtitles_module.search(
                self.entry.get("title") or "", year=self.entry.get("year"),
                season=self.season, episode=self.episode, imdb_id=imdb_id,
                kind=kind, deadline=net.deadline_in(SUBTITLE_BUDGET_S))
            self._work.subs_ready.emit(list(found or []), run)
        except Exception:
            logs.exception("Subtitle search failed")
            self._work.subs_ready.emit([], run)

    def _fetch_subtitle_worker(self, result, run):
        """Fetch, then write UTF-8 to a temp file and hand mpv the path.

        mpv is never given the url. Many of these hosts need the same
        Referer the stream did, and an Arabic .srt is very often
        Windows-1256 - helpers/subtitles.fetch has already decoded it,
        and re-encoding as UTF-8 here means libass reads real text
        instead of one long line of mojibake."""
        try:
            text = subtitles_module.fetch(result, net.deadline_in(SUBTITLE_BUDGET_S))
            if not text:
                self._work.failed.emit("That subtitle could not be downloaded.", run)
                return
            path = self._write_subtitle(text, result.get("format") or "srt")
            label = result.get("release") or result.get("name") or "Subtitle"
            self._work.sub_file_ready.emit(path, label, run)
        except Exception:
            logs.exception("Subtitle download failed")
            self._work.failed.emit("That subtitle could not be downloaded.", run)

    def _write_subtitle(self, text, fmt):
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="atomic-subs-")
        suffix = "ass" if str(fmt).lower() in ("ass", "ssa") else "srt"
        path = os.path.join(self._temp_dir, f"{uuid.uuid4().hex}.{suffix}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    # ---- results back on the UI thread -------------------------------
    def _on_streams(self, found, run):
        if self._closing or run != self._run:
            return
        self._streams = list(found or [])
        if not self._streams:
            self._show_status(
                "No playable source was found for this episode.\n"
                "Try again, or open it on its site.")
            return
        drm = [s for s in self._streams if s.get("kind") == "drm"]
        playable = [s for s in self._streams if s.get("kind") != "drm"]
        if not playable:
            self._show_drm(drm[0])
            return
        self._streams = playable + drm
        self.quality_btn.setVisible(len(self._streams) > 1)
        self._play_stream(0)

    def _on_subtitles(self, found, run):
        if self._closing or run != self._run:
            return
        self._subtitles = list(found or [])
        self._set_subtitle_count(len([s for s in self._subtitles
                                      if str(s.get("lang", "")).lower().startswith("ar")]))
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel()      # rebuild in place with the results

    def _on_subtitle_file(self, path, label, run):
        if self._closing or run != self._run or self.handle is None:
            return
        try:
            self.handle.sub_add(path, "select")
            self.handle["sub-font-size"] = self._sub_size
            self.handle["sub-delay"] = self._sub_delay
            self.handle["sub-visibility"] = True
        except Exception:
            logs.exception("sub-add failed")
            show_toast(self._toast_anchor(), "Subtitle Could Not Be Loaded")
            return
        self._subtitle_label = label
        show_toast(self._toast_anchor(), "Subtitle Loaded")
        if self._panel is not None and getattr(self._panel, "kind", "") == "subs":
            self._open_subtitle_panel()

    def _on_failed(self, message, run):
        if self._closing or run != self._run:
            return
        show_toast(self._toast_anchor(), message)

    def _set_subtitle_count(self, count):
        if count is None:
            self.subs_btn.setText(f"{ICON_SUBTITLES}  Subtitles")
        else:
            self.subs_btn.setText(f"{ICON_SUBTITLES}  Subtitles ({count})")

    # ---- playback ----------------------------------------------------
    def _play_stream(self, index, resume_at=None):
        if self.handle is None or not self._streams:
            return
        index = max(0, min(index, len(self._streams) - 1))
        stream = self._streams[index]
        if stream.get("kind") == "drm":
            self._show_drm(stream)
            return
        self._stream_index = index
        self._hide_status()
        self.source_label.setText(
            " · ".join(p for p in (stream.get("source"), stream.get("quality")) if p))

        # A torrent arrives without a url on purpose: the streaming
        # server has to be given the release's trackers before it can
        # serve anything, and doing that for all thirty-odd results at
        # lookup time would announce to every tracker for things nobody
        # is going to watch. So it happens here, for the one chosen
        # stream - off the UI thread, because it can also have to start
        # the server.
        if not stream.get("url") and stream.get("info_hash") and streams_module:
            self._show_status("Connecting to the source...")
            self._spawn(self._prepare_stream_worker, index, resume_at, self._run)
            return

        self._load_into_mpv(stream, resume_at)

    def _prepare_stream_worker(self, index, resume_at, run):
        """Never raises - a dead worker here leaves the page on
        "Connecting..." with nothing coming.

        Starts the chosen source *and* the next couple of untried ones
        together, playing whichever delivers data first. A release's
        advertised seeder count says nothing about whether its swarm
        answers: one measured film claimed 2081 seeders and never
        completed a piece, and discovering that serially cost 45s before
        the next was even started. Raced, the healthy one arrives while
        the dead one is still failing. Measured end to end: 79.5s to
        12.8s on that film, 31.8s to 6.2s on an episode."""
        try:
            chosen = self._streams[index]
            others = [s for i, s in enumerate(self._streams)
                      if i != index and i not in self._dead_sources
                      and s.get("kind") != "drm"
                      and _canonical_quality(s.get("quality"))
                      == _canonical_quality(chosen.get("quality"))]
            stream = None
            if hasattr(streams_module, "prepare_fastest"):
                stream = streams_module.prepare_fastest(
                    [chosen] + others, season=self.season, episode=self.episode)
            if stream is None:
                stream = streams_module.prepare(
                    chosen, season=self.season, episode=self.episode)
            self._work.stream_prepared.emit(stream, index, resume_at, run)
        except Exception:
            logs.exception("Preparing the stream failed")
            self._work.stream_prepared.emit(None, index, resume_at, run)

    def _on_stream_prepared(self, stream, index, resume_at, run):
        if run != self._run or self.handle is None:
            return
        if not stream or not stream.get("url"):
            reason = (stream or {}).get("reason") or ""
            if reason == "stremio-not-installed":
                self._show_status(
                    "This source is a torrent, which Atomic plays through "
                    "Stremio's streaming server.\nStremio isn't installed on "
                    "this PC, so there's nothing to play it with.")
                return
            # A dead swarm is common and is not worth showing the user:
            # a release can advertise hundreds of seeders and connect to
            # none (measured - 632 claimed, zero reached), while the next
            # one down starts in under a second. Walking the list is what
            # a person would do by hand, so do it for them rather than
            # stopping on the first dud.
            if self._try_next_source(index):
                return
            self._show_status(
                "None of the sources for this would start.\n"
                "Try again in a moment, or pick one from the list.")
            return
        self._streams[index] = stream
        self._hide_status()
        self._load_into_mpv(stream, resume_at)

    # How many dead sources to walk past before giving up and saying so.
    # Bounded rather than open-ended: each attempt costs a create call
    # and a peer wait, and a title where the first several are all dead
    # is a title with a real problem, not one more retry away.
    MAX_SOURCE_ATTEMPTS = 5

    def _try_next_source(self, failed_index) -> bool:
        """Move to the next untried source after one failed to start.

        Returns False once there is nothing left worth trying, which is
        when the page finally says so out loud."""
        self._dead_sources.add(failed_index)
        if len(self._dead_sources) >= self.MAX_SOURCE_ATTEMPTS:
            return False
        for index in range(len(self._streams)):
            if index in self._dead_sources:
                continue
            if self._streams[index].get("kind") == "drm":
                continue
            self._show_status("That source had no peers. Trying the next one...")
            self._play_stream(index)
            return True
        return False

    def _update_startup_status(self):
        """What to say between choosing a source and the first frame.

        A torrent that has just been created has no peers yet, so this
        window is genuinely long - long enough that with nothing on
        screen it reads as a hang. Naming the stage, and showing the
        buffer filling, is the difference between "it's working" and
        "it's broken"."""
        if not self._awaiting_first_frame or self._closing:
            return
        percent = getattr(self, "_buffering_percent", 0)
        if percent > 0:
            self._show_status(f"Buffering... {percent}%")
        else:
            self._show_status("Finding peers for this source...\n"
                              "The first few seconds take longest.")

    def _load_into_mpv(self, stream, resume_at=None):
        self._awaiting_first_frame = True
        self._buffering_percent = 0
        self._update_startup_status()

        # Headers before the load, not after: many of these hosts answer
        # 403 to a request without the Referer the page carried, and mpv
        # reads these options when it opens the url.
        headers = stream.get("headers") or {}
        try:
            fields = [f"{name}: {value}" for name, value in headers.items()
                      if name.lower() != "user-agent"]
            self.handle["http-header-fields"] = fields
            agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
            if agent:
                self.handle["user-agent"] = agent
            self.handle.play(stream.get("url") or "")
            self.handle["pause"] = False
        except Exception:
            logs.exception("Starting playback failed")
            self._show_status("That source could not be opened. Try another one.")
            return

        if resume_at:
            self._pending_resume = float(resume_at)
        else:
            self._resume_where_it_stopped()

    def _resume_where_it_stopped(self):
        """Seek to the stored position, without asking.

        There used to be a "Resume from 12:34?" bar with Resume and Start
        Over on it. It is gone on purpose: the answer was Resume every
        single time, and a question whose answer is always the same is a
        step, not a choice - worse, ignoring it for twelve seconds meant
        starting over, so the default was the option nobody wanted.

        The two guards that made the bar sensible are kept exactly as
        they were, because they are what stops "always resume" from being
        wrong: under RESUME_MIN_S there is nothing worth resuming to, and
        past RESUME_MAX_FRACTION the episode is effectively finished and
        resuming would open on the credits."""
        record = load_resume(self.entry.get("id"), self.season, self.episode)
        if not record:
            return
        position = float(record.get("position") or 0.0)
        duration = float(record.get("duration") or 0.0)
        if position < RESUME_MIN_S:
            return
        if duration and position > duration * RESUME_MAX_FRACTION:
            return
        # Not seeked here: nothing is decoded yet and mpv answers a seek
        # on an unloaded file with an error. _on_property applies it on
        # the first frame, the same path a source switch already uses.
        self._pending_resume = position
        show_toast(self._toast_anchor(),
                   f"Resumed From {_format_time(position)}")

    def _show_drm(self, stream):
        """The one case with no spinner and no retry: Widevine content is
        not decodable here and never will be, so the page says exactly
        that and offers the only thing that does work."""
        source = stream.get("source") or "This service"
        self.controls.hide()
        self._close_panel()
        self._show_status(
            f"{source} is DRM-protected (Widevine), so Atomic cannot play it.\n"
            f"Open it on {source} instead.")
        button = _text_button(f"Open on {source}")
        button.setParent(self)
        _make_native(button)
        url = stream.get("url") or self.entry.get("url") or ""

        def open_page():
            import webbrowser
            if url:
                webbrowser.open(url)
        button.clicked.connect(open_page)
        self._drm_button = button
        # Geometry before show, like every other native overlay here -
        # see _show_status for what showing one first looks like.
        self._layout_overlays()
        button.show()
        button.raise_()

    def _seek_absolute(self, seconds):
        if self.handle is None:
            return
        try:
            # precision="exact", not mpv's default "keyframes": measured
            # on a 10s test mp4, a seek to 6.0s landed at 4.7 because
            # that was the nearest keyframe before it - which reads as
            # the seek bar ignoring where it was dropped.
            self.handle.seek(max(0.0, float(seconds)), reference="absolute",
                             precision="exact")
        except Exception:
            logs.exception("Seek failed")

    def _seek_relative(self, delta):
        self._seek_absolute((self._position or 0.0) + delta)

    def toggle_pause(self):
        if self.handle is None:
            return
        try:
            self.handle["pause"] = not self._paused
        except Exception:
            logs.exception("Pause toggle failed")

    def toggle_mute(self):
        if self.handle is None:
            return
        try:
            self.handle["mute"] = not self._muted
        except Exception:
            logs.exception("Mute toggle failed")

    def _set_volume(self, value):
        if self.handle is None:
            return
        try:
            self.handle["volume"] = int(value)
        except Exception:
            logs.exception("Volume change failed")

    def _cycle_speed(self):
        try:
            index = SPEEDS.index(self._speed)
        except ValueError:
            index = SPEEDS.index(1.0)
        speed = SPEEDS[(index + 1) % len(SPEEDS)]
        if self.handle is not None:
            try:
                self.handle["speed"] = speed
            except Exception:
                logs.exception("Speed change failed")

    def _change_episode(self, delta):
        if not self.episode:
            return
        target = self.episode + delta
        if target < 1:
            return
        self._save_position()
        self.episode = target
        self.prev_btn.setEnabled(target > 1)
        self._given_streams = None       # the handed-in list was for the old episode
        self._begin_episode()

    def toggle_fullscreen(self):
        window = self._window
        if window is None or not hasattr(window, "toggle_fullscreen"):
            return
        window.toggle_fullscreen()
        self.fullscreen_btn.setText(
            ICON_EXIT_FULLSCREEN if window.isFullScreen() else ICON_FULLSCREEN)
        self._wake_controls()

    # ---- mpv callbacks (mpv's thread) --------------------------------
    def _mpv_property(self, name, value):
        try:
            self._bridge.prop.emit(name, value)
        except Exception:
            pass        # the page is going away; nothing to report to

    def _mpv_event(self, event):
        try:
            if str(getattr(event, "event_id", "")).endswith("END_FILE"):
                self._bridge.ended.emit("eof")
        except Exception:
            pass

    # ---- property updates (Qt thread) --------------------------------
    def _on_property(self, name, value):
        if self._closing:
            return
        if name == "time-pos" and value is not None:
            if self._awaiting_first_frame and float(value) > 0:
                # The first frame is the only proof that a source has
                # actually started; until it lands, "nothing on screen"
                # and "broken" look identical.
                self._awaiting_first_frame = False
                self._hide_status()
            self._position = float(value)
            self.seek_bar.set_position(self._position)
            self._update_time_label()
            self._check_watched()
            if self._pending_resume is not None and self._position > 0:
                target, self._pending_resume = self._pending_resume, None
                self._seek_absolute(target)
        elif name == "duration" and value:
            self._duration = float(value)
            self.seek_bar.set_duration(self._duration)
            self._update_time_label()
        elif name == "demuxer-cache-time" and value is not None:
            self.seek_bar.set_buffered(float(value))
        elif name == "pause":
            self._paused = bool(value)
            self.play_btn.setText(ICON_PLAY if self._paused else ICON_PAUSE)
            if self._paused:
                self._wake_controls()
        elif name == "volume" and value is not None:
            self._volume = int(value)
            self.volume_slider.blockSignals(True)
            self.volume_slider.setValue(self._volume)
            self.volume_slider.blockSignals(False)
        elif name == "mute":
            self._muted = bool(value)
            self.mute_btn.setText(ICON_MUTED if self._muted else ICON_VOLUME)
        elif name == "speed" and value:
            self._speed = float(value)
            self.speed_btn.setText(f"{self._speed:g}x")
        elif name == "track-list":
            self._tracks = list(value or [])
        elif name == "paused-for-cache":
            # Only ever a note, never an error: a stalled buffer usually
            # recovers, and a dialog for it would fire constantly on a
            # slow source.
            if value and not self._status_visible():
                self.source_label.setText("Buffering...")
            elif not value and self._streams:
                stream = self._streams[self._stream_index]
                self.source_label.setText(
                    " · ".join(p for p in (stream.get("source"), stream.get("quality")) if p))
        elif name == "cache-buffering-state":
            self._buffering_percent = int(value or 0)
            self._update_startup_status()

    def _on_ended(self, _reason):
        if self._closing:
            return
        self._check_watched(force=True)

    def _update_time_label(self):
        self.time_label.setText(
            f"{_format_time(self._position)} / {_format_time(self._duration)}")

    def _check_watched(self, force=False):
        if self._marked_watched or not self._duration:
            return
        if not force and self._position < self._duration * WATCHED_FRACTION:
            return
        self._marked_watched = True
        if _mark_watched(self.entry, self.season, self.episode):
            show_toast(self._toast_anchor(), "Marked As Watched")
        clear_resume(self.entry.get("id"), self.season, self.episode)

    def _save_position(self):
        if self._closing or not self._duration or self._position <= RESUME_MIN_S:
            return
        if self._position > self._duration * RESUME_MAX_FRACTION:
            return
        try:
            save_resume(self.entry.get("id"), self.season, self.episode,
                        self._position, self._duration)
        except Exception:
            logs.exception("Could not save the resume position")

    # ---- panels ------------------------------------------------------
    def _close_panel(self):
        if self._panel is not None:
            self._panel.deleteLater()
            self._panel = None
        self.setFocus()

    def _new_panel(self, title, kind):
        self._close_panel()
        panel = OverlayPanel(self, title)
        panel.kind = kind
        panel.closed.connect(self._close_panel)
        panel.installEventFilter(self)
        self._panel = panel
        return panel

    def _open_subtitle_panel(self):
        panel = self._new_panel("Arabic Subtitles", "subs")
        panel.add_row("Off", "No external subtitle", self._subtitles_off,
                      selected=self._subtitle_label == "Off")

        arabic = [s for s in self._subtitles
                  if str(s.get("lang", "")).lower().startswith("ar")]
        other = [s for s in self._subtitles if s not in arabic]
        if not self._subtitles:
            panel.add_message(
                "Searching..." if subtitles_module is not None
                else "Subtitle search is not available in this build.")
        for group, items in (("Arabic", arabic), ("Other Languages", other)):
            if not items:
                continue
            by_source = {}
            for item in items:
                by_source.setdefault(item.get("source") or "Unknown", []).append(item)
            for source, entries in by_source.items():
                panel.add_group(f"{group.upper()}  ·  {source.upper()}")
                for item in entries:
                    label = item.get("release") or item.get("name") or "Subtitle"
                    parts = [str(p) for p in
                             (item.get("format"), item.get("lang")) if p]
                    # Say so when a line was produced by machine
                    # translation rather than written by a person. It is
                    # often serviceable and sometimes nonsense, and
                    # which one you picked should not be a guess -
                    # especially for anime, where these are frequently
                    # the only Arabic on offer.
                    if item.get("translated"):
                        parts.insert(0, "auto-translated")
                    panel.add_row(label, " · ".join(parts),
                                  lambda checked=False, r=item: self._pick_subtitle(r),
                                  selected=label == self._subtitle_label)
        panel.finish()

        # Delay first, size second: resyncing a mismatched Arabic release
        # is by far the most common thing anyone needs from this panel.
        set_delay = panel.add_stepper(
            "Delay", self._delay_text(),
            lambda: self._nudge_delay(-SUB_DELAY_STEP),
            lambda: self._nudge_delay(SUB_DELAY_STEP))
        set_size = panel.add_stepper(
            "Font size", str(self._sub_size),
            lambda: self._nudge_size(-SUB_SIZE_STEP),
            lambda: self._nudge_size(SUB_SIZE_STEP))
        # Held on the panel, not captured in the nudge calls: the panel is
        # rebuilt whenever a subtitle is picked, and a setter belonging to
        # a deleted QLabel would take the process with it.
        panel.set_delay_text = set_delay
        panel.set_size_text = set_size
        self._show_panel(panel)

    def _delay_text(self):
        return f"{self._sub_delay:+.1f}s"

    def _subtitles_off(self):
        if self.handle is not None:
            try:
                self.handle["sub-visibility"] = False
                self.handle["sid"] = "no"
            except Exception:
                logs.exception("Turning subtitles off failed")
        self._subtitle_label = "Off"
        self._open_subtitle_panel()

    def _pick_subtitle(self, result):
        if subtitles_module is None:
            return
        show_toast(self._toast_anchor(), "Loading Subtitle...")
        self._spawn(self._fetch_subtitle_worker, result, self._run)

    def _nudge_delay(self, delta):
        self._sub_delay = round(self._sub_delay + delta, 2)
        if self.handle is not None:
            try:
                self.handle["sub-delay"] = self._sub_delay
            except Exception:
                logs.exception("Subtitle delay change failed")
        self._update_stepper("set_delay_text", self._delay_text())

    def _nudge_size(self, delta):
        self._sub_size = max(SUB_SIZE_MIN, min(SUB_SIZE_MAX, self._sub_size + delta))
        if self.handle is not None:
            try:
                self.handle["sub-font-size"] = self._sub_size
            except Exception:
                logs.exception("Subtitle size change failed")
        self._update_stepper("set_size_text", str(self._sub_size))

    def _update_stepper(self, name, text):
        """Write a new value into a stepper row if that row is still on
        screen. RuntimeError is the case that matters: the panel can be
        closed between a click and this call, and its QLabel is then a
        deleted C++ object behind a live Python name."""
        setter = getattr(self._panel, name, None) if self._panel is not None else None
        if setter is None:
            return
        try:
            setter(text)
        except RuntimeError:
            pass

    def _open_tracks_panel(self):
        panel = self._new_panel("Audio and Subtitle Tracks", "tracks")
        audio = [t for t in self._tracks if t.get("type") == "audio"]
        subs = [t for t in self._tracks if t.get("type") == "sub"]
        if not audio and not subs:
            panel.add_message("This source has no selectable tracks.")
        if audio:
            panel.add_group("AUDIO")
            for track in audio:
                panel.add_row(self._track_label(track),
                              track.get("codec") or "",
                              lambda checked=False, t=track: self._pick_track("aid", t),
                              selected=bool(track.get("selected")))
        if subs:
            panel.add_group("SUBTITLES IN THIS FILE")
            panel.add_row("Off", "", lambda: self._pick_track("sid", None))
            for track in subs:
                panel.add_row(self._track_label(track), track.get("codec") or "",
                              lambda checked=False, t=track: self._pick_track("sid", t),
                              selected=bool(track.get("selected")))
        panel.finish()
        self._show_panel(panel)

    @staticmethod
    def _track_label(track):
        parts = [str(track.get("title") or "").strip(),
                 str(track.get("lang") or "").strip()]
        text = " · ".join(p for p in parts if p)
        return text or f"Track {track.get('id')}"

    def _pick_track(self, prop, track):
        if self.handle is None:
            return
        try:
            self.handle[prop] = "no" if track is None else int(track.get("id"))
            if prop == "sid" and track is not None:
                self.handle["sub-visibility"] = True
        except Exception:
            logs.exception("Track change failed")
        self._open_tracks_panel()

    def _open_streams_panel(self):
        """Resolution first, individual releases second.

        A real lookup comes back with thirty-odd streams and listing them
        flat makes picking 1080p a hunt through near-identical rows. What
        someone wants here is a resolution; which release provides it is
        a detail, so the top of the panel offers 2160p/1080p/720p/480p
        and picks the best-seeded release at that resolution - seeders
        being the only available predictor of whether it will actually
        play."""
        panel = self._new_panel("Resolution and Source", "streams")
        if not self._streams:
            panel.add_message("No sources were found.")
            panel.finish()
            self._show_panel(panel)
            return

        current = {}
        if 0 <= self._stream_index < len(self._streams):
            current = self._streams[self._stream_index]
        current_quality = _canonical_quality(current.get("quality"))

        available = streams_module.qualities(self._streams) if streams_module else []
        if len(available) > 1:
            panel.add_message("Resolution")
            for quality in available:
                matches = streams_module.matching_quality(self._streams, quality)
                if not matches:
                    continue
                best = matches[0]
                index = self._streams.index(best)
                seeders = int(best.get("seeders") or 0)
                label = "4K (2160p)" if quality == "2160p" else quality
                panel.add_row(
                    label,
                    f"{len(matches)} source{'s' if len(matches) != 1 else ''}"
                    + (f" · {seeders} seeders" if seeders else ""),
                    lambda checked=False, i=index: self._switch_stream(i),
                    selected=quality == current_quality)
            panel.add_message("Every source")

        for index, stream in enumerate(self._streams):
            quality = stream.get("quality") or ""
            title = " · ".join(str(p) for p in (quality, stream.get("source")) if p)
            seeders = int(stream.get("seeders") or 0)
            meta = stream.get("reason") or ""
            if not meta:
                release = (stream.get("title") or "").strip()
                meta = release[:64] if release else (stream.get("kind") or "")
                if seeders:
                    meta = f"{seeders} seeders · {meta}" if meta else f"{seeders} seeders"
            panel.add_row(title or stream.get("title") or "Source", meta,
                          lambda checked=False, i=index: self._switch_stream(i),
                          selected=index == self._stream_index)
        panel.finish()
        self._show_panel(panel)

    def _switch_stream(self, index):
        """Swap source without losing the seat - a stalling source is
        exactly when this gets used, and restarting from zero would make
        it useless."""
        self._close_panel()
        self._play_stream(index, resume_at=self._position)

    def _show_panel(self, panel):
        # Geometry first - see _show_status.
        self._layout_overlays()
        panel.show()
        panel.raise_()
        _set_window_alpha(panel, BAR_ALPHA)
        self._wake_controls()

    # ---- status ------------------------------------------------------
    def _show_status(self, text):
        """Put the centred message box up.

        `_layout_overlays` runs *before* `show()`, and that order is the
        whole point. These overlays are native child windows, which
        Windows composites itself; a freshly created one has Qt's default
        widget geometry, so showing it first painted a small box in the
        top-left corner of the frame for one frame before this method
        moved it to the middle. That one-frame box in the corner is the
        "window flashing open and closed" - it is not a window of its
        own, and every overlay here now gets its geometry before it is
        allowed on screen."""
        self.status.setText(text)
        # _layout_overlays sizes the box whether or not it is showing -
        # it used to skip a hidden one, which is exactly the case that
        # needed positioning.
        self._layout_overlays()
        self.status.show()
        self.status.raise_()
        _set_window_alpha(self.status, BAR_ALPHA)

    def _hide_status(self):
        self.status.hide()

    def _status_visible(self):
        return self.status.isVisible()

    def _toast_anchor(self):
        return self._window or self

    # ---- controls visibility -----------------------------------------
    def _wake_controls(self):
        if not self.controls.isVisible():
            # Same order as _show_status, for the same reason: a native
            # child window that is shown before it is placed paints once
            # where it used to be. Only on the way back from hidden - this
            # runs on every pointer tick, and setting geometry on four
            # native windows 5x a second is its own flicker.
            self._layout_overlays()
        self.controls.show()
        self.top_bar.show()
        self.controls.raise_()
        self.top_bar.raise_()
        if self._episode_bar is not None and self._episode_bar.isVisible():
            self._episode_bar.raise_()
            _set_window_alpha(self._episode_bar, BAR_ALPHA)
        if self._panel is not None:
            self._panel.raise_()
        # Reapplied on every wake, not once at build time: Qt destroys and
        # recreates a native child window on a reparent or a screen
        # change, and the recreated one has none of the extended style,
        # so the bar silently goes back to opaque.
        _set_window_alpha(self.controls, BAR_ALPHA)
        _set_window_alpha(self.top_bar, BAR_ALPHA)
        # Deliberate, and reversed on every path that hides them again:
        # this is not the sticky-hand-cursor trap in .claude/rules/ui.md,
        # which is about a widget keeping a cursor it no longer earns.
        # A blank cursor over a playing video is the point.
        self.surface.unsetCursor()
        self._set_sub_position(SUB_POS_ABOVE_CONTROLS)
        self._idle_timer.start(CONTROLS_HIDE_MS)

    def _set_sub_position(self, value):
        if self.handle is None:
            return
        try:
            self.handle["sub-pos"] = value
        except Exception:
            logs.exception("Subtitle position change failed")

    def _hide_controls(self):
        if self._closing or self._paused:
            return
        if self._panel is not None:
            return
        # The episode list is opened deliberately and dismissed the same
        # way, so nothing auto-hides while it is up - having the list
        # vanish mid-scroll is what would make it useless.
        if self._episode_bar is not None and self._episode_bar.isVisible():
            return
        if self._status_visible():
            return
        if self._pointer_in(self.controls) or self._pointer_in(self.top_bar):
            # The pointer resting on the controls is not idleness - it is
            # someone about to press something. Re-armed rather than
            # dropped, so it hides once they move off.
            self._idle_timer.start(CONTROLS_HIDE_MS)
            return
        self.controls.hide()
        self.top_bar.hide()
        self.surface.setCursor(Qt.CursorShape.BlankCursor)
        self._set_sub_position(SUB_POS_CLEAR)

    def _pointer_in(self, widget):
        """Is the pointer really over `widget`?

        Not `underMouse()`, which was the first attempt and is wrong
        here: measured with the pointer sitting on the controls bar, it
        answered False, because it depends on an Enter having been
        delivered and this window is not always the active one. The bar
        then hid itself out from under the pointer.

        The rect is built from `window().geometry()`, which is already in
        global coordinates, plus `mapTo(window, ...)`, which is plain
        widget-local arithmetic inside one window. Deliberately not
        mapToGlobal: on two monitors at different scale factors it
        divides by the wrong screen's factor (see .claude/rules/ui.md)."""
        try:
            if not widget.isVisible():
                return False
            window = self.window()
            if window is None:
                return False
            origin = window.geometry().topLeft() + widget.mapTo(window, QPoint(0, 0))
            return QRect(origin, widget.size()).contains(QCursor.pos())
        except RuntimeError:
            return False

    def _poll_pointer(self):
        position = QCursor.pos()
        if position != self._last_pointer:
            self._last_pointer = position
            self._wake_controls()

    # ---- click to play/pause -----------------------------------------
    def _over_video(self, position) -> bool:
        """Is this screen point on the video and on nothing else?

        Same global-rect arithmetic as `_pointer_in` and for the same
        reason (never mapToGlobal - see .claude/rules/ui.md). Everything
        the player draws over the video is subtracted, so a press on the
        controls, the episode list, a panel or the status box is not a
        press on the video."""
        try:
            window = self.window()
            if window is None:
                return False
            origin = window.geometry().topLeft() + self.mapTo(window, QPoint(0, 0))
            if not QRect(origin, self.size()).contains(position):
                return False
        except RuntimeError:
            return False
        overlays = [self.controls, self.top_bar, self.status,
                    self._panel, self._episode_bar,
                    getattr(self, "_drm_button", None)]
        return not any(w is not None and self._widget_rect(w).contains(position)
                       for w in overlays)

    def _widget_rect(self, widget) -> QRect:
        try:
            if not widget.isVisible():
                return QRect()
            window = self.window()
            if window is None:
                return QRect()
            origin = window.geometry().topLeft() + widget.mapTo(window, QPoint(0, 0))
            return QRect(origin, widget.size())
        except RuntimeError:
            return QRect()

    def _poll_mouse(self):
        """Click anywhere on the video to play/pause.

        Polled, not handled as a Qt event, for exactly the reason the
        pointer is polled: mpv renders into a native child window of its
        own, so a press over the video is delivered to mpv's window and
        no Qt widget ever sees it. There is no filter to install - the
        window does not belong to Qt.

        GetAsyncKeyState is read twice per state: 0x8000 is "held right
        now", which a 40ms tick sees for any normal click, and 0x0001 is
        "was pressed since this thread last asked", which catches a click
        that began and ended entirely between two ticks. The second is
        only ever a fallback; it cannot fire without a real press having
        happened."""
        if self._closing or os.name != "nt":
            return
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(_VK_LBUTTON)
        except Exception:
            return
        down = bool(state & 0x8000)
        pressed_since = bool(state & 0x0001)
        position = QCursor.pos()

        if down and not self._mouse_down:
            self._mouse_down = True
            self._press_pos = position
            # Decided at press time, not at release: the window being
            # active is what separates "clicked the video" from "clicked
            # some other app that happens to be over this one", and a
            # click that only raises this window must not also pause it.
            self._press_on_video = (self._is_active() and self._over_video(position))
            return
        if down:
            return
        if self._mouse_down:
            self._mouse_down = False
            if self._press_on_video and self._is_click(position):
                self._click_toggle(position)
            self._press_on_video = False
            self._press_pos = None
        elif pressed_since and self._is_active() and self._over_video(position):
            self._click_toggle(position)

    def _is_active(self) -> bool:
        try:
            window = self.window()
            return window is not None and window.isActiveWindow()
        except RuntimeError:
            return False

    def _is_click(self, position) -> bool:
        if self._press_pos is None:
            return True
        moved = ((position.x() - self._press_pos.x()) ** 2
                 + (position.y() - self._press_pos.y()) ** 2)
        return moved <= CLICK_MOVE_TOLERANCE_PX ** 2

    def _click_toggle(self, position):
        """A click landed on the video. Wake the controls and toggle.

        Nothing plays yet while the status box is up (looking for a
        source, buffering), and toggling pause on a file that has not
        loaded only makes the play button lie about what is happening."""
        if self.handle is None or self._status_visible():
            return
        if not self._over_video(position):
            return
        self._wake_controls()
        self.toggle_pause()

    # ---- layout ------------------------------------------------------
    def _layout_overlays(self):
        rect = self.rect()
        self.surface.setGeometry(rect)
        self.top_bar.setGeometry(0, 0, rect.width(), TOPBAR_HEIGHT)
        self.controls.setGeometry(0, rect.height() - CONTROLS_HEIGHT,
                                  rect.width(), CONTROLS_HEIGHT)
        if self._episode_bar is not None:
            self._episode_bar.setGeometry(
                0, TOPBAR_HEIGHT, EPISODE_BAR_WIDTH,
                max(120, rect.height() - TOPBAR_HEIGHT - CONTROLS_HEIGHT))
        # Placed whether or not it is showing: _show_status calls this
        # while the box is still hidden precisely so it can be put in the
        # right place before it ever appears.
        width = min(620, max(320, rect.width() - 120))
        height = self.status.sizeHint().height() + 20
        self.status.setGeometry(int((rect.width() - width) / 2),
                                int((rect.height() - height) / 2), width, height)
        button = getattr(self, "_drm_button", None)
        if button is not None:
            size = button.sizeHint()
            self.status.adjustSize()
            button.setGeometry(int((rect.width() - size.width()) / 2),
                               int(rect.height() / 2) + 90,
                               size.width(), size.height())
        if self._panel is not None:
            width = min(PANEL_WIDTH, max(280, rect.width() - 60))
            height = min(PANEL_MAX_HEIGHT, max(220, rect.height() - CONTROLS_HEIGHT - 80))
            self._panel.setGeometry(rect.width() - width - 24,
                                    rect.height() - CONTROLS_HEIGHT - height - 10,
                                    width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlays()

    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
            return False
        if event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove):
            self._wake_controls()
        return super().eventFilter(obj, event)

    # ---- keyboard ----------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        self._wake_controls()
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
        elif key == Qt.Key.Key_Left:
            self._seek_relative(-SEEK_STEP_S)
        elif key == Qt.Key.Key_Right:
            self._seek_relative(SEEK_STEP_S)
        elif key == Qt.Key.Key_Up:
            self.volume_slider.setValue(min(100, self._volume + VOLUME_STEP))
        elif key == Qt.Key.Key_Down:
            self.volume_slider.setValue(max(0, self._volume - VOLUME_STEP))
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_M:
            self.toggle_mute()
        elif key == Qt.Key.Key_Escape:
            # One thing at a time, innermost first: a panel, then full
            # screen, then the player. Closing the whole page because a
            # subtitle list was open is the kind of Escape that makes
            # people stop using it.
            if self._panel is not None:
                self._close_panel()
            elif self._episode_bar is not None and self._episode_bar.isVisible():
                self.toggle_episode_bar()
            elif self._window is not None and self._window.isFullScreen():
                self.toggle_fullscreen()
            else:
                self.close_player()
        else:
            super().keyPressEvent(event)

    # ---- teardown ----------------------------------------------------
    def close_player(self):
        if self._closing:
            return
        self._closing = True
        self._save_position()
        self._idle_timer.stop()
        self._pointer_timer.stop()
        self._mouse_timer.stop()
        self._save_timer.stop()
        self._close_panel()
        # Cursor first: the surface is about to go away, and a widget
        # that dies holding the blank cursor leaves Windows painting it
        # over whatever comes next.
        self.surface.unsetCursor()
        handle, self.handle = self.handle, None
        video_backend.shutdown(handle)
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
        parent = self.parent()
        if parent is not None:
            parent.removeEventFilter(self)
        if self.on_close is not None:
            self.on_close()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):
        self.close_player()
        super().closeEvent(event)


# ---------------------------------------------------------------------


def open_player(window, entry, season=None, episode=None, streams=None):
    """Put the player over `window` and return it.

    This is the whole wiring surface. A caller (a tracker card's Play, a
    Home item) needs this one call and nothing else - the page parents
    itself, sizes itself, follows the window's resizes, and puts things
    back when it closes.

    Parented to the central widget rather than to main.py's page
    container so it covers the sidebar too (see PlayerPage's docstring),
    and the previous page is left alone underneath rather than being
    torn down: coming back out of the player should land exactly where
    it was entered from, and rebuilding a tracker page costs a full
    reload of its covers."""
    host = window.centralWidget() if hasattr(window, "centralWidget") else window
    existing = getattr(window, "_player_page", None)
    if existing is not None:
        try:
            existing.close_player()
        except RuntimeError:
            pass

    def on_close():
        window._player_page = None

    page = PlayerPage(host, entry, season=season, episode=episode,
                      streams=streams, on_close=on_close)
    window._player_page = page
    page.setGeometry(host.rect())
    page.show()
    page.raise_()
    page.setFocus()
    return page
