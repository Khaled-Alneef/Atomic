"""The small window the bell opens: what is new under the saved titles.

A tool window owned by the app (see NotificationPanel for the two shapes
that failed): it closes itself on a press anywhere else, in the app or not,
and takes no taskbar entry. One row per
notification - the title, what is new, how long ago - with a dot on the
ones that were unread when it opened; a press on a row opens that title's
page (or Settings, for a new version). The rows are drawn as they were when
it opened: opening is what reads them (main._open_notifications), and a dot
vanishing under the pointer would be the list moving while it is read.
"""

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from . import images, logs, notifications, theme
from .widgets import SmoothTween, use_hover_cursor

PANEL_WIDTH = 420
COVER_W, COVER_H = 40, 60      # a poster's 2:3
PANEL_MAX_LIST = 420        # past this the list scrolls
PANEL_PAD = 10
# How often an open window asks Windows about presses it cannot hear itself
# (on the web page, in another app, on the desktop). **Measured on his
# machine, 7 November 2026**: at 40ms, asking only "is a button down right
# now", the log has four closes on presses in the bar and he reported
# others that did nothing - a quick click, or one while Home is drawing its
# glide frames, starts and ends between two looks. Now every press Qt
# delivers (the bar, the sidebar, anything of this app's own) closes it
# from the event itself (eventFilter), and the poll is 15ms and also reads
# Windows' "pressed since the last look" bit, so a click that fell between
# two looks still counts.
OUTSIDE_POLL_MS = 15


def _buttons_down(since_last=False) -> bool:
    """Whether a mouse button is down, from the system - any window's. With
    `since_last`, also whether one was pressed since the previous call."""
    try:
        import ctypes
        state = ctypes.windll.user32.GetAsyncKeyState
        bits = state(0x01) | state(0x02) | state(0x04)
        return bool(bits & 0x8000 or (since_last and bits & 0x0001))
    except Exception:
        return False


class _Row(QWidget):
    """One notification, pressable."""

    clicked = Signal(dict)

    def __init__(self, item, parent=None):
        super().__init__(parent, objectName="Bare")
        self.item = item
        self._hover = 0.0
        self._tween = SmoothTween(self, self._set_hover, 120)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        use_hover_cursor(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(22, 8, 12, 8)
        row.setSpacing(10)
        # The title's own cover (his ask, the same evening), or the app
        # mark for a new version. A blank rounded slab where a title has
        # no cover on disk yet - the row keeps its shape either way.
        art = QLabel()
        art.setFixedSize(COVER_W, COVER_H)
        art.setStyleSheet("background: transparent;")
        if item.get("kind") == "update":
            art.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon = QApplication.windowIcon()
            if not icon.isNull():
                art.setPixmap(icon.pixmap(QSize(COVER_W - 6, COVER_W - 6),
                                          float(self.devicePixelRatioF() or 1.0)))
        else:
            art.setPixmap(images.thumbnail_or_avatar(item.get("_cover") or None,
                                                     str(item.get("title") or ""),
                                                     size=(COVER_W, COVER_H)))
        row.addWidget(art, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(str(item.get("title") or ""))
        title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 700; background: transparent;")
        title.setWordWrap(True)
        label = QLabel(str(item.get("_display") or item.get("label") or ""))
        label.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        label.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(label)
        row.addLayout(text, 1)
        when = QLabel(notifications.when_text(item.get("at") or ""))
        when.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 8pt; background: transparent;")
        when.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        row.addWidget(when, 0, Qt.AlignmentFlag.AlignTop)
        for child in (title, label, when):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def enterEvent(self, event):
        self._tween.start(self._hover, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._tween.start(self._hover, 0.0)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self.item)
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hover > 0.001:
            fill = QColor(theme.SURFACE_HOVER)
            fill.setAlphaF(self._hover)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(self.rect()), theme.RADIUS_SM, theme.RADIUS_SM)
        if not self.item.get("read"):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawEllipse(QRectF(9, 15, 6, 6))
        painter.end()


class NotificationPanel(QWidget):
    """The bell's window. `opened(item)` asks the window to open what a
    row names; `cleared` after Clear All."""

    opened = Signal(dict)
    cleared = Signal()
    dismissed = Signal(float)       # closed, by any route, at time.monotonic()

    def __init__(self, rows, parent=None):
        # **A top-level tool window owned by the app.** Three shapes were
        # tried on the owner's machine, 7 November 2026:
        #   * a Popup - its mouse grab left hover dead after it closed
        #     ("I still can press it");
        #   * a native child of the main window - never seen: the web page
        #     is a native child of the same window (webview2_host's detach)
        #     and the app restacks its native children constantly, so the
        #     page was on top of it within milliseconds ("only for few ms"),
        #     while the log had it open for 0.6-2.8s each time;
        #   * this - drawn above everything, no grab. What it left behind
        #     is Qt's idea of which window the pointer is in, which a second
        #     press put right ("I needed to press twice"); main re-syncs it
        #     on close (_resync_mouse).
        # Owned by the main window: it stays above it, minimises with it,
        # and takes no taskbar entry.
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("Bare")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # Not activated: showing would otherwise take the window's
        # foreground, and with it the keyboard from the page underneath
        # (measured - the test rig's guard caught it on the first press).
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.closed_at = 0.0
        self.reason = "closed"
        self._watch = QTimer(self)
        self._watch.setInterval(OUTSIDE_POLL_MS)
        self._watch.timeout.connect(self._watch_outside)
        self.setFixedWidth(PANEL_WIDTH)
        column = QVBoxLayout(self)
        column.setContentsMargins(PANEL_PAD, PANEL_PAD, PANEL_PAD, PANEL_PAD)
        column.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(8, 2, 2, 2)
        title = QLabel("Notifications")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 12pt; font-weight: 700;"
                            f" background: transparent;")
        head.addWidget(title)
        head.addStretch(1)
        column.addLayout(head)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER}; border: none;")
        column.addWidget(line)

        # What each row shows beside its own label, worked out once here
        # rather than per paint: the cover on disk and the count waiting
        # (notifications.backlog), one lookup per title however many rows
        # it has.
        waiting, covers = {}, {}
        for item in rows:
            key = item.get("entry_id") or item.get("id")
            try:
                if key not in waiting:
                    waiting[key] = notifications.backlog(item)
                    covers[key] = notifications.cover_path(item)
            except Exception:
                logs.exception("notifications: could not read a row's extras")
                waiting[key], covers[key] = "", ""
            item["_display"] = notifications.display_label(item, waiting[key])
            item["_cover"] = covers[key]

        if not rows:
            empty = QLabel("No new episodes or chapters yet.\n"
                           "New releases of what you saved show up here.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {theme.TEXT_MUTED}; padding: 26px 8px;"
                                f" background: transparent;")
            column.addWidget(empty)
        else:
            holder = QWidget(objectName="Bare")
            holder.setStyleSheet("background: transparent;")
            rows_box = QVBoxLayout(holder)
            rows_box.setContentsMargins(0, 0, 0, 0)
            rows_box.setSpacing(2)
            for item in rows:
                row = _Row(item, holder)
                row.clicked.connect(self._pick)
                rows_box.addWidget(row)
            rows_box.addStretch(1)
            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QFrame.Shape.NoFrame)
            scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroller.setStyleSheet("QScrollArea { background: transparent; border: none; }")
            scroller.viewport().setStyleSheet("background: transparent;")
            scroller.setWidget(holder)
            holder.adjustSize()
            scroller.setFixedHeight(min(PANEL_MAX_LIST, holder.sizeHint().height() + 2))
            column.addWidget(scroller)
            # **A button, at the foot.** The owner, 7 November 2026: "add
            # Clear notifications button on the notifications window" - the
            # grey "Clear All" text in the header was there and read as a
            # caption, not a control. One way to clear, so the header lost
            # it; the face is the details page's secondary grey.
            self.clear_btn = QPushButton("Clear Notifications")
            self.clear_btn.setFixedHeight(36)
            self.clear_btn.setStyleSheet(
                f"QPushButton {{ color: {theme.TEXT}; background: {theme.SURFACE_HOVER};"
                f" border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_SM}px;"
                f" font-weight: 600; padding: 0 14px; }}"
                f"QPushButton:hover {{ background: {theme.BORDER}; }}"
                f"QPushButton:pressed {{ background: {theme.BORDER}; }}")
            self.clear_btn.clicked.connect(self._clear)
            use_hover_cursor(self.clear_btn)
            column.addSpacing(2)
            column.addWidget(self.clear_btn)
        self.adjustSize()

    def _global_rect(self) -> QRect:
        """Where this is on screen - a top-level's own frame."""
        return self.frameGeometry()

    def popup_at(self, point: QPoint):
        """Show at `point`, a global position."""
        self.move(point)
        self.show()
        self._held = _buttons_down()        # the press that opened it
        _buttons_down(since_last=True)      # and its "pressed since" bit
        self._watch.start()
        QApplication.instance().installEventFilter(self)
        rect = self._global_rect()
        logs.info(f"notifications window opened at ({rect.x()},{rect.y()} "
                  f"{rect.width()}x{rect.height()}), held={self._held}")
        QApplication.instance().applicationStateChanged.connect(self._app_state)

    def _watch_outside(self):
        """Close on a press anywhere outside this window. Polled from the
        system rather than heard from Qt: a press on a web page goes to
        WebView2's own native window and Qt never sees it."""
        held = _buttons_down()
        pressed = _buttons_down(since_last=True)
        fresh = pressed and not self._held
        self._held = held
        if fresh:
            pos = QCursor.pos()
            rect = self._global_rect()
            if not rect.contains(pos):
                self.reason = (f"press outside at ({pos.x()},{pos.y()}), window "
                               f"({rect.x()},{rect.y()} {rect.width()}x{rect.height()})")
                self.close()

    def eventFilter(self, obj, event):
        """A press anywhere this app draws, outside this window, closes it -
        straight from the event, so no click can slip between two polls.
        Not consumed: the press still does what it was for."""
        if (event.type() == QEvent.Type.MouseButtonPress
                and not self._global_rect().contains(QCursor.pos())):
            name = type(obj).__name__
            self.reason = f"press on {name} at ({QCursor.pos().x()},{QCursor.pos().y()})"
            QTimer.singleShot(0, self._close_once)
        return False

    def _close_once(self):
        # The poll and the event can both see one press; the second close
        # finds the window already gone, and a RuntimeError escaping a timer
        # slot would end the app (widgets.release_hover_cursor's note).
        try:
            if self.isVisible():
                self.close()
        except RuntimeError:
            pass

    def _app_state(self, state):
        if state != Qt.ApplicationState.ApplicationActive:
            self.reason = f"app no longer active ({state})"
            self.close()

    def closeEvent(self, event):
        import time
        self.closed_at = time.monotonic()
        self._watch.stop()
        QApplication.instance().removeEventFilter(self)
        logs.info(f"notifications window closed: {self.reason}")
        try:
            QApplication.instance().applicationStateChanged.disconnect(self._app_state)
        except (TypeError, RuntimeError):
            pass
        self.dismissed.emit(self.closed_at)
        super().closeEvent(event)

    def _pick(self, item):
        self.reason = f"row pressed ({item.get('kind')})"
        self.close()
        self.opened.emit(item)

    def _clear(self):
        self.reason = "Clear Notifications pressed"
        self.cleared.emit()
        self.close()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.BORDER), 1.0))
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(rect, theme.RADIUS, theme.RADIUS)
        painter.end()
