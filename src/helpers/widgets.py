"""Small reusable widgets shared across windows."""

import weakref

from PyQt6.QtCore import QEvent, QMimeData, QObject, QPointF, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QDrag, QPainter, QRadialGradient
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
    # The C++ widget can already be gone while this registry still lists
    # it: a WeakSet drops an entry when the *Python* wrapper dies, and a
    # wrapper outlives its deleteLater()'d widget for as long as anything
    # still references it - which the Home carousel's slide labels do
    # (see home._transition_hero, which deleteLater()s three of them
    # every 5s while _hero_mid_widget still names one).
    #
    # This is what crashed the app. unsetCursor() on a deleted wrapper
    # raises RuntimeError, and this runs inside the cursor watchdog's
    # timer slot (main._cursor_watchdog_tick, every 120ms) - PyQt6 turns
    # an exception that escapes a slot into qFatal(), which is an
    # immediate abort, not a traceback. Measured: the frozen build died
    # 20.5s after launch with exit code 0xc0000409 (fastfail, faulting
    # module Qt6Core.dll) with the pointer simply resting over the hero,
    # matching the reported "crashes about 20 seconds after opening".
    #
    # Unsetting the cursor on a widget that no longer exists is a no-op
    # worth nothing, so swallow it - but drop it from the registry
    # either way, which is the part that actually needs doing.
    try:
        widget.unsetCursor()
    except RuntimeError:
        pass  # already deleted on the C++ side - nothing to unset
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
        # All None/False unless enable_drag_reorder is called, which is
        # what keeps a plain card (Home's, the ones in previews) on the
        # original click-on-press path untouched.
        self._drag_id = None
        self._drag_reorder = None
        self._press_pos = None
        self._dragged = False

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

    # ---- drag-to-reorder ---------------------------------------------
    def enable_drag_reorder(self, item_id, reorder):
        """Let this card be dragged to a new position in its grid.

        Opt-in per card, because it changes when `clicked` fires: a
        draggable card can't emit on press (which is what the plain card
        does) - the press that starts a drag would launch the game or
        open the entry as well as moving it. It emits on release instead,
        and not at all if the pointer travelled far enough to become a
        drag."""
        self._drag_id = item_id
        self._drag_reorder = reorder

    def drag_id(self):
        return self._drag_id

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            # Any half-finished left press is abandoned: the context menu
            # runs its own event loop, and the release that eventually
            # arrives should not be read as a click on the card behind it.
            self._press_pos = None
            self.rightClicked.emit(event)
        elif event.button() == Qt.MouseButton.LeftButton:
            if self._drag_reorder is None:
                self.clicked.emit()
            else:
                self._press_pos = event.position().toPoint()
                self._dragged = False
                # Accepted explicitly: QFrame's handler ignores the press,
                # which hands the mouse grab to the parent and means no
                # further move events arrive here - and a drag that can
                # never see the pointer move can never start.
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_reorder is None or self._press_pos is None
                or not (event.buttons() & Qt.MouseButton.LeftButton)):
            super().mouseMoveEvent(event)
            return
        travelled = (event.position().toPoint() - self._press_pos).manhattanLength()
        if travelled < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        self._dragged = True
        press_pos, self._press_pos = self._press_pos, None
        # Nothing may touch `self` after this: start_drag runs a nested
        # event loop for the whole gesture, and the drop that ends it
        # rebuilds the grid - so this card is on its way out by the time
        # the call returns.
        self._drag_reorder.start_drag(self, press_pos)

    def mouseReleaseEvent(self, event):
        if (self._drag_reorder is not None and event.button() == Qt.MouseButton.LeftButton
                and self._press_pos is not None and not self._dragged):
            self._press_pos = None
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# The dragged card's id, as the drag's payload. A private type rather
# than text/plain so a stray drag from outside the app - a file, a link -
# can never be read as one of these.
CARD_DRAG_MIME = "application/x-atomic-card-id"

# How close to the top/bottom edge of the scroll viewport the pointer has
# to get before the page starts following it, and how far it moves per
# drag-move event. Without this a grid taller than the window can only be
# reordered within the part of it you can already see.
_AUTOSCROLL_MARGIN = 56
_AUTOSCROLL_STEP = 18


class CardDragReorder(QObject):
    """Drag-to-reorder for a grid of Cards laid out inside `container`.

    The container is the drop site, not the individual cards: a Card sets
    no acceptDrops, so Qt walks up the parent chain and delivers every
    drag event here, to one filter, instead of needing a handler on each
    of the however-many cards a page just built.

    `on_begin()` fires the moment a drag actually starts - before the
    drag's own event loop - so the page can switch its sort to Custom
    Order first. That has to happen at the start and not at the drop:
    a non-custom sort is re-applied on the next redraw and would put the
    dragged card straight back where it came from.

    `on_drop(moved_id, target_id)` gets the two card ids and does the
    reordering; positions are deliberately not passed, since where a card
    sits in the grid says nothing about where its entry sits in the saved
    list (Anime/Reading/Series draw one section per status)."""

    def __init__(self, container, on_begin, on_drop):
        super().__init__(container)
        self._container = container
        self._on_begin = on_begin
        self._on_drop = on_drop
        container.setAcceptDrops(True)
        container.installEventFilter(self)

    def attach(self, card, item_id):
        card.enable_drag_reorder(item_id, self)

    def start_drag(self, card, press_pos):
        self._on_begin()
        drag = QDrag(card)
        mime = QMimeData()
        mime.setData(CARD_DRAG_MIME, str(card.drag_id()).encode("utf-8"))
        drag.setMimeData(mime)
        # grab() already returns a pixmap at the display's devicePixelRatio
        # with the ratio tagged on it, so the dragged card is as sharp as
        # the one it was lifted off - the hot spot below is in logical
        # pixels either way, which is why it can come straight from the
        # press position.
        drag.setPixmap(card.grab())
        drag.setHotSpot(press_pos)
        drag.exec(Qt.DropAction.MoveAction)

    # ------------------------------------------------------------------
    def _cards(self):
        """The cards currently laid out in the container.

        Asked of the container each time rather than kept in a list: the
        grid is torn down and rebuilt on every sort change and every
        edit, and a list of cards would be a list of deleted ones.

        Read off the layout rather than off findChildren, because a
        rebuilt grid's old cards are only takeAt'd and deleteLater'd -
        they stay children of the container, at their old geometry, until
        the event loop actually gets to the delete. Found by findChildren
        they were live drop targets holding stale positions, and a drop
        could resolve to a card that was no longer on screen."""
        layout = self._container.layout()
        if layout is None:
            return []
        cards = []
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if isinstance(widget, Card) and widget.drag_id() is not None:
                cards.append(widget)
        return cards

    def _card_at(self, pos):
        """The card the pointer is over, or - in the gaps between cards,
        and in the empty space past the end of a row - the nearest one.
        Nearest rather than nothing, so a drop that lands a few pixels
        into the 14px gutter still does what it obviously meant."""
        best, best_distance = None, None
        for card in self._cards():
            geometry = card.geometry()
            if geometry.contains(pos):
                return card
            offset = geometry.center() - pos
            distance = offset.x() ** 2 + offset.y() ** 2
            if best_distance is None or distance < best_distance:
                best, best_distance = card, distance
        return best

    def _autoscroll(self, pos):
        parent = self._container.parentWidget()
        area = parent.parentWidget() if parent is not None else None
        if not isinstance(area, QScrollArea):
            return
        bar = area.verticalScrollBar()
        # Into the viewport's own coordinates. mapToParent walks one step
        # up the widget tree by widget position - no screen or global
        # coordinates involved, so nothing here depends on the scale
        # factor of whichever monitor the window is on.
        y = self._container.mapToParent(pos).y()
        if y < _AUTOSCROLL_MARGIN:
            bar.setValue(bar.value() - _AUTOSCROLL_STEP)
        elif y > parent.height() - _AUTOSCROLL_MARGIN:
            bar.setValue(bar.value() + _AUTOSCROLL_STEP)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if not event.mimeData().hasFormat(CARD_DRAG_MIME):
                return False
            if kind == QEvent.Type.DragMove:
                self._autoscroll(event.position().toPoint())
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return True
        if kind == QEvent.Type.Drop:
            if not event.mimeData().hasFormat(CARD_DRAG_MIME):
                return False
            moved_id = bytes(event.mimeData().data(CARD_DRAG_MIME)).decode("utf-8")
            target = self._card_at(event.position().toPoint())
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            if target is not None and str(target.drag_id()) != moved_id:
                self._on_drop(moved_id, str(target.drag_id()))
            return True
        return False


def defer_grid_rebuild(rebuild):
    """Redraw a grid *after* the drag that changed it has finished.

    Called straight from the drop handler, the rebuild deletes the card
    the drag is still being carried by - its mouseMoveEvent is sitting
    underneath the drag's nested event loop, and would return into a
    widget that no longer exists on the C++ side. A zero-delay timer puts
    the rebuild back on the outer event loop, after the gesture is fully
    unwound."""
    QTimer.singleShot(0, rebuild)


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
        """Sit in the bottom-right corner of the anchor window.

        Off window.geometry(), which is already in global coordinates,
        rather than mapToGlobal(width, height) - which this used to do and
        which does not survive two monitors on different scale factors.
        With the primary display at 125% and the app maximized on a 100%
        one, that call came back 1032 -> 826: the window's real bottom
        edge divided by the *other* screen's scale factor. Every message
        was then placed against a bottom edge some 200px too high, which
        is why they floated in the middle of the page instead of sitting
        in the corner.
        """
        self.adjustSize()
        frame = window.geometry()
        x = frame.x() + frame.width() - self.width() - margin
        y = frame.y() + frame.height() - self.height() - margin
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
