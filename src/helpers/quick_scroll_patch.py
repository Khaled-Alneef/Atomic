"""Qt Quick compositor for ordinary high-refresh QScrollArea wheel motion.

QScrollBar stores an integer visual position. At high refresh rates a smooth
wheel glide often advances by less than one logical pixel per refresh, so the
QWidget path repeats a position and then jumps a full pixel. The successful
240 Hz A/B proved that presenting the page through Qt Quick removes that final
quantisation.

The first implementation embedded QQuickWindow with createWindowContainer().
That was the wrong integration boundary for Atomic: even a hidden container can
promote QWidget ancestors to native windows, which made Home/Discover/Saved/
History visually unstable on both the 240 Hz and 165 Hz monitors.

This version never inserts a native child into the QWidget tree. It snapshots
the scroll body at wheel-start and, only while momentum is active, shows a
mouse-transparent top-level QQuickWindow exactly over the viewport. The QWidget
hierarchy therefore remains unchanged. The overlay is lazy, so displays below
the activation threshold never create a QQuickWindow at all.
"""

from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow
    from PyQt6.QtWidgets import QScrollArea, QWidget

    from . import widgets as w

    class _PageTexture(QQuickPaintedItem):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._image = QImage()
            self.setAntialiasing(False)
            self.setMipmap(False)
            self.setOpaquePainting(True)
            self.setSmooth(True)

        def set_image(self, image, logical_width, logical_height):
            self._image = image
            self.setWidth(float(logical_width))
            self.setHeight(float(logical_height))
            self.update()

        def clear(self):
            self._image = QImage()
            self.update()

        def paint(self, painter):
            if self._image.isNull():
                return
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QPointF(0.0, 0.0), self._image)

    class _QuickScrollSurface(QObject):
        # 240 Hz is already user-confirmed smooth. 165 Hz still showed the old
        # integer-position cadence, so let it use the same architecture. Keep
        # 144 Hz and below unchanged until separately measured.
        HIGH_REFRESH_HZ = 150.0
        MAX_PHYSICAL_WIDTH = 4096
        MAX_PHYSICAL_HEIGHT = 8192
        MAX_PHYSICAL_PIXELS = 20_000_000

        def __init__(self, area, body):
            super().__init__(area)
            self.area = area
            self.body = body
            self.viewport = area.viewport()
            self.active = False
            self._image = QImage()
            self.quick = None
            self.texture = None

            self.viewport.installEventFilter(self)
            area.verticalScrollBar()._atomic_quick_scroll_surface = self

        def eventFilter(self, obj, event):
            if obj is not self.viewport:
                return False
            kind = event.type()
            if kind in (QEvent.Type.Resize, QEvent.Type.Hide,
                        QEvent.Type.MouseButtonPress):
                if self.active:
                    self.abort()
            return False

        def _screen_rate(self):
            try:
                screen = self.area.screen()
                return float(screen.refreshRate()) if screen is not None else 0.0
            except Exception:
                return 0.0

        def can_use(self):
            if self._screen_rate() < self.HIGH_REFRESH_HZ:
                return False
            if not self.area.isVisible() or not self.body.isVisible():
                return False
            width = max(self.viewport.width(), self.body.width())
            height = max(self.viewport.height(), self.body.height())
            if width <= 0 or height <= 0:
                return False
            try:
                dpr = float(self.viewport.devicePixelRatioF() or 1.0)
            except Exception:
                dpr = 1.0
            pw = int(round(width * dpr))
            ph = int(round(height * dpr))
            return (pw <= self.MAX_PHYSICAL_WIDTH
                    and ph <= self.MAX_PHYSICAL_HEIGHT
                    and pw * ph <= self.MAX_PHYSICAL_PIXELS)

        def _ensure_quick(self):
            if self.quick is not None:
                return True
            try:
                quick = QQuickWindow()
                quick.setColor(QColor(w.theme.BG))
                flags = (Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
                quick.setFlags(flags)

                # Owned/transient to Atomic's real top-level window, but not a
                # native child of the scroll viewport. This keeps it out of the
                # taskbar and in the same z-order family without changing any
                # QWidget's native-window status.
                try:
                    top = self.area.window()
                    handle = top.windowHandle() if top is not None else None
                    if handle is not None:
                        quick.setTransientParent(handle)
                except (AttributeError, RuntimeError):
                    pass

                texture = _PageTexture(quick.contentItem())
                self.quick = quick
                self.texture = texture
                return True
            except Exception:
                self.quick = None
                self.texture = None
                return False

        def _place_overlay(self):
            if self.quick is None:
                return
            try:
                point = self.viewport.mapToGlobal(QPoint(0, 0))
                self.quick.setGeometry(point.x(), point.y(),
                                       self.viewport.width(),
                                       self.viewport.height())
            except RuntimeError:
                pass

        def _snapshot(self):
            if not self.can_use() or not self._ensure_quick():
                return False
            width = max(self.viewport.width(), self.body.width())
            height = max(self.viewport.height(), self.body.height())
            try:
                dpr = float(self.viewport.devicePixelRatioF() or 1.0)
            except Exception:
                dpr = 1.0

            image = QImage(max(1, int(round(width * dpr))),
                           max(1, int(round(height * dpr))),
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(QColor(w.theme.BG))

            painter = QPainter(image)
            flags = (QWidget.RenderFlag.DrawWindowBackground
                     | QWidget.RenderFlag.DrawChildren)
            region = QRegion(QRect(0, 0, width, height))
            try:
                self.body.render(painter, QPoint(0, 0), region, flags)
            except Exception:
                painter.end()
                return False
            painter.end()

            self._image = image
            self.texture.set_image(image, width, height)
            return True

        def begin(self, pos):
            if self.active:
                self.present(pos)
                return True
            if not self._snapshot():
                return False

            self.active = True
            self.body.setUpdatesEnabled(False)
            self._place_overlay()
            self.present(pos)
            try:
                self.quick.show()
                self.quick.raise_()
                self.quick.requestUpdate()
            except RuntimeError:
                self.abort()
                return False
            return True

        def present(self, pos):
            if not self.active or self.quick is None or self.texture is None:
                return
            # QQuickItem coordinates are qreal. Never round this value: keeping
            # the fraction is the entire reason this compositor exists.
            self.texture.setY(-float(pos))
            try:
                self.quick.requestUpdate()
            except RuntimeError:
                self.abort()

        def _reveal_qwidget(self):
            self.body.setUpdatesEnabled(True)
            try:
                self.viewport.repaint()
            except RuntimeError:
                pass
            self.active = False
            if self.quick is not None:
                try:
                    self.quick.hide()
                except RuntimeError:
                    pass
            if self.texture is not None:
                try:
                    self.texture.clear()
                except RuntimeError:
                    pass
            self._image = QImage()

        def end(self, pos=None):
            if not self.active:
                return
            if pos is not None:
                self.present(pos)
            # Repaint the final integer QWidget state while the Quick overlay is
            # still covering it, then hide the overlay. No stale-frame flash.
            self._reveal_qwidget()

        def abort(self):
            if not self.active:
                return
            self._reveal_qwidget()
            try:
                self.viewport.update()
            except RuntimeError:
                pass

    old_scroll_area = w.scroll_area

    def quick_scroll_area(body, *args, **kwargs):
        area = old_scroll_area(body, *args, **kwargs)
        if isinstance(area, QScrollArea):
            # This object is pure QObject state. It creates no QQuickWindow until
            # a qualifying >=150 Hz wheel glide actually begins.
            try:
                area._atomic_quick_scroll_surface = _QuickScrollSurface(area, body)
            except Exception:
                area._atomic_quick_scroll_surface = None
        return area

    w.scroll_area = quick_scroll_area

    old_start = w._Momentum._start_ticking
    old_tick = w._Momentum._tick
    old_stop = w._Momentum._stop_ticking

    def _surface(motion):
        return getattr(motion._bar, "_atomic_quick_scroll_surface", None)

    def start(self):
        result = old_start(self)
        surface = _surface(self)
        if surface is not None:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.begin(pos)
        return result

    def tick(self):
        result = old_tick(self)
        surface = _surface(self)
        if surface is not None and surface.active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.present(pos)
        return result

    def stop(self):
        surface = _surface(self)
        result = old_stop(self)
        if surface is not None and surface.active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.end(pos)
        return result

    w._Momentum._start_ticking = start
    w._Momentum._tick = tick
    w._Momentum._stop_ticking = stop
