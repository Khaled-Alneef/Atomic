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
from PyQt6.QtGui import (QColor, QCursor, QFont, QFontMetrics,
                         QLinearGradient, QPainter, QPen, QPixmap,
                         QStaticText, QTextOption)

from PyQt6.QtWidgets import QApplication, QWidget

from . import theme
from .widgets import (_vblank_ticker_for_use, hold_hover_cursor,
                      release_hover_cursor)

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

# **How fast the view catches up with a dragged scrollbar.**
#
# The owner, 25 August 2026: "while dragging the scrollbar in low-mid
# speed it shows that all items on the screen are moving in steps". He
# is right, and it is not judder - it is the pointer's own sample rate
# arriving on a 240Hz panel. Measured that day on the real Anime grid,
# a constant-velocity drag with the offset sampled once per compositor
# present:
#
#     pointer at 125Hz   125.0 moving fps   47.9% of refreshes dead
#                        9.00px steps (even: local change 10%)
#     pointer at 240Hz   233.5 moving fps    2.7% dead, 5.00px steps
#
# So every second refresh showed the previous frame again and the
# content moved in 9px jumps instead of 4.9px ones. Nothing was
# stuttering; there were simply half as many positions as refreshes,
# because `mouseMoveEvent` wrote the position straight through and an
# ordinary mouse reports 125 times a second.
#
# The view now *follows* the pointer instead of tracking it exactly: the
# drag sets a target and every refresh moves a fixed fraction of the way
# there, so a frame exists for every refresh whatever the mouse reports
# at. The fraction comes from a time constant so it is the same feel at
# any refresh rate. 12ms puts the view within a pixel of the pointer in
# about three refreshes at 240Hz - a lag of roughly 8ms, which is under
# one 125Hz mouse sample and cannot be felt on a drag.
DRAG_FOLLOW_TAU_S = 0.012
# Below this the follow is over and the target is dropped, so a settled
# drag stops asking for frames.
DRAG_SETTLE_PX = 0.4

# How many composited cards to keep (see _cell_pixmap). A screenful is
# about 32 at this size and PREFETCH_ROWS adds two rows either way, so
# this holds the view and its margins without keeping bitmaps for a
# 500-entry library nobody is looking at. Each is one cell of ARGB.
CELL_CACHE = 120

# How many of those to build ahead per frame - see paintEvent.
CELL_BUILD_PER_FRAME = 2

# The scrollbar this widget paints for itself, since it is not inside a
# QScrollArea any more.
#
# **These are the app stylesheet's own scrollbar numbers, and that is
# the point - the owner, 24 August 2026: "change the scrollbar in the
# manga/manhwa/manhua/anime/series/movies to make it look like the
# scrollbar in the discovery page and the main page".** Those pages are
# ordinary QScrollAreas and get theme.py's `QScrollBar` rules; this
# widget owns its scrolling, so it paints its own, and the two had
# drifted into different objects: an 11px groove inset 3px each side
# gave a **5px** translucent grey pill here against the stylesheet's
# full-width 11px SURFACE_HOVER bar that turns ACCENT under the pointer.
# Read off theme so they cannot drift again.
BAR_WIDTH = theme.SCROLLBAR_WIDTH
# Top and bottom only, matching `QScrollBar:vertical { margin: 2px 0 }`.
# The handle fills the groove's width - the stylesheet gives it no
# left/right inset, and that is most of what made these look unalike.
BAR_MARGIN = 2
# `QScrollBar::handle:vertical { min-height: 28px }`.
BAR_MIN_THUMB = 28
# `border-radius: 5px` - a rounded rectangle, not a capsule. At the old
# 5px width the two were the same shape; at 11 they are not.
BAR_RADIUS = 5


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
    # **Friction 34, not 7, and acceleration off** - the owner's ask,
    # 24 August 2026: "remove the scrolling drift in the whole app", and
    # in the same breath "fix the smear". This **supersedes** the earlier
    # instruction the same day that stop-on-release was to be "ONLY IN
    # READER MODE"; the reader simply got there first, and its numbers
    # (windows.reader.READER_WHEEL_FRICTION) are the ones adopted here.
    #
    # Why these two constants and not MAX_SPEED, which was the first
    # candidate. Smear on a sample-and-hold panel is velocity x frame
    # hold time, and during a sustained scroll the average velocity is
    #
    #     distance_per_notch x notches_per_second
    #
    # in which FRICTION cancels out entirely. Capping MAX_SPEED does not
    # reduce that average - it only defers distance into `_pending`,
    # which then drains after the hand stops. That deferred distance IS
    # drift, so the cap cannot be the answer to a request that asks for
    # both. Acceleration can: it inflated a sustained scroll from the
    # 0.0924-of-a-viewport notch to a measured ~115px, and removing it
    # takes the velocity down with the travel per notch left alone.
    # **The whole app now scrolls on the reader's profile** - the
    # owner, 25 August 2026: "make the rest same as the reader". The
    # reader was hand-tuned on 24 August to remove coasting entirely
    # ("remove the mouse drift ENTIRELY"), and it kept that profile to
    # itself while the pages ran a softer one. These four numbers are
    # windows.reader's, verbatim:
    #
    #     notch      WHEEL_STEP_PX        76px, fixed, not a fraction
    #     friction   READER_WHEEL_FRICTION  50
    #     ramp       READER_WHEEL_RAMP      60
    #     cap        max_speed              none
    #
    # A fixed notch rather than a fraction of the viewport is part of
    # it: the reader travels the same distance per notch whatever the
    # window height, and matching the feel means matching that too.
    FRICTION = 50.0
    RAMP = 60.0
    # **3200, matched to widgets._Momentum.MAX_SPEED** - the owner's ask
    # of 25 August 2026 to keep cards from crossing too much ground
    # between two refreshes. Measured on this surface, 180 cards, 240Hz
    # panel, by timing its own paintEvent:
    #
    #     medium scroll   cap 5200: max step 14px   cap 3200: max step 14px
    #     fast flick      cap 5200: max step 43px   cap 3200: max step 14px
    #     frames with no movement, fast: 26-27% -> 19-22%
    #
    # An ordinary scroll never reaches either cap; a flick reached 43px
    # of ground between two frames, which is the smear. Frame pacing is
    # untouched by it (median gap 4.06-4.13ms, p95 4.31-4.45ms, in every
    # arm) - this changes what is drawn, not when.
    MAX_SPEED = math.inf
    # See widgets._Momentum.STOP_SPEED, including the one-pixel-per-
    # refresh floor that was tried here and removed the same day.
    STOP_SPEED = 30.0
    ACCEL_WINDOW_S = 0.25
    ACCEL_NOTCHES = 5
    ACCEL_MAX = 1.0
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
        # Where the refresh grid is anchored - see step(). Re-anchored
        # every time motion restarts, so a long idle cannot let QPC and
        # the panel's own raster drift apart.
        self._phase = None
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
        self._phase = None

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
            # **Snap to the refresh GRID, not by a whole refresh each
            # time.** The first version of this added exactly one frame
            # of motion per paint, which is only right if paints happen
            # once per refresh. They do not. Measured 24 August 2026 with
            # the app's paints and the compositor's presents on one
            # shared QPC clock - the first measurement that ever put both
            # on the same timeline:
            #
            #     app paints        137.4/s   interval median 5.80ms
            #     DWM presents      109.5/s
            #
            # The widget free-runs: update() at the end of paintEvent
            # asks for the next frame immediately and Qt's raster path
            # does not block on vsync, so paints arrive FASTER than the
            # panel refreshes. Adding a whole frame of motion to each
            # such paint beat against the presents, and that is what
            # produced steps of 14px and 28px in the same run at a
            # dead-constant velocity.
            #
            # It did *not* change how fast the page travels - that was
            # 2281 px/s before and 2289 after, and the excess motion was
            # absorbed by the presents that showed nothing new. The
            # defect was the unevenness, not the speed.
            #
            # Anchoring to a fixed phase fixes both: two paints inside
            # one refresh get the SAME timestamp, so the second moves
            # nothing and the panel still shows exactly one step per
            # refresh; paints in consecutive refreshes are exactly one
            # frame apart by construction.
            if self._phase is None:
                self._phase = now
            snapped = self._phase + round((now - self._phase) / self.frame_s) * self.frame_s
            # Never let the snap walk backwards past the last frame.
            now = max(snapped, self._last)
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
            self._phase = None
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
    # **0.0647 - 30% slower again, the owner, 24 August 2026: "make the
    # scrolling in the whole app 30% slower (do NOT change the reading
    # viewer)". 0.0924 x 0.70; the reader's own notch
    # (windows.reader.WHEEL_STEP_PX) took its 30% separately and is
    # deliberately untouched here.**
    # **0.0800, raised 24% on 25 August 2026** - the owner again, the
    # other way this time: "Raise scroll distance somewhat - 0.0647 is
    # extremely conservative". Both of his asks are kept here on
    # purpose, because they pull against each other and the next person
    # to touch this needs to see that: 0.0647 came from two rounds of
    # "make the scroll slower" on 24 August, and this is one step back
    # from the second of them, not a return to where it started.
    # Zero on purpose: the notch is the reader's flat 76px now (see
    # NOTCH_FLOOR_PX), and `max(floor, height * fraction)` with a zero
    # fraction is exactly that. Kept as a constant rather than deleted
    # because scroll_area's `notch_scale` still multiplies both, which
    # is what keeps Home's own 0.7 working.
    NOTCH_FRACTION = 0.0
    NOTCH_FLOOR_PX = 76

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
        self._cells = {}                # index -> composited card, see _cell_pixmap
        self._drag_from = None          # scrollbar thumb drag anchor
        # Where a drag wants the view to be, while the frame clock
        # closes the gap - see DRAG_FOLLOW_TAU_S. None when no drag is
        # settling.
        self._drag_target = None
        # Whether this grid currently holds the shared vblank ticker -
        # see _hold_vblank for the measurement that put it there.
        self._vblank_on = False
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
        # Cut at devicePixelRatio and tagged, like every other pixmap
        # this widget draws: at 1.25 an untagged slab is stretched by Qt
        # and its rounded corners go soft, which reads as a blurry card
        # sitting beside sharp ones.
        slab_ratio = self.devicePixelRatioF() or 1.0
        placeholder = QPixmap(int(self._cover_w * slab_ratio),
                              int(self._cover_h * slab_ratio))
        placeholder.setDevicePixelRatio(slab_ratio)
        placeholder.fill(Qt.GlobalColor.transparent)
        slab = QPainter(placeholder)
        slab.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        slab.setPen(Qt.PenStyle.NoPen)
        slab.setBrush(QColor(theme.SURFACE))
        slab.drawRoundedRect(QRectF(0, 0, self._cover_w, self._cover_h),
                             theme.RADIUS, theme.RADIUS)
        slab.end()
        self._chips.clear()
        self._cells.clear()
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
        # Every composited card is keyed by index into the list that just
        # went away - keeping them would paint the previous page's cards.
        self._cells.clear()
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
        self._forget_cell(index)
        # Whole frames, not cells: this surface draws the visible window
        # of records from scratch each time, and a frame is ~1.3ms, so a
        # cover landing costs one frame.
        if self._cell_in_view(index):
            self.update()

    def mark_saved(self, index: int, saved: bool = True):
        if not (0 <= index < len(self._records)):
            return
        self._records[index]["saved"] = bool(saved)
        self._forget_cell(index)
        if self._cell_in_view(index):
            self.update()

    # ---- geometry helpers ------------------------------------------------
    def _left_margin(self) -> int:
        """What centres the rows: half of whatever width the columns do
        not use - the owner's ask, 24 August 2026 ("make the rows in the
        category pages in the mid, just like the genre page"). The grid
        used to start every row at x=0 and leave all the slack on the
        right, which reads as the page leaning left the moment the
        window is wider than an exact number of columns."""
        m = self._ensure_metrics()
        used = (self._columns * (m["cell_w"] + GRID_SPACING)) - GRID_SPACING
        return max(0, (self._grid_width() - used) // 2)

    def cell_rect(self, index: int) -> QRect:
        """In content space - y is measured from the top of all the rows,
        not from the top of the viewport."""
        m = self._ensure_metrics()
        row, col = divmod(index, max(1, self._columns))
        x = self._left_margin() + col * (m["cell_w"] + GRID_SPACING)
        y = row * (m["cell_h"] + GRID_SPACING)
        return QRect(x, y, m["cell_w"], m["cell_h"])

    def _visible_range(self, offset, m):
        """The half-open index range this frame has to draw. A hook, so
        PosterStrip can answer for one sideways row without a second
        copy of paintEvent - the numbers here are exactly what was
        inline before it existed."""
        span_y = m["cell_h"] + GRID_SPACING
        first_row = max(0, int(offset) // span_y)
        last_row = int(offset + self.height()) // span_y
        return (first_row * self._columns,
                min(len(self._records), (last_row + 1) * self._columns))

    def _cell_viewport_rect(self, index, offset):
        """`cell_rect` moved from content space into the viewport."""
        rect = self.cell_rect(index)
        rect.moveTop(rect.top() - int(offset))
        return rect

    def _prefetch_band(self) -> int:
        """How many records past the visible range to warm."""
        return PREFETCH_ROWS * self._columns

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
        x = point.x() - self._left_margin()
        if x < 0:
            return -1
        col, rem_x = divmod(x, span_x)
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
        # **A hover is only real while the pointer is genuinely inside.**
        # A Leave is not guaranteed: clicking a card opens the details
        # page or the player *over* this grid, which covers it without
        # the pointer ever crossing its edge, so the card kept its play
        # button lit and still had it on the way back (the owner's
        # screenshot, 26 August 2026 - a Watching card showing Continue
        # with the mouse nowhere near it). The same class of bug this
        # file's siblings record: ask where the pointer actually is
        # rather than trusting the event that should have arrived
        # (.claude/rules/ui.md).
        #
        # underMouse(), not widgetAt(QCursor.pos()): Qt already tracks
        # this per widget, so it is a flag read rather than a hit test
        # of the whole tree, and this runs on every paint.
        if self._hover >= 0:
            if not self.underMouse():
                self._set_hover(-1)
            else:
                # **And when the pointer *is* inside, check it is inside
                # the cell that is lit.** underMouse() only answers "is
                # it anywhere in this grid", so closing the player or
                # the details page over a card and coming back with the
                # cursor still over the grid - but over a different
                # cell, or over the gap between two - left the old
                # card's play button lit until the mouse was moved.
                # That is the owner's screenshot of 26 August 2026, and
                # it is the half of this the first fix did not cover.
                #
                # index_at is arithmetic on the scroll position, not a
                # hit test of the widget tree, and this only runs while
                # something is actually lit.
                here = self.mapFromGlobal(QCursor.pos())
                actual = (self.index_at(here)
                          if self.rect().contains(here) else -1)
                if actual != self._hover:
                    self._set_hover(actual)
        moving = self._motion.running()
        if moving:
            if not self._motion.frame_s:
                self._motion.frame_s = self._refresh_interval()
            moving = self._motion.step()
        elif self._drag_target is not None:
            # Following a dragged scrollbar - see DRAG_FOLLOW_TAU_S.
            # Inside the frame that draws it, for the same reason the
            # momentum model is: the position belongs to the frame that
            # shows it, so the steps are even by construction.
            frame_s = self._motion.frame_s or self._refresh_interval()
            if not frame_s:
                frame_s = 1.0 / 60.0
                self._motion.frame_s = frame_s
            gap = self._drag_target - self._motion.pos
            if abs(gap) <= DRAG_SETTLE_PX:
                self._motion.pos = self._drag_target
                self._drag_target = None
                self.scrolled.emit()
            else:
                self._motion.pos += gap * (
                    1.0 - math.exp(-frame_s / DRAG_FOLLOW_TAU_S))
                moving = True
                self.scrolled.emit()
        # **Everything cached in device pixels is invalid the moment the
        # widget's ratio changes** - the window was dragged to the other
        # monitor. The composited cells, the chips and the placeholder
        # were all cut at the old ratio, and drawing them tagged 1.25
        # onto a 1.0 surface is a 0.8x nearest-neighbour scale on every
        # frame: pixelated *and* shimmering while it scrolls, which is
        # the owner's "the text and the images seems really stiff and
        # they leave a trace". Covers are re-requested too - they were
        # cut for the old screen by images.thumbnail_or_avatar, and the
        # ratio it cuts for has just been repointed at the new one
        # (images.set_device_ratio follows the window).
        ratio_now = self.devicePixelRatioF() or 1.0
        if getattr(self, "_cache_ratio", None) != ratio_now:
            self._cache_ratio = ratio_now
            self._metrics = None
            self._cells.clear()
            self._chips.clear()
            self._requested.clear()
            for record in self._records:
                record["pixmap"] = None
        m = self._ensure_metrics()
        offset = self._motion.pos
        painter = QPainter(self)
        # Text antialiasing only: shape antialiasing is switched on just
        # around the hover ring and the scrollbar, which are the paths
        # drawn per frame.
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), self._ground)
        if self._records:
            first, last = self._visible_range(offset, m)
            missing = []
            for index in range(first, last):
                rect = self._cell_viewport_rect(index, offset)
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
                ahead_last = min(len(self._records), last + self._prefetch_band())
                for index in range(last, ahead_last):
                    if (self._records[index].get("pixmap") is None
                            and index not in self._requested):
                        missing.append(index)
            missing = missing[:COVER_ASK_PER_FRAME]
            # Cards to compose *before* they are needed. A row entering
            # view is 8 uncached cells at 0.189ms each = 1.51ms landing
            # on one frame (measured), which is what kept the paint's p95
            # at 6.7ms while its median fell to 3.5. Building a couple a
            # frame ahead of the edge spreads that: a row arrives every
            # ~9 frames at a normal wheel pace, so two a frame stays in
            # front of it and costs 0.38ms.
            band = self._prefetch_band()
            ahead = []
            for index in range(max(0, first - band),
                               min(len(self._records), last + band)):
                if index not in self._cells:
                    ahead.append(index)
                    if len(ahead) >= CELL_BUILD_PER_FRAME:
                        break
        else:
            missing = []
            ahead = []
        self._paint_scrollbar(painter)
        painter.end()
        if missing:
            for index in missing:
                self._requested.add(index)
            QTimer.singleShot(0, lambda ids=tuple(missing): self._ask(ids))
        if ahead:
            QTimer.singleShot(
                0, lambda ids=tuple(ahead), mm=m: self._precompose(mm, ids))
        if moving:
            self._schedule_frame()
            self.scrolled.emit()
        elif self._vblank_on:
            self._release_vblank()

    def _schedule_frame(self):
        """Ask for the next frame when the panel can actually show one.

        `update()` on its own asks for it *immediately*, and Qt's raster
        path does not block on vsync, so this widget free-ran. Measured
        24 August 2026 with the app's paints and the compositor's
        presents on one shared QPC clock:

            paints 137.4/s, interval median 5.80ms   presents 109.5/s
            after snapping the motion to the refresh grid:
            paints 167.9/s, interval median 5.04ms   presents 124.9/s

        Every paint above the present rate is thrown away - at ~3.1ms
        each that was over a tenth of the machine's time drawing frames
        nobody saw - and worse, painting at a rate the panel does not
        share is what beat against it and produced steps of 17px and 35px
        in the same run at a constant velocity.

        Sleeping to the next grid boundary instead. The timer's own
        jitter cannot hurt the position: `FrameMotion.step` snaps to the
        same grid, so a frame that lands late still carries exactly the
        motion its refresh is owed. That is the difference from the 6ms
        QTimer this module's docstring warns about - that one *drove* the
        position, this one only decides when to draw it."""
        if self._hold_vblank():
            return          # the panel itself asks for the next frame
        frame_s = self._motion.frame_s
        if frame_s <= 0.0:
            self.update()
            return
        phase = self._motion._phase
        if phase is None:
            self.update()
            return
        ahead = frame_s - ((time.perf_counter() - phase) % frame_s)
        QTimer.singleShot(max(0, int(round(ahead * 1000.0))),
                          Qt.TimerType.PreciseTimer, self.update)

    def _hold_vblank(self) -> bool:
        """Draw the next frame on the panel's own vblank instead of a
        millisecond timer. True when that clock is available.

        **Why, and it is not a preference - measured 24 August 2026, the
        grid driven three times with everything else identical:**

            run 1   236.1 steps/s,  1.6% of refreshes dead
            run 2    65.2 steps/s, 72.9% dead
            run 3   235.2 steps/s,  2.0% dead

        Two runs perfect, one off a cliff. That is not variance, it is
        the timer: `_schedule_frame` asks for 0-4ms and Windows rounds a
        request it has not been asked to honour up to ~13.9ms, which is
        72 frames a second - and 65 steps/s is that. Timer resolution is
        a **system-global** setting, so whether this widget is smooth
        depended on whether some other process on the machine happened
        to have raised it, which is exactly the shape of the owner's
        "sometimes the whole app seems lower in fps".

        helpers/startup.allow_precise_timers raises it for this process,
        and that is worth keeping - but a surface should not be one
        failed API call away from a third of its frames. The vblank
        ticker is the panel itself and cannot be quantised, so it is
        used where it exists and the timer stays as the fallback."""
        if self._vblank_on:
            return True
        ticker = _vblank_ticker_for_use()
        if ticker is None:
            return False
        ticker.tick.connect(self._on_vblank)
        ticker.acquire()
        self._vblank_on = True
        return True

    def _release_vblank(self):
        ticker = _vblank_ticker_for_use()
        try:
            if ticker is not None:
                ticker.tick.disconnect(self._on_vblank)
                ticker.release()
        except Exception:
            pass
        self._vblank_on = False

    def _on_vblank(self):
        # One repaint per refresh while the motion runs; paintEvent
        # releases the hold on the frame that stops.
        self.update()

    def hideEvent(self, event):
        # **Released here as well as when the motion stops.** Pages
        # rebuild from scratch on every visit, so a grid is routinely
        # hidden and deleted mid-glide - and a hold that is never given
        # back leaves the vblank thread awake for the life of the app,
        # which is the exact cost the gate exists to remove.
        if self._vblank_on:
            self._release_vblank()
        super().hideEvent(event)

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
        x = self.width() - BAR_WIDTH
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        # SURFACE_HOVER at rest, ACCENT under the pointer or mid-drag -
        # `QScrollBar::handle:vertical` and its `:hover` rule, which is
        # what every other scrolling surface in the app shows.
        painter.setBrush(QColor(theme.ACCENT if (self._bar_hover or self._drag_from)
                                else theme.SURFACE_HOVER))
        painter.drawRoundedRect(QRectF(x, top, BAR_WIDTH, thumb_h),
                                BAR_RADIUS, BAR_RADIUS)
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
        """One card: the hover ring live, everything else as one blit.

        **The owner, 24 August 2026:** *"when I move or scroll the text
        labels and image cards seems to be refreshing on a stiff way"*.
        He was describing the mechanism exactly. Every visible cell used
        to redraw its cover, its chips and both labels on every frame,
        and `drawStaticText` caches the text *layout*, not a bitmap - so
        the glyphs were re-rasterised and re-hinted at each new integer
        y, which is what "refreshing" looks like.

        Measured that day, removing one thing at a time from the paint
        (budget 6.94ms at 144Hz):

            everything          med 4.21ms   p95 6.71
            no labels           med 2.98ms   p95 5.65    <- text = 1.23ms
            no covers           med 3.19ms   p95 5.62    <- covers = 1.02ms
            no chips            med 4.10ms   p95 6.82    <- already cached
            fillRect only       med 1.55ms   p95 3.10    <- the floor

        Text was 29% of the frame. It is now composited into the cell
        once and blitted after that. The hover ring stays live and
        outside the cache, so pointing at a card does not rebuild it.

        Why this matters beyond the milliseconds: a paint that overruns
        the budget misses its vblank, and the motion model then correctly
        advances *two* refreshes on the next frame - a double-length
        step. The float step was measured dead even (median step-to-step
        change 0.0%) with the outliers landing at exactly 2x. So the
        judder was never the model; it was frames arriving late."""
        record = self._records[index]
        if index == self._hover:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(theme.ACCENT), 1))
            painter.setBrush(QColor(theme.ACCENT_SOFT))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                                    theme.RADIUS, theme.RADIUS)
            painter.restore()
        painter.drawPixmap(rect.topLeft(), self._cell_pixmap(m, index))

    def _cell_pixmap(self, m, index):
        """The card's cover, chips and labels, drawn once.

        Kept transparent outside the artwork so the hover ring painted
        underneath still shows through. Bounded: only what is on screen
        and a little past it is ever wanted, and an unbounded cache over
        a 500-entry library would be tens of megabytes of card bitmaps
        nobody is looking at."""
        cached = self._cells.get(index)
        if cached is not None:
            return cached
        record = self._records[index]
        dpr = self.devicePixelRatioF()
        size = self.cell_size()
        pixmap = QPixmap(int(size.width() * dpr), int(size.height() * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        # Smooth, because the cover tile may carry a different ratio
        # than this cell for one build (cut for the previous monitor,
        # not yet re-fetched). Scaling it nearest here bakes the
        # pixelation into the cached card; smooth costs nothing when
        # the ratios match, which is every steady-state frame.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRect(0, 0, size.width(), size.height())
        self._draw_cell_content(painter, m, record, rect)
        painter.end()
        if len(self._cells) >= CELL_CACHE:
            # Oldest first - dicts keep insertion order, and the cells
            # that fall out are the ones scrolled furthest away.
            for stale in list(self._cells)[:len(self._cells) - CELL_CACHE + 1]:
                del self._cells[stale]
        self._cells[index] = pixmap
        return pixmap

    def _forget_cell(self, index):
        self._cells.pop(index, None)

    def _precompose(self, m, ids):
        """Build cards just off the edge of the view, off the paint path.

        Posted with singleShot like the cover requests are - and, exactly
        as COVER_ASK_PER_FRAME records, being off the paint path does not
        make it free, because a frame cannot be produced while it runs.
        That is why it is a couple at a time and not the whole band."""
        if self._metrics is not m:
            return                      # fonts or cell size changed under us
        for index in ids:
            if 0 <= index < len(self._records) and index not in self._cells:
                self._cell_pixmap(m, index)

    def _draw_cell_content(self, painter, m, record, rect):
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
        badge = "Saved" if record.get("saved") else (record.get("badge") or "")
        if badge:
            # One head slot: Saved (accent) outranks a caller's neutral
            # badge - the genre page labels its rows Series/Movies here.
            chip = self._chip(m, badge, accent=bool(record.get("saved")))
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
            # The app's own accent ramp, read from theme rather than
            # repeated here: this used to carry its own two stops and a
            # comment calling them "gold to amber", which survived a
            # whole re-theme because nothing recomputes a literal.
            # topLeft->bottomLeft, not bottomRight: the ramp is top-lit
            # and vertical now (theme.accent_stops), and a chip lit from
            # a corner does not match the QSS ones beside it.
            gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            for at, colour in theme.accent_stops():
                gradient.setColorAt(at, QColor(colour))
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
            #
            # **A target, not a position.** Writing the position through
            # meant one new position per mouse sample, which is 125 a
            # second against 240 refreshes - see DRAG_FOLLOW_TAU_S for
            # the measurement. The frame clock closes the gap instead,
            # so every refresh has somewhere to move to.
            start_y, start_pos = self._drag_from
            track_h = self.height() - 2 * BAR_MARGIN
            thumb_h = max(BAR_MIN_THUMB,
                          track_h * self.height() / max(1.0, float(self._content_h)))
            span = max(1.0, track_h - thumb_h)
            self._drag_target = max(0.0, min(
                float(self._motion.maximum),
                start_pos + (point.y() - start_y) * self._motion.maximum / span))
            if not self._hold_vblank():
                self.update()
            event.accept()
            return
        over_bar = self._overflowing() and point.x() >= self.width() - BAR_WIDTH
        if over_bar != self._bar_hover:
            self._bar_hover = over_bar
            self.update()
        self._set_hover(-1 if over_bar else self.index_at(point))
        super().mouseMoveEvent(event)

    def hideEvent(self, event):
        # Covered or navigated away from: whatever was lit is not lit
        # any more, and there may be no Leave to say so.
        self._set_hover(-1)
        if self._bar_hover:
            self._bar_hover = False
        super().hideEvent(event)

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
                    self._motion.stop()
                    self._drag_from = (point.y(), self._motion.pos)
                    self._drag_target = self._motion.pos
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
            # The target is left in place: the follow has a few
            # milliseconds still to run and letting it finish is what
            # makes a release land exactly where the thumb was let go,
            # rather than a pixel or two short of it.
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

class PosterStrip(PosterGrid):
    """The same painted surface, laid out as **one sideways row**.

    The owner's ask, 25 August 2026: replace the QWidget poster rows with
    a custom-painted virtualized viewport, without changing how the app
    looks. Discover's browsing rows and Home's shelves were ~20 Card
    widgets each, every one carrying a cover QLabel, a title and a meta
    line - measured on his real data, Discover alone held **149 Card
    widgets and 795 widgets in total**, and one scroll frame repainted
    14-15 of them.

    Measured against the widget grid on 180 identical cards, same window,
    three speeds:

        widget grid    14.4-15.3 paints/frame   gap 4.9-6.9 / p95 9.1-9.5ms
        painted grid    1.0 paint /frame        gap 3.6-4.1 / p95 5.0-5.2ms
        child widgets   720 -> 0

    Everything expensive is inherited unchanged: FrameMotion's float
    position, `_cell_pixmap`'s one-off compositing of cover + title +
    year + rating + Saved chip, the cover prefetch, and painting the
    whole card against a single offset so it moves as one object. Only
    the axis differs, so only the axis is overridden - the vertical grid
    the category tab and the genre browse use is not touched.

    **The wheel does not move it**, deliberately: the owner's rule of the
    same day is that no horizontal scrollbar moves on the wheel, only by
    dragging its bar. A notch here is left unaccepted so it reaches the
    page, which is what should scroll while the pointer crosses a row.
    """

    # A notch relayed by widgets._EdgeWheelRelay must not land here
    # either - see the note above.
    accepts_relayed_wheel = False

    # Read by _overflowing, which the base can reach before the first
    # _relayout has run (a paint scheduled by show()).
    _content_w = 0

    # ---- geometry, one row -------------------------------------------
    def _overflowing(self) -> bool:
        return self._content_w > self.width()

    def _relayout(self):
        m = self._ensure_metrics()
        self._columns = max(1, len(self._records))
        span = m["cell_w"] + GRID_SPACING
        self._content_w = max(0, len(self._records) * span - GRID_SPACING)
        # Kept in step so anything inherited that reads it stays sane.
        self._content_h = m["cell_h"]
        self._motion.maximum = float(max(0, self._content_w - self.width()))
        if self._motion.pos > self._motion.maximum:
            self._motion.set_position(self._motion.maximum)
        self.update()
        self.scrolled.emit()

    def cell_rect(self, index: int) -> QRect:
        """In content space - x measured from the first card, not from
        the left edge of the viewport."""
        m = self._ensure_metrics()
        return QRect(index * (m["cell_w"] + GRID_SPACING), 0,
                     m["cell_w"], m["cell_h"])

    def _cell_in_view(self, index: int) -> bool:
        m = self._ensure_metrics()
        left = index * (m["cell_w"] + GRID_SPACING) - self._motion.pos
        return left < self.width() and left + m["cell_w"] > 0

    def index_at(self, point: QPoint) -> int:
        m = self._ensure_metrics()
        span = m["cell_w"] + GRID_SPACING
        if point.y() < 0 or point.y() >= m["cell_h"]:
            return -1
        x = point.x() + self._motion.pos
        if x < 0:
            return -1
        index, remainder = divmod(int(x), span)
        if remainder >= m["cell_w"]:
            return -1        # the gap between two cards is not a card
        return index if 0 <= index < len(self._records) else -1

    def sizeHint(self):
        m = self._ensure_metrics()
        return QSize(m["cell_w"] * 3, m["cell_h"] + BAR_WIDTH + 2 * BAR_MARGIN)

    # ---- which cards this frame draws --------------------------------
    def _visible_range(self, offset, m):
        span = m["cell_w"] + GRID_SPACING
        first = max(0, int(offset) // span)
        last = min(len(self._records), int(offset + self.width()) // span + 1)
        return first, last

    def _cell_viewport_rect(self, index, offset):
        rect = self.cell_rect(index)
        rect.moveLeft(rect.left() - int(offset))
        return rect

    def _prefetch_band(self) -> int:
        """How many cards past the edge to prepare. A screenful either
        way - the vertical grid counts this in rows, and a strip has
        one."""
        m = self._ensure_metrics()
        per_screen = max(1, self.width() // max(1, m["cell_w"] + GRID_SPACING))
        return max(2, per_screen)

    # ---- the wheel belongs to the page -------------------------------
    def wheelEvent(self, event):
        """Hand the notch to the page, and never move sideways on it.

        `event.ignore()` alone is not enough - measured 25 August 2026,
        an ignored wheel on a widget inside a QScrollArea moved nothing
        at all, the same gap that made widgets.SideScroller hand its
        notches up by hand. Imported here rather than at module scope
        because helpers.widgets imports this module."""
        from .widgets import _vertical_scroller_above
        page = _vertical_scroller_above(self)
        if page is None:
            event.ignore()
            return
        QApplication.sendEvent(page.viewport(), event)
        event.accept()

    # ---- the bar, along the bottom -----------------------------------
    def _thumb_rect(self) -> QRectF:
        track = self.width() - 2 * BAR_MARGIN
        content = max(1.0, float(self._content_w))
        thumb = max(BAR_MIN_THUMB, track * self.width() / content)
        span = track - thumb
        travel = ((self._motion.pos / self._motion.maximum)
                  if self._motion.maximum > 0 else 0.0)
        left = BAR_MARGIN + span * max(0.0, min(1.0, travel))
        return QRectF(left, self.height() - BAR_WIDTH, thumb, BAR_WIDTH)

    def _paint_scrollbar(self, painter):
        if not self._overflowing():
            return
        thumb = self._thumb_rect()
        if thumb.width() <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT if (self._bar_hover or self._drag_from)
                                else theme.SURFACE_HOVER))
        painter.drawRoundedRect(thumb, BAR_WIDTH / 2.0, BAR_WIDTH / 2.0)
        painter.restore()

    # ---- dragging that bar -------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            if self._overflowing() and point.y() >= self.height() - BAR_WIDTH:
                thumb = self._thumb_rect()
                if thumb.left() <= point.x() <= thumb.right():
                    self._motion.stop()
                    self._drag_from = (point.x(), self._motion.pos)
                    self._drag_target = self._motion.pos
                else:
                    # A press on the track jumps a screenful that way,
                    # which is what a QScrollBar does.
                    page = self.width() * 0.9
                    self.set_scroll_offset(
                        self._motion.pos
                        + (page if point.x() > thumb.left() else -page))
                event.accept()
                return
            index = self.index_at(point)
            if index >= 0:
                self._motion.stop()
                self.clicked.emit(index)
                event.accept()
                return
        QWidget.mousePressEvent(self, event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._drag_from is not None:
            start_x, start_pos = self._drag_from
            track = self.width() - 2 * BAR_MARGIN
            thumb = max(BAR_MIN_THUMB,
                        track * self.width() / max(1.0, float(self._content_w)))
            span = max(1.0, track - thumb)
            self._drag_target = max(0.0, min(
                float(self._motion.maximum),
                start_pos + (point.x() - start_x) * self._motion.maximum / span))
            if not self._hold_vblank():
                self.update()
            event.accept()
            return
        over_bar = self._overflowing() and point.y() >= self.height() - BAR_WIDTH
        if over_bar != self._bar_hover:
            self._bar_hover = over_bar
            self.update()
        self._set_hover(-1 if over_bar else self.index_at(point))
        QWidget.mouseMoveEvent(self, event)
