"""Native-refresh GPU scroll compositor for Atomic.

The browser A/B proved that the remaining motion trace is not scroll distance
or easing: the same artwork is dramatically clearer when a compositor moves
one texture at the display refresh rate. This module gives ordinary QWidget
scroll pages that same shape while they are moving:

    QWidget tree -> one DPR-correct QImage -> QQuickPaintedItem texture
                 -> fractional scene-graph Y transform -> display present

The live QWidget tree is restored as soon as motion stops, so controls remain
normal widgets and stationary text is never left texture-filtered.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys


_TARGET = "helpers.widgets"
_INSTALLED = False
_PATCHED = False


def _patch_widgets(w):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow
    from PyQt6.QtWidgets import QFrame, QScrollArea, QWidget

    original_present_frame_s = w.present_frame_s

    def native_present_frame_s(widget=None) -> float:
        """One committed motion position per refresh of the active screen."""
        if os.environ.get("ATOMIC_PRESENT_HZ"):
            return original_present_frame_s(widget)
        return w.screen_frame_s(widget)

    w.present_frame_s = native_present_frame_s

    class _ScrollTexture(QQuickPaintedItem):
        """Paint once when the cache changes; movement itself is a GPU transform."""

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

        def clear_image(self):
            self._image = QImage()
            self.update()

        def paint(self, painter):
            if self._image.isNull():
                return
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QPointF(0.0, 0.0), self._image)

    class _QuickCompositor(QObject):
        """One hidden QQuickWindow per scroll viewport, shown only in motion."""

        # Three viewports was enough for short glides but occasionally crossed
        # an edge during a longer wheel burst. Re-rendering the QWidget tree and
        # uploading a replacement texture in the middle of motion produced the
        # rare hitch that remained after the release-handoff fix.
        CACHE_VIEWS = 8.0
        EDGE_SLOP = 192.0
        # Prefer one full-page texture whenever it is comfortably below normal
        # scene-graph texture limits. This removes cache-edge work entirely on
        # Home/Discover-sized pages while keeping very long reader/list pages
        # bounded. The limit is in physical pixels because that is what the GPU
        # actually receives.
        FULL_CACHE_MAX_PHYSICAL_H = 8192

        def __init__(self, area, body, ground):
            super().__init__(area)
            self.area = area
            self.body = body
            self.viewport = area.viewport()
            self._ground = QColor(ground)
            self._cache_top = 0.0
            self._cache_height = 0
            self._active = False
            self._visual = float(area.verticalScrollBar().value())
            self._motion = None
            self._ending_after_swap = False

            self.quick = QQuickWindow()
            self.quick.setColor(self._ground)
            try:
                self.quick.setFlag(Qt.WindowType.WindowTransparentForInput, True)
            except Exception:
                pass
            self.texture = _ScrollTexture(self.quick.contentItem())
            self.container = QWidget.createWindowContainer(self.quick, self.viewport)
            self.container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.container.setGeometry(self.viewport.rect())
            self.container.hide()

            self.quick.frameSwapped.connect(self._frame_swapped)
            self.viewport.installEventFilter(self)
            area.verticalScrollBar()._atomic_motion_surface = self

        def eventFilter(self, obj, event):
            if obj is self.viewport and event.type() == QEvent.Type.Resize:
                self.container.setGeometry(self.viewport.rect())
                if self._active:
                    self._build_cache(self._visual)
                    self._draw(self._visual)
            return False

        def _body_extent(self):
            return max(self.viewport.height(), self.body.height())

        def _cache_bounds_for(self, pos):
            view_h = max(1, self.viewport.height())
            body_h = max(view_h, self._body_extent())
            dpr = self.viewport.devicePixelRatioF() or 1.0

            # Whole-page cache when safe: no cache edge can ever be crossed, so
            # wheel motion after the initial upload is transform-only.
            if body_h * dpr <= self.FULL_CACHE_MAX_PHYSICAL_H:
                return 0, body_h

            wanted = min(body_h, max(view_h, int(round(view_h * self.CACHE_VIEWS))))
            # Put more runway in the likely direction by centering the current
            # position in the larger window instead of placing it one viewport
            # from the top as the old 3-view cache did.
            top = int(round(float(pos) - 0.5 * (wanted - view_h)))
            top = max(0, min(top, max(0, body_h - wanted)))
            return top, wanted

        def _build_cache(self, pos):
            if self.viewport.width() <= 0 or self.viewport.height() <= 0:
                self.texture.clear_image()
                self._cache_height = 0
                return

            top, height = self._cache_bounds_for(pos)
            width = max(1, self.body.width(), self.viewport.width())
            dpr = self.viewport.devicePixelRatioF() or 1.0
            physical_w = max(1, int(round(width * dpr)))
            physical_h = max(1, int(round(height * dpr)))
            image = QImage(physical_w, physical_h,
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(self._ground)

            was_enabled = self.body.updatesEnabled()
            if not was_enabled:
                self.body.setUpdatesEnabled(True)
            painter = QPainter(image)
            region = QRegion(QRect(0, top, width, height))
            flags = (QWidget.RenderFlag.DrawWindowBackground
                     | QWidget.RenderFlag.DrawChildren)
            self.body.render(painter, QPoint(0, -top), region, flags)
            painter.end()
            if self._active or not was_enabled:
                self.body.setUpdatesEnabled(False)

            self._cache_top = float(top)
            self._cache_height = height
            self.texture.set_image(image, width, height)

        def _contains(self, pos):
            if self._cache_height <= 0:
                return False
            view_h = self.viewport.height()
            start = float(pos)
            end = start + view_h
            lo = self._cache_top + self.EDGE_SLOP
            hi = self._cache_top + self._cache_height - self.EDGE_SLOP
            if self._cache_top <= 0.0:
                lo = self._cache_top
            if self._cache_top + self._cache_height >= self._body_extent():
                hi = self._cache_top + self._cache_height
            return start >= lo and end <= hi

        def begin(self, pos, motion):
            if self._active:
                self._ending_after_swap = False
                self._motion = motion
                self._visual = float(pos)
                self._draw(self._visual)
                self.quick.update()
                return
            self._active = True
            self._ending_after_swap = False
            self._motion = motion
            self._visual = float(pos)
            self._build_cache(self._visual)
            self.body.setUpdatesEnabled(False)
            self.container.setGeometry(self.viewport.rect())
            self.container.show()
            self.container.raise_()
            self._draw(self._visual)
            self.quick.update()

        def present(self, pos):
            if not self._active:
                return
            self._visual = float(pos)
            if not self._contains(self._visual):
                self._build_cache(self._visual)
            self._draw(self._visual)
            self.quick.update()

        def _draw(self, pos):
            if self._cache_height <= 0:
                return
            self.texture.setY(-(float(pos) - self._cache_top))

        def _finish_end(self):
            if not self._active:
                return
            self._ending_after_swap = False
            self._active = False
            self._motion = None
            self.body.setUpdatesEnabled(True)
            self.container.hide()
            self.texture.clear_image()
            self.viewport.update()
            self._cache_height = 0

        def _frame_swapped(self):
            if not self._active:
                return
            if self._ending_after_swap:
                self._finish_end()
                return
            motion = self._motion
            if motion is None:
                return
            try:
                motion._tick()
            except RuntimeError:
                self._finish_end()
                return
            if self._active:
                self.quick.update()

        def end(self, pos=None):
            if not self._active:
                return
            if pos is not None:
                self._visual = float(pos)
            self._motion = None
            self._ending_after_swap = True
            self._draw(self._visual)
            self.body.setUpdatesEnabled(True)
            self.viewport.update()
            self.quick.update()

    class NativeQuickScrollArea(QScrollArea):
        def __init__(self, ground, parent=None):
            super().__init__(parent)
            self._atomic_ground = ground
            self._atomic_compositor = None

        def set_atomic_body(self, body):
            self._atomic_compositor = _QuickCompositor(
                self, body, self._atomic_ground)

    old_active = w._Momentum.active
    old_start = w._Momentum._start_ticking
    old_stop = w._Momentum._stop_ticking
    old_tick = w._Momentum._tick

    def _surface_for(motion):
        return getattr(motion._bar, "_atomic_motion_surface", None)

    def patched_active(self):
        surface = _surface_for(self)
        if surface is not None and surface._active and not surface._ending_after_swap:
            return True
        return old_active(self)

    def patched_start(self):
        surface = _surface_for(self)
        if surface is None:
            return old_start(self)
        if not getattr(self, "_counted", False):
            self._counted = True
            w._GLIDING += 1
        pos = self._pos if self._pos is not None else self._bar.value()
        surface.begin(pos, self)

    def patched_stop(self):
        surface = _surface_for(self)
        if surface is None:
            return old_stop(self)
        if getattr(self, "_counted", False):
            self._counted = False
            w._GLIDING = max(0, w._GLIDING - 1)
        self._timer.stop()
        if getattr(self, "_vblank_on", False):
            try:
                ticker = w._vblank_ticker_for_use()
                if ticker is not None:
                    ticker.tick.disconnect(self._tick)
                    ticker.release()
            except Exception:
                pass
            self._vblank_on = False
        pos = self._pos if self._pos is not None else self._bar.value()
        surface.end(pos)

    def patched_tick(self):
        result = old_tick(self)
        surface = _surface_for(self)
        if (surface is not None and surface._active
                and not surface._ending_after_swap):
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.present(pos)
        return result

    w._Momentum.active = patched_active
    w._Momentum._start_ticking = patched_start
    w._Momentum._stop_ticking = patched_stop
    w._Momentum._tick = patched_tick

    def native_scroll_area(body: QWidget, always_show_vbar: bool = False,
                           ground: str = None, notch_scale: float = 1.0):
        area = NativeQuickScrollArea(ground or w.theme.BG)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if always_show_vbar:
            area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        area.setWidget(body)
        if ground:
            w._OpaqueGround(body, ground)
        area.notch_scale = float(notch_scale or 1.0)
        area.set_atomic_body(body)
        w._SmoothWheel(area)
        return area

    w.scroll_area = native_scroll_area


class _WidgetsPatchLoader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch_widgets(module)


class _WidgetsPatchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _WidgetsPatchLoader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch_widgets(module)
        return
    sys.meta_path.insert(0, _WidgetsPatchFinder())
