"""A poster grid that is one widget, painted on the GPU, and scrolls itself.

**Why this exists - the owner's words, 23 August 2026, after four days:**
"the scrolling stutter (cards shaking) while loading. I do not have
anything to say but rewrite the whole code for it !! it is enough!!!!"

He was right, and the reason four patches did not hold is structural. The
category pages were a `QGridLayout` holding one `Card` widget per row -
five hundred of them on the Anime page - each a QFrame with a QVBoxLayout
and three QLabels. That shape has two costs no tuning can remove:

  * **Every append re-solves the layout.** A new card anywhere means the
    grid recomputes every row's height, and a row is as tall as its
    tallest card, so a one-pixel difference moved everything below it.
    That was the shake. Fixing the heights, then the holes, then the
    polish, each removed one trigger; the mechanism stayed.
  * **Every paint walks every child.** Two thousand widgets is two
    thousand paint events per frame whether or not they are on screen.
    Measured as the interval between real paints of the scroll body, the
    grid drew **40.7 frames a second while filling** on a 144Hz panel.

So the grid is one widget holding plain records, painting only the cells
in view. That fixed the shake.

**24 August 2026 - the second half, and the owner was right again:**
"the frames are clearly < 60 while scrolling ... rewrite its whole code
if needed". Every earlier measurement here counted *paint events* and
reported 120-147/s, which is why this was thought fixed. Sampling the
scroll position at the panel's own vblank (IDXGIOutput::WaitForVBlank)
on his real maximized 1920x1080 window said otherwise:

    frames 114-118/s, 19% of them more than 30% off the speed of the
    frame before, worst frame 82ms, 43% of refreshes showing no
    movement at all - an effective 80 moving frames a second

Two causes, both structural again, and both fixed here:

  * **The position was driven by a 6ms QTimer, which beats against a
    6.94ms refresh.** Some refreshes got two positions and the next got
    none, so the *steps* were uneven however high the paint count was.
    Uneven steps are what the eye reads as stutter. The position is now
    computed **inside the frame that draws it**, from that frame's own
    timestamp (`FrameMotion.step`), so every frame carries exactly one
    correctly-spaced position by construction.
  * **Nothing is scrolled.** The widget fills the viewport and the cells
    are drawn at an offset, so there is no backing store to memmove and
    no scroll area to move it - which also means no QScrollArea can put
    the window's raster blit back in front of this.

**A GPU version of this was built, measured and taken out again.** On a
`QOpenGLWidget` with a vsync-locked swap the same grid ran at 142.9
frames/s with 2.0% judder, against 114-118 and 14% for the widget grid
it replaced - a real win, and it is not what ships. The owner's player
draws through libmpv into a *native child window* in the same top-level
window, and after the GPU surfaces went in he reported the video as
"only plays sound, there is just black screen". Qt's own documentation
says a QOpenGLWidget and native child widgets in one window are not
supported; a synthetic reproduction here could not make it fail, which
is exactly why guessing at it was not good enough to keep a broken
player. **Do not reintroduce OpenGL anywhere in this process without
first proving the player still draws.**

What survives is the half that never needed the GPU: the position is
still integrated inside the frame that draws it, so the steps stay even,
and the surface still owns its own scrolling.

Because the surface owns its scrolling, this widget is **not** put inside
a `scroll_area()` - it fills the tab and paints its own scrollbar. Covers
are still requested lazily for the cells in view (`needs_cover`).

What a cell draws is exactly what the old `Card` drew, from the same
theme tokens: the cover tile (already rounded by `images.thumbnail_or_
avatar`), a two-line title in the #CardTitle weight, the year in the
#CardMeta size, a rating chip on the cover's foot and a Saved chip on its
head, and on hover the same ACCENT_SOFT fill with an ACCENT ring.
"""

import math
import time

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient,
                         QPainter, QPen, QPixmap, QStaticText, QTextOption)

from PyQt6.QtWidgets import QWidget

from . import theme
from .widgets import hold_hover_cursor, release_hover_cursor

# The card geometry the old widgets had, kept to the pixel so the page
# looks the same: a 180px card around a 160x216 cover with 10px of air,
# 14px between cards.
CARD_PADDING_X = 10
CARD_PADDING_TOP = 10
CARD_PADDING_BOTTOM = 10
TEXT_GAP = 6
GRID_SPACING = 14
TITLE_LINES = 2

# How many rows past the viewport get their covers asked for. One
# screenful of cards at a normal wheel pace arrives before the rows
# do; two rows is what a fast flick overruns.
PREFETCH_ROWS = 2

# **How many covers may be asked for in one frame, and how fast is too
# fast to ask at all.** Measured 24 August 2026, instrumented run on the
# Anime page: `_ask` - which emits needs_cover, and lands in
# cover_fetch, which decodes a cached cover on the UI thread - blocked
# the UI thread for up to **68.5ms in one go**, which is ten frames. It
# is not inside the paint (it is posted with singleShot(0)), and that
# does not help: a frame cannot be produced while it runs.
#
# So a frame asks for at most a few, and a view moving faster than
# FAST_SCROLL_PX_S asks only for what is actually on screen - a flick
# crosses fifty rows nobody will look at, and every one of them used to
# be requested and decoded on the way past.
COVER_ASK_PER_FRAME = 4
FAST_SCROLL_PX_S = 1500.0

# The scrollbar this widget paints for itself, since it is not inside a
# QScrollArea any more. Same width as the app's QSS scrollbar so the
# page's right edge is unchanged.
BAR_WIDTH = 11
BAR_MARGIN = 3
BAR_MIN_THUMB = 36


class FrameMotion:
    """Wheel scrolling as velocity and friction, stepped by the frame
    that is about to draw it.

    Same physics as `widgets._Momentum` - impulse per notch, ramped in,
    exponential decay, excess speed kept in `_pending` - and the same
    constants, so the wheel feels identical wherever it is used. The one
    difference is the clock, and it is the whole point: `_Momentum` runs
    off a QTimer at the refresh interval, which beats against the real
    refresh and delivers two positions to one frame and none to the
    next (measured 24 August 2026: 19% of frames more than 30% off the
    previous frame's speed). Here `step()` is called from the paint with
    that frame's own timestamp, so one frame is exactly one position.

    Sub-pixel by design: the offset stays a float and is only rounded
    when it is drawn. The old model wrote `int` into a QScrollBar, and
    at slow speeds consecutive frames rounded to the same integer - 19%
    of glide ticks moved nothing at all."""

    # See widgets._Momentum for what each of these was measured against;
    # they are deliberately the same numbers.
    FRICTION = 7.0
    RAMP = 40.0
    MAX_SPEED = 5200.0
    STOP_SPEED = 30.0
    ACCEL_WINDOW_S = 0.25
    ACCEL_NOTCHES = 5
    ACCEL_MAX = 1.7
    # A frame that arrives later than two refreshes is integrated as two:
    # the view lands a little short rather than leaping. 144Hz is this
    # machine's panel; screen_tick_ms is what knows better at runtime.
    MAX_DT = 2.0 / 144.0

    def __init__(self, friction=None, accel_max=None):
        """`friction` and `accel_max` override the class defaults for a
        surface that wants a different feel - the reader passes a high
        friction and no acceleration so its strip stops when the wheel
        stops (windows.reader.READER_WHEEL_FRICTION)."""
        if friction is not None:
            self.FRICTION = float(friction)
        if accel_max is not None:
            self.ACCEL_MAX = float(accel_max)
        # One display refresh, in seconds - see step(). 0 means "not
        # known yet", and step() then integrates raw elapsed time exactly
        # as it always did. The owner of this object fills it in.
        self.frame_s = 0.0
        self.pos = 0.0
        self.vel = 0.0
        self.pending = 0.0
        self.maximum = 0.0
        self._last = None
        self._kicks = []

    def running(self) -> bool:
        return abs(self.vel) >= self.STOP_SPEED or bool(self.pending)

    def kick(self, distance_px, direction):
        """One wheel notch: `direction` +1 forward (down the page)."""
        now = time.monotonic()
        self._kicks = [t for t in self._kicks if now - t <= self.ACCEL_WINDOW_S]
        accel = 1.0 + (self.ACCEL_MAX - 1.0) * min(
            1.0, len(self._kicks) / float(self.ACCEL_NOTCHES))
        self._kicks.append(now)
        if self.vel * direction < 0 or self.pending * direction < 0:
            # A reversal answers now, not after a decay.
            self.vel = self.pending = 0.0
        self.pending += direction * float(distance_px) * self.FRICTION * accel
        if self._last is None:
            self._last = time.perf_counter()

    def stop(self):
        self.vel = self.pending = 0.0
        self._kicks.clear()
        self._last = None

    def set_position(self, value):
        self.pos = max(0.0, min(float(self.maximum), float(value)))
        self.stop()

    def step(self, now=None):
        """Advance to `now`. Returns True while there is still motion."""
        now = now if now is not None else time.perf_counter()
        if self._last is None:
            self._last = now
            return self.running()
        # **Snap the timestep to whole refreshes.** The position is
        # computed when paintEvent runs, but the frame is shown at the
        # next vblank, and the gap between those two moments varies. That
        # variation lands in the position, and the difference between two
        # presented positions IS the step the eye integrates - so jitter
        # in paint scheduling reads directly as uneven motion.
        #
        # Measured 24 August 2026 on the owner's 144Hz panel, against
        # Stremio, using DXGI Desktop Duplication (the compositor's own
        # presented frames - screen-DC BitBlt and PrintWindow were both
        # proved stale first and their numbers discarded):
        #
        #                       presents/s   step spread   uneven steps
        #   Atomic, raw dt          129.1        3.1x         35-36%
        #   Atomic, snapped         124.7        2.0-2.1x     29-30%
        #   Stremio (Chromium)      131-133      1.9x         12-13%
        #
        # Two controls each. The spread now matches Stremio's; what still
        # differs is dropped frames (13% of refreshes here against 7-9%),
        # and every drop is a double-length step by construction. That is
        # a paint-cost problem, not a timing one - the grid's paint costs
        # 4.43ms median, 6.07ms p95, against a 6.94ms budget.
        #
        # Note what this is NOT: frame rate was never the difference.
        # 129.1/s against 131-133/s is within 3%.
        #
        # MAX_DT comes from the real interval too when it is known.
        # Hardcoded 2/144 is 13.89ms, which is SHORTER than one 60Hz
        # frame (16.67ms) - so on a 60Hz panel the clamp would fire on
        # every ordinary frame and undo the snap below. (On its own that
        # clamp measured as changing nothing, 47.8 fps either way; it
        # matters only now that something else depends on dt.)
        max_dt = 2.0 * self.frame_s if self.frame_s > 0.0 else self.MAX_DT
        if self.frame_s > 0.0:
            whole = int(round((now - self._last) / self.frame_s))
            now = self._last + max(1, min(4, whole)) * self.frame_s
        dt = min(max_dt, max(0.0, now - self._last))
        self._last = now
        if self.pending:
            handed = self.pending * (1.0 - math.exp(-self.RAMP * dt))
            self.pending -= handed
            self.vel += handed
            if abs(self.pending) < 1.0:
                self.vel += self.pending
                self.pending = 0.0
        capped = max(-self.MAX_SPEED, min(self.MAX_SPEED, self.vel))
        if capped != self.vel:
            # Kept, not discarded: a flick must travel its whole distance
            # at a bounded speed (widgets._Momentum records the measured
            # failure of the version that threw the excess away).
            self.pending += self.vel - capped
            self.vel = capped
        self.pos += self.vel * dt
        self.vel *= math.exp(-self.FRICTION * dt)
        if self.pos <= 0.0:
            self.pos, self.vel, self.pending = 0.0, 0.0, 0.0
        elif self.pos >= self.maximum:
            self.pos, self.vel, self.pending = float(self.maximum), 0.0, 0.0
        if abs(self.vel) < self.STOP_SPEED and not self.pending:
            self.vel = 0.0
            self._last = None
            return False
        return True


class PosterGrid(QWidget):
    """See the module docstring. Records are dicts:

        title       str
        year        str ("" for none)
        rating      str ("★ 7.4" or "")
        saved       bool
        pixmap      QPixmap or None (the cover tile, already rounded)

    Signals:
        clicked(int)      a cell was left-clicked, by index
        needs_cover(int)  a cell came into view without a cover
        scrolled()        the offset or the content height changed - what
                          load-on-scroll watches, in place of the
                          QScrollBar's valueChanged/rangeChanged
    """

    clicked = Signal(int)
    needs_cover = Signal(int)
    scrolled = Signal()

    # _EdgeWheelRelay looks for this rather than for QAbstractScrollArea,
    # so a notch over the page's dead margins still reaches the grid.
    accepts_relayed_wheel = True

    # Distance per notch, the same fraction of the viewport the rest of
    # the app uses (widgets._SmoothWheel.NOTCH_FRACTION, 40% slower as of
    # 24 August 2026) so one wheel click moves the same amount here.
    NOTCH_FRACTION = 0.0924
    NOTCH_FLOOR_PX = 25

    def __init__(self, cover_size, ground=None, parent=None):
        super().__init__(parent)
        self._cover_w, self._cover_h = int(cover_size[0]), int(cover_size[1])
        self._ground = QColor(ground or theme.PANEL_FILL)
        self._records = []
        self._columns = 1
        self._hover = -1
        self._requested = set()
        self._metrics = None
        self._content_h = 0
        self._motion = FrameMotion()
        self._chips = {}                # (text, accent) -> rendered chip
        self._drag_from = None          # scrollbar thumb drag anchor
        self._bar_hover = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Opaque: nothing behind this is worth painting, and saying so is
        # what lets Qt skip the ancestors on every frame.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # ---- geometry --------------------------------------------------------
    def _ensure_metrics(self):
        """Line heights from the real fonts, once per grid. The fonts come
        from QSS (the app's 10.5pt base, #CardTitle's weight, #CardMeta's
        9pt), which is not applied until polish - so this is lazy, and it
        is one polish per *grid*, not one per card, which is the whole of
        what the old per-card `ensurePolished` was costing."""
        if self._metrics is not None:
            return self._metrics
        self.ensurePolished()
        base = QFont(self.font())
        title_font = QFont(base)
        title_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(base)
        meta_font.setPointSizeF(9.0)
        chip_font = QFont(base)
        chip_font.setPointSizeF(8.0)
        chip_font.setWeight(QFont.Weight.Bold)
        title_line = QFontMetrics(title_font).lineSpacing()
        meta_line = QFontMetrics(meta_font).lineSpacing()
        chip_h = QFontMetrics(chip_font).height() + 4
        cell_w = self._cover_w + 2 * CARD_PADDING_X
        cell_h = (CARD_PADDING_TOP + self._cover_h + TEXT_GAP
                  + title_line * TITLE_LINES + TEXT_GAP + meta_line
                  + CARD_PADDING_BOTTOM)
        # The empty-cover slab, rendered once. Measured 23 August 2026:
        # with a drawRoundedRect per empty cell and a wrapped drawText
        # per title, the painted grid managed 100-118 paints/s - *below*
        # the widget grid it replaced - because thirty-odd text layouts
        # and antialiased paths per frame cost more than blitting. Text
        # is cached per record as QStaticText below; this is the same
        # idea for the placeholder.
        placeholder = QPixmap(self._cover_w, self._cover_h)
        placeholder.fill(Qt.GlobalColor.transparent)
        slab = QPainter(placeholder)
        slab.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        slab.setPen(Qt.PenStyle.NoPen)
        slab.setBrush(QColor(theme.SURFACE))
        slab.drawRoundedRect(QRectF(0, 0, self._cover_w, self._cover_h),
                             theme.RADIUS, theme.RADIUS)
        slab.end()
        self._chips.clear()
        self._metrics = {
            "title_font": title_font, "meta_font": meta_font,
            "chip_font": chip_font, "title_line": title_line,
            "meta_line": meta_line, "chip_h": chip_h,
            "cell_w": cell_w, "cell_h": cell_h, "placeholder": placeholder,
        }
        return self._metrics

    def _static_text(self, record, key, text, font, width, centre=True):
        """A record's laid-out text, built once and drawn many times.
        QStaticText caches its layout against the font it was prepared
        with, so a scroll frame pays a blit per title, not a re-wrap."""
        static = record.get(key)
        if static is None:
            static = QStaticText(text)
            static.setTextFormat(Qt.TextFormat.PlainText)
            option = QTextOption(Qt.AlignmentFlag.AlignHCenter if centre
                                 else Qt.AlignmentFlag.AlignLeft)
            option.setWrapMode(QTextOption.WrapMode.WordWrap)
            static.setTextOption(option)
            static.setTextWidth(float(width))
            static.prepare(font=font)
            record[key] = static
        return static

    def cell_size(self) -> QSize:
        m = self._ensure_metrics()
        return QSize(m["cell_w"], m["cell_h"])

    def _grid_width(self) -> int:
        """The width cells may use - the viewport less the scrollbar the
        widget paints for itself, so a cell is never drawn under it."""
        return max(1, self.width() - (BAR_WIDTH if self._overflowing() else 0))

    def _columns_for(self, width: int) -> int:
        m = self._ensure_metrics()
        span = m["cell_w"] + GRID_SPACING
        return max(1, (width + GRID_SPACING) // span)

    def columns(self) -> int:
        return self._columns

    def _overflowing(self) -> bool:
        return self._content_h > self.height()

    def _relayout(self):
        """Content height from count and columns - the one place geometry
        is decided, and it depends on nothing a cover or a hover can
        change. The widget itself does not resize: it is the viewport."""
        m = self._ensure_metrics()
        # Columns are worked out against the width a scrollbar would
        # leave, then checked once more: a list that only overflows
        # *because* the bar narrowed it is not a real second column loss.
        self._columns = self._columns_for(self._grid_width())
        rows = (len(self._records) + self._columns - 1) // self._columns
        self._content_h = max(0, rows * (m["cell_h"] + GRID_SPACING) - GRID_SPACING)
        self._motion.maximum = float(max(0, self._content_h - self.height()))
        if self._motion.pos > self._motion.maximum:
            self._motion.set_position(self._motion.maximum)
        self.update()
        self.scrolled.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._motion.frame_s = 0.0      # may have moved to another panel
        self._relayout()

    def _refresh_interval(self) -> float:
        """One display refresh in seconds, for FrameMotion.step.

        Read from the screen this widget is actually on, not a constant:
        the panel here runs at 144Hz but was found sitting at 60Hz on
        24 August 2026, and a motion model that assumes one while the
        panel does the other is wrong on every frame. Cached, and thrown
        away on resize because that is when a window changes screens."""
        try:
            screen = self.screen()
            rate = screen.refreshRate() if screen is not None else 0.0
        except Exception:
            rate = 0.0
        return 1.0 / rate if rate and rate > 0 else 0.0

    # ---- scrolling -------------------------------------------------------
    def scroll_offset(self) -> int:
        return int(round(self._motion.pos))

    def max_offset(self) -> int:
        return int(round(self._motion.maximum))

    def set_scroll_offset(self, value):
        self._motion.set_position(value)
        self.update()
        self.scrolled.emit()

    def reset_scroll(self):
        self.set_scroll_offset(0)

    def _notch_px(self) -> float:
        return max(self.NOTCH_FLOOR_PX, self.height() * self.NOTCH_FRACTION)

    def wheelEvent(self, event):
        """A notch is an impulse, not a jump - see FrameMotion.

        Trackpad pixel-delta scrolling is already 1:1 with the finger and
        is applied straight; Ctrl/Shift belong to zoom and horizontal
        scrolling and are left for anything above to use. A notch against
        an end the view is already hard against is *not* consumed, so it
        reaches whatever scrolls behind this."""
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            event.ignore()
            return
        pixels = event.pixelDelta().y()
        if pixels:
            self.set_scroll_offset(self._motion.pos - pixels)
            event.accept()
            return
        steps = event.angleDelta().y() / 120.0
        if not steps or self._motion.maximum <= 0:
            event.ignore()
            return
        direction = -1 if steps > 0 else 1
        at_end = ((direction < 0 and self._motion.pos <= 0)
                  or (direction > 0 and self._motion.pos >= self._motion.maximum))
        if at_end and not self._motion.running():
            event.ignore()
            return
        self._motion.kick(abs(steps) * self._notch_px(), direction)
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        page = max(1.0, self.height() * 0.9)
        key = event.key()
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            self.set_scroll_offset(self._motion.pos
                                   + (page if key == Qt.Key.Key_PageDown else -page))
        elif key == Qt.Key.Key_Home:
            self.set_scroll_offset(0)
        elif key == Qt.Key.Key_End:
            self.set_scroll_offset(self._motion.maximum)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self._motion.kick(self._notch_px(), 1 if key == Qt.Key.Key_Down else -1)
            self.update()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ---- records ---------------------------------------------------------
    def set_items(self, records, keep_position=False):
        self._records = [dict(r) for r in records]
        self._requested.clear()
        self._hover = -1
        if not keep_position:
            self._motion.set_position(0)
        self._relayout()

    def append_items(self, records):
        """Append without touching anything already placed: the content
        grows downward and every existing cell keeps its rect."""
        if not records:
            return
        self._records.extend(dict(r) for r in records)
        self._relayout()

    def count(self) -> int:
        return len(self._records)

    def record(self, index: int):
        return self._records[index] if 0 <= index < len(self._records) else None

    def set_cover(self, index: int, pixmap):
        if not (0 <= index < len(self._records)):
            return
        self._records[index]["pixmap"] = pixmap
        # Whole frames, not cells: this surface draws the visible window
        # of records from scratch each time, and a frame is ~1.3ms, so a
        # cover landing costs one frame.
        if self._cell_in_view(index):
            self.update()

    def mark_saved(self, index: int, saved: bool = True):
        if not (0 <= index < len(self._records)):
            return
        self._records[index]["saved"] = bool(saved)
        if self._cell_in_view(index):
            self.update()

    # ---- geometry helpers ------------------------------------------------
    def cell_rect(self, index: int) -> QRect:
        """In content space - y is measured from the top of all the rows,
        not from the top of the viewport."""
        m = self._ensure_metrics()
        row, col = divmod(index, max(1, self._columns))
        x = col * (m["cell_w"] + GRID_SPACING)
        y = row * (m["cell_h"] + GRID_SPACING)
        return QRect(x, y, m["cell_w"], m["cell_h"])

    def _cell_in_view(self, index: int) -> bool:
        m = self._ensure_metrics()
        row = index // max(1, self._columns)
        top = row * (m["cell_h"] + GRID_SPACING) - self._motion.pos
        return top < self.height() and top + m["cell_h"] > 0

    def index_at(self, point: QPoint) -> int:
        """From a point in *viewport* coordinates."""
        m = self._ensure_metrics()
        span_x = m["cell_w"] + GRID_SPACING
        span_y = m["cell_h"] + GRID_SPACING
        y = point.y() + self._motion.pos
        col, rem_x = divmod(point.x(), span_x)
        row, rem_y = divmod(int(y), span_y)
        if col >= self._columns or rem_x >= m["cell_w"] or rem_y >= m["cell_h"]:
            return -1
        index = int(row) * self._columns + int(col)
        return index if 0 <= index < len(self._records) else -1

    # ---- painting --------------------------------------------------------
    def paintEvent(self, event):
        """One frame: advance the motion by this frame's own timestamp,
        draw the cells that are in view at the resulting offset, and ask
        for another frame while there is still motion.

        This ordering is the whole fix (see the module docstring): the
        position belongs to the frame that shows it, so the steps the eye
        integrates are even by construction."""
        moving = self._motion.running()
        if moving:
            if not self._motion.frame_s:
                self._motion.frame_s = self._refresh_interval()
            moving = self._motion.step()
        m = self._ensure_metrics()
        offset = self._motion.pos
        painter = QPainter(self)
        # Text antialiasing only: shape antialiasing is switched on just
        # around the hover ring and the scrollbar, which are the paths
        # drawn per frame.
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), self._ground)
        if self._records:
            span_y = m["cell_h"] + GRID_SPACING
            first_row = max(0, int(offset) // span_y)
            last_row = int(offset + self.height()) // span_y
            first = first_row * self._columns
            last = min(len(self._records), (last_row + 1) * self._columns)
            missing = []
            for index in range(first, last):
                rect = self.cell_rect(index)
                rect.moveTop(rect.top() - int(offset))
                self._paint_cell(painter, m, index, rect)
                if (self._records[index].get("pixmap") is None
                        and index not in self._requested):
                    missing.append(index)
            # Covers for the cells on screen and a little past them, asked
            # for off the paint path: a signal handler that submits to a
            # pool is not paint work and must not run inside it. The rows
            # ahead are skipped entirely while the view is moving fast -
            # see COVER_ASK_PER_FRAME for what that was measured costing.
            if abs(self._motion.vel) <= FAST_SCROLL_PX_S:
                ahead_last = min(len(self._records),
                                 last + PREFETCH_ROWS * self._columns)
                for index in range(last, ahead_last):
                    if (self._records[index].get("pixmap") is None
                            and index not in self._requested):
                        missing.append(index)
            missing = missing[:COVER_ASK_PER_FRAME]
        else:
            missing = []
        self._paint_scrollbar(painter)
        painter.end()
        if missing:
            for index in missing:
                self._requested.add(index)
            QTimer.singleShot(0, lambda ids=tuple(missing): self._ask(ids))
        if moving:
            # The next frame.
            self.update()
            self.scrolled.emit()

    def _ask(self, ids):
        for index in ids:
            if 0 <= index < len(self._records) \
                    and self._records[index].get("pixmap") is None:
                self.needs_cover.emit(index)

    def _paint_scrollbar(self, painter):
        """The grid's own bar, since it is not inside a QScrollArea.
        Drawn only when there is something to scroll."""
        if not self._overflowing():
            return
        track_h = self.height() - 2 * BAR_MARGIN
        if track_h <= 0:
            return
        content = max(1.0, float(self._content_h))
        thumb_h = max(BAR_MIN_THUMB, track_h * self.height() / content)
        span = track_h - thumb_h
        travel = (self._motion.pos / self._motion.maximum) if self._motion.maximum > 0 else 0.0
        top = BAR_MARGIN + span * max(0.0, min(1.0, travel))
        x = self.width() - BAR_WIDTH + BAR_MARGIN
        w = BAR_WIDTH - 2 * BAR_MARGIN
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.rgba(theme.TEXT_MUTED,
                                           110 if (self._bar_hover or self._drag_from) else 70)))
        painter.drawRoundedRect(QRectF(x, top, w, thumb_h), w / 2.0, w / 2.0)
        painter.restore()

    def _thumb_rect(self) -> QRectF:
        track_h = self.height() - 2 * BAR_MARGIN
        content = max(1.0, float(self._content_h))
        thumb_h = max(BAR_MIN_THUMB, track_h * self.height() / content)
        span = track_h - thumb_h
        travel = (self._motion.pos / self._motion.maximum) if self._motion.maximum > 0 else 0.0
        top = BAR_MARGIN + span * max(0.0, min(1.0, travel))
        return QRectF(self.width() - BAR_WIDTH, top, BAR_WIDTH, thumb_h)

    def _paint_cell(self, painter, m, index, rect):
        record = self._records[index]
        if index == self._hover:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(theme.ACCENT), 1))
            painter.setBrush(QColor(theme.ACCENT_SOFT))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                                    theme.RADIUS, theme.RADIUS)
            painter.restore()

        cover_x = rect.x() + CARD_PADDING_X
        cover_y = rect.y() + CARD_PADDING_TOP
        pixmap = record.get("pixmap")
        if pixmap is None or pixmap.isNull():
            pixmap = m["placeholder"]
        painter.drawPixmap(cover_x, cover_y, pixmap)

        rating = record.get("rating") or ""
        if rating:
            chip = self._chip(m, rating, accent=False)
            painter.drawPixmap(cover_x + 8,
                               cover_y + self._cover_h - 8 - m["chip_h"], chip)
        if record.get("saved"):
            chip = self._chip(m, "Saved", accent=True)
            painter.drawPixmap(cover_x + 8, cover_y + 8, chip)

        text_x = rect.x() + CARD_PADDING_X
        text_w = self._cover_w
        title_y = cover_y + self._cover_h + TEXT_GAP
        title_h = m["title_line"] * TITLE_LINES
        painter.setFont(m["title_font"])
        painter.setPen(QColor(theme.TEXT))
        title = self._static_text(record, "_title_static",
                                  record.get("title") or "",
                                  m["title_font"], text_w)
        painter.save()
        painter.setClipRect(QRect(text_x, title_y, text_w, title_h))
        painter.drawStaticText(QPoint(text_x, title_y), title)
        painter.restore()

        year = record.get("year") or ""
        if year:
            meta_y = title_y + title_h + TEXT_GAP
            painter.setFont(m["meta_font"])
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawStaticText(
                QPoint(text_x, meta_y),
                self._static_text(record, "_year_static", year,
                                  m["meta_font"], text_w))

    def _chip(self, m, text, accent):
        """A rating or Saved chip, rendered once and blitted after that.

        **Measured 24 August 2026:** drawn live, each chip cost a
        `fontMetrics().horizontalAdvance` and a `drawText` *per cell per
        frame* - about thirty text layouts a frame between the two chips,
        and the paint measured 4.6ms median against the 6.94ms the whole
        frame has. Ratings repeat across a catalogue ("★ 7.4") and
        "Saved" is one string for every cell, so the cache is small and
        nearly always warm."""
        key = (text, bool(accent))
        chip = self._chips.get(key)
        if chip is not None:
            return chip
        ratio = self.devicePixelRatioF() or 1.0
        metrics = QFontMetrics(m["chip_font"])
        width = metrics.horizontalAdvance(text) + (16 if accent else 14)
        height = m["chip_h"]
        chip = QPixmap(int(width * ratio), int(height * ratio))
        chip.setDevicePixelRatio(ratio)
        chip.fill(Qt.GlobalColor.transparent)
        painter = QPainter(chip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_chip(painter, m, text, 0, 0, accent, False,
                         width=width)
        painter.end()
        if len(self._chips) > 256:
            self._chips.clear()
        self._chips[key] = chip
        return chip

    def _paint_chip(self, painter, m, text, x, y, accent, anchor_bottom,
                    width=None):
        painter.setFont(m["chip_font"])
        if width is None:
            width = painter.fontMetrics().horizontalAdvance(text) + (16 if accent else 14)
        height = m["chip_h"]
        top = y - height if anchor_bottom else y
        rect = QRectF(x, top, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        if accent:
            # The app's own accent ramp, gold to amber - the same two
            # stops theme.accent_gradient() writes into the stylesheet,
            # so a painted chip matches every QSS one.
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, QColor(theme.ACCENT))
            gradient.setColorAt(1.0, QColor(theme.ACCENT_BLUE))
            painter.setBrush(gradient)
        else:
            painter.setBrush(QColor(theme.rgba(theme.BG, 200)))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawRoundedRect(rect, height / 2.0, height / 2.0)
        painter.restore()
        painter.setPen(QColor(theme.ON_ACCENT if accent else theme.TEXT))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)

    # ---- pointer ---------------------------------------------------------
    def _set_hover(self, index):
        if index == self._hover:
            return
        old, self._hover = self._hover, index
        del old
        if index >= 0:
            hold_hover_cursor(self)
        else:
            release_hover_cursor(self)
        self.update()

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._drag_from is not None:
            # Dragging the painted thumb: the pointer's travel down the
            # track maps to the content's travel past the viewport.
            start_y, start_pos = self._drag_from
            track_h = self.height() - 2 * BAR_MARGIN
            thumb_h = max(BAR_MIN_THUMB,
                          track_h * self.height() / max(1.0, float(self._content_h)))
            span = max(1.0, track_h - thumb_h)
            self.set_scroll_offset(start_pos
                                   + (point.y() - start_y) * self._motion.maximum / span)
            event.accept()
            return
        over_bar = self._overflowing() and point.x() >= self.width() - BAR_WIDTH
        if over_bar != self._bar_hover:
            self._bar_hover = over_bar
            self.update()
        self._set_hover(-1 if over_bar else self.index_at(point))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_hover(-1)
        if self._bar_hover:
            self._bar_hover = False
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            if self._overflowing() and point.x() >= self.width() - BAR_WIDTH:
                thumb = self._thumb_rect()
                if thumb.contains(QRectF(point.x(), point.y(), 1, 1).topLeft()):
                    self._drag_from = (point.y(), self._motion.pos)
                else:
                    # A press on the track jumps a screenful that way,
                    # which is what a QScrollBar does.
                    page = self.height() * 0.9
                    self.set_scroll_offset(
                        self._motion.pos + (page if point.y() > thumb.top() else -page))
                event.accept()
                return
            index = self.index_at(point)
            if index >= 0:
                # A press stops a glide, the same way grabbing a
                # scrollbar does - opening a title from under a moving
                # finger is not what was aimed at.
                self._motion.stop()
                self.clicked.emit(index)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)
