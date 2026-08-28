"""Render-thread wheel presentation for Atomic's main-window scroll surfaces.

Why this exists
---------------
The remaining high-refresh judder is architectural, not another easing constant.
A QWidget scrollbar value is a GUI-thread property, so producing one value per
240 Hz refresh still asks Python + Qt Widgets to meet a 4.17 ms deadline.  The
browser reference does not do that: once a scroll layer exists, its compositor
moves the layer independently of the main/UI thread.

Atomic already proved the same idea with its temporary Qt Quick snapshots, but
that implementation still called Python `_Momentum._tick()` from
QQuickWindow.frameSwapped.  The texture was on the GPU; the *position producer*
was not.

This patch keeps every working QWidget page intact and adds one reusable Quick
surface over the active main-window viewport.  On an angle-wheel gesture:

    QWidget body --paint once--> DPR-correct QImage
                              -> QQuickPaintedItem
                              -> QML YAnimator (render thread)
                              -> vsync

The real scrollbar/body is committed once at the end.  A tiny 60 Hz shadow
update keeps the visible scrollbar thumb near the compositor without moving the
real body (signals are blocked).  There is no Python callback per presented
content frame.

Safety / scope
--------------
* Only opaque main-window vertical surfaces opt in.  Dialogs/translucent areas
  keep the existing path.
* Home and Discover keep their page-owned Quick compositors detached; this is a
  single shared overlay, so the nested-row bugs that page-wide ownership caused
  do not return.
* Reader opts in explicitly after construction; its loading/slot/zoom logic is
  untouched.
* If QML/YAnimator or the snapshot setup fails for any reason, `kick()` falls
  straight through to the already-working motion implementation.
* The monitor/DPR patches are independent.  Every snapshot is cut at the
  viewport's current DPR when the gesture starts.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import math
import sys
import time

_INSTALLED = False
_READER_PATCHED = False
_TARGET_READER = "windows.reader"


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return 1.0 - (1.0 - t) ** 3


def _patch_widgets(w):
    if getattr(w, "_atomic_render_thread_scroll_patched", False):
        return
    w._atomic_render_thread_scroll_patched = True

    from PyQt6.QtCore import (QByteArray, QEvent, QObject, QPoint, QRect,
                              QSignalBlocker, QTimer, QUrl, Qt)
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQml import QQmlComponent, QQmlEngine
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow
    from PyQt6.QtWidgets import QWidget

    class _Texture(QQuickPaintedItem):
        """One image upload; YAnimator moves the scene-graph node afterward."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._image = QImage()
            self.setAntialiasing(False)
            self.setMipmap(False)
            self.setOpaquePainting(True)
            # Fractional movement is intentional.  Smooth sampling avoids the
            # one-physical-pixel shimmer nearest-neighbour movement produced in
            # the earlier clarity experiment.
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
            painter.drawImage(0.0, 0.0, self._image)

    # One QML engine/component for every scroll surface.  Creating an engine per
    # page would add exactly the kind of hidden per-page machinery this patch is
    # removing.
    engine = QQmlEngine()
    component = QQmlComponent(engine)
    component.setData(
        QByteArray(
            b"import QtQuick\n"
            b"YAnimator { easing.type: Easing.OutCubic }\n"
        ),
        QUrl("atomic:render-thread-scroll"),
    )
    # Keep these alive for the process lifetime.  If component creation later
    # fails, the individual gesture simply falls back to the existing path.
    w._atomic_scroll_qml_engine = engine
    w._atomic_scroll_animator_component = component

    class _RenderThreadOverlay(QObject):
        """The single visible Quick scroll layer for the main window."""

        # About two viewports: enough headroom for a normal wheel burst without
        # making a 2560x1440@125% snapshot enormous.  The cache is asymmetric in
        # the gesture direction, so most of those pixels are where the hand is
        # actually travelling.
        CACHE_VIEWS = 2.15
        BACK_VIEWS = 0.28
        EDGE_SLOP = 96
        THUMB_MS = 16

        def __init__(self):
            super().__init__()
            self.quick = None
            self.texture = None
            self.container = None
            self.animator = None
            self.top = None
            self.area = None
            self.body = None
            self.bar = None
            self.motion = None
            self.ground = QColor("#000000")
            self.cache_top = 0.0
            self.cache_height = 0
            self.committed = 0
            self.start_value = 0.0
            self.target_value = 0.0
            self.started_at = 0.0
            self.duration_s = 0.0
            self.direction = 0
            self.running = False
            self.retargeting = False
            self._thumb = QTimer(self)
            self._thumb.setTimerType(Qt.TimerType.PreciseTimer)
            self._thumb.setInterval(self.THUMB_MS)
            self._thumb.timeout.connect(self._shadow_thumb)

        def _ensure_quick(self, top) -> bool:
            try:
                if self.quick is not None:
                    # Atomic has one main QMainWindow.  Do not reparent a native
                    # child into a dialog: those keep the proven QWidget path.
                    return self.top is top

                quick = QQuickWindow()
                quick.setColor(self.ground)
                try:
                    quick.setFlag(Qt.WindowType.WindowTransparentForInput, True)
                except Exception:
                    pass
                texture = _Texture(quick.contentItem())
                container = QWidget.createWindowContainer(quick, top)
                container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                                       True)
                container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                container.hide()

                animator = component.create()
                if animator is None:
                    container.hide()
                    container.deleteLater()
                    try:
                        quick.deleteLater()
                    except Exception:
                        pass
                    return False
                animator.setParent(self)
                if not animator.setProperty("target", texture):
                    animator.deleteLater()
                    container.hide()
                    container.deleteLater()
                    return False
                animator.finished.connect(self._animation_finished)

                self.quick = quick
                self.texture = texture
                self.container = container
                self.animator = animator
                self.top = top
                # A window move while content is in flight should never carry a
                # DPR-specific snapshot across monitors.  Commit where it is and
                # reveal live widgets before Windows begins the migration.
                top.installEventFilter(self)
                return True
            except Exception:
                return False

        def eventFilter(self, obj, event):
            if obj is self.top and event.type() in (
                    QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Hide):
                if self.running:
                    try:
                        self.cancel(commit_current=True)
                    except Exception:
                        self._hard_hide()
            return False

        def _hard_hide(self):
            self._thumb.stop()
            self.running = False
            self.retargeting = False
            try:
                if self.animator is not None:
                    self.animator.setProperty("running", False)
            except Exception:
                pass
            try:
                if self.body is not None:
                    self.body.setUpdatesEnabled(True)
            except RuntimeError:
                pass
            try:
                if self.container is not None:
                    self.container.hide()
            except RuntimeError:
                pass
            self.area = self.body = self.bar = self.motion = None

        @staticmethod
        def _top_for(area):
            try:
                top = area.window()
                # This compositor is deliberately the application's main-window
                # path.  Modal/tool windows retain the old behavior.
                if top is None or not top.inherits("QMainWindow"):
                    return None
                return top
            except RuntimeError:
                return None

        def _geometry_for(self, area, top):
            viewport = area.viewport()
            point = viewport.mapTo(top, QPoint(0, 0))
            return QRect(point, viewport.size())

        def _capture(self, area, body, ground, current, direction) -> bool:
            viewport = area.viewport()
            if viewport.width() <= 0 or viewport.height() <= 0:
                return False
            view_h = max(1, viewport.height())
            body_h = max(view_h, body.height())
            width = max(1, body.width(), viewport.width())

            # Keep a little history behind the current position and put the rest
            # ahead.  For an upward gesture the shape is mirrored.
            total_h = min(body_h, max(view_h, int(round(view_h * self.CACHE_VIEWS))))
            back = int(round(view_h * self.BACK_VIEWS))
            if direction >= 0:
                top = int(round(current)) - back
            else:
                top = int(round(current)) - (total_h - view_h - back)
            top = max(0, min(top, max(0, body_h - total_h)))
            height = min(total_h, body_h - top)

            dpr = float(viewport.devicePixelRatioF() or 1.0)
            physical_w = max(1, int(round(width * dpr)))
            physical_h = max(1, int(round(height * dpr)))
            image = QImage(physical_w, physical_h,
                           QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(QColor(ground))

            painter = QPainter(image)
            region = QRegion(QRect(0, top, width, height))
            flags = (QWidget.RenderFlag.DrawWindowBackground
                     | QWidget.RenderFlag.DrawChildren)
            body.render(painter, QPoint(0, -top), region, flags)
            painter.end()
            if image.isNull():
                return False

            self.cache_top = float(top)
            self.cache_height = int(height)
            self.texture.set_image(image, width, height)
            return True

        def _contains(self, position) -> bool:
            if self.cache_height <= 0 or self.area is None:
                return False
            try:
                view_h = self.area.viewport().height()
            except RuntimeError:
                return False
            lo = self.cache_top
            hi = self.cache_top + self.cache_height
            start = float(position)
            end = start + view_h
            # Slop is only needed away from a real content edge.
            if lo > 0.0:
                lo += self.EDGE_SLOP
            try:
                body_h = max(view_h, self.body.height())
            except RuntimeError:
                body_h = int(hi)
            if hi < body_h:
                hi -= self.EDGE_SLOP
            return start >= lo and end <= hi

        def _current(self) -> float:
            if not self.running or self.duration_s <= 0.0:
                return float(self.target_value)
            elapsed = max(0.0, time.monotonic() - self.started_at)
            t = min(1.0, elapsed / self.duration_s)
            eased = _ease_out_cubic(t)
            return self.start_value + (self.target_value - self.start_value) * eased

        def _shadow_value(self, value):
            bar = self.bar
            if bar is None:
                return
            low, high = bar.minimum(), bar.maximum()
            value = int(round(max(low, min(high, float(value)))))
            blocker = QSignalBlocker(bar)
            bar.setValue(value)
            blocker.unblock()

        def _shadow_thumb(self):
            if not self.running:
                self._thumb.stop()
                return
            try:
                self._shadow_value(self._current())
            except RuntimeError:
                self._hard_hide()

        def _motion_started(self, motion):
            if not getattr(motion, "_counted", False):
                motion._counted = True
                w._GLIDING += 1
            # This path owns the wheel now.  Clear leftovers from an earlier
            # fallback glide so cancel/next-input cannot revive old velocity.
            motion._vel = 0.0
            motion._pending = 0.0
            motion._pos = None
            motion._phase = None
            motion._kicks.clear()

        def _motion_finished(self, motion, final_value):
            if motion is None:
                return
            if getattr(motion, "_counted", False):
                motion._counted = False
                w._GLIDING = max(0, w._GLIDING - 1)
            motion._vel = 0.0
            motion._pending = 0.0
            motion._pos = None
            motion._phase = None
            motion._last_value = int(round(final_value))
            motion._kicks.clear()

        def _commit(self, value):
            bar = self.bar
            body = self.body
            if bar is None:
                return
            low, high = bar.minimum(), bar.maximum()
            final_value = int(round(max(low, min(high, float(value)))))
            # The visible thumb has been shadow-updated with signals blocked.
            # Restore the real body's old value silently, then perform one normal
            # final write so QScrollArea/Reader synchronize exactly once.
            blocker = QSignalBlocker(bar)
            bar.setValue(int(self.committed))
            blocker.unblock()
            try:
                if body is not None:
                    body.setUpdatesEnabled(True)
            except RuntimeError:
                pass
            if bar.value() != final_value:
                bar.setValue(final_value)
            return final_value

        def _duration_ms(self, current, target) -> int:
            # Retargeting adds distance, not queued animations.  A normal notch
            # lands in ~130 ms; a hard burst gets a little longer rather than a
            # much higher per-frame speed.  The render thread supplies all
            # intermediate positions at the panel cadence.
            distance = abs(float(target) - float(current))
            return int(max(105.0, min(220.0, 108.0 + distance * 0.38)))

        def kick(self, area, body, ground, motion, distance, direction) -> bool:
            try:
                top = self._top_for(area)
                if top is None:
                    return False
                if not self._ensure_quick(top):
                    return False

                bar = area.verticalScrollBar()
                if bar.maximum() <= bar.minimum():
                    return False

                # One overlay at a time.  A wheel gesture on another surface
                # commits the old one where the eye currently sees it first.
                if self.running and self.area is not area:
                    self.cancel(commit_current=True)

                if self.running:
                    current = self._current()
                    previous_target = float(self.target_value)
                else:
                    current = float(bar.value())
                    previous_target = current
                    self.committed = int(bar.value())

                direction = 1 if direction > 0 else -1
                old_delta = previous_target - current
                if self.running and old_delta * direction > 0.0:
                    target = previous_target + direction * float(distance)
                else:
                    # Reversal answers from what is actually on screen now, not
                    # from the destination of the animation being abandoned.
                    target = current + direction * float(distance)
                target = max(float(bar.minimum()), min(float(bar.maximum()), target))
                if abs(target - current) < 0.01:
                    return True

                self.area = area
                self.body = body
                self.bar = bar
                self.motion = motion
                self.ground = QColor(ground)
                self.quick.setColor(self.ground)
                self.container.setGeometry(self._geometry_for(area, top))

                # Stop the old render-thread animation before changing its image
                # or origin.  `finished` from a stop/retarget is ignored by the
                # guard; only the final uninterrupted run commits the QWidget.
                self.retargeting = True
                if self.running:
                    self.animator.setProperty("running", False)
                self.running = False

                if (self.cache_height <= 0 or not self._contains(current)
                        or not self._contains(target)
                        or self.direction != direction):
                    if not self._capture(area, body, self.ground, current, direction):
                        self.retargeting = False
                        return False

                body.setUpdatesEnabled(False)
                self.container.show()
                self.container.raise_()

                self.start_value = float(current)
                self.target_value = float(target)
                self.direction = direction
                duration_ms = self._duration_ms(current, target)
                self.duration_s = duration_ms / 1000.0
                self.started_at = time.monotonic()

                start_y = -(self.start_value - self.cache_top)
                target_y = -(self.target_value - self.cache_top)
                self.texture.setY(float(start_y))
                self.animator.setProperty("target", self.texture)
                self.animator.setProperty("from", float(start_y))
                self.animator.setProperty("to", float(target_y))
                self.animator.setProperty("duration", int(duration_ms))

                self._motion_started(motion)
                motion._atomic_rt_active = True
                self.running = True
                self.retargeting = False
                self.animator.setProperty("running", True)
                self._thumb.start()
                return True
            except Exception:
                # Restore anything frozen before letting the caller fall back.
                self.retargeting = False
                try:
                    if self.body is not None:
                        self.body.setUpdatesEnabled(True)
                except RuntimeError:
                    pass
                self._hard_hide()
                return False

        def _animation_finished(self):
            if self.retargeting or not self.running:
                return
            self.running = False
            self._thumb.stop()
            motion = self.motion
            try:
                final_value = self._commit(self.target_value)
                if final_value is None:
                    final_value = self.target_value
                self._motion_finished(motion, final_value)
                if motion is not None:
                    motion._atomic_rt_active = False
                # Keep the texture at the exact final position until the live
                # viewport has been invalidated, then drop the overlay on the
                # next event-loop turn.  No blank handoff frame.
                try:
                    self.texture.setY(-(float(final_value) - self.cache_top))
                    self.area.viewport().update()
                except RuntimeError:
                    pass

                def reveal():
                    try:
                        if self.container is not None:
                            self.container.hide()
                        if self.texture is not None:
                            self.texture.clear_image()
                    except RuntimeError:
                        pass
                    self.area = self.body = self.bar = self.motion = None
                    self.cache_height = 0

                QTimer.singleShot(0, reveal)
            except RuntimeError:
                if motion is not None:
                    motion._atomic_rt_active = False
                    self._motion_finished(motion, self.target_value)
                self._hard_hide()

        def cancel(self, commit_current=True):
            if not self.running:
                return False
            current = self._current()
            motion = self.motion
            self.retargeting = True
            try:
                self.animator.setProperty("running", False)
            except Exception:
                pass
            self.running = False
            self._thumb.stop()
            if commit_current:
                try:
                    final_value = self._commit(current)
                except RuntimeError:
                    final_value = current
            else:
                final_value = float(self.committed)
                try:
                    if self.body is not None:
                        self.body.setUpdatesEnabled(True)
                except RuntimeError:
                    pass
            self._motion_finished(motion, final_value)
            if motion is not None:
                motion._atomic_rt_active = False
            try:
                if self.container is not None:
                    self.container.hide()
                if self.texture is not None:
                    self.texture.clear_image()
                if self.area is not None:
                    self.area.viewport().update()
            except RuntimeError:
                pass
            self.area = self.body = self.bar = self.motion = None
            self.cache_height = 0
            self.retargeting = False
            return True

    overlay = _RenderThreadOverlay()
    w._atomic_render_thread_overlay = overlay

    # Mark scroll_area() surfaces without changing how they are built.  Their
    # existing per-area compositor remains available for scrollbar dragging and
    # as the fallback; wheel glides below use the one shared overlay instead.
    old_scroll_area = w.scroll_area

    def render_thread_scroll_area(body, always_show_vbar=False, ground=None,
                                  notch_scale=1.0):
        area = old_scroll_area(body, always_show_vbar=always_show_vbar,
                               ground=ground, notch_scale=notch_scale)
        if ground:
            try:
                bar = area.verticalScrollBar()
                bar._atomic_rt_area = area
                bar._atomic_rt_body = body
                bar._atomic_rt_ground = ground
            except RuntimeError:
                pass
        return area

    w.scroll_area = render_thread_scroll_area

    old_active = w._Momentum.active
    old_kick = w._Momentum.kick
    old_cancel = w._Momentum.cancel

    def rt_active(self):
        if getattr(self, "_atomic_rt_active", False):
            return True
        return old_active(self)

    def rt_kick(self, distance_px, direction):
        if getattr(self, "_follow", None) is None:
            bar = getattr(self, "_bar", None)
            area = getattr(bar, "_atomic_rt_area", None)
            body = getattr(bar, "_atomic_rt_body", None)
            ground = getattr(bar, "_atomic_rt_ground", None)
            if area is not None and body is not None and ground is not None:
                if overlay.kick(area, body, ground, self, distance_px, direction):
                    return
        return old_kick(self, distance_px, direction)

    def rt_cancel(self):
        if getattr(self, "_atomic_rt_active", False):
            try:
                overlay.cancel(commit_current=True)
            except Exception:
                self._atomic_rt_active = False
        return old_cancel(self)

    w._Momentum.active = rt_active
    w._Momentum.kick = rt_kick
    w._Momentum.cancel = rt_cancel

    # Exposed narrowly for the reader patch below; no page imports this symbol.
    def mark_surface(area, body, ground):
        try:
            bar = area.verticalScrollBar()
            bar._atomic_rt_area = area
            bar._atomic_rt_body = body
            bar._atomic_rt_ground = ground
            return True
        except RuntimeError:
            return False

    w._atomic_mark_render_thread_surface = mark_surface


def _patch_reader(module):
    global _READER_PATCHED
    if _READER_PATCHED:
        return
    cls = getattr(module, "_StripView", None)
    if cls is None:
        return
    _READER_PATCHED = True
    old_init = cls.__init__

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        try:
            from . import widgets
            marker = getattr(widgets, "_atomic_mark_render_thread_surface", None)
            if marker is not None:
                marker(self, self._body, module.theme.BG)
        except Exception:
            # Reader remains on its already-working self-painted path.
            pass

    cls.__init__ = init


class _ReaderLoader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch_reader(module)


class _ReaderFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET_READER:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _ReaderLoader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Loading widgets here is intentional.  All lower-level patches have already
    # installed their import hooks, so this resolves the final shared motion API
    # once and lets us wrap it without adding another competing widgets finder.
    import importlib
    widgets = importlib.import_module("helpers.widgets")
    _patch_widgets(widgets)

    reader = sys.modules.get(_TARGET_READER)
    if reader is not None:
        _patch_reader(reader)
    else:
        sys.meta_path.insert(0, _ReaderFinder())
