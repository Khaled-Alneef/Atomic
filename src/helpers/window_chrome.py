"""The window's own top bar: Back, one search field, and the window
buttons - in the strip Windows used to draw its caption into.

The owner's ask, 25 August 2026: *"make sure that there is no upper bar
like in img 2, make it like img 3"* - img 2 being Windows' native
"Atomic" caption, img 3 an in-app bar - *"do not add 'Harbor'"*.

## The route not taken, and what it cost

The better-looking option is to keep the native frame and only stop
Windows *drawing* the caption: answer **WM_NCCALCSIZE** with the rect
unchanged, and **WM_NCHITTEST** with HTCAPTION over the bar. That keeps
edge-resize, Aero Snap, Snap Layouts and the maximise animation native,
and it is what a C++ app would do.

It was built, and it is not here, because PyQt6 could not carry it. An
overridden `nativeEvent` answering those messages killed the process
while the window was being realised - no traceback, nothing in the log,
no usable exit code, because the fault lands below the level
faulthandler reports from. Narrowed as far as it goes, 25 August 2026:
every construct is fine *in isolation* in six lines of bare Qt (reading
the MSG, answering WM_NCCALCSIZE with `(True, 0)`, answering
WM_NCHITTEST with a hit code); a control with the handler inert built
the window every time; and a message trace showed the fault arriving
*after* the handler had returned cleanly - on PyQt6's side of the call,
not in this file.

One real PyQt6 trap fell out of that dig and is worth keeping even
though the code that found it is gone: **an override must never
`return super().nativeEvent(...)`**. PyQt6 types the result as
`sip.voidptr`, and handing the base class's own tuple back to C++ is an
access violation - reproduced in bare Qt, where an override returning
`(False, 0)` (what the default answers anyway) is fine and one
delegating upward is not.

So this uses Qt's own frameless support, which is what
`widgets._FramelessShell` already does for every dialog in the app:

- `Qt.FramelessWindowHint`, so Windows draws nothing;
- `startSystemMove()` from the bar, which is a *native* drag, so
  Windows is doing the dragging rather than a mouseMoveEvent adding up
  deltas. **That alone is not enough for snapping** - see
  `make_frameless`, which puts back the two style bits Windows checks
  before it will snap a window at all;
- `startSystemResize(edges)` from the perimeter, likewise native, with
  the real resize cursors.

What is genuinely lost: the Snap Layouts flyout that hovering a *native*
maximise button pops, and the frame's drop shadow.

## Why the resize filter is application-wide

Presses on the window's perimeter land on whichever child covers it -
the sidebar down the left, a page on the right and bottom - so a filter
installed on the window alone never sees them. `_MouseNavFilter` in
main.py is app-wide for the same reason and records why it has to be.
This one copies its shape, including one `event.type()` test as the very
first line: main.py also records what an app-wide filter costs when it
asks more than that (13,172 calls across six sidebar folds).
"""

import ctypes
import sys

from PyQt6.QtCore import (QEvent, QObject, QPoint, QRect, QRectF, QSize,
                          Qt, QTimer)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QPushButton,
                             QSizePolicy, QVBoxLayout, QWidget)

from . import images, theme
from .widgets import SmoothTween, search_field, use_hover_cursor

WINDOWS = sys.platform == "win32"

# ---- The bar's own metrics ----------------------------------------------
# 48px: tall enough for a 34px field with air above and below, short
# enough that it does not read as a second header over a page that has
# one of its own. Windows' caption is 32 at 100% - this is the app's
# bar, not an imitation of that one.
BAR_HEIGHT = theme.TITLE_BAR_HEIGHT

# **The three window buttons are discs**, 28 August 2026 (see
# _window_button). Their diameter and the gap between them, and the flat
# circle each rests on - SURFACE rather than SURFACE_HOVER, which is
# what the *hover* lifts them to, so the two states are a step apart.
WINDOW_BUTTON_SIZE = 34
WINDOW_BUTTON_GAP = 8
WINDOW_BUTTON_FILL = theme.SURFACE

# **The maximise and restore marks are drawn, not typed**, 28 August
# 2026. Segoe's ChromeMaximize (U+E922) hints its own top edge to a
# different weight than its other three at 9pt - the owner: "the
# thickness of the upper line in this button is not the same as other
# lines" - and its ChromeRestore (U+E923) is a hard-cornered pair of
# rectangles that reads as coarse beside the round buttons it now sits
# on ("replace it with smoother one").
#
# A painted pair fixes both for the same reason it fixed the tick: one
# stroke width, set here, and rounded joins the font does not offer.
# Drawn on half-pixel centres, which is what stops a 1.2px line landing
# across two rows of pixels and rendering as a scratchy double edge -
# the same trap widgets.magnifier_icon records.
WINDOW_MARK_PX = 10          # the square's side inside the button
WINDOW_MARK_STROKE = 1.2
WINDOW_MARK_RADIUS = 1.6
# How far the back square of the restore mark sits up and to the right.
WINDOW_MARK_OFFSET = 3.0


def _window_mark_pixmap(kind: str, colour: str, size: int) -> QPixmap:
    """One drawn mark at one colour."""
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(colour))
    pen.setWidthF(WINDOW_MARK_STROKE)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    side = WINDOW_MARK_PX
    # Half a stroke in from the centre, so every edge - the top one
    # included - sits on the same sub-pixel offset and comes out the
    # same weight. Centring the *box* and letting the stroke straddle it
    # is what put one edge on a pixel boundary and the opposite edge
    # between two.
    half = WINDOW_MARK_STROKE / 2.0
    if kind == "restore":
        # Front square low-left, back square up-right, and the back one
        # is drawn first so the front sits over it.
        offset = WINDOW_MARK_OFFSET
        left = (size - side - offset) / 2.0
        top = (size - side - offset) / 2.0
        back = QRectF(left + offset + half, top + half, side, side)
        front = QRectF(left + half, top + offset + half, side, side)
        painter.drawRoundedRect(back, WINDOW_MARK_RADIUS, WINDOW_MARK_RADIUS)
        # The front square is filled with the button's own ground before
        # it is stroked, so the back one does not show through it - two
        # transparent outlines overlapping read as a grid, not as two
        # windows.
        painter.setBrush(QColor(WINDOW_BUTTON_FILL))
        painter.drawRoundedRect(front, WINDOW_MARK_RADIUS, WINDOW_MARK_RADIUS)
    else:
        left = (size - side) / 2.0
        painter.drawRoundedRect(QRectF(left + half, left + half, side, side),
                                WINDOW_MARK_RADIUS, WINDOW_MARK_RADIUS)
    painter.end()
    return pixmap


def _window_mark_icon(kind: str, colour: str,
                      size: int = WINDOW_BUTTON_SIZE) -> QIcon:
    """One drawn mark, one colour, as an icon."""
    return QIcon(_window_mark_pixmap(kind, colour, size))


# The field is centred *in the window*, not in the space left over
# between the two side groups - so both groups take the same minimum
# width and the field sits between two equal stretches. Home's header
# already does this with a balancing spacer, for the same reason and
# with the same failure when it is skipped: the field drifts left by
# half the difference between the two sides.
# Wider, at the owner's ask (26 August 2026, with a picture of the bar).
# The stretch either side still centres it, so this only changes how much
# of the room it is allowed to take before the stretches get the rest.
SEARCH_MAX_WIDTH = 760
# Full screen has the whole window to work with and no window
# buttons sharing the row, so the field is allowed more of it
# there (the owner's ask, 26 August 2026).
# **Unused since 28 August 2026** - full screen shares SEARCH_MAX_WIDTH
# now (see set_fullscreen_search_width). Kept as the record of what the
# wider ceiling was, since removing it would lose the number the change
# was measured against.
SEARCH_MAX_WIDTH_FULLSCREEN = 1100
# How much of the page's width the field takes in full screen,
# before the cap above applies. 0.58 rather than 0.60 so folding
# the rail makes a difference you can see: at 0.60 both widths
# clamped to the cap and nothing moved at all.
FULLSCREEN_SEARCH_SHARE = 0.58
SEARCH_MIN_WIDTH = 320
SEARCH_HEIGHT = theme.TOP_SEARCH_HEIGHT

# Segoe Fluent Icons, the faces every other chrome button in this app
# already uses (theme.FONT_FAMILY_ICONS). Monochrome, so they inherit
# the button's colour - which is exactly why this app does not use emoji
# for chrome, as theme.py and rail_icons.py both record.
MINIMISE_GLYPH = ""   # ChromeMinimize


CLOSE_GLYPH = ""      # ChromeClose

# How wide the grab band along each edge is, in logical pixels. 6 rather
# than Windows' own 4: there is no frame drawn to aim at here, so the
# band has to be forgiving enough to hit without hunting for it.
RESIZE_BAND = 6

# **Saved, Schedule and History live up here now** - the owner's ask, 26
# August 2026. They used to be three pills in the header of whichever
# tracker page was showing, which meant two copies of them (one on Watch,
# one on Read) and neither reachable from anywhere else. One set, in the
# window's own bar, and the Watch/Read split moves *inside* the section
# they open (tracker._build_medium_tabs).
SECTION_BUTTONS = (("saved", "Saved"),
                   ("schedule", "Schedule"),
                   ("history", "History"))
SECTION_BUTTON_WIDTH = 44
# 22, up from 17 - the owner's ask, 26 August 2026. The button is
# 44x34, so this is the largest that still leaves a margin either
# side of the glyph rather than butting it against the pill.
SECTION_ICON = 22

_CORNERS = (
    (Qt.Edge.LeftEdge, Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
    (Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.LeftEdge, Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.RightEdge, Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
)


# Window styles. **WS_CAPTION is set, and that note used to say the
# opposite.** "Putting it back is putting the title bar back" was an
# assertion, and it is not what happens: this window answers
# WM_NCCALCSIZE with no non-client area, so measured with the bit on,
# the window rect and the client rect are the same size and nothing is
# drawn for it. What the bit buys is the drag-to-top maximise and the
# Snap Layouts overlay, which Windows gates on a caption and which
# WS_THICKFRAME/WS_MAXIMIZEBOX alone do not give - the owner, 31 August
# 2026, with both of those already set.
GWL_STYLE = -16
SW_MAXIMIZE = 3
SW_RESTORE = 9
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
# **And this one has to come *off*.** Measured 31 August 2026 on the
# running window: style 0x96CF0000 - WS_CAPTION, WS_THICKFRAME,
# WS_MAXIMIZEBOX and WS_MINIMIZEBOX all present exactly as intended, and
# the drag-to-top still did nothing. WS_POPUP was set too, which Qt does
# for a frameless window, and a popup is not an overlapped window:
# Windows will not snap one, will not show the Snap Layouts overlay over
# its maximise button, and ignores every other bit while it is there.
# Adding bits could never have fixed this, which is why two passes of
# adding them did not.
WS_POPUP = 0x80000000


def make_frameless(window):
    """Take Windows' caption off `window`, and keep its snapping.

    **The style bits are not decoration, they are what Aero Snap looks
    for.** Qt.FramelessWindowHint strips the window down to WS_POPUP,
    and Windows decides whether a window may be snapped - dragged to the
    top to maximise, to a side for half the screen, Win+Arrow - by
    asking whether it has WS_THICKFRAME and WS_MAXIMIZEBOX. Without
    them, `startSystemMove()` moves the window and nothing else happens
    at the edges, which is the owner's report of 26 August 2026.

    Adding them back does not bring the caption back: WS_CAPTION is what
    draws that, and it stays off. What they do bring is Windows' own
    resize border, which is why `ResizeFilter` below is now belt and
    braces rather than the only way to resize - it costs nothing and
    still gives the right cursor over the app's own 6px band.

    Everything else is unchanged - taskbar entry, Alt-Tab, minimise, and
    the geometry the app already saves and restores."""
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    if not WINDOWS:
        return True
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        user = ctypes.windll.user32
        user.GetWindowLongW.restype = ctypes.c_long
        style = user.GetWindowLongW(hwnd, GWL_STYLE)
        # WS_POPUP off in the same call. Qt.FramelessWindowHint sets it,
        # and Windows snaps no popup window - see WS_POPUP's note above.
        # The window stays frameless because it answers WM_NCCALCSIZE
        # with no non-client area, not because of this bit.
        user.SetWindowLongW(hwnd, GWL_STYLE,
                            ctypes.c_long((style | WS_CAPTION | WS_THICKFRAME
                                           | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
                                          & ~WS_POPUP))
    except Exception:
        # A platform plugin with no real HWND, or a Windows build that
        # refuses the change - the window is still frameless and still
        # draggable, it just will not snap.
        return True
    return True


def ensure_snap_styles(window) -> bool:
    """Put back the style bits Windows checks before it will snap a
    window. Returns whether any were missing.

    **Going full screen takes them, and not every way back puts them
    on.** Qt clears WS_THICKFRAME for a full-screen window and restores
    the style it saved when its own `setWindowState` brings the window
    back - so any route that reaches the same end state without going
    through that call leaves them off. `maximise_from_fullscreen` is
    exactly such a route: bypassing Qt's state machinery is what makes
    it skip the restored-size frame.

    Measured 27 August 2026 on a window maximised before it went full
    screen, which is how the owner runs it:

        maximised          style 0x970F0000  WS_THICKFRAME set
        full screen        style 0x96080000  cleared by Qt
        back to maximised  style 0x97080000  still cleared

    and a window without WS_THICKFRAME cannot be snapped to an edge at
    all (see make_frameless, where the same bits are put on for the same
    reason). That is the owner's report: after leaving full screen in the
    player, dragging to the top no longer fills the screen.

    Idempotent, so it is safe on every transition rather than only the
    ones known to need it."""
    if not WINDOWS:
        return False
    # WS_CAPTION with them: the other two buy edge snapping, and
    # the drag-to-top maximise and the Snap Layouts overlay are
    # gated on the caption bit - measured 31 August 2026 with
    # both of the others set and the drag still doing nothing.
    # Nothing is drawn for it; the frameless window answers
    # WM_NCCALCSIZE with no non-client area.
    wanted = WS_CAPTION | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        user = ctypes.windll.user32
        user.GetWindowLongW.restype = ctypes.c_long
        style = user.GetWindowLongW(hwnd, GWL_STYLE)
        if style & wanted == wanted and not style & WS_POPUP:
            return False
        # WS_POPUP cleared as well as the four set - see its note above.
        # A popup window is not an overlapped one and Windows snaps
        # neither it nor anything else about it.
        fixed = (style | wanted) & ~WS_POPUP
        user.SetWindowLongW(hwnd, GWL_STYLE, ctypes.c_long(fixed))
        # SWP_FRAMECHANGED, or the new style is not read until something
        # else forces a frame recalculation - which on this window may be
        # never, since it draws no frame.
        #
        # **With NOREDRAW and NOACTIVATE.** A frame change asks Windows
        # to recalculate and repaint the whole non-client area, and on a
        # frameless window that is the entire window: it blanks and
        # comes back, which the owner reported as the app reopening when
        # he first entered a watch or read page. The style still takes
        # effect - the recalculation is what FRAMECHANGED is for - and
        # nothing needs repainting, because this window draws no frame
        # to repaint.
        #
        #   SWP_NOSIZE 0x0001  NOMOVE 0x0002  NOZORDER 0x0004
        #   NOREDRAW   0x0008  NOACTIVATE 0x0010  FRAMECHANGED 0x0020
        user.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                          0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020)
    except Exception:
        return False
    return True


def keep_snap_styles(window):
    """Re-apply the snap bits whenever the window changes state.

    **Qt recreates the native window on some transitions**, and manual
    style bits do not survive that - which is why WS_CAPTION measured
    present after make_frameless and the drag-to-top still did nothing
    later in the session. Cheap and idempotent, so it is simply done
    again on every state change rather than reasoned about."""
    if not WINDOWS:
        return

    def again(*_args):
        try:
            ensure_snap_styles(window)
        except Exception:
            pass

    try:
        window.windowHandle().windowStateChanged.connect(again)
        window.windowHandle().visibilityChanged.connect(again)
    except Exception:
        pass
    again()

    # **And again after Qt has finished.** Measured 31 August 2026: with
    # only the calls above, the window settled at style 0x960B0000 -
    # WS_CAPTION and WS_THICKFRAME gone and WS_POPUP back - because Qt
    # applies FramelessWindowHint to the native window after these
    # signals have fired, and that overwrites whatever was set. The
    # state changes alone therefore cannot win, however many of them are
    # connected. Four single shots, idempotent, and done inside three
    # seconds of launch.
    try:
        from PyQt6.QtCore import QTimer
        for delay in (0, 250, 1000, 3000):
            QTimer.singleShot(delay, again)
    except Exception:
        pass


def is_zoomed(window) -> bool:
    """Whether Windows itself considers the window maximised.

    Not the same question as `isMaximized()` - see the note in
    `restore_from_maximised` for the measurement where the two
    disagreed."""
    if not WINDOWS:
        return bool(window.isMaximized())
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        return bool(ctypes.windll.user32.IsZoomed(hwnd))
    except Exception:
        return bool(window.isMaximized())


def maximise_window(window) -> bool:
    """Fill the screen, including when Qt's own call will not.

    **The mirror of restore_from_maximised, and needed for the same
    reason.** Qt's idea of the window state goes stale the moment
    Windows changes it behind Qt's back, and `showMaximized()` on a
    window Qt already believes is maximized does nothing at all.
    Measured 26 August 2026, driving the real button:

        A normal            1100x700
        B snapped to top    2048x1104
        C pressed restore   1280x840     <- Win32 route, correct
        D pressed maximise  1280x840     <- showMaximized() did nothing
        E pressed restore   1280x840

    D is the owner's "the max and min screen button is not working good
    at all". One press worked and the button was dead from then on,
    because C left Qt still holding the maximized flag."""
    window.showMaximized()
    if is_zoomed(window):
        return False
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
    except Exception:
        return False
    return True


SW_SHOWMAXIMIZED = 3


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", _POINT),
                ("ptMaxPosition", _POINT), ("rcNormalPosition", _RECT)]


def _placement(hwnd):
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return None
    return wp


def normal_rect(window):
    """Windows' own restore rectangle for `window`, in physical pixels,
    or None.

    **Read this on the way *into* full screen, not on the way out.**
    Going full screen overwrites `rcNormalPosition` with the full-screen
    rect - measured 27 August 2026, the same window either side of one
    `showFullScreen()`:

        maximised     showCmd=3  rcNormalPosition=(480, 165, 1600, 1050)
        full screen   showCmd=1  rcNormalPosition=(0, 0, 2560, 1440)

    so by the time `maximise_from_fullscreen` runs there is nothing left
    to preserve unless somebody kept a copy."""
    if not WINDOWS:
        return None
    try:
        wp = _placement(ctypes.c_void_p(int(window.winId())))
    except Exception:
        return None
    if wp is None:
        return None
    n = wp.rcNormalPosition
    return (n.left, n.top, n.right - n.left, n.bottom - n.top)


def maximise_from_fullscreen(window, saved=None) -> bool:
    """Leave full screen for maximised without the window being drawn at
    its restored size on the way. Returns whether this route ran.

    **The window really was drawn small for a frame, and it was Qt's
    own doing.** Measured 27 August 2026 by polling the HWND's rect from
    a second thread 2ms apart, leaving full screen from a
    Windows-side-maximised window:

        0.0ms   2560x1440              full screen
        52.9ms  1600x1050 at 480,165   <- the restored size, 18-25ms
        71.4ms  2560x1380              maximised

    That is the owner's report of 26 August 2026 - "the window goes
    really small and then goes to its original size". `showMaximized()`
    from full screen makes Qt *restore first and maximise second*, and
    the middle step is a real resize the app pays a full relayout for,
    which is why it lasts long enough to see. A bare window skips
    through it in under 2ms and shows nothing; this one does not.

    Note what the trace rules out: at the middle step Windows'
    `rcNormalPosition` is already the full-screen rect, so the size Qt
    lands on is **its own** saved geometry, not Windows'. An earlier
    attempt that lent the window a full-size restore rect through
    `SetWindowPlacement` could therefore never have worked, and did not
    - it left `isFullScreen()` True and lost the real restore rect.

    So this does not ask Qt to change state at all:

      * `setGeometry` to the maximised rect. Qt drops the full-screen
        flag when geometry is set explicitly, and lands the window
        exactly where it is going in one step - no restore in between.
      * `SetWindowPlacement` with `showCmd` alone changed makes it
        genuinely maximised (`IsZoomed`), which `setGeometry` on its own
        does not - measured leaving `zoomed=False qtMax=False`, so the
        maximise button read wrong and restoring afterwards did nothing.
      * `rcNormalPosition` is put back **after** that, never in the same
        call. Setting it alongside `showCmd` restores the window through
        the small rect first, which is the whole bug again - measured
        1600x1050 reappearing at 54.7ms.

    `ShowWindow(SW_MAXIMIZE)` works here too but blips 60px taller for
    ~2.3ms on two runs in three, so `SetWindowPlacement` does the
    maximising instead. Three runs of this route step straight from
    2560x1440 to 2560x1380 and show nothing else, and end on the same
    state the old path did: zoomed, `isFullScreen()` False, restore rect
    (480, 165, 1600, 1050) intact, and restoring returns there."""
    if not WINDOWS:
        return False
    try:
        screen = window.screen()
        avail = screen.availableGeometry() if screen else None
    except (AttributeError, RuntimeError):
        avail = None
    if avail is None:
        return False
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        user = ctypes.windll.user32
    except Exception:
        return False

    done = []

    def _go():
        window.setGeometry(avail)
        wp = _placement(hwnd)
        if wp is None:
            return
        wp.showCmd = SW_SHOWMAXIMIZED
        user.SetWindowPlacement(hwnd, ctypes.byref(wp))
        if saved:
            left, top, width, height = saved
            wp.rcNormalPosition = _RECT(left, top, left + width, top + height)
            user.SetWindowPlacement(hwnd, ctypes.byref(wp))
        done.append(True)

    # **The wrapper is load-bearing here, not decoration.** Without it
    # the window still skips the restored size, but spends ~2.3ms 60px
    # taller than the work area first - Windows' own maximize animation
    # starting from the full-screen rect. Measured 27 August 2026: three
    # runs in three showed it unwrapped, none of three wrapped.
    try:
        theme.without_window_animation(window, _go)
    except Exception:
        return False
    return bool(done)


def restore_from_maximised(window) -> bool:
    """Bring a window down out of maximised, including when Windows put
    it there rather than Qt. Returns whether the Win32 route was needed.

    **`showNormal()` is not enough**, measured 26 August 2026 on the
    owner's report that the bar's maximise button does nothing after
    dragging the window to the top edge. That drag is Windows' own
    maximise (WM_SYSCOMMAND / SC_MAXIMIZE), and afterwards:

        A  normal                       qtMax=False  WS_MAXIMIZE=False  1100x700
        B  windows-side SC_MAXIMIZE     qtMax=True   WS_MAXIMIZE=True   2048x1104
        C  after showNormal()           qtMax=True   WS_MAXIMIZE=True   2048x1104
        D  after clearing Qt's flag     qtMax=False  WS_MAXIMIZE=True   2048x1104
        E  after ShowWindow(SW_RESTORE) qtMax=False  WS_MAXIMIZE=False  1100x700

    C is the bug: the call returns cleanly and changes nothing. D is why
    the obvious workaround is worse than the bug - Qt then believes the
    window is normal while Windows keeps it zoomed, and every
    setGeometry is ignored for as long as that lasts, which is a window
    that can no longer be resized at all. Only E actually works.

    The previous attempt at this fixed the *detection* (see
    `MainWindow._looks_maximised`, which compares geometry because
    `isMaximized()` alone missed the snapped case) and then called
    `showNormal()` anyway - so the button knew perfectly well it should
    restore, asked to, and was ignored. Detection was never the half
    that was broken."""
    window.showNormal()
    if not is_zoomed(window):
        return False
    try:
        hwnd = ctypes.c_void_p(int(window.winId()))
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        return False
    return True


# **How far past its own right edge a window has to reach before that
# edge can be scrolled.** Measured 28 August 2026, on the real reader,
# with real pages and a real OS wheel driven through SendInput:
#
#     window's outermost column   0 px of scroll, every time
#     one column in              68 px, every time
#
# In every state - windowed, maximised, full screen - and on the
# scrollbar as well as beside it. Four px because it is comfortably more
# than one and still invisible off the side of the screen.
#
# Three things it is *not*, each ruled out by measurement rather than
# by argument: it is not the screen's last pixel (the same physical
# column scrolls once the window reaches past it), it is not
# `WS_THICKFRAME` (clearing the style changed nothing), and it is not
# the probe (the cursor lands exactly where it is put, inside the
# window, and Qt agrees).
#
# WM_GETMINMAXINFO would have been the clean way to do this - Windows
# would maximise to the larger rect and Qt would keep its state - and it
# is not used because the message never reaches an application-level
# native filter here: instrumented, it was seen 3 times during window
# creation and **0 times** across a maximise.
EDGE_REACH_PX = 4


def apply_edge_reach(window, on: bool):
    """Push the window `EDGE_REACH_PX` past its own right edge, or put it
    back.

    Returns True when the geometry was changed, so the caller knows it
    owes a restore.

    **This costs the maximised state and the restore rect**, which is
    why it is not simply left on: measured, `setGeometry` on a maximised
    window leaves `isMaximized()` False and `showNormal()` then returns
    to the overhang rect rather than to where the window was. So the
    caller remembers both and puts them back - see
    reader.open_reader, the only place that turns this on, and only for
    as long as a chapter is on screen."""
    try:
        if on:
            frame = window.geometry()
            window.setGeometry(frame.x(), frame.y(),
                               frame.width() + EDGE_REACH_PX, frame.height())
            return True
    except Exception:
        pass
    return False


def edges_at(window, pos):
    """Which window edges `pos` (window-local, logical) is within
    grabbing distance of, or None.

    Never while maximised or full screen: there is no edge to drag in
    either state, and a live band there would eat clicks on whatever
    sits under it - the sidebar's first row, in this layout."""
    if window.isMaximized() or window.isFullScreen():
        return None
    # The edge reach clears Qt's maximised flag (see apply_edge_reach),
    # so without this the reader's own perimeter would turn into resize
    # grips the moment it opened over a maximised window.
    if getattr(window, "_edge_reach_on", False):
        return None
    rect = window.rect()
    edges = Qt.Edge(0)
    if pos.x() <= RESIZE_BAND:
        edges |= Qt.Edge.LeftEdge
    elif pos.x() >= rect.width() - RESIZE_BAND:
        edges |= Qt.Edge.RightEdge
    if pos.y() <= RESIZE_BAND:
        edges |= Qt.Edge.TopEdge
    elif pos.y() >= rect.height() - RESIZE_BAND:
        edges |= Qt.Edge.BottomEdge
    return edges or None


def cursor_for(edges):
    """The resize cursor an edge combination should show."""
    if edges is None:
        return None
    for first, second, shape in _CORNERS:
        if edges & first and edges & second:
            return shape
    if edges & Qt.Edge.LeftEdge or edges & Qt.Edge.RightEdge:
        return Qt.CursorShape.SizeHorCursor
    return Qt.CursorShape.SizeVerCursor


class ResizeFilter(QObject):
    """Edge-resize for a frameless window, installed on the application.

    See the module docstring for why this cannot live on the window
    itself, and why its first line is one `event.type()` test."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._shaped = False

    def eventFilter(self, obj, event):
        kind = event.type()
        if (kind != QEvent.Type.MouseMove
                and kind != QEvent.Type.MouseButtonPress):
            return False
        window = self._window
        try:
            if (not window.isVisible() or window.isMaximized()
                    or window.isFullScreen()):
                self._unshape()
                return False
            # window.geometry() is already global and already logical.
            # Never mapToGlobal, which on a mixed-DPI pair divides by the
            # *other* screen's scale factor - this app's toasts once
            # landed 200px off that way (.claude/rules/ui.md).
            frame = window.geometry()
            glob = event.globalPosition().toPoint()
            local = QPoint(glob.x() - frame.x(), glob.y() - frame.y())
            if not window.rect().contains(local):
                self._unshape()
                return False
            edges = edges_at(window, local)
        except (AttributeError, RuntimeError):
            return False
        if edges is None:
            self._unshape()
            return False
        if kind == QEvent.Type.MouseMove:
            shape = cursor_for(edges)
            if shape is not None:
                window.setCursor(shape)
                self._shaped = True
            return False
        handle = window.windowHandle()
        if handle is not None:
            handle.startSystemResize(edges)
            return True
        return False

    def _unshape(self):
        """Give the arrow back the moment the pointer leaves the band.

        Not left set: .claude/rules/ui.md - a cursor set and forgotten is
        how the pointing hand once stayed on screen indefinitely, and the
        window owns the plain arrow explicitly for that reason."""
        if self._shaped:
            self._window.setCursor(Qt.CursorShape.ArrowCursor)
            self._shaped = False


class DriftButton(QPushButton):
    """A button whose hover *arrives* instead of switching on.

    The owner's ask, 26 August 2026: *"add a short short animation to
    all buttons when hover like make it smooth and drifty"*.

    **Painted, because a stylesheet cannot do this.** QSS has no
    transitions - `:hover` is a different rule, applied whole the
    instant the pointer crosses the edge - so the only ways to fade one
    are to rewrite the stylesheet every frame (a style recomputation per
    frame, which is what the sidebar rail was explicitly built to avoid)
    or to paint the background here and let QPushButton draw only its
    glyph on top. This does the second: its QSS background is
    transparent, the fill below is ours, and `super().paintEvent` puts
    the text over it.

    The press is a deepening, not a scale: a button that jumps size
    under the pointer moves the thing being clicked.
    """

    HOVER_MS = 140
    PRESS_MS = 90

    def __init__(self, text="", parent=None, radius=0, tint=None,
                 objectName="WindowButton"):
        super().__init__(text, parent, objectName=objectName)
        self._radius = radius
        self._tint = tint or theme.SURFACE_HOVER
        self._hover = 0.0
        self._press = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover_tween = SmoothTween(self, self._set_hover, self.HOVER_MS)
        self._press_tween = SmoothTween(self, self._set_press, self.PRESS_MS)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def _set_press(self, value):
        self._press = float(value)
        self.update()

    # A drawn mark in two colours, swapped by hand on hover.
    #
    # **QIcon's Normal/Active modes are not enough here**, 28 August
    # 2026. Qt only reaches for `QIcon::Active` if the *style* asks for
    # it, and the stylesheet style these buttons are drawn by does not -
    # so the two-mode icon rendered its Normal pixmap whether the
    # pointer was on the button or not, while the minimise and close
    # glyphs beside it lit from the sheet's own `:hover { color }`. The
    # owner, twice: "make the shape in this button light like the min
    # when hover".
    _icon_rest = None
    _icon_hover = None

    def set_hover_icons(self, rest, hover):
        """Two icons for one button: at rest, and under the pointer."""
        self._icon_rest, self._icon_hover = rest, hover
        self.setIcon(self._icon_hover if self.underMouse() else self._icon_rest)

    def _sync_icon(self, hovered):
        if self._icon_rest is None:
            return
        self.setIcon(self._icon_hover if hovered else self._icon_rest)

    def enterEvent(self, event):
        self._hover_tween.start(self._hover, 1.0, self.HOVER_MS)
        self._sync_icon(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_tween.start(self._hover, 0.0, self.HOVER_MS)
        self._press_tween.start(self._press, 0.0, self.PRESS_MS)
        self._sync_icon(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._press_tween.start(self._press, 1.0, self.PRESS_MS)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_tween.start(self._press, 0.0, self.PRESS_MS)
        super().mouseReleaseEvent(event)

    # A fill that is there before the pointer is - see _window_button.
    # None keeps the old behaviour (nothing until hover), which is what
    # every other DriftButton in this file wants.
    resting = None

    def paintEvent(self, event):
        if self.resting is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.resting))
            painter.drawRoundedRect(QRectF(self.rect()),
                                    self._radius, self._radius)
            painter.end()
        if self._hover > 0.001 or self._press > 0.001:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            fill = QColor(self._tint)
            # Hover carries the alpha; the press deepens it rather than
            # adding a second layer, so the two never stack into a
            # patch brighter than either state alone.
            alpha = self._hover * (0.78 + 0.22 * self._press)
            fill.setAlphaF(min(1.0, max(0.0, alpha)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            rect = QRectF(self.rect())
            painter.drawRoundedRect(rect, self._radius, self._radius)
            painter.end()
        super().paintEvent(event)


def begin_window_drag(widget, event) -> bool:
    """Hand a left-press to Windows as a window move. Returns whether it
    started one.

    **Native, not a mouseMoveEvent adding up deltas**, because Aero Snap,
    Win+Arrow and drag-to-maximise are things Windows does during *its*
    move loop - a hand-rolled drag moves the window and nothing happens
    at the edges, which is the report of 26 August 2026 that
    `make_frameless` exists to answer.

    Shared rather than written twice: the window's own bar is not the
    only surface that should move the window. The player covers the
    whole window (player.immersive_host), so while it is up there is no
    bar on screen at all and a drag has nothing to land on - the owner's
    ask, 27 August 2026, "make the window draggable while playing and
    not in fullscreen mode".

    Full screen is excluded on purpose in both places: there is no frame
    to move there, and dragging one would only smear the picture."""
    if event.button() != Qt.MouseButton.LeftButton:
        return False
    window = widget.window()
    if window is None or _really_fullscreen(window):
        return False
    handle = window.windowHandle()
    if handle is None:
        return False
    handle.startSystemMove()
    return True


def _really_fullscreen(window) -> bool:
    """Full screen as the *screen* shows it, not only as Qt remembers it.

    **Qt's window state goes stale when Windows changes it behind Qt's
    back**, which is not a theory here - it is the measured cause of the
    dead maximise button (see restore_from_maximised, where Qt held the
    maximized flag over a window Windows had already restored).

    A stale full-screen flag is worse than a stale maximized one,
    because this gate reads it: Qt says full screen, the drag is
    refused, and the window then cannot be moved or snapped **on any
    page** until the app is restarted - which is the owner's report of
    27 August 2026, "it do not go full size or half the monitor even if
    I go back to the other pages".

    So the flag has to agree with the geometry. A window that really is
    full screen covers its screen; one that Qt merely believes is full
    screen does not, and that one is safe to drag."""
    if not window.isFullScreen():
        return False
    try:
        screen = window.screen()
        full = screen.geometry() if screen else None
    except (AttributeError, RuntimeError):
        full = None
    if full is None:
        return True         # cannot check - trust Qt rather than guess
    return window.geometry().contains(full)


class DragStrip(QWidget):
    """A bare strip whose empty parts move the window.

    Presses that land on a child - a button, a field - never reach here,
    so the controls keep working and only the gaps between them drag."""

    def mousePressEvent(self, event):
        if begin_window_drag(self, event):
            return
        super().mousePressEvent(event)


class TitleBar(QWidget):
    """Back on the left, one search field in the middle, the window
    buttons on the right.

    No wordmark: the owner asked for img 3's shape explicitly *without*
    its "Harbor" lettering, and the app's own mark already sits at the
    top of the sidebar directly below this.

    Owns no state - `back`, `minimise`, `maximise` and `close_window`
    are signals, and the window wires them to what it already had."""

    section = Signal(str)          # "saved" / "schedule" / "history"
    minimise = Signal()
    maximise = Signal()
    close_window = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, objectName="TitleBar")
        self.setFixedHeight(BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 0, 0)
        row.setSpacing(0)

        # **No Back button** - the owner's ask, 26 August 2026: remove it
        # entirely, full screen included. Going back was never only this
        # button and is not lost with it: Alt+Left, mouse button 4
        # (main._MouseNavFilter), Escape out of an overlay, and each
        # surface's own way out all still do it.
        #
        # The left group stays as empty space of the same width as the
        # window buttons opposite, because that is what centres the
        # field in the *window* rather than in the room left beside it -
        # the balance Home's header uses, and the thing that would
        # silently drift by half the difference if it were dropped.
        # The two side groups are the same width, which is what centres
        # the field in the *window* rather than in the room left beside
        # it - three section buttons on the left against three window
        # buttons on the right.
        side = 3 * WINDOW_BUTTON_SIZE + 4 * WINDOW_BUTTON_GAP
        left = QWidget(objectName="Bare")
        left_row = QHBoxLayout(left)
        left_row.setContentsMargins(0, 0, 0, 0)
        left_row.setSpacing(2)
        self.section_buttons = {}
        for key, label in SECTION_BUTTONS:
            button = DriftButton("", radius=theme.RADIUS_SM,
                                 tint=theme.SURFACE, objectName="BarSection")
            button.setFixedSize(QSize(SECTION_BUTTON_WIDTH, 34))
            button.setToolTip(label)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            icon = images.tinted_asset(theme.rail_icon(key), theme.TEXT_MUTED,
                                       SECTION_ICON,
                                       float(self.devicePixelRatioF() or 1.0))
            if not icon.isNull():
                button.setIcon(QIcon(icon))
                button.setIconSize(QSize(SECTION_ICON, SECTION_ICON))
            else:
                button.setText(label[0])
            button.clicked.connect(
                lambda _checked=False, k=key: self.section.emit(k))
            # The hand cursor, same as the window buttons below get.
            # Missed when these three moved up here off the tracker
            # pages, where their old header pills were widgets.Card and
            # got it for free - so the only three buttons in the app
            # that kept an arrow were these (the owner, 26 August 2026).
            use_hover_cursor(button)
            # Not into `left_row`: they live beside the search field in
            # both states now (see set_fullscreen). They are built here,
            # before the centre row exists, so they are collected and
            # placed a few lines below rather than built twice.
            self.section_buttons[key] = button
        left_row.addStretch(1)
        left.setFixedWidth(side)
        row.addWidget(left)
        self._left_group = left
        self._left_row = left_row

        row.addStretch(1)
        # The field and the three section buttons beside it, in both
        # states - see set_fullscreen.
        centre = QWidget(objectName="Bare")
        centre_row = QHBoxLayout(centre)
        centre_row.setContentsMargins(0, 0, 0, 0)
        centre_row.setSpacing(8)
        self._centre_row = centre_row
        # Balances the section buttons that join this row in full
        # screen, so the *field* stays centred in the window rather than
        # the field-plus-buttons group - which would push the field left
        # of centre by half the buttons' width.
        # **A fixed-width holder on each side of the field, the same
        # width.** Letting the buttons sit loose in this row and
        # balancing them with a spacer was measured 36px out: the field
        # hits its own maximum width, the leftover goes to the stretches
        # rather than to the spacer, and the symmetry is lost. Two
        # containers of identical fixed width cannot drift.
        balance_width = 3 * SECTION_BUTTON_WIDTH + 2 * 6
        self._centre_balance = QWidget(objectName="Bare")
        self._centre_balance.setFixedWidth(balance_width)
        centre_row.addWidget(self._centre_balance)
        self.search = search_field("Search everything...")
        self.search.setObjectName("TopSearch")
        self.search.setFixedHeight(SEARCH_HEIGHT)
        self.search.setMinimumWidth(SEARCH_MIN_WIDTH)
        # One maximum, in both states - see set_fullscreen_search_width
        # for the measurement that collapsed the two.
        self.search.setMaximumWidth(SEARCH_MAX_WIDTH)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        centre_row.addWidget(self.search, stretch=3)
        self._centre_buttons = QWidget(objectName="Bare")
        self._centre_buttons.setFixedWidth(balance_width)
        buttons_row = QHBoxLayout(self._centre_buttons)
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(6)
        self._centre_buttons_row = buttons_row
        # Where the three of them actually go, in windowed and in full
        # screen alike. set_fullscreen only ever re-adds them, and it
        # runs on a *change* of state - so without this the first frame
        # of a windowed launch would have shown them nowhere at all.
        for button in self.section_buttons.values():
            buttons_row.addWidget(button)
        centre_row.addWidget(self._centre_buttons)
        self._centre_group = centre
        row.addWidget(centre, stretch=3)
        row.addStretch(1)

        right = QWidget(objectName="Bare")
        right_row = QHBoxLayout(right)
        # Breathing room around the discs, the way the reference has it -
        # they were a flush strip and had none. The right margin is what
        # keeps the close button off the window's own edge.
        #
        # **And that has a cost worth writing down**: measured 28 August
        # 2026, the outermost pixel column of a maximised window is not
        # delivered to Qt at all (underMouse False at x=right, True one
        # pixel in), so throwing the pointer into the very corner never
        # reached the old flush button either. This margin does not
        # create that; it does mean the corner is now empty by design
        # rather than by accident.
        right_row.setContentsMargins(0, 0, WINDOW_BUTTON_GAP, 0)
        right_row.setSpacing(WINDOW_BUTTON_GAP)
        right_row.addStretch(1)
        self._min_btn = self._window_button(MINIMISE_GLYPH, "Minimise",
                                            self.minimise.emit)
        self._max_btn = self._window_button("", "Maximise",
                                            self.maximise.emit)
        self._max_btn.setIconSize(QSize(WINDOW_BUTTON_SIZE, WINDOW_BUTTON_SIZE))
        self._apply_max_mark(False)
        self._close_btn = self._window_button(CLOSE_GLYPH, "Close",
                                              self.close_window.emit,
                                              name="WindowClose")
        for button in (self._min_btn, self._max_btn, self._close_btn):
            right_row.addWidget(button)
        right.setMinimumWidth(side)
        row.addWidget(right)
        self._right_group = right
        self._row = row
        # The two flexible gaps either side of the centred group. They
        # are what balances the section buttons against the window
        # buttons when the bar is a full-width strip; in full screen the
        # bar is sized to hug its content and there is nothing left to
        # balance, so they only take width away from the field - the
        # centre had 3/5 of the bar and the field measured 548px of a
        # 1100px allowance because of them.
        self._flex = [i for i in range(row.count())
                      if row.itemAt(i) is not None
                      and row.itemAt(i).spacerItem() is not None]

    def set_fullscreen(self, on: bool):
        """Full screen's arrangement: the field centred with Saved,
        Schedule and History immediately to its right, and the window's
        own buttons gone entirely.

        The owner's ask, 26 August 2026, pointing at Home's header - the
        field sits in the gap between "Good Evening" and the clock, and
        the three buttons follow it. Windowed keeps the design it has
        always had; only this state rearranges.

        Full screen has no use for minimise, maximise or close: there is
        no frame to restore and Escape and F11 both leave. Hiding them
        is what he asked for ("hide these buttons completely only while
        in fullscreen"), and it is also what frees the width the centred
        group needs.

        The buttons are moved between two live layouts rather than being
        duplicated - three widgets carrying tooltips, an event filter
        and a connected signal each, and a second set would double every
        one of those."""
        on = bool(on)
        if on == getattr(self, "_fullscreen", False):
            return
        if not on:
            # Back to being laid out rather than pinned.
            self.search.setMinimumWidth(0)
            self.search.setMaximumWidth(SEARCH_MAX_WIDTH)
        self._fullscreen = on
        # **The arrangement no longer changes with the state**, 28 August
        # 2026, the owner: "make the search bar and the saved/history/
        # schedule icons while non-fullscreen, exactly the same as if
        # fullscreened". The centred field with the three section buttons
        # immediately to its right was full screen's own layout and
        # windowed kept an older one, so the bar reshuffled itself every
        # time F11 was pressed - two designs to keep in step, and the one
        # you saw depended on a state that has nothing to do with where a
        # search box belongs.
        #
        # What is left as a difference is what full screen genuinely has
        # no use for: minimise, maximise and close, since there is no
        # frame to restore and Escape and F11 both leave. Everything
        # below this line is still keyed on `on` for that reason.
        for button in self.section_buttons.values():
            self._centre_buttons_row.addWidget(button)
            button.show()
        self._centre_balance.setVisible(True)
        self._centre_buttons.setVisible(True)
        self.search.setMaximumWidth(SEARCH_MAX_WIDTH)
        # **The stretch still differs, and that is not an arrangement.**
        # Full screen hugs its content (main._position_fullscreen_bar
        # sizes the bar to it, so the empty half stops eating clicks -
        # the measured Watch/Read and Clear Finished bug); windowed sits
        # in a strip of its own and has to fill it, or the field collapses
        # to its size hint. Measured with the stretch removed from both:
        # the first windowed frame gave the field 366px where 812 was
        # right. Same layout, same widths available - only who hands out
        # the leftover.
        for index in self._flex:
            self._row.setStretch(index, 0 if on else 1)
        # **The empty half of the bar stops eating clicks**, and this is
        # not a nicety - it was a bug the owner hit twice.
        #
        # Overlaid, the bar lies across the page's header row, and an
        # invisible widget still hit-tests. Measured with a full-width
        # bar: the Watch/Read tabs on Saved and the Downloads page's
        # Clear Finished button were *0 of 3* reachable, every click
        # landing on TitleBar - his "the watch and read tabs cannot be
        # pressed" and "clear finished cannot be pressed", one cause,
        # full screen only.
        #
        # WA_TransparentForMouseEvents on the bar itself was tried first
        # and is wrong: it takes the bar's *own* children out of hit
        # testing too, and measured the search field and all three
        # section buttons unreachable. So the attribute goes only on the
        # balance spacer, which is genuinely empty, and the real fix is
        # that main._position_fullscreen_bar sizes the bar to hug its
        # content instead of spanning the window.
        if not on:
            self.clearMask()
        self._left_group.setVisible(not on)
        self._right_group.setVisible(not on)
        # Transparent over the page in full screen - it is sitting on
        # the page's own header row there, not in a strip of its own.
        self.setProperty("chrome", "fullscreen" if on else "windowed")
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def _remember_search_width(self):
        """Keep the windowed field's width, for full screen to match.

        **Recorded continuously, not when F11 is pressed.** Reading it at
        the moment of the switch is what the first attempt did, and it
        read the wrong number: `set_fullscreen` runs from the window's
        changeEvent, by which point the window is *already* full screen
        and the bar has been re-laid out - measured, it remembered 760
        where the windowed field had been 370. So the last width the bar
        had while it was genuinely windowed is what gets kept."""
        if getattr(self, "_fullscreen", False):
            return
        width = self.search.width()
        if width > 0:
            self._windowed_search_width = width

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Off a zero timer: the field's own width is not settled until
        # the layout has run, and this fires before it.
        QTimer.singleShot(0, self._remember_search_width)

    def _apply_max_mark(self, maximised: bool):
        """Put the right drawn mark on the maximise button.

        Cached per state, because this is called on every maximise and
        restore and each call would otherwise repaint an icon that has
        not changed."""
        key = "restore" if maximised else "maximise"
        cache = getattr(self, "_max_marks", None)
        if cache is None:
            cache = self._max_marks = {}
        if key not in cache:
            cache[key] = (_window_mark_icon(key, theme.TEXT_MUTED),
                          _window_mark_icon(key, theme.TEXT))
        self._max_btn.set_hover_icons(*cache[key])

    def _window_button(self, glyph, tip, slot, name="WindowButton"):
        """One of the three, as a disc rather than a full-height slab.

        The owner's ask, 28 August 2026, with a picture: three separate
        round buttons, each on its own dark circle, rather than the
        edge-to-edge Windows-caption strip they were. They keep
        everything else they had - the drifting hover, the red on close,
        the arrow cursor - only the shape and the resting fill change.

        `radius` is half the size, which is what makes a square a
        circle; the resting fill is what makes it visible before the
        pointer arrives, since the hover fill alone left three invisible
        glyphs floating in the bar."""
        button = DriftButton(glyph, radius=WINDOW_BUTTON_SIZE / 2.0,
                             tint=(theme.DANGER if name == "WindowClose"
                                   else theme.SURFACE_HOVER),
                             objectName=name)
        button.resting = WINDOW_BUTTON_FILL
        button.setFont(theme.icon_font(9))
        button.setFixedSize(QSize(WINDOW_BUTTON_SIZE, WINDOW_BUTTON_SIZE))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        # **No pointing hand on these three**, 28 August 2026, the
        # owner: "make the hoover cursor on the upper bar buttons
        # max/min/X a normal mouse not a finger pointing". Minimise,
        # maximise and close are window *chrome*, not content - every
        # other app on this desktop leaves the arrow over them, and the
        # hand is what this app uses to mark the things a page offers.
        # The three section buttons beside them keep it (see the note
        # where they are built): those open pages.
        return button

    def mousePressEvent(self, event):
        """A press on the bare bar starts a **native** drag, so Aero
        Snap, Win+Arrow and drag-to-maximise all still happen - Windows
        is doing the dragging, not a mouseMoveEvent adding up deltas.

        Presses on the field and the buttons never arrive here: those
        widgets accept their own."""
        if begin_window_drag(self, event):
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """What a double-click on a caption has always meant - except in
        full screen, where it meant leaving it.

        The owner's ask, 26 August 2026: "do not make the double click
        exit the fullscreen mode". There is no caption to restore there,
        and the maximise this emitted went on to call showMaximized,
        which drops the window straight out of full screen."""
        if (event.button() == Qt.MouseButton.LeftButton
                and not self.window().isFullScreen()):
            self.maximise.emit()
            return
        super().mouseDoubleClickEvent(event)

    def apply_fullscreen_mask(self):
        """Let clicks through every part of the overlaid bar except the
        field and the three buttons.

        **A mask, because the two obvious answers both fail**, and both
        were measured on the way here:

          * Sizing the bar to hug its content is not enough once the
            field is allowed 1100px - the bar is then 1428 wide, its
            left edge lands on the page's own header row, and the Read
            tab underneath it measured unreachable.
          * WA_TransparentForMouseEvents on the bar takes its *own*
            children out of hit testing with it - Qt does not descend
            into a transparent widget - and measured 0 of 4 for the
            field and the three buttons.

        A mask sets the input region as well as the painted one, so the
        gaps stop existing as far as the mouse is concerned while the
        controls keep working normally. Re-applied on every reposition,
        because the rects move with the window."""
        if not getattr(self, "_fullscreen", False):
            self.clearMask()
            return
        layout = self.layout()
        if layout is not None:
            layout.activate()
        region = QRegion()
        for widget in (self.search, self._centre_buttons):
            if widget is None or not widget.isVisible():
                continue
            top_left = widget.mapTo(self, QPoint(0, 0))
            rect = QRect(top_left, widget.size())
            # A couple of pixels of slack, so a click on the very edge
            # of the field is not lost to rounding.
            region = region.united(QRegion(rect.adjusted(-2, -2, 2, 2)))
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)

    def set_fullscreen_search_width(self, room: int) -> int:
        """Give the field a share of the room the page actually has.

        The owner's ask, 26 August 2026: unfolding the sidebar should
        take a little off it. The page loses 148px when the rail opens,
        and a field pinned at its maximum ignored that entirely - it
        stayed the same width in a narrower page. A share of the room
        instead, floored at the windowed width so it can never come out
        narrower there than it is when not full screen.

        **Capped at the windowed maximum, 28 August 2026**, the owner:
        "make the search bar size in the Fullscreen same size as in the
        non-Fullscreen". It used to be capped at
        SEARCH_MAX_WIDTH_FULLSCREEN, which is half as wide again, and
        measured on this machine that is exactly the jump he is
        describing:

            windowed 1400px window   field  370px
            windowed 2000px window   field  730px
            full screen              field 1060px

        The windowed field grows with the window and tops out at
        SEARCH_MAX_WIDTH; sharing the same ceiling is what makes the two
        states land on the same field rather than on two designs. The
        share arithmetic stays, so unfolding the rail still takes a
        little off it."""
        # **And then pinned to the width the windowed bar actually had.**
        # Sharing the ceiling was not enough: windowed is laid out by the
        # row's stretch and full screen by this share, so on a 2048px
        # screen they still landed 682 against 760. The remembered
        # number is the only thing that makes them the *same* rather
        # than merely similar - it is literally the field the user was
        # looking at a moment before pressing F11.
        wanted = max(SEARCH_MAX_WIDTH,
                     min(SEARCH_MAX_WIDTH, int(room * FULLSCREEN_SEARCH_SHARE)))
        remembered = getattr(self, "_windowed_search_width", 0)
        if remembered:
            wanted = remembered
        if wanted != self.search.maximumWidth():
            self.search.setMaximumWidth(wanted)
        self.search.setFixedWidth(wanted)
        return wanted

    def fullscreen_width(self) -> int:
        """How wide the bar needs to be when it is overlaying a page -
        the centred group and nothing more, so everything either side of
        it belongs to the page underneath.

        Read off the field's *current* maximum, not the constant: it
        follows the page width now (see set_fullscreen_search_width) and
        a bar sized from the cap would leave a dead margin whenever the
        field is under it."""
        balance = self._centre_balance.width() or SECTION_BUTTON_WIDTH * 3 + 12
        return (balance
                + self.search.maximumWidth()
                + self._centre_buttons.width()
                + self._centre_row.spacing() * 2
                + 24)

    def set_maximised(self, maximised: bool):
        """Swap the middle glyph. Restore and maximise are different
        shapes in the icon font, and a bar still showing "maximise" on a
        maximised window is the piece of this people notice first."""
        self._apply_max_mark(maximised)
        self._max_btn.setToolTip("Restore" if maximised else "Maximise")
