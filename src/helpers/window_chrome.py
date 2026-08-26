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

from PyQt6.QtCore import QEvent, QObject, QPoint, QRectF, QSize, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from . import theme
from .widgets import SmoothTween, search_field, use_hover_cursor

WINDOWS = sys.platform == "win32"

# ---- The bar's own metrics ----------------------------------------------
# 48px: tall enough for a 34px field with air above and below, short
# enough that it does not read as a second header over a page that has
# one of its own. Windows' caption is 32 at 100% - this is the app's
# bar, not an imitation of that one.
BAR_HEIGHT = theme.TITLE_BAR_HEIGHT
WINDOW_BUTTON_WIDTH = 46
# The field is centred *in the window*, not in the space left over
# between the two side groups - so both groups take the same minimum
# width and the field sits between two equal stretches. Home's header
# already does this with a balancing spacer, for the same reason and
# with the same failure when it is skipped: the field drifts left by
# half the difference between the two sides.
SEARCH_MAX_WIDTH = 560
SEARCH_MIN_WIDTH = 240
SEARCH_HEIGHT = theme.TOP_SEARCH_HEIGHT

# Segoe Fluent Icons, the faces every other chrome button in this app
# already uses (theme.FONT_FAMILY_ICONS). Monochrome, so they inherit
# the button's colour - which is exactly why this app does not use emoji
# for chrome, as theme.py and rail_icons.py both record.
MINIMISE_GLYPH = ""   # ChromeMinimize
MAXIMISE_GLYPH = ""   # ChromeMaximize
RESTORE_GLYPH = ""    # ChromeRestore
CLOSE_GLYPH = ""      # ChromeClose

# How wide the grab band along each edge is, in logical pixels. 6 rather
# than Windows' own 4: there is no frame drawn to aim at here, so the
# band has to be forgiving enough to hit without hunting for it.
RESIZE_BAND = 6

_CORNERS = (
    (Qt.Edge.LeftEdge, Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
    (Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.LeftEdge, Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.RightEdge, Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
)


# Window styles. WS_CAPTION is deliberately absent - putting it back is
# putting the title bar back.
GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000


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
        user.SetWindowLongW(hwnd, GWL_STYLE,
                            ctypes.c_long(style | WS_THICKFRAME
                                          | WS_MAXIMIZEBOX | WS_MINIMIZEBOX))
    except Exception:
        # A platform plugin with no real HWND, or a Windows build that
        # refuses the change - the window is still frameless and still
        # draggable, it just will not snap.
        return True
    return True


def edges_at(window, pos):
    """Which window edges `pos` (window-local, logical) is within
    grabbing distance of, or None.

    Never while maximised or full screen: there is no edge to drag in
    either state, and a live band there would eat clicks on whatever
    sits under it - the sidebar's first row, in this layout."""
    if window.isMaximized() or window.isFullScreen():
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

    def enterEvent(self, event):
        self._hover_tween.start(self._hover, 1.0, self.HOVER_MS)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_tween.start(self._hover, 0.0, self.HOVER_MS)
        self._press_tween.start(self._press, 0.0, self.PRESS_MS)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._press_tween.start(self._press, 1.0, self.PRESS_MS)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_tween.start(self._press, 0.0, self.PRESS_MS)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
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


class TitleBar(QWidget):
    """Back on the left, one search field in the middle, the window
    buttons on the right.

    No wordmark: the owner asked for img 3's shape explicitly *without*
    its "Harbor" lettering, and the app's own mark already sits at the
    top of the sidebar directly below this.

    Owns no state - `back`, `minimise`, `maximise` and `close_window`
    are signals, and the window wires them to what it already had."""

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
        side = 3 * WINDOW_BUTTON_WIDTH
        left = QWidget(objectName="Bare")
        left.setFixedWidth(side)
        row.addWidget(left)

        row.addStretch(1)
        self.search = search_field("Search everything...")
        self.search.setObjectName("TopSearch")
        self.search.setFixedHeight(SEARCH_HEIGHT)
        self.search.setMinimumWidth(SEARCH_MIN_WIDTH)
        self.search.setMaximumWidth(SEARCH_MAX_WIDTH)
        self.search.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        row.addWidget(self.search, stretch=3)
        row.addStretch(1)

        right = QWidget(objectName="Bare")
        right_row = QHBoxLayout(right)
        right_row.setContentsMargins(0, 0, 0, 0)
        right_row.setSpacing(0)
        right_row.addStretch(1)
        self._min_btn = self._window_button(MINIMISE_GLYPH, "Minimise",
                                            self.minimise.emit)
        self._max_btn = self._window_button(MAXIMISE_GLYPH, "Maximise",
                                            self.maximise.emit)
        self._close_btn = self._window_button(CLOSE_GLYPH, "Close",
                                              self.close_window.emit,
                                              name="WindowClose")
        for button in (self._min_btn, self._max_btn, self._close_btn):
            right_row.addWidget(button)
        right.setMinimumWidth(side)
        row.addWidget(right)

    def _window_button(self, glyph, tip, slot, name="WindowButton"):
        button = DriftButton(glyph, radius=0,
                             tint=(theme.DANGER if name == "WindowClose"
                                   else theme.SURFACE_HOVER),
                             objectName=name)
        button.setFont(theme.icon_font(9))
        button.setFixedSize(QSize(WINDOW_BUTTON_WIDTH, BAR_HEIGHT))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        use_hover_cursor(button)
        return button

    def mousePressEvent(self, event):
        """A press on the bare bar starts a **native** drag, so Aero
        Snap, Win+Arrow and drag-to-maximise all still happen - Windows
        is doing the dragging, not a mouseMoveEvent adding up deltas.

        Presses on the field and the buttons never arrive here: those
        widgets accept their own."""
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """What a double-click on a caption has always meant."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximise.emit()
            return
        super().mouseDoubleClickEvent(event)

    def set_maximised(self, maximised: bool):
        """Swap the middle glyph. Restore and maximise are different
        shapes in the icon font, and a bar still showing "maximise" on a
        maximised window is the piece of this people notice first."""
        self._max_btn.setText(RESTORE_GLYPH if maximised else MAXIMISE_GLYPH)
        self._max_btn.setToolTip("Restore" if maximised else "Maximise")

