"""Experimental native-refresh scroll compositor for Atomic.

Installed as a post-import patch for helpers.widgets so ordinary QWidget
scroll areas can present motion from one cached raster surface instead of
repainting/moving the whole child tree on every refresh.

This is deliberately raster-only. Do not replace it with QOpenGLWidget:
Atomic's libmpv player owns a native child window and the repository has
already measured that combination as unsafe.
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
    """Patch helpers.widgets once, after its normal module body ran."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRegion, QSize, Qt
    from PyQt6.QtGui import QColor, QPainter, QPixmap
    from PyQt6.QtWidgets import QFrame, QScrollArea, QWidget

    original_present_frame_s = w.present_frame_s

    def native_present_frame_s(widget=None) -> float:
        """Commit one visual position per refresh of the active screen."""
        if os.environ.get("ATOMIC_PRESENT_HZ"):
            return original_present_frame_s(widget)
        return w.screen_frame_s(widget)

    w.present_frame_s = native_present_frame_s

    class _RasterLayer(QWidget):
        """Viewport-sized layer that displays one cached page raster."""

        def __init__(self, viewport, ground):
            super().__init__(viewport)
            self._pixmap = None
            self._offset = 0.0
            self._ground = QColor(ground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            self.hide()

        def set_frame(self, pixmap, offset):
            self._pixmap = pixmap
            self._offset = float(offset)
            if not self.isVisible():
                self.show()
                self.raise_()
            self.update()

        def clear_frame(self):
            self._pixmap = None
            self.hide()

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(event.rect(), self._ground)
            pixmap = self._pixmap
            if pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(QPointF(0.0, -self._offset), pixmap)
            painter.end()

    class _RasterCompositor(QObject):
        """Move one rasterized page surface during wheel motion.

        This restores the successful one-surface idea from the earlier motion
        work: cache the complete page when practical, otherwise cache a large
        eight-viewport runway. The old 2.5-view window could cross a cache edge
        during a normal glide and synchronously render the whole child tree in
        the middle of motion, which is exactly the hitch this compositor exists
        to avoid.
        """

        CACHE_VIEWS = 8.0
        EDGE_SLOP = 192.0
        FULL_CACHE_MAX_PHYSICAL_H = 8192

        def __init__(self, area, body, ground):
            super().__init__(area)
            self.area = area
            self.body = body
            self.viewport = area.viewport()
            self.layer = _RasterLayer(self.viewport, ground)
            self._cache = None
            self._cache_top = 0.0
            self._cache_height = 0
            self._active = False
            self._visual = float(area.verticalScrollBar().value())
            self.viewport.installEventFilter(self)
            area.verticalScrollBar()._atomic_motion_surface = self

        def eventFilter(self, obj, event):
            if obj is self.viewport and event.type() == QEvent.Type.Resize:
                self.layer.setGeometry(self.viewport.rect())
                self._cache = None
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

            # Home/Main, Discover, and many History/Schedule instances fit here.
            # In that case every card/label is rasterized into one page surface
            # before movement begins and no child widget is repainted mid-glide.
            if body_h * dpr <= self.FULL_CACHE_MAX_PHYSICAL_H:
                return 0, body_h

            wanted = min(body_h, max(view_h, int(round(view_h * self.CACHE_VIEWS))))
            top = int(round(float(pos) - 0.5 * (wanted - view_h)))
            top = max(0, min(top, max(0, body_h - wanted)))
            return top, wanted

        def _build_cache(self, pos):
            if self.viewport.width() <= 0 or self.viewport.height() <= 0:
                self._cache = None
                return
            top, height = self._cache_bounds_for(pos)
            width = max(1, self.body.width(), self.viewport.width())
            dpr = self.viewport.devicePixelRatioF() or 1.0
            physical = QSize(max(1, int(round(width * dpr))),
                             max(1, int(round(height * dpr))))
            pixmap = QPixmap(physical)
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)

            was_enabled = self.body.updatesEnabled()
            if not was_enabled:
                self.body.setUpdatesEnabled(True)
            painter = QPainter(pixmap)
            region = QRegion(QRect(0, top, width, height))
            flags = QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.DrawChildren
            self.body.render(painter, QPoint(0, -top), region, flags)
            painter.end()
            if self._active:
                self.body.setUpdatesEnabled(False)
            elif not was_enabled:
                self.body.setUpdatesEnabled(False)

            self._cache = pixmap
            self._cache_top = float(top)
            self._cache_height = height
            self.layer.setGeometry(self.viewport.rect())

        def _contains(self, pos):
            if self._cache is None or self._cache.isNull():
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

        def begin(self, pos):
            if self._active:
                return
            self._active = True
            self._visual = float(pos)
            self._build_cache(self._visual)
            self.body.setUpdatesEnabled(False)
            self._draw(self._visual)

        def present(self, pos):
            if not self._active:
                return
            self._visual = float(pos)
            if not self._contains(self._visual):
                self._build_cache(self._visual)
            self._draw(self._visual)

        def _draw(self, pos):
            if self._cache is None or self._cache.isNull():
                return
            dpr = self.viewport.devicePixelRatioF() or 1.0
            visual = round(float(pos) * dpr) / dpr
            self.layer.set_frame(self._cache, visual - self._cache_top)

        def end(self, pos=None):
            if not self._active:
                return
            self._active = False
            if pos is not None:
                self._visual = float(pos)
            self.body.setUpdatesEnabled(True)
            self.layer.clear_frame()
            self.viewport.update()
            self._cache = None

    class NativeRasterScrollArea(QScrollArea):
        """QScrollArea with a raster presentation layer during motion."""

        def __init__(self, ground, parent=None):
            super().__init__(parent)
            self._atomic_ground = ground
            self._atomic_compositor = None

        def set_atomic_body(self, body):
            self._atomic_compositor = _RasterCompositor(
                self, body, self._atomic_ground)

    old_start = w._Momentum._start_ticking
    old_stop = w._Momentum._stop_ticking
    old_tick = w._Momentum._tick

    def _surface_for(motion):
        return getattr(motion._bar, "_atomic_motion_surface", None)

    def patched_start(self):
        surface = _surface_for(self)
        if surface is not None and not surface._active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.begin(pos)
        return old_start(self)

    def patched_stop(self):
        surface = _surface_for(self)
        result = old_stop(self)
        if surface is not None:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.end(pos)
        return result

    def patched_tick(self):
        result = old_tick(self)
        surface = _surface_for(self)
        if surface is not None and surface._active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.present(pos)
        return result

    w._Momentum._start_ticking = patched_start
    w._Momentum._stop_ticking = patched_stop
    w._Momentum._tick = patched_tick

    def native_scroll_area(body: QWidget, always_show_vbar: bool = False,
                           ground: str = None, notch_scale: float = 1.0):
        """The normal Atomic scroll area, with native-refresh raster motion."""
        area = NativeRasterScrollArea(ground or w.theme.BG)
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
    """Install the post-import patch without eagerly importing PyQt widgets."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch_widgets(module)
        return
    sys.meta_path.insert(0, _WidgetsPatchFinder())
