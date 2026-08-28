"""Render-thread wheel presentation for Atomic's main-window scroll surfaces.

The remaining high-refresh judder is architectural: QWidget scrollbar values
live on the GUI thread, so asking Python + Qt Widgets to produce every 240 Hz
position still means meeting a 4.17 ms deadline. Browsers instead move an
already-rasterized layer in their compositor.

This patch keeps the existing QWidget pages and their current motion code as the
fallback, but adds one reusable Qt Quick overlay for opaque main-window vertical
scroll surfaces. A wheel gesture snapshots the content once, freezes the real
body, then a QML YAnimator moves the texture on Qt Quick's scene-graph render
thread. Python handles input/retargeting and the final scrollbar commit, not the
presented frames themselves.

Home/Discover keep their page-owned Quick compositors detached, Reader keeps its
existing loading/painting model, Movies is untouched, and monitor/DPR handling
stays in its existing patches. If the QML/Quick path cannot initialize, the
wheel event falls straight through to the proven current implementation.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import time

_INSTALLED = False
_READER_PATCHED = False
_TARGET_READER = "windows.reader"


def _ease_out_cubic(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return 1.0 - (1.0 - value) ** 3


def _patch_widgets(w):
    if getattr(w, "_atomic_render_thread_scroll_patched", False):
        return
    w._atomic_render_thread_scroll_patched = True

    from PyQt6.QtCore import (QByteArray, QEvent, QObject, QPoint, QPointF,
                              QRect, QSignalBlocker, QTimer, QUrl, Qt)
    from PyQt6.QtGui import QColor, QImage, QPainter, QRegion
    from PyQt6.QtQml import QQmlComponent, QQmlEngine
    from PyQt6.QtQuick import QQuickPaintedItem, QQuickWindow
    from PyQt6.QtWidgets import QApplication, QWidget

    class _Texture(QQuickPaintedItem):
        """Upload once; movement after that is a scene-graph transform."""

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

    qml_state = {"engine": None, "component": None, "failed": False}

    def _animator_component():
        """Create the QML runtime only after QApplication actually exists."""
        if qml_state["failed"]:
            return None
        if qml_state["component"] is not None:
            return qml_state["component"]
        if QApplication.instance() is None:
            return None
        try:
            engine = QQmlEngine()
            component = QQmlComponent(engine)
            component.setData(
                QByteArray(
                    b"import QtQuick\n"
                    b"YAnimator { easing.type: Easing.OutCubic }\n"
                ),
                QUrl("atomic:render-thread-scroll"),
            )
            # Creating one instance is the most reliable readiness check and
            # avoids depending on enum names that vary slightly across bindings.
            probe = component.create()
            if probe is None:
                qml_state["failed"] = True
                return None
            probe.deleteLater()
            qml_state["engine"] = engine
            qml_state["component"] = component
            w._atomic_scroll_qml_engine = engine
            w._atomic_scroll_animator_component = component
            return component
        except Exception:
            qml_state["failed"] = True
            return None

    class _RenderThreadOverlay(QObject):
        """Exactly one visible Quick scroll layer for the main window."""

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

        @staticmethod
        def _top_for(area):
            try:
                top = area.window()
                if top is None or not top.inherits("QMainWindow"):
                    return None
                return top
            except RuntimeError:
                return None

        def _ensure_quick(self, top) -> bool:
            try:
                if self.quick is not None:
                    # Never reparent a native Quick child into dialogs/tools.
                    return self.top is top

                component = _animator_component()
                if component is None:
                    return False

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
            self.cache_height = 0

        @staticmethod
        def _geometry_for(area, top):
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
            total_h = min(body_h,
                          max(view_h, int(round(view_h * self.CACHE_VIEWS))))
            back = int(round(view_h * self.BACK_VIEWS))
            if direction >= 0:
                top = int(round(current)) - back
            else:
                top = int(round(current)) - (total_h - view_h - back)
            top = max(0, min(top, max(0, body_h - total_h)))
            height = min(total_h, body_h - top)

            dpr = float(viewport.devicePixelRatioF() or 1.0)
            image = QImage(
                max(1, int(round(width * dpr))),
                max(1, int(round(height * dpr))),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
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
                body_h = max(view_h, self.body.height())
            except RuntimeError:
                return False
            lo = self.cache_top
            hi = self.cache_top + self.cache_height
            if lo > 0.0:
                lo += self.EDGE_SLOP
            if hi < body_h:
                hi -= self.EDGE_SLOP
            start = float(position)
            return start >= lo and start + view_h <= hi

        def _current(self) -> float:
            if not self.running or self.duration_s <= 0.0:
                return float(self.target_value)
            elapsed = max(0.0, time.monotonic() - self.started_at)
            t = min(1.0, elapsed / self.duration_s)
            return self.start_value + (self.target_value - self.start_value) * _ease_out_cubic(t)

        def _shadow_value(self, value):
            if self.bar is None:
                return
            low, high = self.bar.minimum(), self.bar.maximum()
            value = int(round(max(low, min(high, float(value)))))
            blocker = QSignalBlocker(self.bar)
            self.bar.setValue(value)
            blocker.unblock()

        def _shadow_thumb(self):
            if not self.running:
                self._thumb.stop()
                return
            try:
                self._shadow_value(self._current())
            except RuntimeError:
                self._hard_hide()

        @staticmethod
        def _duration_ms(current, target) -> int:
            distance = abs(float(target) - float(current))
            return int(max(105.0, min(220.0, 108.0 + distance * 0.38)))

        @staticmethod
        def _clear_motion_state(motion):
            motion._vel = 0.0
            motion._pending = 0.0
            motion._pos = None
            motion._phase = None
            motion._kicks.clear()

        def _motion_started(self, motion):
            if not getattr(motion, "_counted", False):
                motion._counted = True
                w._GLIDING += 1
            self._clear_motion_state(motion)

        def _motion_finished(self, motion, final_value):
            if motion is None:
                return
            if getattr(motion, "_counted", False):
                motion._counted = False
                w._GLIDING = max(0, w._GLIDING - 1)
            self._clear_motion_state(motion)
            motion._last_value = int(round(float(final_value)))
            motion._atomic_rt_active = False

        def _commit(self, value):
            if self.bar is None:
                return value
            low, high = self.bar.minimum(), self.bar.maximum()
            final_value = int(round(max(low, min(high, float(value)))))
            blocker = QSignalBlocker(self.bar)
            self.bar.setValue(int(self.committed))
            blocker.unblock()
            try:
                if self.body is not None:
                    self.body.setUpdatesEnabled(True)
            except RuntimeError:
                pass
            if self.bar.value() != final_value:
                self.bar.setValue(final_value)
            return final_value

        def kick(self, area, body, ground, motion, distance, direction) -> bool:
            try:
                top = self._top_for(area)
                if top is None or not self._ensure_quick(top):
                    return False
                bar = area.verticalScrollBar()
                if bar.maximum() <= bar.minimum():
                    return False

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
                self._motion_finished(motion, final_value)
                try:
                    self.texture.setY(-(float(final_value) - self.cache_top))
                    self.area.viewport().update()
                except RuntimeError:
                    pass

                area = self.area

                def reveal():
                    try:
                        if self.container is not None:
                            self.container.hide()
                        if self.texture is not None:
                            self.texture.clear_image()
                        if area is not None:
                            area.viewport().update()
                    except RuntimeError:
                        pass
                    self.area = self.body = self.bar = self.motion = None
                    self.cache_height = 0

                QTimer.singleShot(0, reveal)
            except RuntimeError:
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

    # All lower-level widgets patches are already registered at this point.
    # Importing the submodule resolves that final shared API once, without
    # adding another competing helpers.widgets meta-path finder.
    import importlib
    widgets = importlib.import_module("helpers.widgets")
    _patch_widgets(widgets)

    reader = sys.modules.get(_TARGET_READER)
    if reader is not None:
        _patch_reader(reader)
    else:
        sys.meta_path.insert(0, _ReaderFinder())
