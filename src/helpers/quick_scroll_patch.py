"""Qt Quick compositor for ordinary high-refresh QScrollArea wheel motion.

QScrollBar stores an integer visual position. At high refresh rates a smooth
wheel glide often advances by less than one logical pixel per refresh, so the
QWidget path repeats a position and then jumps a full pixel. The successful
165/240 Hz A/B proved that presenting the page through Qt Quick removes that
final quantisation.

The compositor stays on the proven QQuickPaintedItem path. A custom QSG node
experiment was deliberately removed after it crashed the frozen Windows build
at startup: Python scene-graph resource ownership is not an acceptable runtime
boundary here.

Home and Tracker are treated differently only at snapshot time. Their moving
capture is a bounded strip around the viewport and, where the existing texture
budget allows it, is rasterized at 1.25x-2x the native device-pixel density.
QQuickPaintedItem's backing texture is enlarged by the same factor. The item
still moves at the exact same fractional qreal Y positions and cadence; the
extra samples only give text/card edges finer vertical phases while moving.
Other QScrollArea pages keep the exact native-DPR renderer used by 1.10.161.
"""

from __future__ import annotations

import math

_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow
    from PyQt6.QtWidgets import QScrollArea, QWidget

    from . import widgets as w

    class _PageTexture(QQuickPaintedItem):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._image = QImage()
            self._sample_scale = 1.0
            self.setAntialiasing(False)
            self.setMipmap(False)
            self.setOpaquePainting(True)
            self.setSmooth(False)

        def set_image(self, image, logical_width, logical_height,
                      sample_scale=1.0):
            self._image = image
            self._sample_scale = max(1.0, float(sample_scale or 1.0))
            self.setWidth(float(logical_width))
            self.setHeight(float(logical_height))

            # textureSize is expressed in item coordinates; Qt applies the
            # window DPR on top. Matching it to the capture's extra sampling
            # keeps those samples alive in the scene-graph texture instead of
            # downsampling them back to native resolution inside paint().
            try:
                self.setTextureSize(QSize(
                    max(1, int(round(logical_width * self._sample_scale))),
                    max(1, int(round(logical_height * self._sample_scale)))))
            except (AttributeError, RuntimeError):
                # Older Qt bindings still get the safe native path; never make
                # optional supersampling a startup/runtime requirement.
                self._sample_scale = 1.0

            # Native snapshots deliberately use nearest sampling exactly as in
            # 1.10.161. The supersampled target needs linear downsampling or its
            # additional texels would be thrown away when the item is displayed.
            self.setSmooth(self._sample_scale > 1.0)
            self.update()

        def clear(self):
            self._image = QImage()
            self._sample_scale = 1.0
            self.update()

        def paint(self, painter):
            if self._image.isNull():
                return
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform,
                self._sample_scale > 1.0)
            painter.drawImage(QPointF(0.0, 0.0), self._image)

    class _QuickScrollSurface(QObject):
        HIGH_REFRESH_HZ = 150.0
        MAX_PHYSICAL_WIDTH = 4096
        MAX_PHYSICAL_HEIGHT = 8192
        MAX_PHYSICAL_PIXELS = 20_000_000

        # The high-quality path never exceeds the old full-page pixel budget.
        # It spends that same budget on a shorter, denser strip instead.
        TARGET_OVERSCAN_MIN = 520
        TARGET_OVERSCAN_VIEWPORT = 0.80
        TARGET_SAMPLE_FACTORS = (2.0, 1.75, 1.5, 1.25, 1.0)

        def __init__(self, area, body):
            super().__init__(area)
            self.area = area
            self.body = body
            self.viewport = area.viewport()
            self.active = False
            self._image = QImage()
            self.quick = None
            self.texture = None
            self._snapshot_top = 0.0
            self._snapshot_bottom = 0.0
            self._sample_scale = 1.0
            self._target_quality = False

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

        def _is_target_page(self):
            """Home and Tracker only; checked after final parenting exists."""
            for start in (self.area, self.body):
                node = start
                seen = set()
                for _ in range(32):
                    if node is None or id(node) in seen:
                        break
                    seen.add(id(node))
                    try:
                        classes = type(node).__mro__
                    except Exception:
                        classes = ()
                    for cls in classes:
                        module = getattr(cls, "__module__", "")
                        name = getattr(cls, "__name__", "")
                        if module == "windows.home" and name == "HomePage":
                            return True
                        if module == "windows.tracker" and name == "TrackerPage":
                            return True
                    try:
                        node = node.parentWidget()
                    except (AttributeError, RuntimeError):
                        break
            return False

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

        def _target_strip(self, pos, body_height):
            viewport_h = max(1, self.viewport.height())
            overscan = max(self.TARGET_OVERSCAN_MIN,
                           int(round(viewport_h * self.TARGET_OVERSCAN_VIEWPORT)))
            top = max(0, int(math.floor(float(pos))) - overscan)
            bottom = min(body_height,
                         int(math.ceil(float(pos))) + viewport_h + overscan)

            # Near an edge there may not be enough room on one side. Spend the
            # unused margin on the other side so the strip keeps approximately
            # the same total runway for a momentum tail.
            wanted = min(body_height, viewport_h + 2 * overscan)
            if bottom - top < wanted:
                missing = wanted - (bottom - top)
                grow_up = min(top, missing)
                top -= grow_up
                missing -= grow_up
                bottom = min(body_height, bottom + missing)
            return top, max(top + 1, bottom)

        def _sample_factor(self, width, height, dpr):
            native_w = max(1, int(round(width * dpr)))
            native_h = max(1, int(round(height * dpr)))
            for factor in self.TARGET_SAMPLE_FACTORS:
                pw = max(1, int(round(native_w * factor)))
                ph = max(1, int(round(native_h * factor)))
                if (pw <= self.MAX_PHYSICAL_WIDTH
                        and ph <= self.MAX_PHYSICAL_HEIGHT
                        and pw * ph <= self.MAX_PHYSICAL_PIXELS):
                    return factor
            return 1.0

        def _snapshot(self, pos=0.0):
            if not self.can_use() or not self._ensure_quick():
                return False

            width = max(self.viewport.width(), self.body.width())
            body_height = max(self.viewport.height(), self.body.height())
            self._target_quality = self._is_target_page()

            try:
                dpr = float(self.viewport.devicePixelRatioF() or 1.0)
            except Exception:
                dpr = 1.0

            if self._target_quality:
                top, bottom = self._target_strip(pos, body_height)
                height = bottom - top
                sample = self._sample_factor(width, height, dpr)
            else:
                top, bottom = 0, body_height
                height = body_height
                sample = 1.0

            sample_dpr = max(1.0, dpr * sample)
            pw = max(1, int(round(width * sample_dpr)))
            ph = max(1, int(round(height * sample_dpr)))
            image = QImage(pw, ph, QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(sample_dpr)
            image.fill(QColor(w.theme.BG))

            painter = QPainter(image)
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, sample > 1.0)
            flags = (QWidget.RenderFlag.DrawWindowBackground
                     | QWidget.RenderFlag.DrawChildren)
            region = QRegion(QRect(0, top, width, height))
            try:
                self.body.render(painter, QPoint(0, -top), region, flags)
            except Exception:
                painter.end()
                return False
            painter.end()

            self._image = image
            self._snapshot_top = float(top)
            self._snapshot_bottom = float(bottom)
            self._sample_scale = float(sample)
            self.texture.set_image(image, width, height, sample)
            return True

        def _covers(self, pos):
            if not self._target_quality:
                return True
            viewport_h = float(max(1, self.viewport.height()))
            value = float(pos)
            return (value >= self._snapshot_top
                    and value + viewport_h <= self._snapshot_bottom)

        def begin(self, pos):
            if self.active:
                if not self._covers(pos):
                    # Continuous wheel input can outrun the initial strip. A
                    # recapture is rare because the strip has overscan, and it
                    # does not touch the motion clock or QWidget scrollbar.
                    if not self._snapshot(pos):
                        return False
                self.present(pos)
                return True
            if not self._snapshot(pos):
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

            if self._target_quality and not self._covers(pos):
                if not self._snapshot(pos):
                    self.abort()
                    return

            # The strip's logical top is an absolute body coordinate. Shift by
            # (top - scroll position) so the viewport sees exactly the same
            # content as the old full-page texture at -pos.
            self.texture.setY(self._snapshot_top - float(pos))
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
            self._snapshot_top = 0.0
            self._snapshot_bottom = 0.0
            self._sample_scale = 1.0
            self._target_quality = False

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
