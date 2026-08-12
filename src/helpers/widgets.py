"""Small reusable widgets shared across windows."""

from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtWidgets import QFrame, QLabel, QScrollArea, QWidget

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
    feedback and left/right click signals."""

    clicked = Signal()
    rightClicked = Signal(object)  # emits the originating QMouseEvent

    def __init__(self, parent=None, hoverable=True):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("hoverable", hoverable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        elif event.button() == Qt.MouseButton.RightButton:
            self.rightClicked.emit(event)
        super().mousePressEvent(event)


class Toast(QLabel):
    """A small, self-dismissing confirmation message in the bottom-right
    corner of `anchor`'s window - for lightweight feedback (e.g. "Saved")
    that doesn't need a modal dialog click to dismiss."""

    def __init__(self, anchor, text, duration_ms=2000):
        window = anchor.window()
        super().__init__(text, window)
        self.setWindowFlags(Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.TEXT}; "
            f"border: 1px solid {theme.ACCENT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 10px 18px; font-weight: 700;")
        self.adjustSize()
        margin = 24
        corner = window.mapToGlobal(QPoint(window.width(), window.height()))
        self.move(corner.x() - self.width() - margin, corner.y() - self.height() - margin)
        self.show()
        QTimer.singleShot(duration_ms, self.close)


def show_toast(anchor, text, duration_ms=2000):
    Toast(anchor, text, duration_ms)


def scroll_area(body: QWidget) -> QScrollArea:
    """Wrap `body` in a frameless, resizable, mouse-wheel-scrollable area."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(body)
    return area
