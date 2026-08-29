"""Qt Quick wheel compositor for small vertical PosterGrid surfaces.

Small grids can cache their already-composed cards once and move that cache as
one Qt Quick scene-graph texture during wheel momentum. The successful 240 Hz
A/B proved this architecture can make motion genuinely smooth.

Do not embed the QQuickWindow with createWindowContainer(). Atomic's first Quick
experiment did that and the resulting native-child promotion destabilised the
widgets around Home/Discover/Saved/History. The compositor below is instead a
mouse-transparent transient top-level window positioned exactly over the grid
only while wheel momentum is active. The QWidget hierarchy remains untouched.

165 Hz uses the same path as 240 Hz. 144 Hz and below remain unchanged until
separately measured.

The cached grid is already painted at native device-pixel ratio. Texture
smoothing is intentionally disabled: bilinear sampling of poster edges and text
at a new fractional phase every refresh makes individual cards appear to
shimmer even though the overall trajectory is smooth.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import time

_TARGET = "helpers.poster_grid"
_INSTALLED = False
_PATCHED = False


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QObject, QPoint, QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow

    PosterGrid = module.PosterGrid

    class _GridTexture(QQuickPaintedItem):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._image = QImage()
            self.setAntialiasing(False)
            self.setMipmap(False)
            self.setOpaquePainting(True)
            self.setSmooth(False)

        def set_image(self, image, width, height):
            self._image = image
            self.setWidth(float(width))
            self.setHeight(float(height))
            self.update()

        def clear(self):
            self._image = QImage()
            self.update()

        def paint(self, painter):
            if self._image.isNull():
                return
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawImage(QPointF(0.0, 0.0), self._image)

    class _GridBar(QQuickPaintedItem):
        def __init__(self, grid, parent=None):
            super().__init__(parent)
            self.grid = grid
            self._pos = 0.0
            self.setAntialiasing(True)

        def sync_geometry(self):
            self.setX(float(max(0, self.grid.width() - module.BAR_WIDTH)))
            self.setY(0.0)
            self.setWidth(float(module.BAR_WIDTH))
            self.setHeight(float(max(1, self.grid.height())))

        def set_position(self, pos):
            self._pos = float(pos)
            self.update()

        def paint(self, painter):
            grid = self.grid
            if not grid._overflowing():
                return
            track_h = grid.height() - 2 * module.BAR_MARGIN
            if track_h <= 0:
                return
            content = max(1.0, float(grid._content_h))
            thumb_h = max(module.BAR_MIN_THUMB,
                          track_h * grid.height() / content)
            span = track_h - thumb_h
            maximum = float(grid._motion.maximum)
            travel = self._pos / maximum if maximum > 0.0 else 0.0
            top = module.BAR_MARGIN + span * max(0.0, min(1.0, travel))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(module.theme.SURFACE_HOVER))
            painter.drawRoundedRect(
                QRectF(0.0, float(top), float(module.BAR_WIDTH), float(thumb_h)),
                float(module.BAR_RADIUS), float(module.BAR_RADIUS))

    class _GridQuickSurface(QObject):
        HIGH_REFRESH_HZ = 150.0
        MAX_RECORDS = 80
        MAX_PHYSICAL_H = 8192
        MAX_PHYSICAL_W = 8192
        MAX_PHYSICAL_PIXELS = 12_000_000

        def __init__(self, grid):
            super().__init__(grid)
            self.grid = grid
            self._active = False
            self._ending_after_swap = False
            self._dirty = True
            self.quick = None
            self.texture = None
            self.bar = None

        def high_refresh(self):
            try:
                screen = self.grid.screen()
                return (screen is not None
                        and float(screen.refreshRate()) >= self.HIGH_REFRESH_HZ)
            except Exception:
                return False

        def can_use(self):
            grid = self.grid
            if not self.high_refresh() or type(grid) is not PosterGrid:
                return False
            if not grid._records or len(grid._records) > self.MAX_RECORDS:
                return False
            if grid.width() <= 0 or grid.height() <= 0 or grid._content_h <= 0:
                return False
            try:
                dpr = float(grid.devicePixelRatioF() or 1.0)
            except Exception:
                dpr = 1.0
            pw = int(round(grid.width() * dpr))
            ph = int(round(grid._content_h * dpr))
            return (pw <= self.MAX_PHYSICAL_W
                    and ph <= self.MAX_PHYSICAL_H
                    and pw * ph <= self.MAX_PHYSICAL_PIXELS)

        def _ensure_quick(self):
            if self.quick is not None:
                return True
            try:
                quick = QQuickWindow()
                quick.setColor(self.grid._ground)
                quick.setFlags(
                    Qt.WindowType.Tool
                    | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowDoesNotAcceptFocus
                    | Qt.WindowType.WindowTransparentForInput)
                try:
                    top = self.grid.window()
                    handle = top.windowHandle() if top is not None else None
                    if handle is not None:
                        quick.setTransientParent(handle)
                except (AttributeError, RuntimeError):
                    pass

                texture = _GridTexture(quick.contentItem())
                bar = _GridBar(self.grid, quick.contentItem())
                quick.frameSwapped.connect(self._frame_swapped)
                self.quick = quick
                self.texture = texture
                self.bar = bar
                return True
            except Exception:
                self.quick = None
                self.texture = None
                self.bar = None
                return False

        def _place_overlay(self):
            if self.quick is None:
                return
            try:
                point = self.grid.mapToGlobal(QPoint(0, 0))
                self.quick.setGeometry(point.x(), point.y(),
                                       self.grid.width(), self.grid.height())
            except RuntimeError:
                pass

        def invalidate(self):
            self._dirty = True

        def _build(self):
            grid = self.grid
            if not self.can_use() or not self._ensure_quick():
                return False
            m = grid._ensure_metrics()
            width = max(1, grid.width())
            height = max(1, grid._content_h)
            dpr = float(grid.devicePixelRatioF() or 1.0)
            image = QImage(max(1, int(round(width * dpr))),
                           max(1, int(round(height * dpr))),
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(grid._ground)

            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            hover = grid._hover
            grid._hover = -1
            try:
                for index in range(len(grid._records)):
                    grid._paint_cell(
                        painter, m, index, QRectF(grid.cell_rect(index)))
            finally:
                grid._hover = hover
                painter.end()

            self.texture.set_image(image, width, height)
            self.bar.sync_geometry()
            self._dirty = False
            return True

        def begin(self):
            if self._active:
                self._ending_after_swap = False
                self._draw()
                self.quick.requestUpdate()
                return True
            if self._dirty and not self._build():
                return False
            if self._dirty or self.quick is None:
                return False
            self._active = True
            self._ending_after_swap = False
            self._place_overlay()
            self.bar.sync_geometry()
            self._draw()
            try:
                self.quick.show()
                self.quick.raise_()
                self.quick.requestUpdate()
            except RuntimeError:
                self.abort_to_live(stop_motion=False)
                return False
            return True

        def _draw(self):
            if self.texture is None or self.bar is None:
                return
            pos = float(self.grid._motion.pos)
            self.texture.setY(-pos)
            self.bar.set_position(pos)

        def _frame_swapped(self):
            if not self._active or self.quick is None:
                return
            if self._ending_after_swap:
                self._finish()
                return

            motion = self.grid._motion
            if not motion.running():
                self._ending_after_swap = True
                self._draw()
                self.quick.requestUpdate()
                return

            frame_s = float(self.grid._refresh_interval() or 0.0)
            if frame_s <= 0.0:
                frame_s = 1.0 / max(self.HIGH_REFRESH_HZ, 150.0)
            motion.frame_s = frame_s
            if motion._last is None:
                motion._last = time.perf_counter()
            moving = motion.step(motion._last + frame_s)
            self._draw()
            self.grid.scrolled.emit()
            if moving:
                self.quick.requestUpdate()
            else:
                self._ending_after_swap = True
                self.quick.requestUpdate()

        def _hide_overlay(self):
            if self.quick is not None:
                try:
                    self.quick.hide()
                except RuntimeError:
                    pass

        def _finish(self):
            if not self._active:
                return
            self._active = False
            self._ending_after_swap = False
            try:
                self.grid.repaint()
            except RuntimeError:
                pass
            self._hide_overlay()

        def abort_to_live(self, stop_motion=True):
            if not self._active:
                return
            if stop_motion:
                self.grid._motion.stop()
            self._active = False
            self._ending_after_swap = False
            try:
                self.grid.repaint()
            except RuntimeError:
                pass
            self._hide_overlay()

        def resize(self):
            self.invalidate()
            if self._active:
                self.abort_to_live(stop_motion=False)
            if self.bar is not None:
                self.bar.sync_geometry()

    old_init = PosterGrid.__init__
    old_wheel = PosterGrid.wheelEvent
    old_paint = PosterGrid.paintEvent
    old_resize = PosterGrid.resizeEvent
    old_set_items = PosterGrid.set_items
    old_append_items = PosterGrid.append_items
    old_set_cover = PosterGrid.set_cover
    old_mark_saved = PosterGrid.mark_saved
    old_mouse_press = PosterGrid.mousePressEvent
    old_hide = PosterGrid.hideEvent

    def patched_init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._atomic_grid_quick = None

    def _qualifies_for_quick(self):
        if type(self) is not PosterGrid:
            return False
        try:
            screen = self.screen()
            return screen is not None and float(screen.refreshRate()) >= 150.0
        except Exception:
            return False

    def patched_wheel(self, event):
        old_wheel(self, event)
        if not event.isAccepted() or not self._motion.running() \
                or self._drag_from is not None:
            return
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is None and _qualifies_for_quick(self):
            surface = _GridQuickSurface(self)
            self._atomic_grid_quick = surface
        if surface is not None:
            surface.begin()

    def patched_paint(self, event):
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None and surface._active:
            return
        return old_paint(self, event)

    def patched_resize(self, event):
        result = old_resize(self, event)
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None:
            surface.resize()
        return result

    def patched_set_items(self, records, keep_position=False):
        result = old_set_items(self, records, keep_position)
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None:
            surface.invalidate()
        return result

    def patched_append_items(self, records):
        result = old_append_items(self, records)
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None:
            surface.invalidate()
        return result

    def patched_set_cover(self, index, pixmap):
        result = old_set_cover(self, index, pixmap)
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None:
            surface.invalidate()
        return result

    def patched_mark_saved(self, index, saved=True):
        result = old_mark_saved(self, index, saved)
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None:
            surface.invalidate()
        return result

    def patched_mouse_press(self, event):
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None and surface._active:
            surface.abort_to_live(stop_motion=True)
        return old_mouse_press(self, event)

    def patched_hide(self, event):
        surface = getattr(self, "_atomic_grid_quick", None)
        if surface is not None and surface._active:
            surface.abort_to_live(stop_motion=True)
        return old_hide(self, event)

    PosterGrid.__init__ = patched_init
    PosterGrid.wheelEvent = patched_wheel
    PosterGrid.paintEvent = patched_paint
    PosterGrid.resizeEvent = patched_resize
    PosterGrid.set_items = patched_set_items
    PosterGrid.append_items = patched_append_items
    PosterGrid.set_cover = patched_set_cover
    PosterGrid.mark_saved = patched_mark_saved
    PosterGrid.mousePressEvent = patched_mouse_press
    PosterGrid.hideEvent = patched_hide


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    module = sys.modules.get(_TARGET)
    if module is not None:
        _patch(module)
        return
    sys.meta_path.insert(0, _Finder())
