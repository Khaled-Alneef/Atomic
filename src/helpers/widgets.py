"""Small reusable widgets shared across windows."""

import collections
import ctypes
import math
import os
import threading
import time
import weakref

from PyQt6.QtCore import (QEasingCurve, QEvent, QMimeData, QObject, QPoint,
                          QPointF, QPropertyAnimation, QRect, QRectF, QSize,
                          Qt, QTimer, QVariantAnimation)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QBitmap, QBrush, QColor, QCursor, QDrag, QIcon,
                         QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPixmap, QRegion, QTransform)
from PyQt6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDialog, QFrame, QGraphicsDropShadowEffect, QGraphicsPixmapItem,
    QGraphicsScene, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QScrollBar, QSlider, QStyle,
    QStyleOptionSlider,
    QToolTip, QVBoxLayout, QWidget,
)

from . import frame_pacing, logs, theme


def _alpha_region(image, cut: int):
    """A QRegion of every pixel in `image` whose alpha is at least `cut`.

    Done through Qt's own Grayscale8 -> Mono conversion rather than a
    per-pixel Python loop: a 420x130 logo is 54,600 pixels, and
    `QImage.pixelColor` per pixel is tens of milliseconds on the UI
    thread at exactly the moment the player is already stalling.

    The route is: take the alpha plane as 8-bit grey, threshold it with
    `bytes.translate` (one C-speed pass over the buffer), and let Qt turn
    a two-valued grey image into a bitmap. Returns None rather than
    raising - the caller's fallback is a rounded badge, which is worse
    but not broken."""
    try:
        alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
        width, height, stride = alpha.width(), alpha.height(), alpha.bytesPerLine()
        raw = alpha.constBits()
        raw.setsize(stride * height)
        # 0 below the cut, 255 at or above it. One table, one pass.
        table = bytes(0 if value < cut else 255 for value in range(256))
        grey = QImage(bytes(raw).translate(table), width, height, stride,
                      QImage.Format.Format_Grayscale8).copy()
        mono = grey.convertToFormat(QImage.Format.Format_Mono,
                                    Qt.ImageConversionFlag.MonoOnly
                                    | Qt.ImageConversionFlag.ThresholdDither)
        region = QRegion(QBitmap.fromImage(mono))
        # QRegion(QBitmap) keeps the color1 pixels, and which of black
        # and white that is depends on the mono image's colour table -
        # so the polarity is checked rather than assumed. An inverted
        # mask is the whole rectangle minus the logo, which is bigger
        # than the logo's own bounding box can be.
        if region.boundingRect().width() * region.boundingRect().height() > \
                width * height * 0.9 and not region.isEmpty():
            region = QRegion(0, 0, width, height).subtracted(region)
        return region
    except Exception:
        return None


class LogoProgress(QWidget):
    """A title's logo that fills with its own colour as loading advances.

    The logo is drawn twice from one transparent PNG: once faint, as the
    part still to come, and once at full strength clipped to the
    fraction loaded. So the shape is always the title, and the colour
    arriving *is* the progress - no separate bar, and nothing that has to
    be read to be understood.

    Falls back to nothing at all when there is no logo (TMDB has none, or
    no key is set); the player keeps its text status underneath, so this
    only ever adds.

    Left-to-right rather than bottom-up: a title treatment is a word, and
    a word filling the way it is read is legible at a glance where a
    rising waterline is not."""

    # Raised from 0.22: over the title's own backdrop (a warm frame for
    # Bleach) the fainter ghost blended into the picture and the logo
    # read as broken sepia art rather than as an empty gauge waiting to
    # fill. At 0.42 the shape is unmistakably the logo from the first
    # frame, and the full-colour fill arriving still reads as progress.
    GHOST_OPACITY = 0.42

    # One breath of the pulse, and how far it dips. The whole logo
    # breathes between full strength and PULSE_FLOOR while it is on
    # screen - the movement is what says "loading" now that the loading
    # screen carries no text (the owner's ask). Driven by a linear 0-1
    # loop mapped through a cosine in paintEvent, so the loop's wrap
    # from 1 back to 0 lands on the same opacity it left and the breath
    # never visibly snaps.
    PULSE_MS = 1600
    PULSE_FLOOR = 0.55

    # Where the logo's silhouette ends, for `logo_region`. Half alpha:
    # above it a pixel is part of the mark, below it a pixel is the
    # shadow or glow around it. See logo_region for what asking Qt to
    # decide this produced instead.
    ALPHA_CUT = 128

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._fraction = 0.0
        self._pulse = 0.0
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setStartValue(0.0)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.setDuration(self.PULSE_MS)
        self._pulse_anim.setLoopCount(-1)
        self._pulse_anim.valueChanged.connect(self._on_pulse)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _on_pulse(self, value):
        self._pulse = float(value)
        self.update()

    def _pulse_opacity(self) -> float:
        wave = 0.5 - 0.5 * math.cos(2 * math.pi * self._pulse)
        return self.PULSE_FLOOR + (1.0 - self.PULSE_FLOOR) * (1.0 - wave)

    # Started and stopped by visibility rather than by the caller: the
    # player shows/hides this with its backdrop, and an animation left
    # running behind a hidden widget is a 60Hz repaint of nothing.
    def showEvent(self, event):
        self._pulse_anim.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._pulse_anim.stop()
        super().hideEvent(event)

    def set_logo(self, path: str) -> bool:
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self._pixmap = None
            self.update()
            return False
        self._pixmap = pixmap
        self.update()
        return True

    def has_logo(self) -> bool:
        return self._pixmap is not None

    def logo_region(self):
        """The logo's own silhouette inside this widget, as a QRegion, or
        None when there is no logo.

        Used to clip the window *behind* the logo down to the logo, so a
        loading mark over live video is the mark and nothing else - see
        player.StartupBackdrop's stall mode. Built from the pixmap's
        alpha, so it is a 1-bit shape: the edge is hard where the artwork
        is soft, which is the price of a native child window that can
        only be blended whole.

        **Not `QPixmap.mask()`, and that is the whole fix - the owner, 24
        August 2026: "in the loading logo while the ep is playing why
        does it have a bg (gray)???? make it just the logo".** The clip
        was already here and already being applied; it was simply not
        clipping anything. `QPixmap.mask()` goes through
        `QImage::createAlphaMask`, which **dithers** by default, so a
        title treatment's soft shadow and antialiased edges come back as
        scattered opaque pixels spread over the whole rectangle.
        Measured over the owner's twelve cached TMDB logos: the region
        covered **26.5% to 88.5%** of its own bounding box, where the
        real ink of a logo is a fraction of that - so the badge's fill
        had almost nowhere it was actually clipped away from, and what
        he saw was the grey box the clip exists to remove.

        Thresholded at ALPHA_CUT instead, with no dither at all. Measured
        on the same logos, coverage drops to roughly the artwork's own
        ink. Then dilated by one pixel, so the antialiased rim is kept
        rather than shaved - a hard edge one pixel outside the glyph
        reads as the glyph; a hard edge one pixel inside it reads as
        damage."""
        if self._pixmap is None:
            return None
        target = self._target_rect(self._pixmap)
        if target.width() <= 0 or target.height() <= 0:
            return None
        try:
            scaled = self._pixmap.scaled(
                target.width(), target.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            region = _alpha_region(scaled.toImage(), self.ALPHA_CUT)
            if region is None or region.isEmpty():
                return None
            # One pixel of growth in each direction. Union of four
            # translations rather than a real morphological dilate: this
            # runs once per stall on the UI thread, and the difference
            # between the two is invisible at one pixel.
            grown = QRegion(region)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                moved = QRegion(region)
                moved.translate(dx, dy)
                grown = grown.united(moved)
            grown.translate(target.x(), target.y())
            return grown
        except Exception:
            return None

    def set_fraction(self, fraction: float):
        fraction = max(0.0, min(1.0, float(fraction or 0.0)))
        # Repaint only on a visible change: this is driven by a buffer
        # percentage that can tick many times a second.
        if abs(fraction - self._fraction) < 0.005:
            return
        self._fraction = fraction
        self.update()

    def _target_rect(self, pixmap):
        """The logo centred and scaled to fit, keeping its aspect."""
        available = self.rect()
        scaled = pixmap.size()
        scaled.scale(available.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (available.width() - scaled.width()) // 2
        y = (available.height() - scaled.height()) // 2
        return QRect(x, y, scaled.width(), scaled.height())

    def paintEvent(self, event):
        if self._pixmap is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # Scaled by devicePixelRatio and tagged, or the logo blurs on any
        # display that is not at 100% (.claude/rules/ui.md).
        ratio = self.devicePixelRatioF()
        target = self._target_rect(self._pixmap)
        scaled = self._pixmap.scaled(
            int(target.width() * ratio), int(target.height() * ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)

        pulse = self._pulse_opacity()
        painter.setOpacity(self.GHOST_OPACITY * pulse)
        painter.drawPixmap(target, scaled)

        filled = int(target.width() * self._fraction)
        if filled > 0:
            painter.setOpacity(pulse)
            # Clip in *widget* coordinates to the filled portion, then
            # draw the same pixmap again - so the two halves line up
            # exactly and the seam is a straight edge through the art
            # rather than two differently-scaled copies.
            painter.setClipRect(QRect(target.x(), target.y(),
                                      filled, target.height()))
            painter.drawPixmap(target, scaled)
        painter.end()


class GlassPage(QWidget):
    # **Every page follows the fold, frame by frame.** main._toggle_
    # sidebar can hold a page at its widest and blit one screenshot for
    # the whole animation, which is cheap and is why nothing on the page
    # moved until the fold landed - the banners in particular snapped
    # into their new width at the end (the owner reported it twice).
    #
    # Measured 26 August 2026, page paintEvent during one fold:
    #
    #     Read grid   frozen: 2 paints, 190ms apart
    #     Read grid   live:  35 paints, 4.5ms apart, worst 0.06ms
    #     Discover    live:  28 paints, 5.6ms apart, worst 0.05ms
    #
    # The old note that justified the freeze counted paint *events*, not
    # their cost; the page's own paint is trivial because the cards
    # repaint themselves. A page with a genuinely expensive paintEvent
    # can still set this False and get the screenshot back.
    FOLD_LIVE = True

    """Base for a page: one flat, uniform near-black ground behind
    whatever layout/content the subclass adds on top.

    The radial "nebula" glow that used to bleed in from the right edge
    is gone at the owner's ask ("make it all the same color, remove the
    gold in the top") - under the gold palette the lobes read as a stain
    on the frame rather than distant light. The glow parameters are kept
    in the signature so no caller changes; they simply no longer paint.
    """

    def __init__(self, base=theme.BG, glow=theme.GLOW, parent=None):
        super().__init__(parent)
        self._base = QColor(base)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._base)
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
        kind = event.type()
        if kind == QEvent.Type.Enter:
            # **Not while it is disabled.** A greyed-out control that
            # still offers the pointing hand says "press me" about the
            # one thing that cannot be pressed - the owner's report, 26
            # August 2026, about Back on the setup wizard's first page.
            if obj.isEnabled():
                hold_hover_cursor(obj)
        elif kind == QEvent.Type.Leave:
            release_hover_cursor(obj)
        elif kind == QEvent.Type.EnabledChange:
            # A control disabled *while* the pointer is on it has to give
            # the cursor back there and then; there will be no Leave
            # until the pointer moves, and by then it has been wrong for
            # as long as the user was looking at it.
            if not obj.isEnabled():
                release_hover_cursor(obj)
            elif obj.underMouse():
                hold_hover_cursor(obj)
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

    # Resolved once at import. `QEvent.Type.ToolTip` is an attribute walk
    # and an enum construction every time it is written out, and the
    # override below runs for *every* event delivered to *every* card.
    _TOOLTIP = QEvent.Type.ToolTip

    # **A class-level default, and it is not decoration.** `event()`
    # below reads this attribute before anything else, and `QFrame`'s
    # own constructor delivers events to the widget *before*
    # `Card.__init__` has run its assignment - so an instance-only
    # attribute leaves `event()` raising AttributeError from inside a
    # C++ virtual, where Python cannot unwind it. Windows fail-fasts the
    # process: 0xC0000409, no traceback, the app simply vanishes.
    # Measured 22 August 2026 - every harness building a Card died at
    # this line until the default was added. It also makes the fast path
    # marginally faster, since the lookup resolves on the class for the
    # cards that never set one.
    _tooltip_provider = None

    def event(self, event):
        # **The provider is tested first, and that is the whole point.**
        # Profiled 22 August 2026: this override took **20,128 calls and
        # 257ms of a 1.14s scroll** of One Piece's chapter list, and
        # 5,274 calls / 96ms while a details page opened. Most cards
        # carry no tooltip provider at all, so a plain attribute test
        # that fails immediately skips both the `event.type()` call and
        # the enum lookup for all of them. Shared by every page in the
        # app, which is why it is worth the comment.
        if self._tooltip_provider is not None and event.type() == self._TOOLTIP:
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
        try:
            super().mousePressEvent(event)
        except RuntimeError:
            # **`clicked` can delete this card.** A card's handler opens
            # a page, and opening a page rebuilds the grid the card is
            # in - so by the time the base handler runs, the C++ object
            # under it is gone. From the owner's log, 22 August 2026:
            #
            #     widgets.py:350 mousePressEvent
            #     RuntimeError: Card has been deleted
            #
            # An exception out of a Qt slot is qFatal in PyQt6, so this
            # took the whole app down rather than raising. There is
            # nothing left to hand the press to; returning is the whole
            # correct behaviour.
            return

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
            elif (isinstance(widget, SideScroller)
                  and widget.content_widget() is not None):
                # The strip's scroll area is a hand-positioned child of
                # the SideScroller - no layout anywhere on the way down,
                # so the walk below cannot reach it. Measured: with the
                # tracker's sections wrapped in SideScrollers, _cards()
                # found 0 cards and a drag had nothing to drop on.
                cards.extend(self._cards_in(widget.content_widget().layout()))
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
        # Parked far off-screen for that first show, though - shown at
        # Qt's default position it painted one frame wherever the window
        # happened to be created, which the owner saw as "a window opens
        # and closes really fast" whenever a download was queued.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.move(-20000, -20000)
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
        self.bulk_delete_btn = trash_button(self._delete_selected)
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
        tells them apart.

        Border stays 1px in both states - QSS border width comes out of
        the widget's content rect, so a thicker one would shift the icon
        and label inside the card the moment it was picked.

        **A tick inside the disc**, 28 August 2026, the owner: "make the
        circles on the cards when selected also contain a check mark
        (Correct) on all pages related". The note this replaces said "no
        glyph in it", and the reason it gave was sound and is still
        respected: the missing-glyph box that rendered as a sliver on
        the filter button came from asking a *font* for a symbol. This
        asks no font for anything - it is the same drawn asset the
        checkboxes use (theme.checkmark_pixmap), scaled down with a
        smooth transform and tagged with the screen's ratio, so it
        cannot go missing and cannot go soft.

        Cleared rather than left in place when unpicked: a QLabel holding
        a pixmap keeps drawing it under whatever background the
        stylesheet sets, so the disc would still carry a tick in
        ON_ACCENT on a BG-filled circle.
        """
        badge.setStyleSheet(
            f"background: {theme.ACCENT if selected else theme.BG}; "
            f"border: 2px solid {theme.TEXT if selected else theme.TEXT_MUTED}; "
            f"border-radius: 8px;")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tick = theme.checkmark_pixmap(9) if selected else None
        if tick is not None:
            badge.setPixmap(tick)
        else:
            badge.clear()
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
        if not confirm(self, f"Delete {self.SELECTION_NOUN[1].capitalize()}",
                       f"Delete {self._selection_count(len(ids))}?"):
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


# The hero banner, Harbor's shape: a title's backdrop filling a rounded
# panel edge to edge under a scrim that rises from the left so text over
# it always reads. Shared by Home's continue hero and the tracker's
# Discover featured slot, so the two heads of the app are one design.
HERO_BANNER_HEIGHT = 300
# The scrim's stops, left to right, as (position, alpha) over theme.BG:
# heaviest where the text sits, nearly clear at the artwork's edge.
#
# **Carried further right, 23 August 2026, because the text moved.** The
# hero is now cover-left / details-right (see hero_split), so the title
# and its chips sit around the middle of the banner - where the old ramp
# had already fallen to alpha 175 and was heading for 95. Text that read
# cleanly when it was hard against the left edge does not read there. The
# stops below keep the same shape and the same near-clear right edge, and
# simply hold the dark longer across the band the words now occupy.
HERO_BANNER_SCRIM = ((0.0, 244), (0.52, 214), (0.80, 140), (1.0, 56))

# The portrait cover on the left of a hero, at banner scale.
#
# 3:4-ish and 264 tall so it clears the 300px banner with the 18px of
# air hero_split leaves above and below. Deliberately not POSTER_SIZE
# (160x216, windows.tracker): a card's cover is a thumbnail in a grid,
# this one is the subject of the banner.
HERO_COVER_SIZE = (196, 264)

# The hero's own corner radius, larger than the app's RADIUS_LG.
#
# **The owner has reported square corners on this banner three times**,
# most recently 22 August 2026 ("make the corners ALWAYS rounded"), and
# the geometry was measured again each time. It is not square: probed at
# dpr 1 and 1.25, flat, mid-fade, cross-fading, mid-resize and settled,
# the pixel in every corner is the ground behind the banner and never
# the artwork - the fill-through-a-path in paintEvent does round it.
#
# What is wrong is the *scale*. 18px of curve on a banner that is 300px
# tall and 1600px wide at the owner's full-screen size is a quarter of
# one percent of the edge, and at the dark end the 242-alpha scrim turns
# the interior into very nearly the page's own background, so there is
# almost nothing for that quarter-percent to read against. A radius
# proportionate to the banner is what actually makes it visible - it is
# not a fix to the shape, which was already right, but to how much of it
# there is to see.
HERO_BANNER_RADIUS = 28


# Backdrops already decoded, by path. **Decoding one is a 144ms freeze
# on the UI thread**, measured while profiling the sidebar fold - a
# single QPixmap(path) on a full-resolution backdrop was the largest
# stall in the whole profile, larger than every paint in it put
# together. Home's hero rotates every five seconds, so that stall lands
# again and again, and anything animating when it lands - a fold, a
# scroll - drops a visible chunk of frames.
#
# The carousel cycles a small fixed set, so caching turns every rotation
# after the first into a dictionary lookup. Bounded by count rather than
# bytes because these are all one shape - a handful of banners, not the
# reader's two-orders-of-magnitude page images (which is why
# _PixmapCache bounds itself in bytes instead).
_BACKDROP_CACHE = {}
_BACKDROP_CACHE_MAX = 12


# Banners decoded off the GUI thread, waiting to be turned into
# pixmaps - see warm_backdrop.
_BACKDROP_IMAGE_CACHE = {}


def warm_backdrop(path):
    """Decode a banner ahead of time, from a worker thread.

    **The decode was costing a dropped frame at every slide change.**
    The owner, 27 August 2026: the hero's background "glitches in a
    really fast way" when it moves to the next or previous slide.
    Measured on the real Home page, driving four slides twice and
    timing the Qt event loop across each change:

        first time each slide is shown   one 31-43ms stall
        the same four again, cached      0 gaps over 16.7ms, worst 4.2ms

    So the fade itself was never the problem - it measures 2.4ms a paint
    - and neither was anything on screen. It was `QPixmap(path)` running
    on the GUI thread inside set_backdrop, two frames' worth of JPEG
    decode at the instant the transition starts.

    It kept coming back because the pixmap cache holds twelve and is
    shared by every banner in the app: Home's hero, Discover's featured
    row and the top result all evict each other, so a slide that was
    warm five minutes ago decodes again.

    QImage, not QPixmap, because only QImage may be built off the GUI
    thread. The conversion left for the main thread is a format change,
    not a decode."""
    path = str(path or "")
    if not path or path in _BACKDROP_CACHE or path in _BACKDROP_IMAGE_CACHE:
        return
    image = QImage(path)
    if image.isNull():
        return
    if len(_BACKDROP_IMAGE_CACHE) >= _BACKDROP_CACHE_MAX:
        _BACKDROP_IMAGE_CACHE.pop(next(iter(_BACKDROP_IMAGE_CACHE)), None)
    _BACKDROP_IMAGE_CACHE[path] = image


def _decoded_backdrop(path):
    pixmap = _BACKDROP_CACHE.get(path)
    if pixmap is None:
        # A banner warmed by a worker costs a conversion here; one that
        # was not still decodes, exactly as before.
        image = _BACKDROP_IMAGE_CACHE.pop(path, None)
        pixmap = QPixmap.fromImage(image) if image is not None else QPixmap(path)
        if len(_BACKDROP_CACHE) >= _BACKDROP_CACHE_MAX:
            _BACKDROP_CACHE.pop(next(iter(_BACKDROP_CACHE)), None)
        _BACKDROP_CACHE[path] = pixmap
    return pixmap


class HeroBanner(QFrame):
    """The hero's canvas: paints the backdrop and its scrim, and the
    caller lays labels and buttons out on top.

    Clickable as a whole with the hover-cursor contract every clickable
    thing here follows. The backdrop arrives late (a TMDB/AniList
    lookup) and lands through set_backdrop, cross-fading over whatever
    was there; until then the banner is a flat surface panel, which is
    what it stays for a title with no landscape art anywhere.

    `ground` is the colour of the page *behind* the banner, which
    paintEvent paints back over the four corner outsides - see the note
    there. It is not one constant across the app: Home's hero sits on
    theme.BG (#0a0e16) while Discover's featured banner sits on a
    theme.PANEL_FILL (#141b28) scroll body, and a corner filled with the
    wrong one would be a visible dark or light notch rather than no
    corner at all."""

    FADE_MS = 260

    clicked = Signal()

    def __init__(self, ground=None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(HERO_BANNER_HEIGHT)
        self._ground = QColor(ground or theme.BG)
        self._backdrop = None
        self._previous = None
        self._path = None
        # How much of the new backdrop is showing, 0-1 - the cross-fade
        # a rotating hero needs so a slide change is a dissolve, not a
        # blink. Scaled copies are cached per size, not per paint, the
        # same stutter the details page's ground caches away.
        self._mix = 1.0
        # **SmoothTween, not QVariantAnimation** - the same swap this
        # module's screen_tick_ms docstring argues for, applied to the
        # last two fades that had not had it. Measured 24 August 2026 on
        # a 240Hz panel, sampling the value once per compositor present
        # over six hover cycles: the QVariantAnimation produced **65
        # positions/s and 92.0% of refreshes repeated the previous
        # one** - Qt's unified animation timer, ~16ms, whatever the
        # panel does. A 220ms fade therefore showed about thirteen
        # levels of frost where fifty-three were available.
        self._fade = SmoothTween(self, self._on_fade, self.FADE_MS)
        self._scaled = {}
        # Armed by every resize, fired 90ms after the last one - see
        # _scaled_for. Long enough to cover a whole fold animation
        # without re-cutting mid-flight, short enough that the sharp
        # copy is there before anyone looks at it.
        self._resize_settle = QTimer(self)
        self._resize_settle.setSingleShot(True)
        self._resize_settle.setInterval(90)
        self._resize_settle.timeout.connect(self._resmooth)
        use_hover_cursor(self)

    def set_ground(self, colour):
        """Change the page colour the corners are cut back to, for a
        caller that moves the banner between grounds."""
        self._ground = QColor(colour)
        self.update()

    def _on_fade(self, value):
        self._mix = float(value)
        self.update()

    def set_backdrop(self, path, fade=True) -> bool:
        """Show `path` as the ground. None clears back to the flat
        panel. Returns whether there is now art on screen."""
        path = str(path) if path else None
        if path == getattr(self, "_path", None):
            return self._backdrop is not None
        pixmap = _decoded_backdrop(path) if path else QPixmap()
        incoming = None if pixmap.isNull() else pixmap
        self._path = path if incoming is not None else None
        self._previous = self._backdrop if fade else None
        self._backdrop = incoming
        self._scaled = {}
        if fade and (self._previous is not None or incoming is not None):
            # **Set the mix before arming the tween, not after.**
            # SmoothTween.start only arms its timer - it does not call
            # back - so `_mix` kept the finished value of the *last*
            # fade, which is 1.0, until the first tick a frame later.
            # Any paint in that window drew the incoming art at full
            # opacity and the outgoing at none.
            #
            # Measured 27 August 2026, recording the opacity every frame
            # of a real transition drew with:
            #
            #     1.0, 0.052, 0.148, 0.183, 0.258, ...
            #
            # so the banner hard-cut to the new picture, snapped back to
            # the old one, and only then crossfaded. That is the owner's
            # "the banner bg glitches in a really fast way in
            # transition", and it happened on every slide change - which
            # is why removing the decode stall from the same transition
            # did not touch it.
            self._mix = 0.0
            self._fade.start(0.0, 1.0, self.FADE_MS)
        else:
            self._mix = 1.0
            self.update()
        return incoming is not None

    def has_backdrop(self) -> bool:
        """Whether art is on screen - what a caller upgrading a small
        copy to the sharp original asks before deciding to cross-fade."""
        return self._backdrop is not None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        # Every width the fold animation passes through lands here.
        self._note_resize()
        super().resizeEvent(event)

    def _scaled_for(self, key, pixmap, size):
        # Cut at devicePixelRatio and tagged with it, or the banner is
        # rendered at logical size and stretched by Qt on any non-100%
        # display - which is what made the owner's slider look soft
        # (.claude/rules/ui.md). The cache key carries the ratio too, so
        # dragging the window to a differently-scaled monitor re-cuts
        # rather than reusing the other screen's pixmap.
        #
        # Keyed with the size it was cut for, because the expanding
        # scale never *equals* the rect - one axis overshoots - so
        # comparing the result's own size would rescale every paint.
        ratio = self.devicePixelRatioF() or 1.0
        cached = self._scaled.get(key)
        if cached is None or cached[0] != (size, ratio):
            target = QSize(max(1, int(size.width() * ratio)),
                           max(1, int(size.height() * ratio)))
            # **Smooth only when the size has stopped moving.**
            # Measured 21 August 2026, profiling the sidebar fold: this
            # one call was **4.9ms** and ran seven times per fold, which
            # was the whole of "the fold stutters" - every step of the
            # animation is a new width, so every step missed this cache
            # and re-scaled a multi-megapixel banner with
            # SmoothTransformation. The tween underneath was ticking at
            # 7ms and the work was taking 17.
            #
            # A frame mid-animation is on screen for one refresh and
            # nobody can see the difference between the two filters at
            # that speed; the settled frame is the one that has to be
            # sharp, and _resmooth below re-cuts it 90ms after the last
            # size change. So this costs a fast scale per step and one
            # smooth scale per resize, instead of one smooth scale per
            # step.
            # **While the size is still moving, keep the copy we have.**
            # Re-cutting was measured at 6.9ms a call and ran once per
            # animation step, which is the whole of "the fold stutters":
            # the tween underneath ticks every 7ms and the work took
            # nearly three times that. Trying FastTransformation first
            # barely helped - the cost is allocating and filling a
            # multi-megapixel pixmap, not the filter choosing between
            # neighbours.
            #
            # So a resize in flight simply reuses the last cut. It is the
            # wrong size by a few percent for a few frames, and the
            # brush's own transform in paintEvent stretches it to fit -
            # which the GPU does as part of the blit it was doing
            # anyway. _resmooth re-cuts it properly 90ms after the last
            # size change, which is the frame anyone actually looks at.
            if cached is not None and self._resize_settle.isActive():
                return cached[1]
            scaled = pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            cached = ((size, ratio), scaled, True)
            self._scaled[key] = cached
        return cached[1]

    def _note_resize(self):
        """Called from resizeEvent: mark the widget as moving and arm the
        re-cut that follows the last change."""
        self._resize_settle.start()

    def _resmooth(self):
        """Re-cut anything whose cached copy no longer matches the size
        now showing. A copy already cut for this size is left alone -
        re-cutting it would be exactly the cost this avoids."""
        ratio = self.devicePixelRatioF() or 1.0
        size = self.rect().size()
        self._scaled = {key: row for key, row in self._scaled.items()
                        if row[0] == (size, ratio)}
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(rect), HERO_BANNER_RADIUS,
                             HERO_BANNER_RADIUS)
        # **Filled through the path, not clipped to it.** setClipPath is
        # not antialiased in Qt's raster engine - the clip is applied per
        # whole pixel - so the artwork kept hard square corners under a
        # rounded panel however large the radius (the owner's screenshot,
        # twice). Painting each layer as a *brush* over the same path
        # antialiases the edge properly, which is what actually rounds
        # the picture rather than only the frame behind it.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_HOVER))
        painter.drawPath(shape)

        def draw(key, pixmap, opacity):
            if pixmap is None or opacity <= 0.0:
                return
            scaled = self._scaled_for(key, pixmap, rect.size())
            # Centred on the pixmap's *logical* size: it carries a
            # devicePixelRatio now, so width()/height() are device
            # pixels and using them raw would offset the art by a
            # quarter of the banner on a 150% display.
            ratio = scaled.devicePixelRatio() or 1.0
            width = scaled.width() / ratio
            height = scaled.height() / ratio
            brush = QBrush(scaled)
            # The brush paints from the origin, so the same centring
            # offset has to be carried as the brush's own transform -
            # and in logical units, since the pixmap is ratio-tagged.
            # **Stretched to the rect it is being drawn into, not just
            # centred in it.** While a resize is in flight `_scaled_for`
            # deliberately hands back the *last* cut rather than paying
            # 6.9ms to re-cut per animation step - and a copy that is
            # the wrong size only looks right if something scales it.
            # Centring alone left the art at its old pixel size for the
            # whole fold and let `_resmooth` snap it at the end, which
            # is the owner's report of 26 August 2026: the banner moved
            # with the sidebar but the picture inside it changed size
            # only once the fold had finished.
            #
            # Uniform, and taken from the larger axis, because the cut
            # is KeepAspectRatioByExpanding - the art always covers the
            # rect and the overflow is what the centring hides. The GPU
            # does this as part of the blit it was already doing, so it
            # costs nothing the old centring did not.
            grow = max(rect.width() / width if width else 1.0,
                       rect.height() / height if height else 1.0)
            if abs(grow - 1.0) < 0.001:
                grow = 1.0
            width *= grow
            height *= grow
            transform = QTransform()
            transform.translate((rect.width() - width) / 2.0,
                                (rect.height() - height) / 2.0)
            if grow != 1.0:
                transform.scale(grow, grow)
            # **No 1/ratio scale** - Qt's raster brush already applies
            # the texture pixmap's devicePixelRatio, so scaling again
            # drew the banner art at 1/ratio of its size and tiled the
            # gap. Measured 24 August 2026; the same line and the same
            # wrong comment were in details.DetailsPage.paintEvent,
            # which carries the marker-texture measurement that settled
            # it. Invisible at DPR 1.0, which is every display this was
            # written on.
            brush.setTransform(transform)
            painter.setOpacity(opacity)
            painter.setBrush(brush)
            painter.drawPath(shape)
            painter.setOpacity(1.0)

        draw("previous", self._previous, 1.0 - self._mix)
        draw("current", self._backdrop, self._mix if self._previous is not None
             else 1.0)
        gradient = QLinearGradient(0.0, 0.0, float(rect.width()), 0.0)
        # The scrim is theme.BG whatever the page behind is - it is the
        # dark wash the title text has to read against, not the page's
        # own colour (self._ground, below, is that one).
        scrim = QColor(theme.BG)
        for stop, alpha in HERO_BANNER_SCRIM:
            scrim.setAlpha(alpha)
            gradient.setColorAt(stop, QColor(scrim))
        painter.setBrush(QBrush(gradient))
        painter.drawPath(shape)
        # **Then the four corner outsides are painted back out in the
        # page's own colour** - bounding rect minus the rounded rect,
        # filled opaque. The owner's own suggestion, on his third report
        # of square corners (22 August 2026), and taken because it is the
        # one fix that does not depend on how Qt rasterises a *textured*
        # brush along a curve: whatever the fills above leave in the
        # corners - a hard square edge, a stray row, nothing at all - an
        # opaque layer of the page colour is over it, so the corner is
        # subtracted rather than merely never drawn. The banner was
        # rendered here at 8-10x on both grounds and measured correct
        # before this went in; it is belt-and-braces for a machine where
        # it is not, and costs one small path fill per paint.
        #
        # This is why `ground` is a constructor argument: the corner has
        # to become the page, and the two callers sit on different pages.
        corners = QPainterPath()
        corners.addRect(QRectF(rect))
        painter.setBrush(QColor(self._ground))
        painter.drawPath(corners.subtracted(shape))
        # A hairline of BORDER around the shape, because the rounding is
        # real but was unreadable: measured in the full Discover
        # composition (offscreen grab, loud magenta art), the left
        # corner pixel differed from the banner's interior by 27 summed
        # channels out of 765 - the 242-alpha scrim above turns the left
        # half into the page's own BG, so the curve had nothing to read
        # against and the owner reported square corners twice. The
        # stroke path is inset half a pixel so the pen isn't clipped by
        # the widget edge.
        edge = QPainterPath()
        edge.addRoundedRect(QRectF(rect).adjusted(0.75, 0.75, -0.75, -0.75),
                            HERO_BANNER_RADIUS, HERO_BANNER_RADIUS)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 1.5px, not 1: at one physical pixel the hairline is antialiased
        # into two half-lit rows around the curve and reads as a smudge
        # rather than an edge - which on the scrimmed left half, where it
        # is the only thing separating banner from page, is the whole of
        # the "square corners" report. The pen is inset by half its own
        # width so it is not clipped by the widget edge.
        painter.setPen(QPen(QColor(theme.BORDER), 1.5))
        painter.drawPath(edge)
        painter.end()


def hero_split(banner, cover_size=None):
    """Lay a hero out as **cover on the left, details on the right**, and
    hand back `(cover_label, details_column)` for the caller to fill.

    The owner's ask, 23 August 2026, with a mock-up: "change all banners
    design to make it like image 2, the cover image (as in the cards) on
    the left and the details on the right, do it for all watch and read
    (Featured, top result, Continue reading, etc...)".

    One function rather than the same QHBoxLayout written twice, because
    "all banners" is exactly the requirement that a second copy quietly
    fails: Home's continue hero (windows.home) and the tracker's Discover
    featured/top-result banner (windows.tracker) are the two hero
    surfaces in the app, they were already drifting apart in margins and
    spacing, and any third one should be this shape by construction.

    The cover is a plain QLabel because `images.thumbnail_or_avatar`
    already returns a rounded, corner-cut, cache-warm portrait pixmap -
    the very one the cards on the same page are drawing, so a hero cover
    is a cache hit rather than a decode (see images._fitted). It is
    transparent to the mouse so the banner's own `clicked` still fires
    when the cover is what was pressed - the banner is one click target,
    and a hole in the middle of it would be a bug nobody would look for.

    The artwork behind is unchanged: it is still the landscape backdrop
    under HERO_BANNER_SCRIM, which is what gives the banner its colour.
    The cover sits on top of it, on the darkest end of the ramp."""
    # Resolved at call time, not bound as a default: a default is
    # evaluated when this module is imported, which is before
    # layout.adopt() has had a screen to size the cover against.
    cover_size = cover_size or HERO_COVER_SIZE
    row = QHBoxLayout(banner)
    row.setContentsMargins(26, 18, 32, 18)
    row.setSpacing(24)

    cover = QLabel(banner)
    cover.setFixedSize(int(cover_size[0]), int(cover_size[1]))
    cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
    cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    row.addWidget(cover, 0, Qt.AlignmentFlag.AlignVCenter)

    column = QVBoxLayout()
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(8)
    row.addLayout(column, 1)
    return cover, column


# The title logo drawn in a hero in place of its typed name. Height
# first, then a width cap so a very wide treatment (One Piece) does not
# run into the details column's right edge on a narrow window; scaled
# keeping aspect inside that box.
HERO_LOGO_HEIGHT = 84
HERO_LOGO_MAX_WIDTH = 480


def hero_logo_label(parent=None) -> QLabel:
    """The label a hero's TMDB title treatment is drawn into, hidden
    until `set_hero_logo` gives it one.

    Back by the owner's ask of 24 August 2026 - "instead of the name in
    the banner, make it use the logo from TMDB, if it has no logo then
    use the name normally" - after the 23 August redesign had dropped
    it. What that redesign was actually reacting to was a banner with
    *neither* logo nor name (a hide-the-name rule for AniList banner art
    with no logo to stand in); the rule this time is only ever "logo
    replaces name", never "hide the name", so a banner cannot lose its
    title again. Shared between Home's continue hero and the tracker's
    featured/top-result banner, like hero_split, so both wear it.

    A soft shadow so a logo reads over the art whatever its own colour -
    most treatments are light, some (Demon Slayer) are near-black, and
    the scrim that carries white text cannot help those. Offset 0: a
    halo lifting the shape off the ground rather than a drop under it.

    **The halo is baked into the pixmap now, not a live
    QGraphicsDropShadowEffect** (the owner, 25 August 2026: the
    scrolling frame rate). A live effect re-runs its Gaussian blur every
    time the widget repaints - and every scroll frame repaints the whole
    viewport on these pages (measured: 167 of 217 body paints during one
    scroll were full-viewport, on every page tried and on a minimal
    QScrollArea built in the same window, so it is how QScrollArea
    scrolls rather than anything one page does). Blurring a 1304x84
    logo 240 times a second is pure waste when the image never changes.
    Measured on Watch > Discover, real wheel scrolling on the owner's
    240Hz panel:

        with the live effect     218-222 positions/s
        with it removed          232 positions/s

    `bake_halo` renders the same effect once, through the same Qt code
    path, so the picture is unchanged - checked against a live-effect
    render, 0.9% of sampled pixels differing by more than 16 levels, all
    of it resampling noise at the edges."""
    label = QLabel("", parent)
    label.setVisible(False)
    label.setStyleSheet("background: transparent;")
    return label


HERO_LOGO_HALO_RADIUS = 28
HERO_LOGO_HALO_COLOUR = QColor(0, 0, 0, 200)


def bake_halo(pixmap: QPixmap, radius=HERO_LOGO_HALO_RADIUS,
              colour=HERO_LOGO_HALO_COLOUR):
    """`pixmap` with a soft dark halo painted around it, and the padding
    that was added on each side.

    Rendered through QGraphicsDropShadowEffect itself rather than an
    approximation, so this is the same blur the live effect drew - just
    computed once instead of once per frame. ~1.7ms for a hero logo,
    paid when the logo arrives.

    The canvas grows by `radius` on every side because that is where the
    blur goes; `set_hero_logo` gives the label negative contents margins
    of the same size, so neither the label's size hint nor the ink's
    position moves."""
    dpr = pixmap.devicePixelRatio() or 1.0
    pad = int(round(radius * dpr))
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pixmap)
    effect = QGraphicsDropShadowEffect()
    # **In scene units, not device pixels.** A QGraphicsPixmapItem's
    # bounding rect is the pixmap's *device-independent* size, so the
    # scene is already 1/dpr the canvas and rendering scales it back up
    # by dpr - a blur of radius*dpr here came out as radius*dpr*dpr on
    # screen. `radius` becomes radius*dpr device pixels through that
    # same scale, which is what `pad` reserves.
    effect.setBlurRadius(radius)
    effect.setOffset(0, 0)
    effect.setColor(colour)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    canvas = QImage(pixmap.width() + 2 * pad, pixmap.height() + 2 * pad,
                    QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(0)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    # **The item's own bounds as the source, not the pixmap's size.**
    # Measured 26 August 2026, and this is the owner's "the logos in the
    # featured and continue watching banners seem blurry in the 2K
    # monitor": QGraphicsPixmapItem reports its bounding rect in
    # device-independent units, so a 600x200 pixmap tagged dpr=2 is a
    # 300x100 item. Asking the scene for a 600x200 source rect therefore
    # asked for the item plus an equal amount of empty space, and the
    # logo was rendered at half scale into the corner of the canvas and
    # then drawn back at full size - soft on any scaled display, and
    # exactly right at 100%, which is why it only ever showed on the
    # second monitor.
    scene.render(painter,
                 QRectF(pad, pad, pixmap.width(), pixmap.height()),
                 item.boundingRect())
    painter.end()
    baked = QPixmap.fromImage(canvas)
    baked.setDevicePixelRatio(dpr)
    return baked, int(round(pad / dpr))


def set_hero_logo(logo_label: QLabel, title_label: QLabel, path,
                  height=HERO_LOGO_HEIGHT, max_width=HERO_LOGO_MAX_WIDTH) -> bool:
    """Show `path` as the hero's logo and hide the typed title - or, when
    there is no usable logo, the other way round. Returns whether a logo
    is showing. Never leaves both hidden."""
    from . import images
    pixmap = None
    if path:
        try:
            # **The window's ratio, not the label's.** A hero logo
            # arrives from a background lookup and is set on a label that
            # may not be on a screen yet, and an unparented widget
            # answers with the *primary* screen's factor - so on a second
            # monitor at another scale the logo was cut for the wrong one.
            window = logo_label.window()
            dpr = ((window.devicePixelRatioF() if window is not None else 0)
                   or logo_label.devicePixelRatioF() or 1.0)
            # **Scaled once, not twice.** This used to scale to `height`
            # and then, for anything too wide, scale the *result* again
            # to the width cap - two resamplings of the same artwork,
            # and the second one working from an image that had already
            # lost detail in the first. Every one of the owner's hero
            # logos is ~500px wide against a ~300px cap, so the second
            # pass ran every time, and softening it is exactly what he
            # reported ("the logo in the CONTINUE WATCHING/READING and
            # FEATURED are blurred", 26 August 2026).
            #
            # The height that satisfies both caps is worked out from the
            # source's own aspect ratio first, and the single scale is
            # made to that.
            size = images.image_size(path)
            wanted = height
            if size and size[0] > 0 and size[1] > 0:
                by_width = max_width * size[1] / float(size[0])
                wanted = max(1, int(round(min(height, by_width))))
            pixmap = images.logo_pixmap(path, wanted, dpr)
            if pixmap.isNull():
                pixmap = None
        except Exception:
            pixmap = None
    if pixmap is not None:
        # The halo, once (see hero_logo_label). Negative margins of
        # exactly the padding keep the label the size it was and the ink
        # where it was - a QLabel draws its pixmap from the *contents*
        # rect, so shrinking that by the padding cancels it out on both
        # axes at once.
        try:
            haloed, pad = bake_halo(pixmap)
            logo_label.setContentsMargins(-pad, -pad, -pad, -pad)
            pixmap = haloed
        except Exception:
            logs.exception("hero logo halo failed")
            logo_label.setContentsMargins(0, 0, 0, 0)
        logo_label.setPixmap(pixmap)
        logo_label.setVisible(True)
        title_label.setVisible(False)
        return True
    logo_label.clear()
    logo_label.setVisible(False)
    title_label.setVisible(True)
    return False


class GlyphButton(QPushButton):
    """A glyph button whose icon is painted rather than set as text.

    Built for the two exit controls (player and reader top bars). They
    carried Segoe Fluent's SignOut door, painted through a mirrored
    transform so it led left; the owner replaced the door with the same
    left arrow the episode list's prev/next carry, which already points
    the right way - so the mirroring is gone and only the painting
    remains.

    Painting it ourselves is what ends the "scratched" look for good: a
    text glyph on a QSS-styled button is clipped by whatever padding the
    app-wide rule leaves (player._icon_button records the measurement),
    where a painted one is centred on the button's own rect with no
    padding in the arithmetic at all."""

    def __init__(self, glyph, tooltip, size=(38, 38), font_pt=14, parent=None):
        super().__init__("", parent)
        self._glyph = glyph
        self._font_pt = font_pt
        self.setToolTip(tooltip)
        self.setFixedSize(*size)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none;"
            f" padding: 0px; border-radius: {theme.RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {theme.SURFACE_HOVER}; }}"
            f"QPushButton:pressed {{ background: {theme.SURFACE_ACTIVE}; }}")
        use_hover_cursor(self)

    def paintEvent(self, event):
        super().paintEvent(event)       # QSS hover/pressed fill
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        font = theme.icon_font(self._font_pt)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT if self.isEnabled() else theme.TEXT_DIM))
        painter.drawText(QRect(0, 0, self.width(), self.height()),
                         Qt.AlignmentFlag.AlignCenter, self._glyph)
        painter.end()


# Segoe Fluent's waste bin ("Delete", U+E74D). The one destructive
# action in this app has one picture, defined once - the details page's
# Remove From My List, and every page's bulk Delete.
TRASH_GLYPH = ""
TRASH_ICON_SIZE = 16


def glyph_icon(glyph: str, color: str = None, size: int = TRASH_ICON_SIZE,
               disabled_color: str = None) -> QIcon:
    """One icon-font codepoint as a QIcon.

    A QPushButton draws its text in one font, so a button that has to
    read "Remove From My List" *and* carry a Fluent glyph cannot do it
    with text - the app font has no U+E74D and would draw a hollow box.
    An icon can sit beside the label in whatever font it likes.

    The device pixel ratio is taken from the **screen**, not the widget:
    a widget's own devicePixelRatioF is 1.0 until it has been shown, and
    an icon built at 1.0 is upscaled by a 125% display and looks soft.
    Same trap magnifier_icon above records, same fix."""
    screen = QApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    # In points, and the glyph is drawn to fill the box: Segoe's icons
    # sit inside their em, so a pt size equal to the pixel size renders
    # noticeably smaller than the box asked for.
    painter.setFont(theme.icon_font(int(size * 0.75)))
    painter.setPen(QColor(color or theme.TEXT))
    painter.drawText(QRect(0, 0, size, size),
                     Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    icon = QIcon(pixmap)
    if disabled_color:
        # **Drawn again in the dim colour, not left to Qt.** A stylesheet
        # cannot reach inside an icon, so a disabled button's QSS fade
        # leaves the glyph at full strength on top of it - which on the
        # trash button is the whole control, and read as still live. Qt
        # does synthesise a disabled pixmap, but it is a generic fade of
        # whatever it was given and lands nowhere near TEXT_DIM against
        # this ground; painting it is one more drawText and is exact.
        dim = QPixmap(pixmap.size())
        dim.setDevicePixelRatio(dpr)
        dim.fill(Qt.GlobalColor.transparent)
        p2 = QPainter(dim)
        p2.setRenderHint(QPainter.RenderHint.Antialiasing)
        p2.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p2.setFont(theme.icon_font(int(size * 0.75)))
        p2.setPen(QColor(disabled_color))
        p2.drawText(QRect(0, 0, size, size),
                    Qt.AlignmentFlag.AlignCenter, glyph)
        p2.end()
        icon.addPixmap(dim, QIcon.Mode.Disabled)
    return icon


def trash_button(on_click, tooltip="Delete", size=40) -> QPushButton:
    """The bulk-delete control: the bin alone, no word beside it.

    The owner's ask, 28 August 2026 - "in all pages that contains Select
    button then when pressed it will show delete button, make it only
    trash symbol button instead of the delete button". Every page's
    selection bar builds its delete through here, so the two that used
    to spell out "Delete" independently (this module's SelectMixin and
    tracker's own copy of the bar) cannot drift apart again.

    Still #Danger, and still last in its row: shrinking the control does
    not make it less destructive, and the red is most of what says so
    once the word is gone. The tooltip carries the word for anyone who
    wants it."""
    button = QPushButton(objectName="Danger")
    button.setIcon(glyph_icon(TRASH_GLYPH, theme.ON_ACCENT,
                              disabled_color=theme.TEXT_DIM))
    button.setIconSize(QSize(TRASH_ICON_SIZE, TRASH_ICON_SIZE))
    button.setFixedSize(size, size)
    button.setToolTip(tooltip)
    button.clicked.connect(on_click)
    use_hover_cursor(button)
    return button


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


# How far a hovered button's face rises, and how far a press puts it
# back. Small numbers on purpose - the ask was "gently energized", not a
# mobile button popping out.
LIFT_PX = 1.5
PRESS_PX = 1.0
# The upper-left highlight's strength at rest, as an alpha of TEXT.
CORNER_GLINT = 0.055


class DriftButton(QPushButton):
    """A button whose hover *arrives* instead of switching on.

    The owner's ask, 26 August 2026: *"add a short short animation to
    all buttons when hover like make it smooth and drifty"*, pointing at
    the Continue and View Episodes buttons on the hero.

    **Painted, because a stylesheet cannot do this.** QSS has no
    transitions - `:hover` is a different rule applied whole the instant
    the pointer crosses the edge - so the only ways to fade one are to
    rewrite the stylesheet every frame (a style recomputation per frame,
    which is exactly what the sidebar rail was rebuilt to avoid) or to
    paint the fill here and let QPushButton draw its label on top. This
    does the second: its QSS background is transparent, the fill under
    the text is ours, and both the resting and hover ramps are read from
    theme so a drifting button and a static one are the same colour.

    `kind` picks the ramp: "accent" is the pressable teal
    (theme.accent_button_stops), "quiet" is the translucent slab the
    secondary hero button uses over artwork.

    The press deepens rather than scaling: a button that changes size
    under the pointer moves the thing being clicked.
    """

    HOVER_MS = 140
    PRESS_MS = 90

    def __init__(self, text="", parent=None, kind="accent", radius=None,
                 objectName="DriftAccent"):
        super().__init__(text, parent, objectName=objectName)
        self._kind = kind
        self._radius = theme.RADIUS if radius is None else radius
        self._hover = 0.0
        self._press = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover_tween = SmoothTween(self, self._set_hover, self.HOVER_MS)
        self._press_tween = SmoothTween(self, self._set_press, self.PRESS_MS)
        use_hover_cursor(self)

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

    def _ramp(self, rect, hover):
        """The fill, lit corner to corner.

        Diagonal rather than top-down: a tone that shifts across both
        axes is what gives a 46px button depth, where a lit lip alone
        leaves the body flat. Same stops the stylesheet uses
        (theme.accent_button_stops), so a painted button and a QSS one
        are the same material."""
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        if self._kind == "accent":
            for at, colour in theme.accent_button_stops(hover=hover):
                gradient.setColorAt(at, QColor(colour))
            return gradient
        # "quiet": the translucent slab over artwork. Its hover picks up
        # a teal *tint* rather than becoming a primary button - the
        # hierarchy is the point, and a secondary that turns solid teal
        # on hover reads as two calls to action.
        top = QColor(theme.mix(theme.SURFACE, theme.ACCENT_DEEP, 0.22)
                     if hover else theme.SURFACE)
        top.setAlpha(215 if hover else 185)
        foot = QColor(theme.SURFACE if hover else theme.BG)
        foot.setAlpha(215 if hover else 195)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, foot)
        return gradient

    def paintEvent(self, event):
        # **Lit only while the pointer is genuinely on it.** A Leave is
        # not guaranteed - a click here can open a full-window surface
        # over this button, which covers it without the pointer crossing
        # its edge, and it would then still be lit on the way back. Same
        # class of bug as the poster grid's stuck play button (the
        # owner's screenshot, 26 August 2026); same cure, which is to
        # ask where the pointer is rather than trust the event that
        # should have come. Unwound rather than snapped, so a button
        # caught this way still fades out like any other.
        if self._hover > 0.0 and not self.underMouse():
            self._hover_tween.start(self._hover, 0.0, self.HOVER_MS)
            self._press_tween.start(self._press, 0.0, self.PRESS_MS)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # **The lift.** Hover raises the fill a pixel and a half, a
        # press puts it back down - the label does not move with it,
        # deliberately: QPushButton draws that through the style and
        # translating this painter would not reach it, and at this
        # distance the surface rising under a fixed label reads as the
        # button lifting rather than as a misalignment. Nothing about
        # the layout changes either way.
        lift = -LIFT_PX * self._hover + PRESS_PX * self._press
        rect = QRectF(self.rect()).adjusted(0.0, lift, 0.0, lift)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._ramp(rect, hover=False))
        painter.drawRoundedRect(rect, self._radius, self._radius)
        # The hover ramp faded in over the resting one, so the colour
        # crosses rather than switching. The press deepens the same
        # fade instead of adding a layer, which keeps the two states
        # from stacking into something brighter than either.
        mix = self._hover * (1.0 - 0.25 * self._press)
        if mix > 0.001:
            painter.setOpacity(min(1.0, max(0.0, mix)))
            painter.setBrush(self._ramp(rect, hover=True))
            painter.drawRoundedRect(rect, self._radius, self._radius)
            painter.setOpacity(1.0)
        # A soft highlight in the upper-left corner, present at rest and
        # a little stronger under the pointer. Low opacity on purpose:
        # this is depth, not gloss.
        glint = QLinearGradient(rect.topLeft(), rect.center())
        edge = QColor(theme.TEXT)
        edge.setAlphaF(CORNER_GLINT * (0.55 + 0.45 * self._hover))
        gone = QColor(edge)
        gone.setAlphaF(0.0)
        glint.setColorAt(0.0, edge)
        glint.setColorAt(1.0, gone)
        painter.setBrush(glint)
        painter.drawRoundedRect(rect, self._radius, self._radius)
        # Pressed: the whole face takes a shade of the page under it.
        if self._press > 0.001:
            shade = QColor(theme.BG)
            shade.setAlphaF(0.16 * self._press)
            painter.setBrush(shade)
            painter.drawRoundedRect(rect, self._radius, self._radius)
        painter.end()
        super().paintEvent(event)


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


# The longest a refresh-rate-driven tick is allowed to be. A screen
# reporting 0Hz (a headless or offscreen platform) falls back to this,
# which is Qt's own animation granularity.
MAX_TICK_MS = 16


def screen_frame_s(widget=None) -> float:
    """One display refresh in SECONDS, unrounded. 0.0 if unknown.

    screen_tick_ms floors to whole milliseconds because a QTimer only
    takes whole milliseconds. Nothing else may use that number as the
    refresh interval: 6ms against a real 6.944ms is a 14% error, and
    _Momentum._tick now snaps timestamps to this grid, where that error
    would accumulate into exactly the beat the snap exists to remove."""
    try:
        screen = (widget.screen() if widget is not None else None)             or QApplication.primaryScreen()
        rate = screen.refreshRate() if screen is not None else 0.0
    except Exception:
        rate = 0.0
    return 1.0 / rate if rate and rate > 0 else 0.0


# The fastest a **painted** surface will try to move anything, whatever
# the panel is capable of - see poster_grid._refresh_interval, which is
# the only caller now.
#
# **It was applied to the widget clock too, and that was wrong.** The cap
# was measured on 27 August 2026 against the painted grids, which
# schedule their own frames off the refresh phase and genuinely cannot
# present faster; taking it as a rule for everything put an 8ms QTimer
# against a 4.17ms panel, and a timer that beats against the refresh is
# how uneven steps are made. That is the owner's "Home and Discover
# still not smooth while scrolling like Movies page or Anime page".
#
# Measured the same way on both surfaces - the sequence of distinct
# integer scroll positions and the interval between them, one wheel
# notch every 110ms, his 240Hz panel. Three runs of each, because the
# first pass of this read 0% off a single run and that was an outlier:
#
#     Discover (widgets)  8ms tick   55, 55, 55 pos/s  8.0ms  judder 33/30/30%
#                         panel rate 82, 86, 86 pos/s  4.6ms  judder 25/17/17%
#     Movies (painted)    8ms tick   94-111 pos/s      8.5ms  judder 20%
#                         4ms tick   61 pos/s         15.3ms  judder 25%
#
# The two want opposite things and now get them: the cap stays where it
# was measured, and the widget clock below runs at the panel's own rate.
# Discover ends up ahead of Movies on evenness (17% against 20%) and
# produces a position every refresh instead of every other one.
#
# The vblank ticker was tried as the alternative and is not it: 22% with
# it on, against 17% for simply matching the timer to the refresh.
MOTION_MAX_HZ = 120.0


def screen_tick_ms(widget=None) -> int:
    """One display refresh, in whole milliseconds.

    **Qt's animation clock is not this number, and that is the whole
    problem it exists to solve.** QPropertyAnimation and friends run off
    a unified timer that ticks about every 16ms - 60 steps a second -
    while both of this machine's monitors run at **144Hz** (measured). So
    any animation driven by Qt produces a new value only every 2.4
    display refreshes: the panel shows each position two or three times
    over, which reads as stepping however well the curve was chosen.
    Nothing is dropping frames; there are simply not enough positions.

    Everything the owner has called "stuttering" - the wheel, the sidebar
    fold, the sideways rows - has been this.

    **ATOMIC_SCROLL_HZ pins it, for A/B measurement.** The owner's ask,
    27 August 2026, after describing the symptom as an afterimage rather
    than a stutter: a steady 120Hz may present more evenly than an
    irregular attempt at 240. Measured on Home the same day, one scroll:
    the vblank clock is live (dwm, 4ms nominal = 240Hz) and yet the GUI
    thread completed only **115 position updates a second** and the
    screen showed about **55 distinct positions** - with 16% of the
    visible steps more than 1.5x the median. Asking for more positions
    than the thread can paint is what makes the steps uneven.

    Nothing about the physics changes: the integrator reads real elapsed
    time, so a slower tick moves further per tick rather than travelling
    less."""
    # **Capped at 120Hz, because that is all the app can present.**
    # Measured 27 August 2026 by capturing the *screen* at 241fps during
    # a fling and correlating each frame against the last, so this is
    # what the panel actually showed rather than what the app intended:
    # a new position arrived every ~8.3ms whatever the clock was set to,
    # while the vblank ticker was generating one every 4.2ms. Half of
    # every position computed was discarded before it could be seen.
    #
    # Asking for positions that cannot be presented is not free - it is
    # what makes the sizes of the ones that *are* presented uneven, and
    # uneven step sizes are what the owner sees as an afterimage.
    hz = os.environ.get("ATOMIC_SCROLL_HZ")
    if hz:
        try:
            wanted = float(hz)
            if wanted > 0:
                return max(1, int(1000.0 / wanted))
        except ValueError:
            pass
    try:
        screen = (widget.screen() if widget is not None else None)             or QApplication.primaryScreen()
        rate = screen.refreshRate() if screen is not None else 0.0
    except Exception:
        rate = 0.0
    if not rate or rate <= 0:
        return MAX_TICK_MS
    # **No MOTION_MAX_HZ here** - see the constant for the measurement.
    # This clock drives _Momentum and SmoothTween, which move widgets that
    # Qt paints; capping it below the panel makes the timer beat against
    # the refresh, which is judder rather than a saving.
    # **Floor, not round - and 144Hz is the one rate where that matters.**
    # 1000/144 is 6.944ms; rounding gives 7ms, which is 142.86 positions a
    # second against a panel asking for 144. So the tick this function
    # exists to match was itself landing *under* the refresh rate, and
    # every glide in the app - the wheel, the page slide, the sidebar
    # fold, the sideways rows - was capped just below 144fps by the
    # rounding alone. Checked at every rate involved: 60 -> 16ms (62.5/s),
    # 120 -> 8ms (125/s), 144 -> 6ms (166.7/s), 165 -> 6ms (166.7/s),
    # 240 -> 4ms (250/s); only 144 was short, and only under round().
    # Flooring is never worse - a tick slightly faster than the panel is
    # coalesced by the compositor, one slightly slower drops a frame.
    return max(4, min(MAX_TICK_MS, int(1000.0 / rate)))


def ease_out_cubic(fraction: float) -> float:
    """1-(1-t)^3, by hand - the curve every glide in this app uses."""
    return 1.0 - (1.0 - fraction) ** 3


class SmoothTween(QObject):
    """A number moved from one value to another at the screen's refresh
    rate, instead of at Qt's animation rate.

    A drop-in for the QPropertyAnimation-on-a-scalar pattern wherever
    the movement is something the eye follows: same curve, same
    duration, 2.4x the positions on a 144Hz panel (see screen_tick_ms).
    Retargets rather than restarting - a second start() mid-flight
    re-aims from wherever the value currently is, so repeated presses
    never queue a backlog of animations.

    `apply` is called with a float on every tick; `on_done` once when it
    lands, if given. Both are held on this object, which is parented to
    the widget it animates, so the pair die with it."""

    def __init__(self, owner, apply, duration_ms, on_done=None):
        super().__init__(owner)
        self._owner = owner
        self._apply = apply
        self._on_done = on_done
        self._duration = max(1, int(duration_ms))
        self._from = 0.0
        self._to = 0.0
        self._started_at = 0.0
        self._running = False
        self._timer = QTimer(self)
        # Precise, or Qt coalesces a 7ms timer back up to ~16ms and
        # hands back exactly the stepping this removes.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self._running

    def start(self, start_value, end_value, duration_ms=None):
        if duration_ms is not None:
            self._duration = max(1, int(duration_ms))
        self._from = float(start_value)
        self._to = float(end_value)
        self._started_at = time.monotonic()
        self._running = True
        # Re-read every run: the window can have moved to the other
        # monitor since the last one.
        self._timer.setInterval(screen_tick_ms(self._owner))
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._running = False
        self._timer.stop()

    def _tick(self):
        if not self._running:
            self._timer.stop()
            return
        elapsed = (time.monotonic() - self._started_at) * 1000.0
        fraction = min(1.0, elapsed / float(self._duration))
        value = self._from + (self._to - self._from) * ease_out_cubic(fraction)
        try:
            self._apply(value)
        except RuntimeError:
            self.stop()         # the widget went away mid-flight
            return
        if fraction >= 1.0:
            self.stop()
            if self._on_done is not None:
                try:
                    self._on_done()
                except RuntimeError:
                    pass


class PickCombo(QComboBox):
    """A drop-down whose popup answers the *first* click on a row.

    The owner's report, 22 August 2026: *"in some lists like the seasons
    list in the ep list page I need to click 2 times to go to season 1"*.

    Qt blocks every mouse release over a combo popup for
    `QApplication::doubleClickInterval()` after `showPopup()`
    (`QComboBoxPrivateContainer::blockMouseReleaseTimer`), so that the
    release belonging to the click which *opened* the popup cannot
    instantly pick whatever row landed under the pointer. It cancels
    that block on the first mouse move over the popup - and that is the
    hole. **Measured on this page, 22 August 2026: the popup takes
    166-190ms to appear** (twelve opens, the app-wide stylesheet being
    re-polished for the popup window). A hand that is already moving
    spends those milliseconds over the page, not over a popup that does
    not exist yet, so nothing cancels the block, and the row click
    inside the remaining ~330ms is dropped in silence - the popup simply
    stays open.

    Confirmed by moving the input rather than by reading Qt: the same
    gesture, driven with real SendInput mouse events, flipped both ways
    with `doubleClickInterval` alone -

        interval   click 139ms after the popup appeared   click 1014ms after
        0ms        picked                                 picked
        500ms      **eaten**                              picked
        2000ms     **eaten**                              **eaten**

    500ms is this machine's setting, and Windows' slider goes to 900.

    So the release is answered here instead, before Qt's own filter sees
    it (an event filter installed later runs first). Qt's protection is
    kept, just tested at release time rather than trusted to intervening
    move events: a release that has not travelled from the click which
    opened the popup is that click's own release and is left alone."""

    # Qt's own threshold for "the pointer has moved", in
    # QComboBoxPrivateContainer::eventFilter. Same number on purpose.
    MOVED_ENOUGH_PX = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self._opened_at = QPoint()
        self._filtered_view = None
        # From construction, not only from the first open - see
        # smooth_combo. showPopup calls it again in case Qt has replaced
        # the view since.
        smooth_combo(self)

    def showPopup(self):
        # QCursor.pos(), not mapToGlobal - see .claude/rules/ui.md; this
        # value is compared against a real event's global position.
        self._opened_at = QCursor.pos()
        super().showPopup()
        view = self.view()
        if view is not None and self._filtered_view is not view:
            # After super().showPopup(), so the container has already
            # installed its own filters and ours is the newer - Qt runs
            # the most recently installed filter first, which is the
            # whole mechanism here.
            view.viewport().installEventFilter(self)
            # The popup is a list like any other - see ScrollBarDrag.
            smooth_combo(self)
            self._filtered_view = view

    def eventFilter(self, obj, event):
        view = self.view()
        if (view is not None and obj is view.viewport()
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and view.isVisible()):
            travelled = (event.globalPosition().toPoint()
                         - self._opened_at).manhattanLength()
            index = view.indexAt(event.position().toPoint())
            if travelled > self.MOVED_ENOUGH_PX and index.isValid():
                flags = index.flags()
                if (flags & Qt.ItemFlag.ItemIsEnabled
                        and flags & Qt.ItemFlag.ItemIsSelectable):
                    # The order QComboBoxPrivate::_q_itemSelected uses:
                    # the index first, so a slot on activated reading
                    # currentData() sees the row that was clicked.
                    self.hidePopup()
                    self.setCurrentIndex(index.row())
                    self.activated.emit(index.row())
                    self.textActivated.emit(self.itemText(index.row()))
                    return True
        return super().eventFilter(obj, event)


class CardTextLabel(QLabel):
    """A word-wrapped line of text on a card, sized honestly for the
    width it will actually be given.

    A plain wrapped QLabel is not, and that clipped the second line of
    every long card name on Apps, Websites and Games. Two Qt behaviours
    combine to do it:

    * `QLabel.sizeHint()` for a wrapped label is a heuristic - it picks a
      wrap width it thinks looks balanced rather than the one it will be
      laid out at, and reports the height *that* width needs. Measured on
      "A Really Long Missing Application Name": a sizeHint wide enough
      for two lines, in a card that only ever offers 104px, where the
      same text needs three.
    * A QBoxLayout with an alignment set (these cards centre their
      contents) lays itself out inside `alignmentRect`, which clamps the
      layout's *width* to what the card has - but keeps the height the
      too-wide sizeHint asked for. So the label is narrowed without ever
      being asked how tall it now needs to be.

    Fixing the width and answering sizeHint from `heightForWidth` at that
    same width removes both halves: the layout cannot narrow it further,
    and the height it reports is the height the text really occupies.
    Deliberately lazy rather than measured in `__init__` - the fonts here
    come from QSS (#CardTitle's weight, the badge's 8pt), which is not
    applied to a widget until it is polished, some time after it is
    built."""

    def __init__(self, text, width, parent=None):
        super().__init__(text, parent)
        self._text_width = width
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setFixedWidth(width)

    def sizeHint(self):
        return QSize(self._text_width, self.heightForWidth(self._text_width))

    def minimumSizeHint(self):
        # Same answer as sizeHint: QLabel's own minimumSizeHint for
        # wrapped text is another heuristic, and a minimum shorter than
        # the real height is all a grid row needs to squeeze the last
        # line back off the card.
        return self.sizeHint()


class PageSlide(QWidget):
    """Two pages sliding past each other, painted as two flat pictures.

    **This is the owner's "the page transition stutters", and the cause
    was never the curve.** Measured 22 August 2026 in a real window on
    the owner's data, Home -> Movies & Series with the shipped
    `QPropertyAnimation(page, b"pos")` on both pages:

        ticks delivered  13 over 227ms  ->  57 fps   (best run)
        ticks delivered   6 over 438ms  ->  13.7 fps (third run, 271ms stall)
        paint events     832 over 13 ticks -> **64 per tick**
                         654 over  6 ticks -> **109 per tick**

    Moving a widget moves its children, and because a page is not marked
    opaque Qt cannot blit its backing store - so every QLabel, Card,
    QPushButton and scroll viewport on *both* pages re-rendered on every
    single step. Sixty-odd repaints to move a picture 40 pixels. Exactly
    the shape of the scroll-frame cause in CLAUDE.md rule 7, and the same
    answer: give Qt something it can blit.

    So each page is rendered **once** into a pixmap, both pages are
    handed to this one opaque widget, and a step is two `drawPixmap`
    calls - one paint event per frame instead of sixty-four, with the
    real widgets hidden behind it and not repainting at all.

    Driven by SmoothTween rather than QPropertyAnimation for the other
    half of the same complaint: Qt's animation clock ticks every ~16ms
    whatever the panel does, so an animation can never have more
    positions than 60 a second. SmoothTween runs at screen_tick_ms.

    `direction` is +1 when the new page comes up from below (or in from
    the right), -1 when it comes down from above (or in from the left).
    `axis` is "y" for the page stack and "x" for the sidebar swap, which
    slides two full bars over each other and had exactly the same cost."""

    def __init__(self, parent, old_pixmap, new_pixmap, direction, duration_ms,
                 on_done=None, axis="y"):
        super().__init__(parent)
        # Opaque and no system background: this covers the whole
        # container and paints every pixel of it, so telling Qt that
        # saves an erase per frame and stops the parent painting under
        # it. Without the attribute the measured win is roughly halved.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._old = old_pixmap
        self._new = new_pixmap
        self._dy = direction
        self._axis = axis
        self._offset = 0.0
        self._on_done = on_done
        self._tween = SmoothTween(self, self._apply, duration_ms,
                                  on_done=self._finish)

    def start(self):
        self.setGeometry(self.parent().rect())
        self.show()
        self.raise_()
        self._tween.start(0.0, 1.0)

    def _apply(self, fraction):
        self._offset = float(fraction)
        self.update()

    def _finish(self):
        if self._on_done is not None:
            self._on_done()

    def stop(self):
        """Abandon the slide - a second navigation arriving mid-flight.
        The callback still runs, so the pending page is never orphaned."""
        self._tween.stop()
        self._finish()

    def paintEvent(self, event):
        painter = QPainter(self)
        span = self.height() if self._axis == "y" else self.width()
        # The new page travels from `dy * span` to 0; the old one from 0
        # to `-dy * span`. Rounded to whole pixels: a pixmap drawn at a
        # fractional offset is resampled, which is both slower and
        # visibly soft on text.
        travel = int(round(self._offset * span))
        if self._dy > 0:
            new_at, old_at = span - travel, -travel
        else:
            new_at, old_at = travel - span, travel
        for pixmap, at in ((self._old, old_at), (self._new, new_at)):
            if pixmap is None or pixmap.isNull():
                continue
            if self._axis == "y":
                painter.drawPixmap(0, at, pixmap)
            else:
                painter.drawPixmap(at, 0, pixmap)
        painter.end()


# ---------------------------------------------------------------------
# The vblank ticker: motion clocked by the panel itself, not by QTimer.
#
# **Why this exists - measured 24 August 2026 on the owner's new PC
# (Windows 11 26200, 2560x1440 @ 240Hz):** a 4ms PreciseTimer QTimer
# fired every **13.9ms median** - with the window foreground, with
# timeBeginPeriod(1) active, and with the tick itself costing 0.26ms -
# so _Momentum produced ~70 positions a second against a panel showing
# 240, and 71-74% of refreshes moved nothing at all (per-refresh steps
# sampled at IDXGIOutput::WaitForVBlank). That is the owner's "in 2k it
# is not smooth the reader". The OS quantised the timer; nothing inside
# the process was slow.
#
# So the clock is now the display: one daemon thread blocks on
# WaitForVBlank and emits a queued signal every real refresh, and
# _Momentum steps on that signal instead of a timer. The position is
# still computed from the *snapped timestamp* (_tick), so a burst of
# queued ticks after a busy stretch collapses into no-ops rather than a
# lurch - which is what went wrong the first time a vblank clock was
# tried (24 August 2026, 62-64% judder): that version *drove* the
# position per tick; this one only decides when to look at the clock.
#
# The primary output's vblank, deliberately: enumerating per-window
# outputs buys nothing here because the timestamp snap already quantises
# to the widget's own refresh interval - on the 165Hz second monitor the
# 240Hz ticks between its refreshes land on the same snapped time and
# move nothing. Falls soft back to the QTimer path when DXGI is not
# there (RDP, a test rig), and ATOMIC_NO_VBLANK=1 forces that fallback
# so the two paths stay measurable against each other.

_IID_IDXGIFactory1 = (ctypes.c_ubyte * 16)(
    0x78, 0xae, 0x0a, 0x77, 0x6f, 0xf2, 0xba, 0x4d,
    0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87)


class _VBlankTicker(QObject):
    """The shared per-refresh tick, awake only while something scrolls.

    **The gate is the point.** The first version looped on
    `WaitForVBlank` for the life of the process, so a 240Hz panel meant
    a thread waking 240 times a second and emitting a queued signal into
    the UI thread forever - while reading a page, while watching a
    video, while the app sat idle. That is a constant tax on everything,
    which is the shape of the owner's report the same day: "the
    scrolling now in the whole app seems lower in fps". The wait itself
    is a driver spin, not a cheap sleep.

    So the thread parks on an Event and is woken only between the first
    `acquire()` and the last `release()` - i.e. only while at least one
    _Momentum is actually mid-glide."""

    tick = Signal()

    def __init__(self):
        super().__init__()
        self.failed = False
        # Which clock the thread settled on ("dwm"/"dxgi"), or None while
        # it is still timing them. Read by _vblank_ticker_for_use to know
        # the decision has been made, and worth having by name: "which
        # clock is this machine on" is the first question any future
        # scroll measurement here will ask.
        self.clock = None
        self._wanted = 0
        self._lock = threading.Lock()
        self._awake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="vblank-ticker")
        self._thread.start()

    def acquire(self):
        """One more surface wants ticks."""
        with self._lock:
            self._wanted += 1
            if self._wanted == 1:
                self._awake.set()

    def release(self):
        """One fewer. At zero the thread parks until the next kick."""
        with self._lock:
            self._wanted = max(0, self._wanted - 1)
            if self._wanted == 0:
                self._awake.clear()

    def _com(self, obj, index, restype, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p)).contents.value
        fn = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)

    # What a clock has to do to be believed. A display refresh is
    # somewhere between 24Hz and 1000Hz in any world this app runs in,
    # so a "wait" that returns faster than a millisecond is not waiting
    # for anything and one that takes longer than a tenth of a second is
    # not a refresh either.
    CLOCK_MIN_MS = 1.0
    CLOCK_MAX_MS = 100.0
    CLOCK_SAMPLES = 8

    def _plausible(self, wait) -> bool:
        """Time `wait` a few times and say whether it behaves like a
        display.

        **This check is the whole reason the ticker was rewritten, and
        it is a measurement, not a precaution.** Measured 24 August 2026
        on the owner's 2560x1440 240Hz panel:

            IDXGIOutput::WaitForVBlank   2000 calls in 1.2ms
                                         = 1,677,149/s, every one S_OK
            DwmFlush                      240 calls in 999.4ms
                                         = 240.1/s

        DXGI's wait returns *immediately and successfully* on this
        machine - the desktop is composited, the enumerated output is
        not owned, and nothing about the return code says so. The old
        code took S_OK as proof of a clock, so `failed` stayed False,
        the millisecond-timer fallback was never used, and every
        scrolling surface was driven by a thread spinning a whole core
        and posting queued signals into the UI thread as fast as it
        could.

        What the panel then showed. Measured on the real pages, the real
        window, a seeded 240-title library, the scroll position sampled
        once per compositor present and the wheel driven at a fixed 20
        notches a second - "before" is this same build with
        ATOMIC_VBLANK_TRUST=1, which is what shipped:

            surface            before                after
            Watch categories   104.9 fps, 56.3% dead   221.8,  7.6%
            Saved grid         148.6 fps, 38.1% dead   220.1,  8.3%
            Home               194.0 fps, 19.2% dead   230.8,  3.8%
            reader strip       200.5 fps, 16.4% dead   234.0,  2.5%

        and the evenness with it - on the category grid the step spread
        went x2.22 -> x1.20 and the local step-to-step change 20/140% ->
        17/57%. The *travel* moved too, which is the half that explains
        the feel: the same wheel cadence carried the reader strip 898
        px/s before and 1330 after, because a starved clock is a starved
        motion, not merely a low frame rate. That is the owner's "the text labels and image cards
        seem to be refreshing on a stiff way"; his "sometimes the whole
        app seems lower in fps" is the same fault on a machine where the
        call happens to work, since whether it does is a property of the
        display path and not of this app.

        **The millisecond timer measured better still, and is not what
        this returns to.** Same surface, ATOMIC_NO_VBLANK=1: 236.0 fps,
        1.7% dead, spread x1.25. It is faster because it schedules the
        paint *before* the boundary rather than reacting after one - but
        its worst case is a cliff, not a slope: the handover records
        65.2 steps/s and 72.9% dead on one run in three, because
        `_schedule_frame` asks for 0-4ms and Windows quantises a timer
        request to ~13.9ms unless some process on the machine has raised
        the global resolution. A clock that cannot be quantised is worth
        eleven frames a second."""
        try:
            wait()                              # first one may be partial
            start = time.perf_counter()
            for _ in range(self.CLOCK_SAMPLES):
                wait()
            each_ms = (time.perf_counter() - start) * 1000.0 / self.CLOCK_SAMPLES
        except Exception:
            return False
        return self.CLOCK_MIN_MS <= each_ms <= self.CLOCK_MAX_MS

    def _dwm_clock(self):
        """DwmFlush, which blocks until the compositor's next present.

        Preferred over DXGI now: it is what the user actually sees (the
        compositor is the last stop before the panel), it needs no COM
        plumbing, and it measured exact - 240.1/s against a panel
        reporting 240Hz."""
        try:
            flush = ctypes.windll.dwmapi.DwmFlush
        except Exception:
            return None
        return flush

    def _dxgi_clock(self):
        """IDXGIOutput::WaitForVBlank on the first output of the first
        adapter. Kept as the fallback for a machine with no DWM."""
        try:
            dxgi = ctypes.windll.dxgi
            factory = ctypes.c_void_p()
            if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1),
                                       ctypes.byref(factory)) != 0:
                return None
            adapter = ctypes.c_void_p()
            if self._com(factory, 7, ctypes.c_long, ctypes.c_uint,
                         ctypes.POINTER(ctypes.c_void_p))(
                    factory, 0, ctypes.byref(adapter)) != 0:
                return None
            output = ctypes.c_void_p()
            if self._com(adapter, 7, ctypes.c_long, ctypes.c_uint,
                         ctypes.POINTER(ctypes.c_void_p))(
                    adapter, 0, ctypes.byref(output)) != 0:
                return None
            call = self._com(output, 10, ctypes.c_long)
        except Exception:
            return None
        # `output` is captured by the closure so the COM pointer cannot
        # be collected while the thread is still waiting on it.
        return lambda: call(output)

    def _run(self):
        # Each candidate is *timed* before it is trusted - see
        # _plausible for the machine where the second one lies.
        wait = None
        builders = [("dwm", self._dwm_clock), ("dxgi", self._dxgi_clock)]
        # Two measurement switches, in the spirit of ATOMIC_NO_VBLANK
        # above: ATOMIC_VBLANK_CLOCK names which candidate to try first,
        # and ATOMIC_VBLANK_TRUST=1 skips the plausibility check - which
        # is the only way to reproduce the shipped fault on a machine
        # where DXGI lies, and therefore the only way to A/B a fix for
        # it. Neither is read anywhere but here.
        wanted = os.environ.get("ATOMIC_VBLANK_CLOCK", "").strip().lower()
        if wanted:
            builders.sort(key=lambda row: row[0] != wanted)
        trust = os.environ.get("ATOMIC_VBLANK_TRUST") == "1"
        for name, build in builders:
            candidate = build()
            if candidate is not None and (trust or self._plausible(candidate)):
                wait, self.clock = candidate, name
                break
        if wait is None:
            self.failed = True
            return
        while True:
            # Parked when nothing is scrolling - see the class docstring.
            self._awake.wait()
            try:
                if wait() != 0:
                    # A clock that starts failing - DWM restarting, a
                    # mode change, an output lost - must not become a
                    # spin. This guard is why the old loop slept on a
                    # non-zero return, and it is worth keeping now that
                    # the *zero* return is the one that was lying.
                    time.sleep(0.01)
            except Exception:
                self.failed = True
                return
            if not self._awake.is_set():
                continue        # the last surface stopped mid-wait
            try:
                frame_pacing.note_vblank()
                self.tick.emit()
            except RuntimeError:
                return          # the app is shutting down under us


_vblank_ticker = None


def _vblank_ticker_for_use():
    """The shared ticker, or None - which is now the default.

    **Off unless ATOMIC_VBLANK=1 asks for it.** This reverses the choice
    recorded in _Momentum._start_ticking, and it does so on a different
    measurement rather than a better argument: that A/B was judged from
    inside the app, on a 144Hz panel, by how evenly the *scrollbar value*
    advanced. This one captures the screen at 241fps on the owner's
    240Hz panel and correlates consecutive frames, so it measures what
    was actually presented.

    Three flings each, counting the artefact that reads as a double
    image - a presented step *larger* than the one before it, during a
    decay where every step should be smaller:

        vblank ticker, 240Hz    1, 1, 4 hitches
        plain timer, 120Hz      1, 0, 0 hitches

    with the same 8.3ms presented cadence and the same interval spread
    either way. The ticker was generating a position every 4.2ms and the
    screen was showing one every 8.3ms, so half of them were discarded -
    and discarding half the positions unevenly is what made the surviving
    steps uneven.

    Kept rather than deleted: ATOMIC_VBLANK=1 puts it back, which is how
    the comparison above can be repeated on another panel."""
    global _vblank_ticker
    if os.environ.get("ATOMIC_VBLANK") != "1":
        return None
    if _vblank_ticker is None:
        _vblank_ticker = _VBlankTicker()
        # Give the thread long enough to *time* its candidate clocks, not
        # merely to construct them - _VBlankTicker._plausible waits eight
        # refreshes, which is 33ms at 240Hz and 133ms at 60. The old 20ms
        # grace predates that check and would return the ticker before it
        # had decided anything, which is the one case where a surface
        # commits to a clock that is about to mark itself failed.
        deadline = time.monotonic() + 0.30
        while (not _vblank_ticker.failed
               and _vblank_ticker.clock is None
               and time.monotonic() < deadline):
            time.sleep(0.005)
    return None if _vblank_ticker.failed else _vblank_ticker


def warm_display_clock():
    """Decide which display clock this machine has, before anything
    scrolls. Returns its name ("dwm"/"dxgi") or None.

    Public because main() calls it at startup: `_vblank_ticker_for_use`
    now *times* its candidates before trusting one, and that probe waits
    eight refreshes - 33ms at 240Hz, 133ms at 60. Paid once at launch
    rather than by whoever scrolls first."""
    ticker = _vblank_ticker_for_use()
    clock = getattr(ticker, "clock", None) if ticker is not None else None
    # Written to the log, because "which clock did this machine get" is
    # the first question any report of stiff scrolling has to answer and
    # it is not visible from inside the app otherwise. One line per
    # launch.
    try:
        logs.info(f"display clock: {clock or 'none (millisecond timer)'}")
    except Exception:
        pass
    return clock


# How many surfaces are mid-glide right now. **The frame-pacing
# profiler asked _Momentum for a `_live` list that has never existed**,
# so every stall it logged from the owner's machine on 27 August 2026
# said `scrolling=no` - including a 67ms one on the Series page while he
# was browsing it. A field that cannot say yes is worse than no field:
# it reads as evidence that scrolling was innocent.
#
# A counter rather than a list: the question is only "is anything
# gliding", the answer is read once per stall, and a list of live
# QObjects here would be one more thing to leak.
_GLIDING = 0


def momentum_active() -> bool:
    """Whether any surface is mid-glide - see _GLIDING."""
    return _GLIDING > 0


class _Momentum(QObject):
    """Wheel scrolling as velocity and friction, not as a curve restarted
    on every notch.

    **Why the old model felt "stiff as a board" (the owner, 24 August
    2026), measured on Watch > Anime in a real 1600x900 window at
    144Hz:** each notch restarted a 220ms OutCubic from the current
    position, so speed depended only on the undelivered backlog, never
    on the wheel's cadence. At one notch per 150ms the velocity was a
    sawtooth - ~1500 px/s right after each notch, 215-290 px/s just
    before the next, a 5-7x step fourteen times in a row. After a burst
    the first 7ms frame moved 9.2% of the whole remaining distance (51-63
    px in one frame). An 8-notch flick in 107ms travelled exactly as far
    as 8 slow notches and was dead-stopped 205ms after the last one. And
    19% of the glide's ticks moved nothing, because the cubic's tail
    advances less than a pixel per tick and round() ate it. Frame rate
    was never the problem: paint gaps were 5.9-6.4ms at every cadence.

    So: a notch is an impulse added to a velocity; position integrates
    every screen tick from real elapsed time; velocity decays as
    exp(-FRICTION*dt). A single notch still travels its old distance
    (impulse = distance x FRICTION) but settles along an exponential
    tail rather than stopping dead; a second notch arrives while the
    first is still moving and simply adds to it, so a steady cadence is
    a steady speed; and notches in quick succession earn a little more
    each (ACCEL_*), which is what makes a flick go somewhere. A notch
    against the current direction kills that momentum first, so a
    change of mind answers at once.

    Shared by the pages' vertical scroll and the sideways card rows; the
    arrow buttons keep their discrete tween because a press is a jump,
    not scrolling. The reader's strip keeps its own physics as before."""

    # Exponential decay rate, 1/s. A single notch's unfinished fraction
    # after t seconds is exp(-FRICTION*t): at 16/s a 122px notch is
    # under a pixel from done at ~300ms, against the cubic's 188ms dead
    # stop - longer, and shaped like something that was moving.
    # **12, down from 16, measured at the mid cadence.** At 16/s the speed
    # between two notches 150ms apart fell from ~1650 to ~330 px/s - a
    # steady hand still produced a pulse per notch. At 12 it keeps ~1/6
    # more between notches and a lone notch still settles in ~350ms.
    #
    # **7, down from 12 - the owner, 24 August 2026: "super super stiff",
    # make it smooth like Stremio/Harbor.** Measured on Watch > Anime at
    # the mid cadence (one notch / 150ms), velocity in the 25ms before a
    # notch against the 25ms after it: at 12 it was 444 -> 1352 px/s, a
    # 3x pulse on every click - and with the 40% shorter notch asked for
    # in the same breath it got *worse*, 184 -> 969 (5.3x), because a
    # smaller impulse on the same steep decay is a smaller kick followed
    # by the same brake. At 7 it is 413 -> 606 (1.5x); at 5 it is 1.3x
    # but a lone notch then takes 553ms to settle and a flick coasts a
    # full second, which is floaty rather than smooth. 7: lone notch
    # settles in 440ms, peak 666 px/s for 75px, a flick coasts ~0.7s.
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
    # **24, down from 34** - the owner, 25 August 2026, asking for the
    # glide to carry further after the stop. Total travel is unchanged
    # by this: `kick` scales the impulse by FRICTION (impulse = distance
    # x FRICTION), so a notch still comes to rest exactly `distance_px`
    # away and only the time it takes to get there grows. Lower friction
    # is a longer, flatter tail, not a longer scroll.
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
    # **5200, and the excess is kept, not thrown away.** The first cut
    # clamped the velocity and discarded what would not fit, so an
    # 8-notch flick travelled *less* per notch than slow scrolling
    # (measured 826px against 1025) - the opposite of what a flick is.
    # Whatever exceeds the ceiling now waits in `_pending` and feeds in
    # as the speed decays, so a flick travels its full distance at a
    # bounded speed. At 5200 px/s a tick two frames late moves ~72px.
    # (4500 and the discard were measured on the way here: a 104px
    # single-frame jump at 7000, then the short flick at 4500.)
    # **3200, down from 5200** - the owner, 25 August 2026, and it is a
    # cap on how far the view moves *between two frames*, never on how
    # far a flick travels. Measured on Series by instrumenting the
    # scroll body's own paints, 240Hz panel:
    #
    #     flick                 cap 5200            cap 3200
    #     8 notches @ 30ms      2459 px/s,  9px     2337 px/s,  9px
    #     15 notches @ 10ms     5457 px/s, 22px     3558 px/s, 14px
    #     25 notches @ 6ms      5655 px/s, 22px     3900 px/s, 14px
    #     travel, all six runs  510-721px           512-721px
    #
    # An ordinary scroll never reaches either cap - the two are
    # indistinguishable at 30ms spacing. A hard flick did, and 22px of
    # ground between two frames is what reads as a smear; the distance
    # covered is identical.
    MAX_SPEED = math.inf
    # superseded - see above; kept so the next reader knows it was tried
    _OLD_MAX_SPEED_NOTE = "7000 then 4500"
    # Below this the remaining distance (speed/FRICTION) is ~2px: snap
    # it and stop, rather than ticking sub-pixel amounts into round().
    # Below this the remaining distance (speed/FRICTION) is ~2px: snap
    # it and stop, rather than ticking sub-pixel amounts into round().
    #
    # **A "one pixel per refresh" floor was tried here on 24 August 2026
    # and taken out again.** The reasoning read well - a frame that
    # cannot move a whole pixel shows nothing - and it is wrong twice
    # over: the position is a float that *accumulates*, so two 0.6px
    # frames are a real 1.2px move rather than two wasted ones, and at
    # 240Hz the floor works out at 240px/s, which is a perfectly normal
    # gentle scroll speed to refuse to render. Measured at friction 16 it
    # made things far worse (65.6% dead refreshes) by stopping and
    # restarting the motion continuously. Left at 30.
    STOP_SPEED = 30.0
    # An impulse is not applied in one tick: it is handed over at this
    # rate (1/s), so ~63% of a notch's speed has arrived after 14ms and
    # ~90% after 33ms. Measured without it: velocity 313 px/s 25ms before
    # a mid-cadence notch and 2001 px/s 25ms after - the same 6x step the
    # old curve had, just with a tail. Ramping the impulse in is what
    # makes a notch a push rather than a kick.
    # **40, down from 70**, alongside FRICTION 7: the impulse now arrives
    # over ~60ms instead of ~30, so the start of a notch is as soft as
    # its end. Measured against 70 at friction 7: peak velocity of a
    # lone notch 511 -> 666 px/s but no pulse difference at the mid
    # cadence (1.5x both), and no change to travel or settle.
    RAMP = 60.0
    # How much of a notch's impulse becomes velocity on the spot rather
    # than arriving through RAMP. Enough that the first frame after the
    # wheel already moves a visible pixel or two; small enough that the
    # start still reads as a push. See kick for the measurement.
    IMMEDIATE_SHARE = 0.34
    # Cadence acceleration: notches inside this window count, and the
    # impulse scales from 1x for a lone notch to ACCEL_MAX once
    # ACCEL_NOTCHES have landed in the window. Modest on purpose - the
    # owner asked for smooth, not for a page that runs away.
    ACCEL_WINDOW_S = 0.25
    ACCEL_NOTCHES = 5
    ACCEL_MAX = 1.0
    # The longest step the integrator will take. A tick that is later
    # than two frames is integrated as two frames: the position lands a
    # little short rather than leaping - smoothness over accuracy, the
    # whole point of the model.
    MAX_DT = 2.0 / 144.0

    def __init__(self, bar, tick_ms, parent=None, friction=None,
                 accel_max=None, frame_s=None, ramp=None, max_speed=None):
        """`friction`, `accel_max`, `ramp` and `max_speed` override the
        class defaults for one surface. The reader passes all four so its
        strip **stops when the wheel stops** - the owner's ask, 24 August
        2026: "while in reader mode remove the scrolling movement after
        the mouse scroll stops ... ONLY IN READER MODE", and then, when
        that was not enough, "remove the mouse drift ENTIRELY from the
        reader view!!!". Reading is aiming at a panel, not travelling,
        and a coast overshoots what you were looking at.

        `max_speed=math.inf` turns the ceiling off, and for a
        drift-free surface that is not optional: the cap does not
        discard the excess, it parks it in `_pending` and feeds it in as
        the speed decays - so the distance a flick could not deliver
        while the hand was moving is delivered *after* it stopped. That
        deferred distance is the drift. See windows.reader for the
        numbers the reader's four values were chosen against."""
        super().__init__(parent)
        self._bar = bar
        self._tick_ms = tick_ms
        self._frame_s = frame_s
        if friction is not None:
            self.FRICTION = float(friction)
        if accel_max is not None:
            self.ACCEL_MAX = float(accel_max)
        if ramp is not None:
            self.RAMP = float(ramp)
        if max_speed is not None:
            self.MAX_SPEED = float(max_speed)
        self._pos = None
        # Where a scrollbar drag wants the view, while the frame clock
        # closes the gap - see follow(). None when no drag is settling.
        self._follow = None
        # The last position the pointer asked for, when it asked, and how
        # long the ask before it took to arrive - kept apart from
        # `_follow` because `_follow_step` clears that one on arrival.
        self._follow_aim = None
        self._follow_at = None
        self._follow_pace_s = 0.0
        self._vel = 0.0
        self._pending = 0.0          # impulse not yet handed to the velocity
        self._last = 0.0
        # Whether this instance is currently connected to the shared
        # vblank ticker (see _start_ticking) - the QTimer below is only
        # the fallback clock for machines with no vblank to wait on.
        self._vblank_on = False
        self._last_value = None
        # Where the refresh grid is anchored - see _tick. Re-anchored
        # whenever motion restarts.
        self._phase = None
        self._kicks = collections.deque()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        # **A vblank-driven clock was built for this and measured
        # worse - do not build it again.** The reasoning was sound: a
        # QTimer takes whole milliseconds, so on a 144Hz panel it runs at
        # 6ms against a 6.94ms refresh and the two beat, which is what
        # poster_grid's docstring records as 43% of refreshes showing no
        # movement. A thread blocking on IDXGIOutput::WaitForVBlank and
        # posting a queued tick fixes the beat and creates a worse
        # problem: the ticks arrive in bursts whenever the UI thread was
        # busy, so the movement *between two paints* varies far more
        # than the timer's does. Measured 24 August 2026 on Home, two
        # runs each - judder 12.3/15.2% on the timer against **62.2 and
        # 64.1%** on the vblank clock, with 73-77% of frames more than
        # 30% off the frame before.
        #
        # The pacing can only be fixed where the position is computed
        # *inside* the frame that draws it, which needs the surface to
        # own its own painting - see helpers/poster_grid, which does
        # exactly that and measures 2-6% judder. For a page built out of
        # widgets there is no such point, and the timer is the best of
        # the bad options.

    def active(self) -> bool:
        return self._vblank_on or self._timer.isActive()

    def kick(self, distance_px, direction):
        """One notch: `direction` is +1 to scroll forward (value up), -1
        back. `distance_px` is the notch's resting travel."""
        now = time.monotonic()
        while self._kicks and now - self._kicks[0] > self.ACCEL_WINDOW_S:
            self._kicks.popleft()
        accel = 1.0 + (self.ACCEL_MAX - 1.0) * min(
            1.0, len(self._kicks) / float(self.ACCEL_NOTCHES))
        self._kicks.append(now)
        if not self.active() or self._pos is None:
            self._pos = float(self._bar.value())
            self._vel, self._pending = 0.0, 0.0
            self._last = now
        if self._vel * direction < 0 or self._pending * direction < 0:
            # A reversal answers now, not after a decay.
            self._vel, self._pending = 0.0, 0.0
        impulse = direction * float(distance_px) * self.FRICTION * accel
        # **A share of the notch is handed over at once.** The rest
        # arrives through RAMP as before, which is what keeps a notch a
        # push rather than a kick - but ramping from *zero* meant the
        # position did not move a whole pixel for the first fortieth of
        # a second, and a stall followed by a run is exactly what reads
        # as "it jumps to the next position".
        #
        # Measured on Home, 26 August 2026, one notch: 21 changes over
        # 144ms in a clean ease-out - and the first of them at **43.7ms**.
        # The travel was never the problem and is not changed here; the
        # same impulse is simply not all deferred.
        self._vel += impulse * self.IMMEDIATE_SHARE
        self._pending += impulse * (1.0 - self.IMMEDIATE_SHARE)
        self._start_ticking()
        # **And move on this frame, not on the clock's first tick.**
        # That tick is where the notch's delay actually lived: measured
        # on Home, 26 August 2026, the ticks after a kick run at a clean
        # 4ms cadence on a 240Hz panel and the interpolation between
        # them is a textbook ease-out - but the *first* one arrived
        # **39.1ms** after the wheel event, while the shared ticker woke
        # up. Nothing moved for two and a half frames and then the view
        # ran, which is precisely what reads as "it jumps to the next
        # position" rather than travelling there.
        #
        # So one frame's worth is integrated here, synchronously, by
        # dating `_last` a frame into the past and letting the ordinary
        # tick do the arithmetic - no second code path for the first
        # step, and the ticker carries on from wherever this leaves it.
        frame = 0.0
        if self._frame_s is not None:
            frame = self._frame_s() or 0.0
        if frame <= 0.0:
            frame = max(0.004, self._tick_ms / 1000.0)
        self._last = time.monotonic() - frame
        try:
            self._tick()
        except RuntimeError:
            pass        # the bar went away between the wheel and here

    def _start_ticking(self):
        """Clock the motion: the panel's own vblank where there is one,
        the fallback QTimer where there is not.

        **Turning the ticker off was tried on 25 August 2026 and
        measured worse - do not try it again without re-reading this.**
        The proposal was reasonable and the note further up this class
        supported it, recording an *earlier, ungated* vblank clock that
        measured 62% judder. The gated one in this module is not that
        clock. Measured A/B the same day, everything else held fixed,
        two rounds of each on Home and Series, judder taken only over
        frames that actually moved (a glide's 1px tail is 100% relative
        variation and says nothing about what the eye sees):

            PreciseTimer     home 23.9 / 20.2%    series 32.0 / 33.0%
            vblank ticker    home 12.9 / 12.1%    series 15.0 / 10.0%

        Roughly half the judder, on every run. The ticker stays."""
        global _GLIDING
        if not getattr(self, "_counted", False):
            self._counted = True
            _GLIDING += 1
        ticker = _vblank_ticker_for_use()
        if ticker is not None:
            if not self._vblank_on:
                ticker.tick.connect(self._tick)
                ticker.acquire()
                self._vblank_on = True
            return
        self._timer.setInterval(self._tick_ms())
        if not self._timer.isActive():
            self._timer.start()

    def _stop_ticking(self):
        global _GLIDING
        if getattr(self, "_counted", False):
            self._counted = False
            _GLIDING = max(0, _GLIDING - 1)
        if self._vblank_on:
            # **Disconnect first, flag second.** The other order leaks:
            # if the disconnect raises, the flag is already False and
            # the next kick connects a *second* time, so _tick runs
            # twice per refresh and the position advances double - and
            # it compounds for the life of the page.
            ticker = _vblank_ticker_for_use()
            try:
                if ticker is not None:
                    ticker.tick.disconnect(self._tick)
                    ticker.release()
            except Exception:
                logs.exception("could not release the vblank ticker")
            self._vblank_on = False
        self._timer.stop()

    def _set_value(self, value: int):
        """Write the bar only when the rounded pixel actually changed - a
        redundant setValue repaints the whole scroll body for no visible
        movement."""
        if value != self._last_value:
            self._last_value = value
            self._bar.setValue(value)

    def _tick(self):
        if self._pos is None:
            self._stop_ticking()
            return
        now = time.monotonic()
        # **Snap to the refresh grid**, and this has now survived being
        # taken out twice, the second time against a much better
        # instrument than the first.
        #
        # The timer runs at screen_tick_ms, a whole number of
        # milliseconds, so it beats against the panel's refresh: without
        # anchoring, the app produced 137 positions a second while the
        # compositor presented 109. Anchoring to a fixed phase makes two
        # ticks inside one refresh carry the same timestamp and ticks in
        # consecutive refreshes exactly one frame apart. `now` is also
        # floored at `_last`, which is what stops it walking backwards.
        #
        # **Measured 25 August 2026 by instrumenting the scroll body's
        # own paints and the pixels moved between them** - the right
        # measurement, and not the one used the first time round. As
        # shipped, 8 notches on Home and Series:
        #
        #     body paints 214/s and 198/s, median gap 4.2ms on a 240Hz
        #     panel - one paint per refresh
        #     0% and 2% of paints moved 0px - no duplicate frames
        #     deltas a clean ramp: 2 2 3 3 2 4 5 5 5 6 6 5 5 6 7 7 7 ...
        #
        # With the snapping removed and the model on raw elapsed time,
        # the same run on Series gave
        #
        #     2 2 2 -1 -1 -3 -2 1 2 2 2 3 3 3 1 1 2 3 3 3 -1 -2 1 2 -1 ...
        #
        # - the view moving *backwards* between frames during a downward
        # scroll. The "duplicate frame" this was meant to cure does not
        # exist on this surface; the earlier 41-61% figure counted every
        # widget in the app rather than the scroll body.
        frame_s = self._frame_s() if self._frame_s is not None else 0.0
        if frame_s > 0.0:
            if self._phase is None:
                self._phase = now
            now = max(self._phase + round((now - self._phase) / frame_s) * frame_s,
                      self._last)
        # Real elapsed time, capped: a tick that arrives late lands where
        # it should, and one that arrives after a stall does not teleport.
        # The cap comes from the real interval when it is known - a
        # hardcoded 2/144 is 13.89ms, shorter than one 60Hz frame, and
        # would fire on every ordinary tick on a 60Hz panel.
        max_dt = 2.0 * frame_s if frame_s > 0.0 else self.MAX_DT
        dt = min(max_dt, max(0.0, now - self._last))
        self._last = now
        if self._follow is not None:
            # A drag owns the view while it lasts; the momentum model
            # below is what a *wheel* does and the two must not both be
            # writing the same value.
            self._follow_step(dt)
            return
        if self._pending:
            handed = self._pending * (1.0 - math.exp(-self.RAMP * dt))
            self._pending -= handed
            self._vel += handed
            if abs(self._pending) < 1.0:
                self._vel += self._pending
                self._pending = 0.0
        capped = max(-self.MAX_SPEED, min(self.MAX_SPEED, self._vel))
        if capped != self._vel:
            self._pending += self._vel - capped     # kept, fed in later
            self._vel = capped
        self._pos += self._vel * dt
        self._vel *= math.exp(-self.FRICTION * dt)
        low, high = float(self._bar.minimum()), float(self._bar.maximum())
        if self._pos <= low:
            self._pos, self._vel = low, 0.0
        elif self._pos >= high:
            self._pos, self._vel = high, 0.0
        if abs(self._vel) < self.STOP_SPEED and not self._pending:
            # Snap the last couple of pixels and stop, so the tail is
            # never a string of ticks that round to nothing.
            self._pos = max(low, min(high, self._pos + self._vel / self.FRICTION))
            self._set_value(int(round(self._pos)))
            self._stop_ticking()
            self._vel, self._pos = 0.0, None
            self._phase = None
            return
        self._set_value(int(round(self._pos)))

    # **How fast the view catches up with a dragged scrollbar**, and why
    # a drag needs catching up with at all: an ordinary mouse reports 125
    # positions a second and this panel refreshes 240 times, so writing
    # the pointer's value straight through leaves half the refreshes
    # showing the frame before and moves the content in double-size
    # steps. Measured on the painted grid, which got this first
    # (poster_grid.DRAG_FOLLOW_TAU_S): 125.0 moving fps and 47.9% dead
    # refreshes before, 236.4 and 1.5% after, at the same travel. The
    # owner's words for the before, 25 August 2026: "while dragging the
    # scrollbar in low-mid speed it shows that all items on the screen
    # are moving in steps".
    #
    # The same 12ms constant as the grid's, so the two surfaces feel
    # alike: about 8ms of lag, under one mouse sample.
    #
    # **And superseded with it, 27 August 2026, for the same measured
    # reason** - see poster_grid.DRAG_PACE_MIN_S, which carries the
    # numbers off the owner's screen recording. In short: a mouse
    # reports whole pixels, and one of them is tens of pixels of content
    # on a long page, so the target the follow is handed is a staircase
    # and no time constant applied to a staircase is smooth. A/B'd
    # through the painted grid's own drag path at his ratio (40.5px of
    # content per pointer pixel, 9 pointer px/s), px moved per frame at
    # 120Hz:
    #
    #     12ms constant   20.3 10.1 5.1 2.5 1.3 0.6 0.3 0.3 0 0 0 0 0
    #                     56% of frames moved under half a pixel
    #     paced            3.1  3.1 3.1 3.1 3.1 3.1 3.1 3.1 3.1 3.1 ...
    #                      5% of frames moved under half a pixel
    #
    # Same travel to the pixel (1053 against 1050 over three seconds);
    # this changes when the distance is delivered, never how much.
    FOLLOW_TAU_S = 0.012        # kept: the value that was measured wrong
    FOLLOW_SETTLE_PX = 0.4
    # Bounds on the estimated interval between pointer steps - a mouse
    # sample at the bottom, and at the top the point past which a step is
    # already smooth on its own. poster_grid.DRAG_PACE_MIN_S is the pair
    # of these, deliberately the same numbers so the two surfaces match.
    FOLLOW_PACE_MIN_S = 0.008
    FOLLOW_PACE_MAX_S = 0.50
    # **Aim to arrive a little after the next step, not exactly on it.**
    # Pacing a step over the interval the last one took means finishing
    # early whenever the hand slows even slightly - and then standing
    # still until it crosses the next pixel, which is the freeze the
    # owner filmed third (cc.mp4: 26% of frames showing nothing new,
    # runs of three and four). With the estimate led by this factor the
    # follow is always still moving when the next step lands, so the
    # motion never stops; the standing lag it costs is (LEAD - 1) of one
    # pointer pixel, which at 1.25 is a quarter of one.
    FOLLOW_PACE_LEAD = 1.25

    def follow(self, target):
        """Track a dragged scrollbar thumb: write the value, now.

        **This used to pace the step, and pacing is what froze Home and
        Discover.** The idea was sound where it came from - a mouse
        reports 125 positions a second against a 240Hz panel, so on the
        painted grids one pointer step is forty pixels of content and
        landing it in one frame reads as a jump. Spreading it over the
        estimated interval to the next step fixed that *there*, and
        poster_grid still does it, untouched.

        On a QScrollArea page it is the opposite of a fix. The value is
        an integer, and Home's range is 441 against ~380px of thumb
        travel - about 1.19 content pixels per pointer pixel - so a step
        *is* a pixel. Spreading one pixel over the 90-450ms the estimate
        had grown to advances `_pos` by thousandths, `int(round())`
        never changes, and the view sits still until the arithmetic
        finally crosses 1. The owner reported it for days as "the whole
        app freezing while scrolling", and the tell he eventually gave -
        that the scrollbar handle flickers between teal and the dark
        colour while he holds it - is the same fact seen from outside:
        theme.py tints a handle teal on :hover only, so a flickering
        handle is a handle that is not staying under the pointer.
        
        Measured on his own Home page, dragging the real thumb, before
        and after (a plateau is a stretch where the value did not change
        at all):

            paced      value frozen 46% of the drag, longest 136ms
            direct     value frozen  1%,              longest   8ms

        and the paced path cost about 7ms of work per tick on top, which
        is why the drag felt heavy as well as stuck.

        `_Momentum` still owns the wheel, its momentum and its friction;
        this is only the thumb, which is the one gesture where the user
        is already telling the view exactly where to be. Nothing needs
        smoothing between a hand and the pixel it is pointing at."""
        low, high = float(self._bar.minimum()), float(self._bar.maximum())
        # A hand on the thumb is the only thing moving the view.
        self._vel = self._pending = 0.0
        self._kicks.clear()
        self._stop_ticking()
        self._pos = None
        self._follow = self._follow_aim = None
        self._follow_at = None
        self._follow_pace_s = 0.0
        self._set_value(int(round(max(low, min(high, float(target))))))

    def finish_follow(self):
        """The hand let go. Nothing to land: `follow` writes the value on
        the sample it is given, so the view is already where the thumb
        is. Kept because ScrollBarDrag calls it on release."""
        self._follow_at = None
        self._follow_pace_s = 0.0

    def following(self) -> bool:
        return self._follow is not None

    def _follow_step(self, dt) -> bool:
        """One frame of the drag follow. True while it is still moving."""
        gap = self._follow - self._pos
        if abs(gap) <= self.FOLLOW_SETTLE_PX:
            self._pos = self._follow
            self._bar.setValue(int(round(self._pos)))
            self._follow = None
            self._stop_ticking()
            self._pos = None
            return False
        # Spread this pointer step over the time the next one is due in,
        # re-asked every frame off the time actually left, so the frames
        # all move the same distance - see FOLLOW_TAU_S for the A/B.
        left = 0.0
        if self._follow_at is not None:
            # **Never a pace shorter than two frames.** Below that there
            # is no spreading left to do - the whole step lands in the
            # next frame - and a fast hand on a short page hits it
            # constantly. Measured on Discover (2.5px per pointer pixel,
            # 81 px/s): with an 8ms floor, 36% of frames moved nothing
            # and the rest moved up to 6px; with this floor, 4% and 2px.
            pace = max(self._follow_pace_s, 2.0 * dt)
            left = pace - (time.monotonic() - self._follow_at)
        self._pos += gap * min(1.0, dt / max(dt, left, 1e-6))
        self._bar.setValue(int(round(self._pos)))
        return True

    def shift(self, delta):
        """Move the glide's own position by `delta`, because something
        else just moved the bar under it.

        **Without this the two fight, and the frame rate is what pays.**
        The reader compensates the scrollbar when a page above the
        viewport settles into its real height (reader._resize_slot);
        this model integrates a *float* position and writes
        int(round(pos)) every refresh, so after such a correction its
        next write lands back where the bar already was - no value
        change, no repaint, a dead frame. Measured on the owner's
        240Hz panel, a 25-notch scroll on a manhwa chapter:

            downward   528 value changes, 4.41ms mean gap,  3.5% late
            upward     314 value changes, 9.57ms mean gap, 79.7% late

        - and upward is exactly the direction in which pages above the
        viewport are being loaded and resized. Scrolling *down* resizes
        slots below the viewport, which needs no correction at all.
        """
        if self._pos is not None:
            self._pos += float(delta)

    def cancel(self):
        self._stop_ticking()
        self._vel, self._pos, self._pending = 0.0, None, 0.0
        self._follow = self._follow_aim = self._follow_at = None
        self._follow_pace_s = 0.0
        self._phase = None
        self._kicks.clear()


class ScrollBarDrag(QObject):
    """Take over dragging a vertical scrollbar's thumb.

    Split out of _SmoothWheel on 27 August 2026 so the surfaces that do
    **not** go through `scroll_area()` can have it as well - the reader's
    strip above all, which was still on Qt's raw drag and therefore had
    the whole staircase this was written to remove. `smooth_bar_drag`
    is the way to attach one.

    `motion` is the _Momentum that owns this bar's position. A surface
    that already has one passes it, so the two are never both writing;
    anything else gets one of its own, used for nothing but this.
    """

    _DRAG_EVENTS = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.MouseButtonDblClick)

    def __init__(self, area, motion):
        super().__init__(area)
        self._area = area
        self._motion = motion
        # Set before the filter goes on, not after: an event arriving in
        # between reaches eventFilter with the attribute missing, and an
        # exception raised inside a Qt event filter takes the process
        # down rather than propagating.
        self._drag_from = None
        # Held by identity rather than asked of the area on every event.
        # **That question was fatal**: `area.verticalScrollBar()` reaches
        # into C++, and these pages rebuild constantly - once the area is
        # deleted the call raises RuntimeError, inside an event filter,
        # which killed the whole app while building a page.
        self._bar_widget = area.verticalScrollBar()
        self._bar_widget.installEventFilter(self)

    def _cancel(self):
        self._motion.cancel()

    def _thumb_rect(self, bar):
        """The slider handle's rectangle, asked of the style rather than
        computed: a scrollbar's groove is inset by the arrow buttons on
        some styles and not on others, and guessing that is how a drag
        ends up a few pixels out from the first frame."""
        option = QStyleOptionSlider()
        option.initFrom(bar)
        option.orientation = bar.orientation()
        option.minimum, option.maximum = bar.minimum(), bar.maximum()
        option.sliderPosition = option.sliderValue = bar.value()
        option.pageStep, option.singleStep = bar.pageStep(), bar.singleStep()
        return bar.style().subControlRect(
            QStyle.ComplexControl.CC_ScrollBar, option,
            QStyle.SubControl.SC_ScrollBarSlider, bar)

    def _groove_span(self, bar, thumb):
        """How far the handle may travel, in pixels."""
        option = QStyleOptionSlider()
        option.initFrom(bar)
        option.orientation = bar.orientation()
        groove = bar.style().subControlRect(
            QStyle.ComplexControl.CC_ScrollBar, option,
            QStyle.SubControl.SC_ScrollBarGroove, bar)
        return max(1, groove.height() - thumb.height()), groove

    def _bar_drag(self, bar, event):
        """Stop any wheel glide when a hand lands on the bar, and let Qt
        drag the thumb itself.

        **This used to take the drag over completely, and that is no
        longer worth anything.** The takeover existed so the pointer set
        a *target* and `_Momentum.follow` closed the gap on the frame
        clock, instead of Qt writing the value on all 125 mouse samples a
        second. Now that `follow` writes the value on the sample it is
        given - see the note there, and the measurement that forced it -
        the takeover computes precisely what Qt's own slider drag
        computes, from the same numbers.

        What it still cost was the *state*. Consuming the press means Qt
        never enters slider-drag mode: `bar.isSliderDown()` measured
        False for 100% of a drag, with no mouse grabber, so nothing kept
        the handle under the pointer. theme.py tints a handle teal on
        `:hover` only, so the handle kept dropping out of teal and back
        as the pointer crossed off it - the owner's "the teal color
        stutters while I am holding and dragging", 28 August 2026. A
        native drag cannot do that: Qt grabs the mouse and the handle
        follows the pointer exactly.

        The press is still watched, for the one thing Qt does not do:
        a wheel glide still running has to stop, or it and the hand
        would both be writing the value."""
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._cancel()
        return False

    def eventFilter(self, obj, event):
        if event.type() in self._DRAG_EVENTS and obj is self._bar_widget:
            # Never let anything out of here: a filter that raises kills
            # the process (see __init__).
            try:
                if self._bar_drag(obj, event):
                    return True
            except Exception:
                logs.exception("scrollbar drag failed")
        return False


def smooth_scrolling(area, notch_scale: float = 1.0):
    """Give any scrolling widget the app's wheel *and* its paced thumb
    drag - what `scroll_area()` builds in, for the surfaces built some
    other way: the lists in Settings and the global search, the reader's
    chapter panel, a combo's popup.

    Not for a surface that already has its own motion model: the reader's
    strip has one, and two writing one scrollbar fight every frame. That
    one takes `smooth_bar_drag` instead.
    """
    area.notch_scale = float(notch_scale or 1.0)
    return _SmoothWheel(area)


def smooth_combo(box):
    """Give a drop-down's popup the app's scrolling.

    A combo popup is a scrolling list like any other and some of them are
    long - the reader's chapter jump list runs to hundreds of rows - so
    without this they keep Qt's raw thumb drag, which moves the whole
    step in one frame and shows nothing in the frames between.

    `box.view()` exists from construction and QComboBox keeps it unless
    the model or the delegate is replaced, so this attaches once here
    rather than on every open. Flagged on the view because PickCombo
    calls it again from showPopup, where a replaced view would need it.
    """
    view = box.view()
    if view is not None and not getattr(view, "_atomic_smooth", False):
        view._atomic_smooth = True
        smooth_scrolling(view)
    return box


def smooth_bar_drag(area, motion=None):
    """Give any scrolling widget's vertical scrollbar the paced drag.

    For `scroll_area()` pages this comes with _SmoothWheel; this is for
    everything else - the reader's strip, the plain lists in Settings and
    the search, the chapter jump list. Returns the ScrollBarDrag, which
    is parented to `area` and needs no keeping.

    A surface with its own _Momentum must pass it: two models writing one
    scrollbar fight every frame.
    """
    if motion is None:
        motion = _Momentum(area.verticalScrollBar(),
                           lambda: screen_tick_ms(area), area,
                           frame_s=lambda: screen_frame_s(area))
    return ScrollBarDrag(area, motion)


class _SmoothWheel(QObject):
    """Animated wheel scrolling for one QScrollArea.

    A wheel notch used to land as an instant jump, which is most of why
    the pages read as stiff (the owner's word): everything else in the
    app eases, and the single most-repeated interaction snapped. This
    glides the same distance over a short curve instead.

    Retargeting, not restarting: a second notch mid-glide moves the
    destination and the animation re-aims from wherever the view
    currently is, so spinning the wheel feels like momentum rather than
    a queue of little animations.

    Deliberately narrow about what it takes over:
      * trackpad pixel-delta scrolling is already 1:1 with the finger
        and is left alone;
      * Ctrl/Shift chords belong to zoom and horizontal scrolling;
      * a notch past the end of the range is *not* consumed, so Qt still
        propagates it to whatever scrolls behind this area.
    The reader's strip keeps its own physics (windows.reader.WHEEL_STEP_PX
    and a hand-tuned feel) - it does not go through scroll_area()."""

    # **Driven at the screen's refresh rate, not Qt's animation clock.**
    # This is what the owner's "it works, but I need it smoother" was.
    # Qt's unified animation timer ticks every 16ms - about 60 steps a
    # second - and both of this machine's monitors run at **144Hz**
    # (measured). So the glide produced a new position only every 2.4
    # display refreshes: the panel was showing each scroll position two
    # or three times over, which reads as stepping however well the
    # curve is chosen. Nothing was dropping frames; there simply were
    # not enough positions to show.
    #
    # A plain QTimer at the screen's own interval fixes that, and gives
    # two things QVariantAnimation could not: the position is computed
    # from real elapsed time (so a late tick lands where it should
    # rather than behind), and it is carried as a float, so a slow
    # stretch of a long glide still advances instead of rounding to the
    # same integer pixel twice in a row.
    #
    # Measured, one notch, positions produced per second:
    #     before  60/s      after  ~144/s
    #
    # 220ms rather than 160: with 2.4x the steps there is room for a
    # longer tail, and a longer tail is most of what reads as "glide"
    # rather than "jump". Retargeting keeps it responsive - a second
    # notch re-aims from where the view is now, so spinning the wheel
    # never queues 220ms of backlog.
    DURATION_MS = 220
    # Distance per notch, as a fraction of the viewport, and the floor
    # under it for a short one.
    #
    # **0.154 and 42px, down 30% from 0.22 and 60px** - the owner's ask,
    # 23 August 2026: "make the scrolling in all app slower by ~30%, BUT
    # do not change the scrolling speed in the reading mode". Both numbers
    # move together or the slowdown simply does not happen on a short
    # viewport: the floor binds below 60/0.22 = 273px, so a dialog or a
    # small panel would have kept the old stride entirely.
    #
    # The floor's original premise was "nothing scrolls slower than Qt's
    # own wheelScrollLines(3) x singleStep(20) = 60px", and that premise
    # is exactly what this ask overrides.
    #
    # DURATION_MS is deliberately *not* touched: it is how long a notch
    # takes to settle, not how far it goes. Stretching it would make each
    # notch feel laggy instead of shorter - the same distance/duration
    # split reader.py:340-361 records across six tunings of its own step.
    #
    # Reading mode is untouched by construction: the reader's strip has
    # its own physics (windows.reader.WHEEL_STEP_PX) and does not go
    # through scroll_area(), so nothing here can reach it.
    #
    # **0.0924 and 25px, down 40% from 0.154 and 42px** - the owner, 24
    # August 2026: "make scrolling speed 40% slower in the whole app".
    # Measured on Watch > Anime, ten slow notches: 1267px -> 760px, i.e.
    # 127 -> 76 per notch. The motion model's FRICTION/RAMP were retuned
    # in the same pass (see _Momentum) because a shorter notch on the
    # old decay measured *stiffer*, not just slower.
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
    # **58, up a further 30% from 45 - the owner, 27 August 2026:
    # "increase it by 30% more", the second raise in a row after three cuts
    # earlier the same day. Applied to the constant, which is what his
    # correction established this number means.
    #
    # **45 was up 50% from 30: "increase the scrolling speed by 50%".
    #
    # **30 was his own number that day: "no no 30 instead of 24",
    # correcting a first reading of "make it faster by 50%" as +50% on the
    # travel (which gave 36) - the constant itself, not a percentage.
    #
    # Fourth change to this one number in a day, and the asks below pull
    # both ways; they are kept so the next person sees the whole swing
    # rather than the last step of it.**
    #
    # **24 was a further 37% off 38, from "make it even smaller (mouse
    # scroller steps per tick)".**
    #
    # **38, half of 76 - the same day: "make each mouse
    # scroller tick travels 50% less".** The seventh tuning of this number
    # and the fifth in the same direction; the history above is kept because
    # two of those asks pulled the other way and the next person needs to
    # see that. Halved rather than re-derived from the viewport, because
    # NOTCH_FRACTION has been 0.0 since the notch became a flat pixel
    # distance - `max(floor, height * 0.0)` is the floor - so this constant
    # alone is the travel. scroll_area's `notch_scale` still multiplies it,
    # which keeps Home's 0.7 at 26px rather than flattening every surface to
    # one number.
    NOTCH_FLOOR_PX = 58
    # Never slower than this, whatever the screen claims. A refresh rate
    # of 0 is what a headless/offscreen platform reports.
    MAX_TICK_MS = 16

    def __init__(self, area: QScrollArea):
        super().__init__(area)
        self._area = area
        self._from = 0.0
        self._started_at = 0.0
        # The motion itself lives in _Momentum (see its docstring for the
        # measurement that retired the per-notch curve this used to run).
        self._motion = _Momentum(area.verticalScrollBar(), self._tick_ms, self,
                                 frame_s=lambda: screen_frame_s(self._area))
        # The thumb drag is its own object now, shared with the
        # surfaces that have no _SmoothWheel - see ScrollBarDrag.
        self._bar_drag_filter = ScrollBarDrag(area, self._motion)
        area.viewport().installEventFilter(self)

    def _tick_ms(self) -> int:
        return screen_tick_ms(self._area)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind != QEvent.Type.Wheel:
            return False
        if not event.pixelDelta().isNull():
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            return False
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return False
        bar = self._area.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False
        # `notch_scale` is a per-area multiplier - see scroll_area's
        # argument. Only Home passes one today (the owner asked for that
        # page alone to be slower still); everything else is 1.0 and
        # lands on exactly NOTCH_FRACTION.
        scale = float(getattr(self._area, "notch_scale", 1.0) or 1.0)
        notch = max(int(self.NOTCH_FLOOR_PX * scale),
                    int(self._area.viewport().height()
                        * self.NOTCH_FRACTION * scale))
        direction = -1 if steps > 0 else 1      # wheel up = value down
        at_end = ((direction < 0 and bar.value() <= bar.minimum())
                  or (direction > 0 and bar.value() >= bar.maximum()))
        if at_end and not self._motion.active():
            # Already hard against this end - hand the notch back to Qt
            # so a parent scroller (if any) can take it.
            return False
        self._motion.kick(abs(steps) * notch, direction)
        event.accept()
        return True


class _EdgeWheelRelay(QObject):
    """A wheel notch over a page's dead margins scrolls the page.

    The gap this closes: a page's scroll area does not reach the window
    edge, so the strip to the *right of the scrollbar* (and the header
    band above the content) belongs to the page widget, which scrolls
    nothing. A notch there did nothing at all - the owner's report, with
    the pointer parked at the far right of the window.

    Installed once on the application, not per page, because "all pages"
    is the ask and every page grows its own scroll areas. It is
    deliberately timid about what it takes over:

      * a pointer genuinely inside something that can scroll vertically
        is left completely alone - that widget's own handling wins;
      * controls that answer the wheel themselves (combo boxes, spin
        boxes, sliders) keep it, or picking a season would scroll the
        page instead of changing seasons;
      * a page with nothing scrollable under the pointer's own window
        gets nothing - the notch falls through as before.

    The relayed event is sent to the target's viewport, which is what
    _SmoothWheel filters, so a relayed notch glides exactly like a
    direct one rather than jumping."""

    def __init__(self, app):
        super().__init__(app)
        self._relaying = False

    @staticmethod
    def _scrolls_vertically(area) -> bool:
        # **A surface that answers this at all answers it here.** The
        # flag is three-valued on purpose: absent means an ordinary
        # QAbstractScrollArea, True means a surface that scrolls itself
        # (helpers.poster_grid) and False means one that has decided the
        # wheel must not move it.
        #
        # Only the True case used to be read, so a PosterStrip - which
        # sets it False deliberately, because no horizontal row moves on
        # the wheel - fell through to `verticalScrollBar()`, and a
        # PosterGrid is a plain QWidget with no such method. Measured in
        # the owner's log, 27 August 2026: one wheel gesture over a
        # Discover row raised `AttributeError: 'PosterStrip' object has
        # no attribute 'verticalScrollBar'` **180 times in 50ms**, every
        # one of them unhandled, because the guard below catches only
        # RuntimeError.
        relayed = getattr(area, "accepts_relayed_wheel", None)
        try:
            if relayed is not None:
                return bool(relayed) and area.isVisible() and area.max_offset() > 0
            bar = area.verticalScrollBar()
        except (RuntimeError, AttributeError):
            # AttributeError as well: anything reached here that is
            # neither a scroll area nor a poster surface simply does not
            # scroll, and must not take the wheel handler down with it.
            return False
        return (bar is not None and area.isVisible()
                and bar.maximum() > bar.minimum())

    def _target_for(self, widget):
        """The scroll area a notch over `widget` should move: the
        nearest one among the widget's ancestors' children, searching
        outward. Outward rather than from the window down, so a notch in
        a dialog's margin scrolls that dialog's list and not the page
        behind it.

        **And it stops at that window**, 28 August 2026 - the owner:
        "while the mouse is in the settings window (positioned), do not
        able to scroll the app main window". It did not stop: a QDialog's
        `parentWidget()` is the main window, so the walk climbed straight
        out of the dialog and found the page's scroll area behind it. The
        wheel then moved the page under a *modal* dialog, which Qt's own
        modality would never have allowed - this filter sits on the
        application and posts the event to its target directly, so it
        goes around modality rather than through it. The old docstring's
        promise was right; the loop was one line short of keeping it."""
        from .poster_grid import PosterGrid
        top = widget.window()
        node = widget
        depth = 0
        while node is not None and depth < 12:
            for area in node.findChildren((QAbstractScrollArea, PosterGrid)):
                if self._scrolls_vertically(area):
                    return area
            if node is top:
                break           # never past the window the pointer is in
            node = node.parentWidget()
            depth += 1
        return None

    def eventFilter(self, obj, event):
        if self._relaying or event.type() != QEvent.Type.Wheel:
            return False
        if event.angleDelta().y() == 0:
            return False        # horizontal / trackpad pixel scrolling
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            return False        # zoom and the horizontal chord
        # widgetAt hit-tests the real widget tree, so it has no scale
        # factor in it (.claude/rules/ui.md on mapToGlobal).
        widget = QApplication.widgetAt(QCursor.pos())
        if widget is None:
            return False
        node = widget
        top = widget.window()
        while node is not None:
            if isinstance(node, (QComboBox, QAbstractSpinBox, QSlider)):
                return False    # these answer the wheel themselves
            if isinstance(node, QAbstractScrollArea):
                if self._scrolls_vertically(node):
                    return False        # it can scroll; leave it alone
                # A scroll area with nothing to scroll (a short list) is
                # not a reason to stop looking - the page behind it may
                # still have somewhere to go.
            if node is top:
                break           # same boundary _target_for keeps
            node = node.parentWidget()
        area = self._target_for(widget)
        if area is None:
            return False
        self._relaying = True
        try:
            target = (area if getattr(area, "accepts_relayed_wheel", False)
                      else area.viewport())
            QApplication.sendEvent(target, event)
        finally:
            self._relaying = False
        return True


class _StrayWindowGuard(QObject):
    """Keep an accidental top-level window off the screen, and say what
    it was.

    **The owner's report, 25 August 2026, with a screenshot:** *"when I
    press on discover a small window (white) appear then closes in a
    moment"* - a title bar with the app's icon and an empty grey body,
    over the details page while its list said "Loading...".

    A widget with no parent and no window flags of its own is a *child*
    that nobody parented; Qt turns it into a window the moment it is
    shown, with a title bar and everything. Nothing in this app ever
    wants that: every real window here sets flags (Window, Dialog,
    Popup, ToolTip) or is the main window itself. So the rule is exact -
    default flags plus no parent plus being shown - and anything
    matching it is a bug rather than a design.

    I could not reproduce it here (two passes watching every top-level
    widget during Discover and a details load, 3ms apart, nothing
    appeared), so this both prevents it and names the class in the log
    the next time it happens - which is the only way to fix the cause
    from a machine that is not this one.
    """

    def __init__(self, app):
        super().__init__(app)
        self._named = set()

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.Show:
            return False
        if not isinstance(obj, QWidget) or obj.parentWidget() is not None:
            return False
        # **Not by window flags.** By the time Show arrives Qt has
        # already promoted the accident to a Window, so the flags say
        # the same thing for both cases (measured: a parentless QLabel
        # shown reports Qt.Window, exactly as a QDialog does). What
        # separates them is what the object *is* and what it asked for:
        # every deliberate window here is a dialog, a menu, the main
        # window, or carries a flag that makes it a popup/tooltip/
        # frameless surface. A bare QLabel or QWidget is nobody's window.
        if isinstance(obj, (QDialog, QMenu, Toast)) or obj.inherits("QMainWindow"):
            return False
        flags = obj.windowFlags()
        # **The type is a masked field, not a bit.** Qt.Tool is
        # Popup|Dialog and Qt.ToolTip is Popup|Sheet, so `flags & Tool`
        # is true for a plain Window as well - measured: the parentless
        # label this exists to catch reports 0x8800f001, whose low
        # nibble is Window, and it matched Tool, Popup and Dialog alike.
        # Comparing the masked value is the only test that separates
        # them.
        kind = flags & Qt.WindowType.WindowType_Mask
        if kind in (Qt.WindowType.Dialog, Qt.WindowType.Popup,
                    Qt.WindowType.ToolTip, Qt.WindowType.Tool,
                    Qt.WindowType.Sheet, Qt.WindowType.Drawer,
                    Qt.WindowType.SplashScreen, Qt.WindowType.Desktop):
            return False
        # A hint, so `&` *is* right for this one: anything drawing its
        # own chrome (frameless_dialog) is deliberate.
        if flags & Qt.WindowType.FramelessWindowHint:
            return False
        name = f"{type(obj).__name__}#{obj.objectName() or '-'}"
        if name not in self._named:
            self._named.add(name)
            logs.info(f"stray window suppressed: {name} - a parentless "
                      f"widget was shown and Qt made it a window")
        obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        obj.hide()
        # **A widget that gains a parent a moment later was never a
        # stray, and suppressing it for good is worse than the flash
        # this guard exists to stop.** It cost the Apps page its
        # browse-for-an-exe button ("..."), which is built, shown, and
        # only then added to its row - the flash never reached the
        # screen, but WA_DontShowOnScreen stayed set and the button was
        # gone from then on. One turn of the event loop later is enough
        # to tell the two apart: a real stray still has no parent.
        QTimer.singleShot(0, lambda ref=weakref.ref(obj): self._reconsider(ref))
        return True

    def _reconsider(self, ref):
        obj = ref()
        if obj is None:
            return
        try:
            if obj.parentWidget() is None:
                return          # genuinely parentless: stay suppressed
            obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            obj.setVisible(True)
        except RuntimeError:
            pass                # deleted in the meantime


def install_stray_window_guard(app) -> QObject:
    """Stop a parentless widget from flashing up as a window - see
    _StrayWindowGuard. Call once, from main()."""
    guard = _StrayWindowGuard(app)
    app.installEventFilter(guard)
    return guard


class _HorizontalWheelGuard(QObject):
    """The wheel never moves a horizontal scrollbar; dragging it does.

    The owner's ask, 25 August 2026: "do not make the mouse scroll moves
    any horizontal scrollbar, make it only used by dragging the
    scrollbar in the whole app". Sideways rows are everywhere here -
    Discover's strips, Home's shelves, the reader's zoomed page - and a
    tilt wheel, or the sideways component a trackpad reports while the
    finger is going down, slid them under a pointer that was trying to
    scroll the page.

    **Filtered at the scrollbar, not at each viewport, because that is
    where Qt actually decides.** Measured on Qt 6.11, 25 August 2026,
    against a scroll area with both bars and one with only a horizontal
    bar:

        vertical wheel       -> V bar   (h-only area: moves nothing)
        horizontal wheel     -> H bar   dH +60
        shift + wheel        -> V bar   dV +284

    So QAbstractScrollArea forwards the wheel to the horizontal bar
    only on a genuine horizontal delta, and it forwards it as a real
    event to that bar - which means one filter here covers every scroll
    area in the app, including the ones not written yet, and needs no
    special case for Shift (it does not swap axes on this platform).

    Only Wheel is taken. Press, move and release reach the bar exactly
    as before, so the handle still drags."""

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.Wheel
                and isinstance(obj, QScrollBar)
                and obj.orientation() == Qt.Orientation.Horizontal):
            event.accept()
            return True
        return False


def _vertical_scroller_above(widget):
    """The nearest ancestor scroll area that can actually scroll
    vertically, or None. Ancestors only - a sibling is not behind the
    pointer."""
    node = widget.parentWidget() if widget is not None else None
    while node is not None:
        if isinstance(node, QAbstractScrollArea):
            try:
                bar = node.verticalScrollBar()
            except RuntimeError:
                return None
            if bar is not None and bar.maximum() > bar.minimum():
                return node
        node = node.parentWidget()
    return None


def install_horizontal_wheel_guard(app) -> QObject:
    """Stop the wheel from moving any horizontal scrollbar - see
    _HorizontalWheelGuard. Call once, from main()."""
    guard = _HorizontalWheelGuard(app)
    app.installEventFilter(guard)
    return guard


def install_edge_wheel(app) -> QObject:
    """Make the wheel work over page margins app-wide - see
    _EdgeWheelRelay. Returns the filter so the caller can keep it
    alive; call once, from main()."""
    relay = _EdgeWheelRelay(app)
    app.installEventFilter(relay)
    return relay


class _OpaqueGround(QObject):
    """Paints a scroll body's ground, so the body can tell Qt it is
    opaque - which is the whole of the scroll fix (see `ground` in
    scroll_area).

    An event filter rather than a stylesheet, and that is not a style
    preference. A declaration-only sheet set on one of these hosts
    cascades into every descendant and outranks the app stylesheet:
    details.py carries two comments recording the day that silently
    killed the row Cards' hover ring. An ID-scoped sheet would dodge the
    cascade but not the fact that these hosts are named `#Bare`, whose
    own rule is what currently makes them transparent.

    Painting first and returning False leaves the widget's own paint and
    all of its children exactly as they were - the only difference on
    screen is that this fills the pixels the page underneath used to
    fill, in the same colour."""

    def __init__(self, widget, colour):
        super().__init__(widget)
        self._colour = QColor(colour)
        widget.installEventFilter(self)
        self._claim(widget)

    @staticmethod
    def _claim(widget):
        """Set the opaque flag, and keep setting it.

        **Setting it once does not hold**, which cost a measurement that
        showed the fix doing nothing at all: the app stylesheet gives
        this body a background, so Qt polishes it as a styled widget and
        clears WA_OpaquePaintEvent again some time after construction.
        Read back live, the flag was False on both pages while the
        filter sat installed - and setting it *after* the page was built
        stuck. So it is re-asserted below rather than trusted."""
        if not widget.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent):
            widget.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self._claim(obj)
            painter = QPainter(obj)
            # event.rect(), never obj.rect(): during a scroll this is the
            # newly exposed strip, and filling the whole body instead
            # would hand back the cost this exists to remove.
            painter.fillRect(event.rect(), self._colour)
            painter.end()
        elif kind in (QEvent.Type.Polish, QEvent.Type.PolishRequest,
                      QEvent.Type.StyleChange):
            self._claim(obj)
        return False


def scroll_area(body: QWidget, always_show_vbar: bool = False,
                ground: str = None, notch_scale: float = 1.0) -> QScrollArea:
    """Wrap `body` in a frameless, resizable, mouse-wheel-scrollable area.

    `always_show_vbar` reserves the vertical scrollbar's width whether or
    not it's actually needed, instead of the default "only when content
    overflows" - the scrollbar only ever eats width from the right, so
    on a page whose content only sometimes overflows, its coming and
    going shifts anything centered against the viewport's width left/
    right depending on scroll state. Pages with fixed-width centered
    content (Home's hero) want that width reserved unconditionally so
    centering math stays consistent either way; pages that never center
    anything against the full viewport width don't need it.

    **`ground` is what makes scrolling smooth**, and it is the colour the
    page behind this body already paints - pass `theme.BG` on a page,
    leave it None anywhere the ground is not that (a translucent dialog,
    an overlay bar over video).

    The measurement, on the owner's machine over the owner's real data,
    one wheel-sized step at a time:

        Home    14.5ms per frame, 123 paints per frame  ->  3.5ms, 24
        Read    15.3ms per frame, 179 paints per frame  ->  3.7ms, 38

    at a 60Hz budget of 16.7ms - so *every* frame on Home was over
    budget, which is the stutter, and none is now. The cause is that
    theme.py makes a scroll body transparent (`QScrollArea > QWidget`),
    and a transparent body cannot be scrolled by blitting: Qt has to
    repaint the page underneath and then every widget over it, for every
    frame. Telling Qt the body is opaque restores the blit path, so a
    frame repaints the strip that actually came into view instead of the
    whole viewport. Nothing about the easing curve or the wheel handler
    was ever the problem.

    The body still has to *be* opaque for that promise to hold, or
    scrolling smears; _OpaqueGround is what keeps it true."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    if always_show_vbar:
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    area.setWidget(body)
    if ground:
        _OpaqueGround(body, ground)
    # How far one wheel notch travels here, as a multiple of the app's
    # own step - see _SmoothWheel._notch_px. 1.0 everywhere except Home,
    # which the owner asked to slow on its own (24 August 2026: "make
    # the scrolling in the main page 30% slower", after the whole app
    # had already been cut).
    area.notch_scale = float(notch_scale or 1.0)
    # Parented to the area, so it lives and dies with it.
    _SmoothWheel(area)
    return area


# **The sideways rows have no arrow buttons** - the owner's ask, 24
# August 2026: "remove the arrows buttons from all horizontal rows
# scrolling, make it just the scrollbar". The wheel over the row and the
# row's own scrollbar are what move it.
#
# **The edge fades came back on 28 August 2026, for a different reason
# than they left.** They went with the arrows because a fade existed
# only so a card would dissolve rather than be cut in half by the disc
# sitting on top of it - no disc, no need. The owner asked for them
# again that day - "ALL horizontal scrolls make them have a fade effect
# on the right most, and the left most if scrolled to the right" - and
# what they say now is *where the row continues*: solid at an end means
# that is the end, a fade means there is more that way. With the arrows
# gone there is otherwise nothing on a full row to say it scrolls at
# all.
#
# So the fade is state-driven, not decoration: each side is drawn only
# when the bar has somewhere to go on that side, and both are absent on
# a row short enough to need no scrolling.

# How wide each fade is, and where it stops being drawn. FADE_PX is
# about a third of a poster, wide enough to read as a soft edge rather
# than a hard vignette; FADE_SLACK_PX keeps a fade off the last few
# pixels of travel, where a hairline of gradient over a card that is
# already fully on screen just looks like dirt.
FADE_PX = 56
FADE_SLACK_PX = 6


class _EdgeFade(QWidget):
    """The two gradients over a horizontal row's ends.

    A child painted on top rather than something SideScroller draws
    itself: a QWidget paints *before* its children, and the row is a
    child, so anything the scroller painted would land underneath the
    cards it is supposed to be fading out.

    Mouse-transparent, so it changes nothing about clicking, dragging a
    card out of the row, or the wheel."""

    def __init__(self, parent, ground):
        super().__init__(parent)
        self._ground = QColor(ground)
        self._left = False
        self._right = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def set_ends(self, left, right):
        if (left, right) == (self._left, self._right):
            return          # nothing to repaint - this is called per scroll frame
        self._left, self._right = left, right
        self.update()

    def paintEvent(self, event):
        if not (self._left or self._right):
            return
        painter = QPainter(self)
        height = self.height()
        for side, wanted in (("left", self._left), ("right", self._right)):
            if not wanted:
                continue
            if side == "left":
                gradient = QLinearGradient(0, 0, FADE_PX, 0)
                gradient.setColorAt(0.0, self._ground)
                gradient.setColorAt(1.0, rgba_color(self._ground, 0))
                painter.fillRect(QRect(0, 0, FADE_PX, height), gradient)
            else:
                start = max(0, self.width() - FADE_PX)
                gradient = QLinearGradient(start, 0, self.width(), 0)
                gradient.setColorAt(0.0, rgba_color(self._ground, 0))
                gradient.setColorAt(1.0, self._ground)
                painter.fillRect(QRect(start, 0, FADE_PX, height), gradient)
        painter.end()


def rgba_color(colour, alpha):
    """`colour` at `alpha`. A QColor copy, because setAlpha mutates and
    these are held on the widget."""
    out = QColor(colour)
    out.setAlpha(int(alpha))
    return out


class SideScroller(QWidget):
    """A horizontally scrolling row: the wheel over it, its own
    scrollbar, Shift+wheel, and dragging a card out of it.

    The area is positioned by hand rather than laid out - it filled the
    whole widget under the arrows that used to float over it, and a
    layout would have pushed them into a column beside it. Kept as-is
    now they are gone: the geometry is one line and a layout would only
    add a solver to it."""

    def __init__(self, area: QScrollArea, ground=None, parent=None):
        super().__init__(parent)
        self._area = area
        area.setParent(self)
        self._bar = area.horizontalScrollBar()

        # `ground` is what the fades dissolve into, so it has to be the
        # colour the row is actually sitting on - PANEL_FILL for the
        # tracker's strips, BG for Home's. Defaulted rather than
        # required: every caller passes one today, and a future one that
        # forgets gets the common case instead of a black band.
        self._fade = _EdgeFade(self, ground or theme.PANEL_FILL)
        self._bar.valueChanged.connect(self._sync_fade)
        self._bar.rangeChanged.connect(lambda *_a: self._sync_fade())

        # **The arrows use the same momentum the wheel does** - the
        # owner's ask, 24 August 2026, that the horizontal rows be made
        # smooth. They used to run a 240ms SmoothTween, which is the
        # per-press version of exactly the curve that was retired from
        # the wheel for reading as stiff: it restarts from the current
        # position and stops dead. Measured on a Discover row, twelve
        # presses, two runs each:
        #
        #     tween     72-75 frames/s, judder 11.4-11.6%, 13-15% of
        #               frames more than 30% off the frame before
        #     momentum  126-129 frames/s, judder 7.0-7.7%, 3-4%
        #
        # One motion object shared with the wheel, so a press landing
        # mid-glide adds to it instead of fighting it - which is what
        # the old code needed `_wheel_target = None` to paper over.
        self._motion = _Momentum(self._bar, lambda: screen_tick_ms(self), self,
                                 frame_s=lambda: screen_frame_s(self))

        area.viewport().installEventFilter(self)
        # maximumHeight, not height(): the caller pins the area's height
        # before wrapping it, and the widget has not been laid out yet,
        # so height() is still the default 480.
        self.setFixedHeight(area.maximumHeight())

    def eventFilter(self, obj, event):
        """The wheel does not move this row - it moves the page behind
        it. See the note below for why, and why it has to be handed up
        by hand rather than simply not consumed."""
        if event.type() != QEvent.Type.Wheel or obj is not self._area.viewport():
            return False
        page = _vertical_scroller_above(self)
        if page is None:
            return False
        QApplication.sendEvent(page.viewport(), event)
        return True

    # **A wheel notch over a sideways row no longer scrolls it sideways.**
    # The owner, 25 August 2026: "do not make the mouse scroll moves any
    # horizontal scrollbar, make it only used by dragging the scrollbar
    # in the whole app" - said twice, the second time after a first
    # attempt that only blocked the wheel at the scrollbar itself and
    # left this untouched. That guard was necessary and not sufficient:
    # Qt routes a wheel to a horizontal QScrollBar only on a genuine
    # horizontal delta (measured on Qt 6.11), and this filter never sent
    # one - it took a *vertical* notch on the row's viewport and drove
    # the horizontal bar through _Momentum, writing setValue() directly.
    # Nothing watching the scrollbar could ever have seen it.
    #
    # It replaces an earlier ask of the owner's ("the row is the thing
    # under the pointer, and aiming at its 11px scrollbar to move it was
    # the alternative"), so both are recorded here rather than one
    # quietly overwriting the other.
    #
    # **Handed up by hand, because Qt will not do it.** Simply not
    # consuming the notch was tried first and leaves the row swallowing
    # it: measured 25 August 2026 on a horizontal-only QScrollArea
    # nested inside a vertical one, a vertical notch on the inner
    # viewport moved *neither* - QAbstractScrollArea accepts a wheel
    # whether or not it has anywhere to go, so nothing propagates to the
    # page. _EdgeWheelRelay would catch it, but only by hit-testing the
    # real cursor, which is the wrong thing to depend on for the app's
    # most repeated gesture. So the notch is sent to the nearest
    # ancestor that can actually scroll vertically, which lands it on
    # that area's _SmoothWheel and glides exactly like a direct one.

    def content_widget(self):
        """The widget the wrapped area scrolls - what a walk over the
        row's contents (CardDragReorder._cards_in) descends into,
        since nothing here is reachable through layouts."""
        return self._area.widget()

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
        # _row_height, not height(): the scrollbar's strip along the
        # bottom is not somewhere the fade should paint - see that
        # method, which records the 11px this got wrong before.
        self._fade.setGeometry(0, 0, self.width(), self._row_height())
        self._fade.raise_()
        self._sync_fade()

    def _sync_fade(self, *_args):
        """Fade the ends the row continues past, and only those.

        FADE_SLACK_PX of dead zone at each end: a bar one pixel off its
        minimum is at the start for every purpose a reader has, and a
        hairline of gradient there reads as grime on the artwork rather
        than as "there is more this way"."""
        try:
            low, high = self._bar.minimum(), self._bar.maximum()
            value = self._bar.value()
        except RuntimeError:
            return
        scrollable = high > low
        self._fade.set_ends(scrollable and value > low + FADE_SLACK_PX,
                            scrollable and value < high - FADE_SLACK_PX)


# ---- Frameless dialogs ---------------------------------------------------
# Every dialog in the app is a rounded, borderless panel (the owner's ask:
# "all windows like settings have round edges and no upper bar"). Only the
# main window keeps its native title bar.

# How near the dialog's edge a press counts as a resize grab rather than a
# move, on dialogs that were resizable under their native frame.
_RESIZE_BAND = 6

# The panel ground is BG - the exact ground every dialog's contents were
# built on - not SURFACE or BG_ALT. Two reasons, both hit: inputs and
# lists are themselves SURFACE and would read as holes on a SURFACE
# ground; and the app stylesheet fills every plain QWidget child with
# BG, so on any *other* panel tone each full-bleed child shows as a
# darker patch (measured on SettingsDialog's stacked pages against a
# BG_ALT panel). The 1px border and the rounded corners are what
# separate the panel from the window behind it.
_DIALOG_FILL = theme.BG


class _FramelessShell(QObject):
    """The chrome a frameless dialog loses, put back by hand: the rounded
    panel it paints itself as, drag-anywhere-to-move, and edge-resize.

    An event filter rather than a subclass so one call retrofits every
    existing QDialog in the app - SettingsDialog, the entry forms, the
    download dialogs - without touching their class hierarchies.

    Painting happens here (consuming the Paint event) instead of QSS
    border-radius on the dialog: the app stylesheet's QDialog background
    fill is a square over the whole window rect, and a setMask cut gives
    jagged corners on a 125% display - an antialiased QPainterPath on a
    translucent window is what renders clean."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self._dialog = dialog
        self._drag = None  # (global pos at press, window pos at press)
        dialog.installEventFilter(self)

    # ---- geometry helpers -------------------------------------------
    def _resizable(self):
        return self._dialog.minimumSize() != self._dialog.maximumSize()

    def _edges_at(self, pos):
        """Which window edges a point is within grabbing distance of."""
        edges = Qt.Edge(0)
        rect = self._dialog.rect()
        if pos.x() <= _RESIZE_BAND:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - _RESIZE_BAND:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= _RESIZE_BAND:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - _RESIZE_BAND:
            edges |= Qt.Edge.BottomEdge
        return edges

    # ---- events ------------------------------------------------------
    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self._paint_panel()
            return True   # the QSS square background fill must not run
        if (kind == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            # Presses on interactive children never arrive here - a
            # button or field accepts its own press. What does arrive is
            # the panel ground, layout gaps and labels, which is exactly
            # the "anywhere that is not a control" the native title bar
            # used to be.
            if self._resizable():
                edges = self._edges_at(event.position().toPoint())
                if edges and self._dialog.windowHandle() is not None:
                    self._dialog.windowHandle().startSystemResize(edges)
                    return True
            self._drag = (event.globalPosition().toPoint(),
                          self._dialog.pos())
            return True
        if (kind == QEvent.Type.MouseMove and self._drag is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            # The delta between the press's global point and this move's,
            # applied to where the window sat at the press - never
            # mapToGlobal arithmetic, which is divided by the wrong
            # screen's scale factor on a mixed-DPI pair.
            start_global, start_pos = self._drag
            offset = event.globalPosition().toPoint() - start_global
            self._dialog.move(start_pos + offset)
            return True
        if kind == QEvent.Type.MouseButtonRelease:
            self._drag = None
        return False

    def _paint_panel(self):
        painter = QPainter(self._dialog)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Half-pixel inset so the 1px border pen straddles its own line
        # instead of being clipped by the window edge.
        rect = QRectF(self._dialog.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, theme.RADIUS_LG, theme.RADIUS_LG)
        painter.fillPath(path, QColor(_DIALOG_FILL))
        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.drawPath(path)
        painter.end()


def frameless_dialog(dialog, title=""):
    """Make `dialog` a frameless rounded panel: no native title bar, no
    X/minimize/maximize, soft RADIUS_LG corners, draggable by its own
    ground, edge-resizable if it was resizable before.

    Esc keeps rejecting - QDialog's own key handling is untouched.

    `title` puts a small heading at the top of the panel *and* remains
    the windowTitle (taskbar/Alt-Tab still name the window). Pass it only
    for dialogs whose native bar was their one name - dialogs that
    already draw their own header (Settings, the wizard, What's New)
    must not be titled twice. Call after the dialog's layout exists, or
    the heading has nowhere to go."""
    dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    # Translucent, so the corners outside the painted rounding really
    # are see-through rather than square slabs of the QSS background.
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    _FramelessShell(dialog)
    if title:
        dialog.setWindowTitle(title)
        layout = dialog.layout()
        if layout is not None and hasattr(layout, "insertWidget"):
            layout.insertWidget(0, QLabel(title, objectName="DialogTitle"))
    return dialog


def _message_dialog(parent, title, text, accept_text, reject_text=None,
                    danger=False, default_reject=False):
    """The one shape behind confirm/inform - a small frameless panel with
    the message and an accent action, replacing the natively-framed
    QMessageBox stock dialogs."""
    dialog = QDialog(parent)
    body = QVBoxLayout(dialog)
    body.setContentsMargins(24, 20, 24, 18)
    body.setSpacing(14)
    label = QLabel(text)
    label.setWordWrap(True)
    body.addWidget(label)

    row = QHBoxLayout()
    row.setSpacing(10)
    row.addStretch()
    quiet = None
    if reject_text:
        quiet = QPushButton(reject_text)
        quiet.clicked.connect(dialog.reject)
        use_hover_cursor(quiet)
        row.addWidget(quiet)
    accent = QPushButton(accept_text,
                         objectName="Danger" if danger else "Accent")
    accent.clicked.connect(dialog.accept)
    use_hover_cursor(accent)
    row.addWidget(accent)
    body.addLayout(row)

    # Enter lands on the accent action - except where a caller says the
    # accidental Return keypress must not be the destructive one
    # (Restore Data, Uninstall), which keep their old default-No.
    default = quiet if (default_reject and quiet is not None) else accent
    default.setDefault(True)
    default.setFocus()
    dialog.setMinimumWidth(400)
    frameless_dialog(dialog, title=title)
    return dialog


def confirm(parent, title, text, yes_text="Yes", no_text="No",
            danger=False, default_no=False) -> bool:
    """A themed yes/no question. True only on the accent answer; Esc,
    the quiet button and closing all mean no - the same contract as
    `QMessageBox.question(...) == Yes`."""
    dialog = _message_dialog(parent, title, text, yes_text, no_text,
                             danger=danger, default_reject=default_no)
    return dialog.exec() == QDialog.DialogCode.Accepted


def inform(parent, title, text, ok_text="OK"):
    """A themed notice with one OK. For what a toast is too quiet for;
    Enter and Esc both dismiss it."""
    _message_dialog(parent, title, text, ok_text).exec()


class _CoveredFreeze(QObject):
    """Stops the widgets a full-window overlay covers from repainting.

    The reader, the player and the details page are all opened as a
    child of the window's *central widget*, laid over the sidebar and
    the page container rather than replacing them - which is what makes
    the sidebar disappear while reading with no state in main.py to get
    stuck in (see reader.open_reader). What that costs is that the
    covered widgets are still `isVisible()`, so every timer, clock,
    marquee and hover on the page underneath still asks Qt to repaint,
    and Qt still walks them - invisibly, behind an opaque overlay.

    Measured 25 August 2026, scrolling the reader by wheel notch, and
    the direction is the whole tell: scrolling *down* left the UI thread
    38% idle inside win32u (waiting on the present), while scrolling
    *up* was only 12% idle and spent 40% inside Qt6Gui - painting, not
    presenting. Per-widget paint counts named the culprits, and none of
    them were in the reader: Card#HomeItem, HeroBanner, the sidebar's
    NavList, ContinueCover. Freezing them:

        upward   180 -> 233 frames/s, 67,090 -> 16,880 paint events
        downward 234 -> 235 frames/s (already idle; nothing to win)

    which also removes the up/down asymmetry the owner reported - the
    two directions now measure the same.

    `setUpdatesEnabled(False)` rather than `hide()`, deliberately:
    hiding changes `isVisible()` for everything underneath and hands
    the host's QHBoxLayout a reason to re-lay-out the page container,
    where freezing touches neither. Both measured the same win (+63%),
    so the safer one wins on nothing but risk.

    Restoring is tied to *both* the overlay hiding and its destruction,
    because a frozen sidebar that never thaws is a frozen app: the
    overlays close with `hide()` then `deleteLater()`, and a crash
    between the two would otherwise leave the chrome painted once and
    never again.
    """

    def __init__(self, overlay, covered):
        super().__init__(overlay.parent())
        self._overlay = overlay
        self._covered = list(covered)
        self._held = False
        overlay.installEventFilter(self)
        overlay.destroyed.connect(self._finish)
        self._hold()

    def _set(self, enabled: bool):
        for widget in self._covered:
            try:
                widget.setUpdatesEnabled(enabled)
            except RuntimeError:
                pass        # deleted underneath us; nothing to restore

    def _hold(self):
        if not self._held:
            self._held = True
            self._set(False)

    def _release(self):
        if self._held:
            self._held = False
            self._set(True)

    def _finish(self):
        self._release()
        self.deleteLater()

    def eventFilter(self, obj, event):
        if obj is self._overlay:
            kind = event.type()
            if kind in (QEvent.Type.Hide, QEvent.Type.Close):
                self._release()
            elif kind == QEvent.Type.Show:
                self._hold()
        return False


def freeze_covered(overlay) -> QObject:
    """Stop what `overlay` covers from repainting behind it, until it
    goes away. Call once, right after a full-window overlay is shown.

    Only siblings that are visible *and* still painting are taken, so
    overlays stack correctly: a player opened over a details page
    freezes the details page and leaves the sidebar - already frozen by
    details - alone, and closing the player thaws only what it froze.
    """
    host = overlay.parent()
    if host is None:
        return None
    covered = [child for child in host.children()
               if isinstance(child, QWidget) and child is not overlay
               and child.isVisible() and child.updatesEnabled()]
    if not covered:
        return None
    return _CoveredFreeze(overlay, covered)
