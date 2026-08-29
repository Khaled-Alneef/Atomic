"""Qt Quick compositor for ordinary high-refresh QScrollArea wheel motion.

QScrollBar stores an integer visual position. At high refresh rates a smooth
wheel glide often advances by less than one logical pixel per refresh, so the
QWidget path repeats a position and then jumps a full pixel. The successful
165/240 Hz A/B proved that presenting the page through Qt Quick removes that
final quantisation.

The page itself remains QWidget-based. At wheel-start it is captured once at
native device-pixel ratio and the immutable capture is translated fractionally
by a mouse-transparent QQuickWindow while momentum is active.

Important: the capture is NOT presented through QQuickPaintedItem anymore.
QQuickPaintedItem creates its own QImage-backed paint target and then uploads
that result to the scene graph, adding a second raster/painter boundary around
an image which is already fully composed. Home and Tracker were the surfaces
where that extra boundary showed up as unstable/shaking card pixels even though
the trajectory itself was smooth.

Instead, _PageTexture is a plain QQuickItem with ItemHasContents. Its
updatePaintNode() uploads the already-rendered native-DPR QImage directly to a
QSGTexture once and draws it with QQuickWindow.createImageNode(). Every motion
frame after that changes only the item's qreal Y transform; the pixels are not
repainted or re-rasterized. The motion clock and fractional positions are the
same as the user-confirmed smooth path.
"""

from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQuick import QQuickItem, QQuickWindow, QSGTexture
    from PyQt6.QtWidgets import QScrollArea, QWidget

    from . import widgets as w

    class _PageTexture(QQuickItem):
        """One immutable native-DPR snapshot uploaded straight to the SG.

        A fresh item is created for each glide. That keeps texture ownership
        simple: the image node owns its QSGTexture, and destroying the item at
        the end of the glide lets Qt destroy the node/texture on the render
        thread. There is no texture replacement while a node is live.
        """

        def __init__(self, parent=None):
            super().__init__(parent)
            self._image = QImage()
            self.setFlag(QQuickItem.Flag.ItemHasContents, True)

        def set_image(self, image, logical_width, logical_height):
            self._image = QImage(image)
            self.setWidth(float(logical_width))
            self.setHeight(float(logical_height))
            self.update()

        def updatePaintNode(self, old_node, update_data):
            # Called on the Qt Quick render thread with the GUI thread blocked.
            # Only create QSG resources here; after creation the node is reused
            # unchanged for the whole glide and only QQuickItem.y moves.
            try:
                if self._image.isNull():
                    return None
                if old_node is not None:
                    return old_node

                window = self.window()
                if window is None:
                    return None
                node = window.createImageNode()
                if node is None:
                    return None
                texture = window.createTextureFromImage(self._image)
                if texture is None:
                    return None
                node.setTexture(texture)
                node.setOwnsTexture(True)
                try:
                    # Match the accepted smooth path: fractional scene-graph
                    # translation uses linear sampling, but there is no second
                    # QPainter image/texture pass anymore.
                    node.setFiltering(QSGTexture.Filtering.Linear)
                except Exception:
                    pass
                node.setRect(0.0, 0.0, float(self.width()), float(self.height()))
                return node
            except Exception:
                # An exception escaping a Qt render callback can terminate a
                # frozen PyQt application. A missing frame is preferable.
                return old_node

    class _QuickScrollSurface(QObject):
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
                try:
                    top = self.area.window()
                    handle = top.windowHandle() if top is not None else None
                    if handle is not None:
                        quick.setTransientParent(handle)
                except (AttributeError, RuntimeError):
                    pass
                self.quick = quick
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

        def _drop_texture_item(self):
            item = self.texture
            self.texture = None
            if item is None:
                return
            try:
                item.setVisible(False)
                item.deleteLater()
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

            # One scene-graph item/texture per glide. The old item's node owns
            # its texture and will be cleaned up on the render thread.
            self._drop_texture_item()
            texture = _PageTexture(self.quick.contentItem())
            texture.set_image(image, width, height)
            self.texture = texture
            self._image = image
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
            # Preserve the exact successful motion model: this remains a qreal
            # scene-graph transform and is never rounded to QWidget pixels.
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
            self._drop_texture_item()
            self._image = QImage()

        def end(self, pos=None):
            if not self.active:
                return
            if pos is not None:
                self.present(pos)
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
