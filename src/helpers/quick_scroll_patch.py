"""Qt Quick compositor for ordinary high-refresh QScrollArea wheel motion.

The QWidget scroll path stores its visual position in QScrollBar::value, which
is an integer.  At 240 Hz a smooth deceleration frequently advances by less
than one logical pixel per refresh, so several refreshes repeat the same
position and the next refresh jumps a full pixel.  No timer/easing change can
remove that final quantisation.

For >=200 Hz screens this patch keeps the existing QWidget page as the source
of truth but snapshots it at the beginning of wheel momentum and presents that
snapshot through a real QQuickWindow.  The scene-graph item moves at the
Momentum object's floating-point position, so the monitor can receive a new
fractional position on every refresh.  The underlying QScrollArea continues to
receive its normal integer values for state/scrollbar correctness, but its body
is not repainted while the Quick surface is covering it.

Important boundaries:
  * <=165 Hz stays on the existing QWidget path unchanged.
  * Only pages whose complete body can be cached inside conservative memory/
    texture limits qualify. Large pages fall back rather than rebuilding a
    cache in the middle of a glide.
  * This uses QWidget.createWindowContainer(QQuickWindow), never QQuickWidget;
    QQuickWidget disables the threaded render loop that is the point here.
  * The surface is mouse-transparent and exists only during wheel momentum.
"""

from __future__ import annotations

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, QRegion, QSize, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter
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

        def paint(self, painter):
            if self._image.isNull():
                return
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QPointF(0.0, 0.0), self._image)

    class _QuickScrollSurface(QObject):
        HIGH_REFRESH_HZ = 200.0
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

            self.quick = QQuickWindow()
            self.quick.setColor(QColor(w.theme.BG))
            try:
                self.quick.setFlag(Qt.WindowType.WindowTransparentForInput, True)
            except Exception:
                pass
            self.texture = _PageTexture(self.quick.contentItem())
            self.container = QWidget.createWindowContainer(self.quick, self.viewport)
            self.container.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.container.setGeometry(self.viewport.rect())
            self.container.hide()

            self.viewport.installEventFilter(self)
            area.verticalScrollBar()._atomic_quick_scroll_surface = self

        def eventFilter(self, obj, event):
            if obj is not self.viewport:
                return False
            kind = event.type()
            if kind == QEvent.Type.Resize:
                self.container.setGeometry(self.viewport.rect())
                if self.active:
                    self.abort()
            elif kind in (QEvent.Type.Hide, QEvent.Type.MouseButtonPress):
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

        def _snapshot(self):
            if not self.can_use():
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
                self.body.render(painter, QPointF(0.0, 0.0).toPoint(), region, flags)
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
            self.container.setGeometry(self.viewport.rect())
            self.body.setUpdatesEnabled(False)
            self.present(pos)
            self.container.show()
            self.container.raise_()
            self.quick.update()
            return True

        def present(self, pos):
            if not self.active:
                return
            # QQuickItem coordinates are qreal. Do not round here: preserving
            # this fraction is the entire reason this surface exists.
            self.texture.setY(-float(pos))
            self.quick.update()

        def end(self, pos=None):
            if not self.active:
                return
            if pos is not None:
                self.present(pos)
            self.body.setUpdatesEnabled(True)
            # Paint the QWidget state synchronously while the Quick child is
            # still covering it, then reveal it. This prevents a one-frame
            # flash of the pre-scroll body when momentum ends.
            try:
                self.viewport.repaint()
            except RuntimeError:
                pass
            self.active = False
            self.container.hide()
            self._image = QImage()

        def abort(self):
            if not self.active:
                return
            self.body.setUpdatesEnabled(True)
            self.active = False
            self.container.hide()
            self._image = QImage()
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
                # Qt Quick must be an optional presentation upgrade, never a
                # reason an otherwise-valid page cannot be constructed.
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
