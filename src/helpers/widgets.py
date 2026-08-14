"""Small reusable widgets shared across windows."""

import weakref

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QLabel, QScrollArea, QToolTip, QWidget,
)

from . import theme


class GlassPage(QWidget):
    """Base for a page: paints a soft dark radial-gradient backdrop
    behind whatever layout/content the subclass adds on top of it."""

    def __init__(self, base=theme.BG, glow=theme.GLOW, parent=None):
        super().__init__(parent)
        self._base = QColor(base)
        self._glow = QColor(glow)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._base)
        w, h = self.width(), self.height()
        center = QPointF(w * 0.5, h * 0.0)
        radius = max(w, h) * 0.95
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0.0, self._glow)
        gradient.setColorAt(1.0, self._base)
        painter.fillRect(self.rect(), gradient)
        super().paintEvent(event)


# Widgets currently holding the pointing-hand cursor because the pointer
# is inside them. Weak, so a card being torn down on a page rebuild drops
# out on its own rather than being kept alive by this.
#
# The registry exists because Qt's Leave event cannot be relied on. After
# a modal dialog closes, Qt's idea of which widget the pointer is over is
# stale, and moving off the widget it thinks you are on generates no
# Leave at all - so that widget goes on answering "pointing hand" for
# every cursor query, and the hand follows the pointer across the whole
# app. Knowing who is holding one makes it possible to check them against
# the pointer's real position and let go (see release_stale_hover_cursors).
_HOVER_CURSOR_WIDGETS = weakref.WeakSet()


def hold_hover_cursor(widget):
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    _HOVER_CURSOR_WIDGETS.add(widget)


def release_hover_cursor(widget):
    widget.unsetCursor()
    _HOVER_CURSOR_WIDGETS.discard(widget)


def release_stale_hover_cursors(global_pos):
    """Let go of the hand cursor on any widget the pointer is not really
    inside, whatever Qt believes. Cheap: at most a couple of widgets are
    ever holding one at a time."""
    for widget in list(_HOVER_CURSOR_WIDGETS):
        try:
            inside = (widget.isVisible()
                      and widget.rect().contains(widget.mapFromGlobal(global_pos)))
        except RuntimeError:
            inside = False   # already deleted on the C++ side
        if not inside:
            release_hover_cursor(widget)


class _HoverCursorFilter(QObject):
    """Gives a plain widget the same hover-only cursor behaviour the
    cards below have, without needing to subclass it."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Enter:
            hold_hover_cursor(obj)
        elif event.type() == QEvent.Type.Leave:
            release_hover_cursor(obj)
        return False


def use_hover_cursor(widget):
    """Point-and-click cursor for `widget`, only while the pointer is
    genuinely inside it. Parented to the widget so the filter lives
    exactly as long as it does."""
    widget.installEventFilter(_HoverCursorFilter(widget))
    return widget


class Card(QFrame):
    """A clickable card (icon + label, cover art, etc.) with hover
    feedback and left/right click signals.

    `matte` swaps the glossy gradient fill for a flat one (see the
    #Card rules in theme.py) - Home/Games/Apps/Websites pass it, the
    poster grids on Anime/Reading/Series don't."""

    clicked = Signal()
    rightClicked = Signal(object)  # emits the originating QMouseEvent

    def __init__(self, parent=None, hoverable=True, matte=False):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("hoverable", hoverable)
        self.setProperty("matte", matte)
        self._tooltip_provider = None

    # A card carries no cursor of its own until the pointer is genuinely
    # inside it. Setting the pointing hand in __init__ instead - which is
    # what this used to do - meant every card was born holding a cursor,
    # and a page rebuild creates a screenful of them at once. Rebuilds
    # happen while the pointer is somewhere else entirely (on the
    # Settings dialog floating above, after launching a game), and that
    # left the hand on screen with nothing under the pointer wanting it,
    # stuck there across every page afterwards.
    #
    # Enter/Leave is the reliable signal for "the pointer is really in
    # here": it's the same mechanism behind the :hover rule that already
    # paints these cards' highlight, so if the highlight is correct the
    # cursor now is too. Nothing gets a hand cursor it didn't earn by
    # actually being hovered.
    def enterEvent(self, event):
        hold_hover_cursor(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        release_hover_cursor(self)
        super().leaveEvent(event)

    def set_tooltip_provider(self, provider):
        """Build this card's tooltip fresh on every hover instead of once
        at construction - for content that goes stale sitting there, like
        a countdown to the next episode. The generated text is also set
        as the plain tooltip, so the card still has one if the event
        below never fires (and so Qt knows to offer one at all)."""
        self._tooltip_provider = provider
        self.setToolTip(provider())

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and self._tooltip_provider:
            QToolTip.showText(event.globalPos(), self._tooltip_provider(), self)
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        elif event.button() == Qt.MouseButton.RightButton:
            self.rightClicked.emit(event)
        super().mousePressEvent(event)


def _toast_anchor_window(widget):
    """The app's main window, not whatever dialog happens to sit on top
    of it - a scan started from the Settings dialog should still drop its
    toast in the app's own bottom-right corner rather than the dialog's
    (which floats mid-screen, so the toast looked misplaced)."""
    window = widget.window()
    while isinstance(window, QDialog) and window.parent() is not None:
        window = window.parent().window()
    return window


# How long a "sticky" toast (duration_ms=None - one that waits for a
# background job to report back) is allowed to sit there before it gives
# up and closes itself. Nothing should ever reach this: it is purely so a
# lookup that never returns - or a page torn down mid-refresh, taking the
# handler that would have finished the toast with it - can't leave
# "Updating..." on screen for the rest of the session.
STICKY_TOAST_MAX_MS = 120_000


class Toast(QLabel):
    """A small, self-dismissing confirmation message in the bottom-right
    corner of the app's main window - for lightweight feedback (e.g.
    "Saved") that doesn't need a modal dialog click to dismiss.

    `duration_ms=None` makes it stick around instead of fading on its own,
    for the "working... / here's the result" pattern: show one while a
    background job runs, then hand it its result with set_text (see
    finish_toast, which is what callers should actually use)."""

    def __init__(self, anchor, text, duration_ms=2000):
        window = _toast_anchor_window(anchor)
        super().__init__(text, window)
        self._anchor_window = window
        self.setWindowFlags(Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.TEXT}; "
            f"border: 1px solid {theme.ACCENT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 10px 18px; font-weight: 700;")
        # A timer owned by this toast, rather than QTimer.singleShot: the
        # message can be replaced while it's up (see set_text), and the
        # dismissal that was scheduled for the old message has to be
        # called off when that happens.
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.close)
        # Shown before being positioned: a ToolTip-flagged window only
        # settles at its final polished size once shown, and positioning
        # against a stale size would push it past the intended corner.
        self.show()
        self._move_to_corner(window)
        self._dismiss_timer.start(duration_ms if duration_ms else STICKY_TOAST_MAX_MS)

    def set_text(self, text, duration_ms=2000):
        """Swap the message in place and restart the countdown to closing.

        In place rather than closing this one and opening another, so the
        box doesn't blink out and back in between "Updating..." and its
        result - it just re-reads."""
        self.setText(text)
        # Re-anchors as well as re-measures: the replacement message is a
        # different width, and these are positioned from their bottom-
        # right corner, so the box would otherwise grow off to the right.
        self._move_to_corner(self._anchor_window)
        self._dismiss_timer.stop()
        self._dismiss_timer.start(duration_ms if duration_ms else STICKY_TOAST_MAX_MS)

    def _move_to_corner(self, window, margin=24):
        self.adjustSize()
        corner = window.mapToGlobal(QPoint(window.width(), window.height()))
        x, y = corner.x() - self.width() - margin, corner.y() - self.height() - margin
        # Clamped to the visible desktop so it can't end up off-screen if
        # the anchor window itself is partly outside it.
        screen = window.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)


def show_toast(anchor, text, duration_ms=2000):
    """Drop a message in the corner. `duration_ms=None` keeps it up until
    finish_toast replaces it; the returned Toast is the handle for that."""
    return Toast(anchor, text, duration_ms)


def finish_toast(toast, anchor, text, duration_ms=2600):
    """Report a background job's result into the toast that announced it,
    replacing "Updating..." with what actually happened.

    `toast` may be gone by now - a toast is deleted when it closes, and a
    sticky one closes itself eventually (see STICKY_TOAST_MAX_MS) - so the
    result is shown as a fresh toast in that case rather than silently
    dropped. Slightly longer than the default dwell: a result is worth
    reading, where "Updating..." only needed to be noticed."""
    try:
        if toast is not None:
            toast.set_text(text, duration_ms)
            return
    except RuntimeError:
        pass  # already closed and deleted on the C++ side
    show_toast(anchor, text, duration_ms)


def scroll_area(body: QWidget, always_show_vbar: bool = False) -> QScrollArea:
    """Wrap `body` in a frameless, resizable, mouse-wheel-scrollable area.

    `always_show_vbar` reserves the vertical scrollbar's width whether or
    not it's actually needed, instead of the default "only when content
    overflows" - the scrollbar only ever eats width from the right, so
    on a page whose content only sometimes overflows, its coming and
    going shifts anything centered against the viewport's width left/
    right depending on scroll state. Pages with fixed-width centered
    content (Home's hero) want that width reserved unconditionally so
    centering math stays consistent either way; pages that never center
    anything against the full viewport width don't need it."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    if always_show_vbar:
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    area.setWidget(body)
    return area
