"""One native Qt Quick scrolling surface for Atomic.

The earlier render-thread experiment used YAnimator. That was the wrong primitive
for wheel scrolling: Qt documents that Animator target properties are *not*
updated while an Animator is running. Every wheel notch therefore had to stop
that render-thread animation and restart it from a Python estimate of its visual
position. At 240 Hz even a tiny estimate/render-thread phase error is visible as
judder.

This implementation uses Qt Quick's own Flickable instead. Flickable owns
contentY, velocity, deceleration, bounds and sub-pixel motion in Qt/C++; Python
runs only when input arrives and once when motion ends. There is no Python
per-frame position producer and no Animator retarget/restart seam.

There is also only one Quick window used by this path. The legacy per-QScrollArea
Quick windows are discarded as each opaque scroll area is constructed, because
Qt's threaded scene-graph documentation says its smoothest/vsync-driven case is
one visible QQuickWindow. If this shared Quick path cannot initialize, the
existing live QWidget motion remains available as fallback.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import math
import sys

_INSTALLED = False
_READER_PATCHED = False
_TARGET_READER = "windows.reader"


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
        """One static raster layer; Flickable moves it without repainting it."""

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

    def _flickable_component():
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
                    b"Flickable {\n"
                    b"  id: flick\n"
                    b"  clip: true\n"
                    b"  interactive: false\n"
                    b"  pixelAligned: false\n"
                    b"  boundsBehavior: Flickable.StopAtBounds\n"
                    b"  boundsMovement: Flickable.StopAtBounds\n"
                    b"  flickDeceleration: 9000\n"
                    b"  maximumFlickVelocity: 8000\n"
                    b"  property real kickVelocity: 0\n"
                    b"  property int kickSerial: 0\n"
                    b"  property int cancelSerial: 0\n"
                    b"  property bool suppressSettle: false\n"
                    b"  signal settled()\n"
                    b"  onKickSerialChanged: flick.flick(0, kickVelocity)\n"
                    b"  onCancelSerialChanged: flick.cancelFlick()\n"
                    b"  onMovementEnded: if (!suppressSettle) settled()\n"
                    b"}\n"
                ),
                QUrl("atomic:native-flick-scroll"),
            )
            probe = component.create()
            if probe is None:
                qml_state["failed"] = True
                return None
            probe.deleteLater()
            qml_state["engine"] = engine
            qml_state["component"] = component
            w._atomic_scroll_qml_engine = engine
            w._atomic_scroll_flickable_component = component
            return component
        except Exception:
            qml_state["failed"] = True
            return None

    def _discard_legacy_surface(area):
        """Remove motion_patch's per-area native Quick child before it is used."""
        surface = getattr(area, "_atomic_compositor", None)
        if surface is None:
            return
        try:
            viewport = getattr(surface, "viewport", None)
            if viewport is not None:
                viewport.removeEventFilter(surface)
        except (RuntimeError, AttributeError):
            pass
        try:
            quick = getattr(surface, "quick", None)
            if quick is not None:
                try:
                    quick.frameSwapped.disconnect(surface._frame_swapped)
                except (TypeError, RuntimeError):
                    pass
        except (RuntimeError, AttributeError):
            pass
        try:
            container = getattr(surface, "container", None)
            if container is not None:
                container.hide()
                container.deleteLater()
        except (RuntimeError, AttributeError):
            pass
        try:
            bar = area.verticalScrollBar()
            if hasattr(bar, "_atomic_motion_surface"):
                delattr(bar, "_atomic_motion_surface")
        except (RuntimeError, AttributeError):
            pass
        try:
            area._atomic_compositor = None
        except RuntimeError:
            pass

    class _NativeFlickOverlay(QObject):
        """One reusable native Quick Flickable over the active scroll viewport."""

        # Large enough for a sustained wheel burst without rebuilding the raster
        # in motion, but much smaller than the old eight-view cache at DPR 1.25.
        CACHE_VIEWS = 3.25
        BACK_VIEWS = 0.42
        EDGE_SLOP = 96
        DECELERATION = 9000.0
        MAX_VELOCITY = 8000.0
        THUMB_MS = 32

        def __init__(self):
            super().__init__()
            self.quick = None
            self.root = None
            self.texture = None
            self.container = None
            self.top = None
            self.area = None
            self.body = None
            self.bar = None
            self.motion = None
            self.ground = QColor("#000000")
            self.cache_top = 0.0
            self.cache_height = 0
            self.committed = 0
            self.running = False
            self._serial = 0
            self._cancel_serial = 0
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
                    return self.top is top
                component = _flickable_component()
                if component is None:
                    return False

                quick = QQuickWindow()
                quick.setColor(self.ground)
                try:
                    quick.setFlag(Qt.WindowType.WindowTransparentForInput, True)
                except Exception:
                    pass
                root = component.create()
                if root is None:
                    return False
                root.setParent(self)
                root.setParentItem(quick.contentItem())
                texture = _Texture(root.property("contentItem"))
                root.settled.connect(self._movement_finished)

                container = QWidget.createWindowContainer(quick, top)
                container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                                       True)
                container.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                container.hide()

                self.quick = quick
                self.root = root
                self.texture = texture
                self.container = container
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
            try:
                if self.root is not None:
                    self.root.setProperty("suppressSettle", True)
                    self._cancel_serial += 1
                    self.root.setProperty("cancelSerial", self._cancel_serial)
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
            try:
                if self.root is not None:
                    self.root.setProperty("suppressSettle", False)
            except Exception:
                pass
            self.area = self.body = self.bar = self.motion = None
            self.cache_height = 0

        @staticmethod
        def _geometry_for(area, top):
            viewport = area.viewport()
            return QRect(viewport.mapTo(top, QPoint(0, 0)), viewport.size())

        def _global_position(self):
            if self.root is None:
                return float(self.committed)
            try:
                return self.cache_top + float(self.root.property("contentY") or 0.0)
            except Exception:
                return float(self.committed)

        def _velocity(self):
            if self.root is None:
                return 0.0
            try:
                return float(self.root.property("verticalVelocity") or 0.0)
            except Exception:
                return 0.0

        def _stop_flick(self, suppress=True):
            if self.root is None:
                return
            self.root.setProperty("suppressSettle", bool(suppress))
            self._cancel_serial += 1
            self.root.setProperty("cancelSerial", self._cancel_serial)

        def _capture(self, area, body, ground, current, direction) -> bool:
            viewport = area.viewport()
            if viewport.width() <= 0 or viewport.height() <= 0:
                return False
            view_h = max(1, viewport.height())
            body_h = max(view_h, body.height())
            width = max(1, body.width(), viewport.width())
            total_h = min(body_h, max(view_h,
                                      int(round(view_h * self.CACHE_VIEWS))))
            back = int(round(view_h * self.BACK_VIEWS))
            if direction >= 0:
                top = int(round(current)) - back
            else:
                top = int(round(current)) - (total_h - view_h - back)
            top = max(0, min(top, max(0, body_h - total_h)))
            height = min(total_h, body_h - top)

            dpr = float(viewport.devicePixelRatioF() or 1.0)
            image = QImage(max(1, int(round(width * dpr))),
                           max(1, int(round(height * dpr))),
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
            self.texture.setX(0.0)
            self.texture.setY(0.0)
            self.root.setWidth(float(viewport.width()))
            self.root.setHeight(float(viewport.height()))
            self.root.setProperty("contentWidth", float(width))
            self.root.setProperty("contentHeight", float(height))
            local = max(0.0, min(float(max(0, height - view_h)),
                                 float(current) - self.cache_top))
            self.root.setProperty("contentX", 0.0)
            self.root.setProperty("contentY", local)
            return True

        def _contains(self, position) -> bool:
            if self.cache_height <= 0 or self.area is None:
                return False
            try:
                view_h = self.area.viewport().height()
                body_h = max(view_h, self.body.height())
            except RuntimeError:
                return False
            lo = self.cache_top + (self.EDGE_SLOP if self.cache_top > 0 else 0)
            hi_raw = self.cache_top + self.cache_height
            hi = hi_raw - (self.EDGE_SLOP if hi_raw < body_h else 0)
            position = float(position)
            return position >= lo and position + view_h <= hi

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
                self._shadow_value(self._global_position())
            except RuntimeError:
                self._hard_hide()

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
            motion._atomic_rt_active = True

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

        def _projected_stop(self, current, velocity, distance, direction):
            velocity = float(velocity)
            same = velocity == 0.0 or velocity * direction > 0.0
            remaining = ((abs(velocity) ** 2) / (2.0 * self.DECELERATION)
                         if same else 0.0)
            return current + direction * (remaining + float(distance))

        def _kick_velocity(self, velocity, distance, direction):
            velocity = float(velocity)
            same = velocity == 0.0 or velocity * direction > 0.0
            remaining = ((abs(velocity) ** 2) / (2.0 * self.DECELERATION)
                         if same else 0.0)
            speed = math.sqrt(max(0.0, 2.0 * self.DECELERATION
                                  * (remaining + float(distance))))
            return direction * min(self.MAX_VELOCITY, speed)

        def kick(self, area, body, ground, motion, distance, direction) -> bool:
            try:
                top = self._top_for(area)
                if top is None or not self._ensure_quick(top):
                    return False
                bar = area.verticalScrollBar()
                if bar.maximum() <= bar.minimum():
                    return False
                direction = 1 if direction > 0 else -1

                if self.running and self.area is not area:
                    self.cancel(commit_current=True)

                if self.running:
                    current = self._global_position()
                    velocity = self._velocity()
                else:
                    current = float(bar.value())
                    velocity = 0.0
                    self.committed = int(bar.value())

                projected = self._projected_stop(current, velocity,
                                                 distance, direction)
                projected = max(float(bar.minimum()),
                                min(float(bar.maximum()), projected))
                if abs(projected - current) < 0.01:
                    return True

                self.area = area
                self.body = body
                self.bar = bar
                self.motion = motion
                self.ground = QColor(ground)
                self.quick.setColor(self.ground)
                self.container.setGeometry(self._geometry_for(area, top))

                if (self.cache_height <= 0 or not self._contains(current)
                        or not self._contains(projected)):
                    if self.running:
                        self._stop_flick(suppress=True)
                    if not self._capture(area, body, self.ground,
                                         current, direction):
                        if self.root is not None:
                            self.root.setProperty("suppressSettle", False)
                        return False

                try:
                    body.setUpdatesEnabled(False)
                except RuntimeError:
                    return False
                self.container.show()
                self.container.raise_()

                # Ask Flickable for a new velocity only when input arrives.
                # Its C++ timeline owns every intermediate sub-pixel position.
                velocity = self._kick_velocity(self._velocity(), distance,
                                               direction)
                self.root.setProperty("suppressSettle", False)
                self.root.setProperty("kickVelocity", float(velocity))
                self._serial += 1
                self.root.setProperty("kickSerial", self._serial)

                if not self.running:
                    self._motion_started(motion)
                self.running = True
                if not self._thumb.isActive():
                    self._thumb.start()
                return True
            except Exception:
                self._hard_hide()
                return False

        def _movement_finished(self):
            if not self.running:
                return
            self.running = False
            self._thumb.stop()
            motion = self.motion
            try:
                final_value = self._commit(self._global_position())
                self._motion_finished(motion, final_value)
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
                self._motion_finished(motion, self._global_position())
                self._hard_hide()

        def cancel(self, commit_current=True):
            if not self.running:
                return False
            current = self._global_position()
            motion = self.motion
            self._stop_flick(suppress=True)
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
                if self.root is not None:
                    self.root.setProperty("suppressSettle", False)
            except RuntimeError:
                pass
            self.area = self.body = self.bar = self.motion = None
            self.cache_height = 0
            return True

    overlay = _NativeFlickOverlay()
    w._atomic_render_thread_overlay = overlay

    old_scroll_area = w.scroll_area

    def native_flick_scroll_area(body, always_show_vbar=False, ground=None,
                                 notch_scale=1.0):
        area = old_scroll_area(body, always_show_vbar=always_show_vbar,
                               ground=ground, notch_scale=notch_scale)
        if ground:
            try:
                # The shared overlay is now the only Quick scroll window. If it
                # cannot initialize, _Momentum simply falls back to live QWidget
                # scrolling; we do not wake a second native Quick swapchain.
                _discard_legacy_surface(area)
                bar = area.verticalScrollBar()
                bar._atomic_rt_area = area
                bar._atomic_rt_body = body
                bar._atomic_rt_ground = ground
            except RuntimeError:
                pass
        return area

    w.scroll_area = native_flick_scroll_area

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
                if overlay.kick(area, body, ground, self,
                                distance_px, direction):
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
    import importlib
    widgets = importlib.import_module("helpers.widgets")
    _patch_widgets(widgets)

    reader = sys.modules.get(_TARGET_READER)
    if reader is not None:
        _patch_reader(reader)
    else:
        sys.meta_path.insert(0, _ReaderFinder())
