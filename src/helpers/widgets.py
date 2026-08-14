"""Small reusable widgets shared across windows."""

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
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
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tooltip_provider = None

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


class Toast(QLabel):
    """A small, self-dismissing confirmation message in the bottom-right
    corner of the app's main window - for lightweight feedback (e.g.
    "Saved") that doesn't need a modal dialog click to dismiss."""

    def __init__(self, anchor, text, duration_ms=2000):
        window = _toast_anchor_window(anchor)
        super().__init__(text, window)
        self.setWindowFlags(Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.TEXT}; "
            f"border: 1px solid {theme.ACCENT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 10px 18px; font-weight: 700;")
        # Shown before being positioned: a ToolTip-flagged window only
        # settles at its final polished size once shown, and positioning
        # against a stale size would push it past the intended corner.
        self.show()
        self._move_to_corner(window)
        QTimer.singleShot(duration_ms, self.close)

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
    Toast(anchor, text, duration_ms)


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
