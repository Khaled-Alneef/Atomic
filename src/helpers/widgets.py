"""Small reusable widgets shared across windows."""

import weakref

from PyQt6.QtCore import (QEasingCurve, QEvent, QMimeData, QObject, QPoint,
                          QPointF, QPropertyAnimation, QRect, QRectF, Qt,
                          QTimer)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QColor, QDrag, QIcon, QLinearGradient, QPainter, QPen,
                         QPixmap, QRadialGradient)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QToolTip, QWidget,
)

from . import logs, theme


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
        could resolve to a card that was no longer on screen.

        Walked recursively, through nested layouts and through a
        QScrollArea's own widget, because a container's layout no longer
        holds cards directly on every page: the tracker draws a section
        title and a sideways-scrolling strip per status, so a flat read
        of the top layout found no cards at all and drag-to-reorder
        silently stopped working there - a drag would start, switch the
        sort to Custom Order, and drop nothing."""
        return self._cards_in(self._container.layout())

    def _cards_in(self, layout):
        if layout is None:
            return []
        cards = []
        for i in range(layout.count()):
            item = layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, Card) and widget.drag_id() is not None:
                cards.append(widget)
            elif isinstance(widget, QScrollArea) and widget.widget() is not None:
                cards.extend(self._cards_in(widget.widget().layout()))
            elif widget is not None and widget.layout() is not None:
                cards.extend(self._cards_in(widget.layout()))
            elif item.layout() is not None:
                cards.extend(self._cards_in(item.layout()))
        return cards

    def _card_at(self, pos):
        """The card the pointer is over, or - in the gaps between cards,
        and in the empty space past the end of a row - the nearest one.
        Nearest rather than nothing, so a drop that lands a few pixels
        into the 14px gutter still does what it obviously meant."""
        best, best_distance = None, None
        for card in self._cards():
            # In the container's coordinates, not the card's parent's: a
            # card inside a section strip has a geometry relative to that
            # strip, and comparing it against a pointer position measured
            # on the container matched the wrong card (or none).
            geometry = QRect(card.mapTo(self._container, QPoint(0, 0)), card.size())
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

    def __init__(self, anchor, text, duration_ms=2000, clickable=False):
        window = _toast_anchor_window(anchor)
        super().__init__(text, window)
        self._anchor_window = window
        self.setWindowFlags(Qt.WindowType.ToolTip)
        if not clickable:
            # Transparent to the mouse so a message sitting in the corner
            # can never swallow a click meant for the page underneath.
            # Set here rather than cleared later by the clickable
            # subclass: Qt folds this attribute into
            # Qt::WindowTransparentForInput when it creates the native
            # window, which has happened by the time __init__ returns
            # (see show() below), and clearing the attribute afterwards
            # leaves the real window still transparent to input.
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


# How long an undo offer stays up. Deliberately longer than the 2s a
# plain confirmation gets: that one only has to be noticed, this one has
# to be read and acted on. Letting it expire is the "no" answer - the
# removal is then as final as it was before undo existed.
UNDO_TOAST_MS = 8000


class UndoToast(Toast):
    """A toast offering back what was just removed. Clicking it anywhere
    undoes the removal; ignoring it lets the removal stand.

    The whole box is the button rather than a small "Undo" word inside
    it: a toast is a ToolTip-flagged window with no layout of its own,
    and a 40px target in the corner of the screen is worse to hit than
    the box that is already there and already says what it does."""

    def __init__(self, anchor, text, on_undo, duration_ms=UNDO_TOAST_MS,
                 on_redo=None):
        super().__init__(anchor, text, duration_ms, clickable=True)
        self._on_undo = on_undo
        self._on_redo = on_redo
        self._spent = False
        use_hover_cursor(self)

    def mousePressEvent(self, event):
        self.trigger_undo()

    def trigger_undo(self):
        """Take the offer. Called by a click on the box and by Ctrl+Z,
        which reaches this same offer rather than keeping an undo history
        of its own."""
        if self._spent:
            return
        # Marked spent before the callback runs, not after: restoring
        # rebuilds a page, which spins the event loop, and a second click
        # arriving in there would put the same entry back twice.
        self._spent = True
        try:
            message = self._on_undo()
        except Exception:
            # Never let this out. An exception escaping a reimplemented
            # event handler is not a traceback in PyQt6, it is an abort -
            # the same failure mode release_hover_cursor above is written
            # around. A failed undo has to read as a failed undo, not as
            # the app vanishing while the user watches.
            logs.exception("undo failed")
            message = "Couldn't Undo That"
            self.set_text(message, 2600)
            return
        # Only a successful undo leaves something to redo.
        global _live_redo
        _live_redo = self._on_redo
        self.set_text(message or "Restored", 2600)


# The undo offer currently on screen, if any. Ctrl+Z has to reach the
# same offer the toast shows rather than keeping an undo history of its
# own: the offer already knows what was removed, already knows how to put
# it back, and already expires. A second record would be a second answer
# to "what does undo do right now".
_live_undo_toast = None


_live_redo = None


def take_live_redo():
    """What Ctrl+Y should re-apply, once, or None.

    Set only by an undo that actually ran, and cleared by taking it or by
    the next removal - so redo always means "put back the thing Ctrl+Z
    just took away", never something from three pages ago. A page that
    has been rebuilt since is not a problem the way it is for undo: redo
    re-runs the page's own action, which reads the file itself."""
    global _live_redo
    redo, _live_redo = _live_redo, None
    return redo


def take_live_undo():
    """The undo offer on screen, once. None when there is nothing to
    undo - an expired, spent or withdrawn offer never comes back."""
    global _live_undo_toast
    toast, _live_undo_toast = _live_undo_toast, None
    if toast is None or not toast.isVisible() or toast._spent:
        return None
    return toast


def show_undo_toast(page, text, on_undo, duration_ms=UNDO_TOAST_MS, on_redo=None):
    """Offer back the removal `page` just made. `on_undo` does the
    restoring and returns the message to replace the offer with.

    The offer is withdrawn when the page that made it is destroyed:
    navigating away builds the next page from scratch and deletes this
    one (main._show_page), so an undo clicked afterwards would write the
    entry back to disk behind a page that had already loaded the list
    without it - correct on disk, wrong on screen, with nothing to
    prompt a redraw."""
    global _live_undo_toast, _live_redo
    toast = UndoToast(page, text, on_undo, duration_ms, on_redo)
    page.destroyed.connect(toast.close)
    _live_undo_toast = toast
    # A new removal supersedes whatever redo was pending: redo means the
    # last thing undone, and that is no longer it.
    _live_redo = None
    return toast


class GridSelection:
    """Multi-select and batch delete for a flat grid of Cards - Games,
    Apps and Websites.

    A mixin rather than a copy per page: Games and the two link grids
    draw the same card in the same grid, and the one thing this has to
    get right (what is selectable, and what a delete writes) is exactly
    what would drift if it were written twice. It is deliberately *not*
    shared with the tracker pages, which carry their own version -
    theirs adds Set Status, and its cards are posters in per-status
    sideways-scrolling strips rather than one flat grid.

    The shape follows the tracker's, including the two rules that decide
    how selection coexists with what is already on these pages:

    * **Only what is on screen can be selected.** _prune_selection is
      called from every redraw, so a search or a re-sort can never leave
      an entry selected that the user cannot see, and "Select All" means
      the cards in front of them.
    * **Dragging is off while selecting.** Both want the same left
      press, and dragging is already off whenever a search narrows the
      grid - this is the same switch with one more reason in it.

    A mode rather than Ctrl+click: `Card.clicked` carries no modifiers,
    and nothing on screen would ever say the page could do this. The
    mode announces itself - the button reads "Done", a bar appears,
    every card carries a mark.

    The page supplies `_refresh_grid()` and `_mutate(apply_change)` (the
    re-read-then-apply save path); this supplies everything else.
    """

    # (singular, plural) for the counts this puts in front of the user.
    SELECTION_NOUN = ("entry", "entries")

    def _init_selection(self):
        """Call before the page's first _refresh_grid.

        Ids rather than entries or cards: a page rebuilds every card from
        scratch on every redraw (.claude/rules/ui.md), so anything holding
        widgets or dicts across one is holding what has just been torn
        down."""
        self._select_mode = False
        self._selected_ids = set()
        # entry id -> (card, badge) for the cards currently drawn, so a
        # click can repaint one mark instead of rebuilding the grid.
        self._selection_cards = {}

    # ---- the two controls the page puts in its own layout -------------
    def _build_select_button(self, tooltip):
        """The mode's switch, for the page's toolbar row.

        Its label is the state - "Select" when off, "Done" when on -
        rather than a checkable button: theme.py gives a plain
        QPushButton no :checked rule, so a ticked one would look
        identical to an unticked one, and a mode nobody can see they are
        in is the whole failure this is written around."""
        self.select_btn = QPushButton("Select")
        self.select_btn.setFixedHeight(40)
        self.select_btn.setToolTip(tooltip)
        self.select_btn.clicked.connect(self._toggle_select_mode)
        return self.select_btn

    def _build_selection_bar(self):
        """The row that appears under the toolbar while selecting.

        Built once and hidden, not built on entering the mode: it sits in
        the page's layout above the grid, and adding it later would push
        every card down by its height at the moment the mode is switched
        on."""
        bar = QWidget(objectName="Bare")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.selection_label = QLabel("", objectName="Muted")
        row.addWidget(self.selection_label)
        row.addStretch()
        # A checkbox rather than a button: "select everything" is a
        # state, not an action, and a tick can show that everything is
        # already picked where a button can only ever say it.
        self.select_all_check = QCheckBox("Select All")
        self.select_all_check.toggled.connect(self._on_select_all_toggled)
        row.addWidget(self.select_all_check)
        self.bulk_delete_btn = QPushButton("Delete", objectName="Danger")
        self.bulk_delete_btn.clicked.connect(self._delete_selected)
        row.addWidget(self.bulk_delete_btn)
        bar.setVisible(False)
        self.selection_bar = bar
        return bar

    # ---- state --------------------------------------------------------
    def _selection_count(self, count) -> str:
        singular, plural = self.SELECTION_NOUN
        return f"{count} {singular if count == 1 else plural}"

    def _update_selection_bar(self):
        # getattr because _refresh_grid can run before the bar exists on
        # a page still being built, the same reason the pages' own
        # _search_query guards their search box.
        label = getattr(self, "selection_label", None)
        if label is None:
            return
        count = len(self._selected_ids)
        label.setText(f"{self._selection_count(count)} selected" if count
                      else "Click cards to pick them")
        self.bulk_delete_btn.setEnabled(count > 0)
        # Blocked, because this is the tick catching up with the
        # selection - not the user asking for one. Unblocked it would
        # re-enter _on_select_all_toggled and re-apply what it is only
        # reporting.
        self.select_all_check.blockSignals(True)
        self.select_all_check.setChecked(self._everything_visible_selected())
        self.select_all_check.blockSignals(False)

    def _toggle_select_mode(self):
        self._set_select_mode(not self._select_mode)

    def _set_select_mode(self, on, first_id=None):
        self._select_mode = on
        self._selected_ids = {first_id} if on and first_id else set()
        self.select_btn.setText("Done" if on else "Select")
        self.selection_bar.setVisible(on)
        # A full rebuild, unlike picking a card below: every card has to
        # gain or lose its mark, and what its click does changes with it.
        self._refresh_grid()

    def _toggle_selected(self, entry_id):
        """Pick or unpick one card, repainting that card alone - not
        _refresh_grid, which would rebuild the whole grid and put the
        page's scroll position back to the top under the pointer."""
        if entry_id in self._selected_ids:
            self._selected_ids.discard(entry_id)
        else:
            self._selected_ids.add(entry_id)
        drawn = self._selection_cards.get(entry_id)
        if drawn:
            self._paint_selection(*drawn, entry_id in self._selected_ids)
        self._update_selection_bar()

    def _everything_visible_selected(self) -> bool:
        """Whether the selection already holds every card on screen -
        which is what turns Select All into Unselect All. Read off the
        cards actually drawn, so under a search "everything" means the
        handful in front of the user, the same rule the selection itself
        follows."""
        visible = {entry_id for entry_id in self._selection_cards if entry_id}
        return bool(visible) and visible <= self._selected_ids

    def _on_select_all_toggled(self, checked):
        if checked:
            self._select_all_visible()
        else:
            self._clear_selection()

    def _select_all_visible(self):
        # _selection_cards is exactly the cards currently drawn, which is
        # the point: under a search this picks what is on screen and
        # nothing else.
        self._selected_ids = {entry_id for entry_id in self._selection_cards if entry_id}
        self._repaint_all_selection()

    def _clear_selection(self):
        self._selected_ids.clear()
        self._repaint_all_selection()

    def _repaint_all_selection(self):
        for entry_id, (card, badge) in self._selection_cards.items():
            self._paint_selection(card, badge, entry_id in self._selected_ids)
        self._update_selection_bar()

    # ---- redraw hooks -------------------------------------------------
    def _clear_selection_cards(self):
        """Called from _refresh_grid once the old cards are out of the
        layout: every entry in here points at a widget that has just been
        taken out and deleteLater'd, and repainting one afterwards is a
        call into a C++ object on its way out."""
        self._selection_cards = {}

    def _prune_selection(self, visible_ids):
        """Keep only what the redraw about to happen will actually draw,
        so "3 entries selected" always names three cards the user can see
        and check before pressing Delete."""
        self._selected_ids &= set(visible_ids)
        self._update_selection_bar()

    def _attach_selection_badge(self, card, entry_id):
        """The mark on one card, and its registration for repainting.

        A child of the card at a fixed corner offset, not a row in its
        layout: a layout row would make every card taller the moment the
        mode came on, so entering selection would reflow the whole grid
        under the pointer. It sits in the card's own 8px margin rather
        than over the icon, which on these cards is 44px and would be
        half-covered by a mark big enough to see."""
        badge = QLabel(card)
        badge.setFixedSize(16, 16)
        badge.move(6, 6)
        # The card is what handles the click; a badge that took the press
        # would leave a hole in the middle of its own target.
        badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._selection_cards[entry_id] = (card, badge)
        self._paint_selection(card, badge, entry_id in self._selected_ids)
        return badge

    def _paint_selection(self, card, badge, selected):
        """Mark or unmark one card.

        The badge is the mark that decides it; the accent border is only
        there so a picked card reads as picked from across the page. That
        way round because an accent border is exactly what theme.py's
        #Card:hover rule already draws, so the card under the pointer
        looks bordered whether or not it is picked - the badge is what
        tells them apart. No glyph in it: a filled accent disc needs no
        font, and this app has already had a missing-glyph box render as a
        sliver on a button.

        Border stays 1px in both states - QSS border width comes out of
        the widget's content rect, so a thicker one would shift the icon
        and label inside the card the moment it was picked."""
        badge.setStyleSheet(
            f"background: {theme.ACCENT if selected else theme.BG}; "
            f"border: 2px solid {theme.TEXT if selected else theme.TEXT_MUTED}; "
            f"border-radius: 8px;")
        # Scoped to QFrame#Card so it cannot reach the labels inside the
        # card, and naming only `border` so the app stylesheet's fill
        # still paints - a widget stylesheet is merged with the
        # application one property by property, not instead of it.
        card.setStyleSheet(f"QFrame#Card {{ border: 1px solid {theme.ACCENT}; }}"
                           if selected else "")

    # ---- the batch ----------------------------------------------------
    def _delete_selected(self):
        """Delete every picked entry, asked about once.

        One question for the batch, not one per entry: the point of
        picking eight cards is not to answer eight dialogs, and the count
        in the question is what the user checks the selection against.
        The undo offer is what lets it be a single question - the whole
        batch comes back from it, every record intact.

        Through the page's _mutate, never a whole-list save of what this
        page holds: Home keeps its own copy of these same files and
        Settings can write them while the page sits open behind a dialog,
        so the list is re-read, the removals applied to *that*, and the
        result saved (see GamesPage._mutate for the defect this comes
        from)."""
        ids = set(self._selected_ids)
        if not ids:
            return
        if QMessageBox.question(
                self, f"Delete {self.SELECTION_NOUN[1].capitalize()}",
                f"Delete {self._selection_count(len(ids))}?"
        ) != QMessageBox.StandardButton.Yes:
            return
        removed = []   # (index, record) as they were on disk, for undo

        def apply_change(entries):
            # Descending, so each pop leaves the indices still to come
            # exactly where they were. The records come out of the list
            # _mutate has just re-read off disk - which is both the
            # freshest copy of each entry and, once it is popped, the
            # only surviving one, so there is nothing to deep-copy from.
            for index in sorted((i for i, e in enumerate(entries)
                                 if e.get("id") in ids), reverse=True):
                removed.append((index, entries.pop(index)))

        self._selected_ids.clear()
        # Stays in selection mode, with nothing picked: the common case
        # after one batch is a second one, and dropping out of the mode
        # would fight it.
        self._mutate(apply_change)
        if not removed:
            return
        ids_removed = [record.get("id") for _index, record in removed]
        show_undo_toast(
            self, f"Deleted {self._selection_count(len(removed))} - Click to Undo",
            lambda: self._restore_selected(removed),
            on_redo=lambda: self._delete_ids(ids_removed))

    def _delete_ids(self, ids):
        """Delete by id with nothing asked - what Ctrl+Y re-applies.

        No confirmation: the batch was confirmed when it was deleted, and
        redo is an explicit ask to put that back the way it was. The undo
        offer it raises is a fresh one, so Ctrl+Z still works afterwards."""
        wanted = set(ids)
        removed = []

        def apply_change(entries):
            for index in sorted((i for i, e in enumerate(entries)
                                 if e.get("id") in wanted), reverse=True):
                removed.append((index, entries.pop(index)))

        self._mutate(apply_change)
        if not removed:
            return
        show_undo_toast(
            self, f"Deleted {self._selection_count(len(removed))} - Click to Undo",
            lambda: self._restore_selected(removed),
            on_redo=lambda: self._delete_ids(ids))

    def _restore_selected(self, removed):
        """Put a deleted batch back, each record at the index it held."""
        def apply_change(entries):
            # Ascending, the mirror of the descending pop above: each
            # insert lands before the ones still to come, so the list ends
            # up as it was rather than reversed. Clamped because _mutate
            # re-reads the file and another page (or Settings > Clear
            # Data) can have shortened it since.
            for index, entry in sorted(removed, key=lambda pair: pair[0]):
                entries.insert(min(index, len(entries)), entry)

        self._mutate(apply_change)
        return f"Restored {self._selection_count(len(removed))}"


SEARCH_ICON_SIZE = 16


def magnifier_icon(color: str = None, size: int = SEARCH_ICON_SIZE) -> QIcon:
    """The search glass, drawn rather than bundled.

    A 14px glyph is a circle and a stroke; shipping a PNG for it would be
    one more asset to keep in step with the palette, and tinted_asset
    exists precisely because a fixed-colour PNG sat wrong beside text
    drawn from theme.

    Two things it has to get right, both of which it got wrong first:
    the ratio comes from the *screen*, since a widget's own
    devicePixelRatioF is 1.0 until it has been shown and an icon built
    then is upscaled by the display and looks soft; and the strokes are
    laid on half-pixel centres with a round cap, or a 1.4px line drawn on
    the pixel grid renders as a broken, scratchy edge."""
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color or theme.TEXT_MUTED))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Lens in the top-left, handle from its lower-right diagonal out to
    # the corner. Inset by the pen's half-width so neither stroke is
    # clipped by the edge of the pixmap.
    inset = 1.25
    lens = size * 0.55
    painter.drawEllipse(QRectF(inset, inset, lens, lens))
    from math import sqrt
    edge = inset + lens * (0.5 + 0.5 / sqrt(2))
    painter.drawLine(QPointF(edge, edge), QPointF(size - inset, size - inset))
    painter.end()
    return QIcon(pixmap)


def search_field(placeholder: str, width: int = None) -> QLineEdit:
    """A search box with the glass on its left, which is what every
    search field in this app is.

    One helper rather than four copies: the icon is drawn (see above), so
    a copy per page would mean four chances to draw it at a different
    size or colour."""
    field = QLineEdit()
    field.setPlaceholderText(placeholder)
    field.setClearButtonEnabled(True)
    field.addAction(magnifier_icon(), QLineEdit.ActionPosition.LeadingPosition)
    if width:
        field.setFixedWidth(width)
    return field


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


# The round arrow buttons over a sideways-scrolling row, and the soft
# edge they sit on. The fade is wider than the button so the cards
# dissolve into the panel rather than sliding under a hard-edged disc.
SIDE_ARROW_SIZE = 30
SIDE_ARROW_INSET = 4
SIDE_FADE_WIDTH = 64
# One click moves most of a screenful, not a fixed number of cards: the
# rows this wraps hold different card widths, and "nearly a viewport"
# is the movement these arrows read as everywhere else.
SIDE_SCROLL_STEP = 0.85
SIDE_SCROLL_ANIM_MS = 240


class _EdgeFade(QWidget):
    """The soft edge under a scroll arrow: the panel colour at the outer
    edge, fading to nothing a little way in, so a card being scrolled
    past thins out instead of being cut in half by the button on top of
    it.

    Painted rather than a QSS gradient because it has to be transparent
    at one end - a stylesheet background is composited as one opaque
    rectangle, which would just paint a bar over the row. Mouse-
    transparent, so the card underneath is still clickable right up to
    the button itself."""

    def __init__(self, parent, at_left: bool):
        super().__init__(parent)
        self._at_left = at_left
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        solid = QColor(theme.BG_ALT)
        clear = QColor(theme.BG_ALT)
        clear.setAlpha(0)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0.0, solid if self._at_left else clear)
        gradient.setColorAt(1.0, clear if self._at_left else solid)
        painter.fillRect(self.rect(), gradient)


class SideScroller(QWidget):
    """A horizontally scrolling row with an arrow button at each end.

    The row keeps everything it already had - its scrollbar, Shift+wheel,
    dragging a card out of it; the arrows are for reaching the rest of a
    long row with the mouse alone, which previously meant aiming at a
    2px scrollbar.

    An arrow is shown only while there is something that way to reach, so
    a row that fits entirely on screen carries no chrome at all. The area
    is positioned by hand rather than laid out: the arrows and their
    fades sit *over* it, and a layout would push them into a column
    beside it."""

    def __init__(self, area: QScrollArea, parent=None):
        super().__init__(parent)
        self._area = area
        area.setParent(self)
        self._bar = area.horizontalScrollBar()

        # Animated rather than a jump: at this distance an instant
        # scroll gives no sense of which way the row moved.
        self._anim = QPropertyAnimation(self._bar, b"value", self)
        self._anim.setDuration(SIDE_SCROLL_ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._fades = {}
        self._buttons = {}
        for at_left, glyph, tip in ((True, "‹", "Scroll left"),
                                    (False, "›", "Scroll right")):
            self._fades[at_left] = _EdgeFade(self, at_left)
            button = QPushButton(glyph, self, objectName="ScrollArrow")
            button.setFixedSize(SIDE_ARROW_SIZE, SIDE_ARROW_SIZE)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked=False, left=at_left: self._scroll(left))
            use_hover_cursor(button)
            self._buttons[at_left] = button

        # Both signals: the value moves when the user scrolls, and the
        # range only becomes non-zero once the row has been laid out -
        # at construction there is nothing to scroll yet and both arrows
        # would be hidden forever.
        self._bar.valueChanged.connect(self._sync_arrows)
        self._bar.rangeChanged.connect(lambda *_: self._sync_arrows())
        # maximumHeight, not height(): the caller pins the area's height
        # before wrapping it, and the widget has not been laid out yet,
        # so height() is still the default 480.
        self.setFixedHeight(area.maximumHeight())
        self._sync_arrows()

    def _row_height(self):
        """The card area's height - the scrollbar's strip along the
        bottom is not somewhere an arrow should be centred against, or
        somewhere the fade should be painting over.

        The bar's sizeHint, not whether it is currently visible: the
        caller reserves that height whether the bar is up or not, and
        visibility only flips *after* the range change that brings us
        here - measured, reading it live left the fade 11px too tall and
        tinting the ends of the bar it was supposed to sit above."""
        return self.height() - self._bar.sizeHint().height()

    def resizeEvent(self, event):
        self._layout_children()
        super().resizeEvent(event)

    def _layout_children(self):
        self._area.setGeometry(self.rect())
        row_height = self._row_height()
        for at_left in (True, False):
            fade = self._fades[at_left]
            button = self._buttons[at_left]
            fade.setGeometry(0 if at_left else self.width() - SIDE_FADE_WIDTH, 0,
                             SIDE_FADE_WIDTH, row_height)
            button.move(SIDE_ARROW_INSET if at_left
                        else self.width() - SIDE_ARROW_SIZE - SIDE_ARROW_INSET,
                        (row_height - SIDE_ARROW_SIZE) // 2)
            fade.raise_()
            button.raise_()

    def _sync_arrows(self, *_args):
        bar = self._bar
        for at_left, visible in ((True, bar.value() > bar.minimum()),
                                 (False, bar.value() < bar.maximum())):
            self._fades[at_left].setVisible(visible)
            self._buttons[at_left].setVisible(visible)

    def _scroll(self, at_left: bool):
        bar = self._bar
        step = max(1, int(self._area.viewport().width() * SIDE_SCROLL_STEP))
        target = bar.value() + (-step if at_left else step)
        target = max(bar.minimum(), min(bar.maximum(), target))
        # Stopped first: clicking twice quickly should continue from
        # where the row is heading, not restart from where it was.
        self._anim.stop()
        self._anim.setStartValue(bar.value())
        self._anim.setEndValue(target)
        self._anim.start()
