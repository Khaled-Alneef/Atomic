"""Qt Quick compositor for QScrollArea wheel motion - **retired, kept inert.**

QScrollBar stores an integer visual position, so at high refresh a smooth glide
can advance less than one logical pixel per refresh and the QWidget path repeats
a position then jumps a whole one. Presenting the page through a QQuickWindow
removes that quantisation, and this module is that compositor.

It is switched off (`RETIRED`). What it cost was never sub-pixel smoothness but
*entry*: a synchronous full-body QImage render plus a QQuickWindow creation on
the first glide after every page build, measured at 28-101ms of stalled motion,
against no gap over 16.7ms on the plain path. `can_use` carries the table. The
owner reported this as glitching twice, against two successive versions of the
compositor (the second being bounded supersampled strips re-captured mid-glide,
clocked off frameSwapped - retired first, see `_is_target_page`).

Nothing is deleted, because two other patches key off a surface being active and
a surface that never activates keeps them correctly out of the way:
quick_scroll_scope_patch (which freezes the QWidget underlay during a glide) and
scroll_presentation_patch. Flipping `RETIRED` re-enables the whole path for an
A/B; beating the table in `can_use` on the same measurement is the bar.
"""

from __future__ import annotations

import math

_INSTALLED = False

# The compositor is off. See _QuickScrollSurface.can_use for the measured
# table that retired it; flip this to re-enable the whole path for an A/B
# without unpicking the module.
RETIRED = True


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

            try:
                self.setTextureSize(QSize(
                    max(1, int(round(logical_width * self._sample_scale))),
                    max(1, int(round(logical_height * self._sample_scale)))))
            except (AttributeError, RuntimeError):
                self._sample_scale = 1.0

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

            # Home/Tracker use one presentation clock while Quick is visible.
            # _motion_proxy is normally quick_scroll_scope_patch's freeze
            # wrapper; keeping it lets the final integer scrollbar commit happen
            # through the existing handoff rather than bypassing that policy.
            self._frame_driven = False
            self._motion = None
            self._motion_proxy = None
            self._driving_frame = False
            self._terminal_pos = None
            self._finish_after_swap = False
            self._handoff_from_swap = False

            self.viewport.installEventFilter(self)
            area.verticalScrollBar()._atomic_quick_scroll_surface = self

        def eventFilter(self, obj, event):
            if obj is not self.viewport:
                return False
            kind = event.type()
            if kind in (QEvent.Type.Resize, QEvent.Type.Hide,
                        QEvent.Type.MouseButtonPress):
                if self.active:
                    proxy = self._motion_proxy
                    if proxy is not None and proxy is not self:
                        try:
                            proxy.abort()
                        except (AttributeError, RuntimeError):
                            self.abort()
                    else:
                        self.abort()
            return False

        def _screen_rate(self):
            try:
                screen = self.area.screen()
                return float(screen.refreshRate()) if screen is not None else 0.0
            except Exception:
                return 0.0

        def _is_target_page(self):
            """Always False - the Home/Tracker specialization is retired.

            It selected Home and every TrackerPage descendant (Discover,
            Saved, History and the genre pages included) for a bounded
            strip snapshot at 1.25x-2x density, re-captured mid-glide
            whenever the strip ran out, with the motion integrator
            re-clocked from QQuickWindow.frameSwapped. The owner's
            report, 29 August 2026: scrolling on exactly those pages is
            *glitching* - and the mechanism carries the hitch in plain
            sight: leaving the captured strip forces a synchronous
            body.render() of several viewport-heights at up to 2x
            density in the middle of a glide, and the final handoff
            waits on an extra frame swap. Every page is back on the
            full-body native-DPR snapshot below - the same path every
            other QScrollArea page kept since 1.10.161, which is the
            baseline that was never reported against. The strip/
            supersample/frameSwapped members this used to drive stay
            inert while this returns False."""
            return False

        def _inside_player(self):
            """True for a scroll area living inside the video player.

            The player's panels (episode list, sources, subtitles) come
            through widgets.scroll_area like every page does, so a glide
            there used to raise this overlay - a whole extra window - on
            top of mpv's native child while it was presenting video, and
            pay a synchronous body.render() snapshot per gesture with
            the GPU already busy. That is the owner's "when I play any
            watchable, the scrolling in the app becomes stuttering"
            (29 August 2026). Measured: a playing mpv leaves the plain
            momentum tick cadence untouched (p50 33.34ms both ways), so
            the plain path is what the panels get - the sub-pixel
            quantisation this overlay exists to hide is invisible on a
            short panel list, and ui.md's rule stands: over the video,
            native or nothing."""
            node = self.area
            for _ in range(32):
                if node is None:
                    return False
                cls = type(node)
                if (getattr(cls, "__module__", "") == "windows.player"
                        and getattr(cls, "__name__", "") == "PlayerPage"):
                    return True
                try:
                    node = node.parentWidget()
                except (AttributeError, RuntimeError):
                    return False
            return False

        def can_use(self):
            # **The compositor is retired for QScrollArea pages.**
            #
            # It existed to remove sub-pixel quantisation at high refresh,
            # and it buys that at the price of a synchronous full-body
            # QImage render plus a QQuickWindow creation on the first
            # glide of every page - and pages here rebuild from scratch
            # on every visit, so "first glide" is not once, it is each
            # time the page is opened. Measured 30 August 2026 on the
            # real window (165Hz panel, gap between presented positions
            # during one glide, two rounds of each, alternating):
            #
            #                     first glide   second glide
            #     Discover  on      101.1ms         10.1ms
            #     Discover  off       6.6ms         20.6ms
            #     Saved     on       28.4ms          6.8ms
            #     Saved     off       6.7ms          6.8ms
            #
            # A 101ms gap is ~17 missed frames in the middle of a glide,
            # and it also ate the motion it stalled (827px travelled
            # against 1122px for the same input with the overlay off).
            # That is the owner's "the scrolling in the Home, Discover,
            # Saved and History and the same generes pages are
            # glitching", reported twice against two different versions
            # of this compositor. The plain QWidget path produced no gap
            # over 16.7ms in any run.
            #
            # Left inert rather than deleted: quick_scroll_scope_patch
            # and scroll_presentation_patch both key off a surface being
            # active, and a surface that never activates keeps them
            # correctly out of the way. Any future attempt has to beat
            # the table above on the same measurement.
            if RETIRED:
                return False
            if self._screen_rate() < self.HIGH_REFRESH_HZ:
                return False
            if self._inside_player():
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
                quick.frameSwapped.connect(self._frame_swapped)
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

        def bind_motion(self, motion, proxy):
            self._motion = motion
            self._motion_proxy = proxy
            self._frame_driven = bool(self._target_quality)
            self._terminal_pos = None
            self._finish_after_swap = False

        def begin(self, pos):
            if self.active:
                if not self._covers(pos):
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

            self.texture.setY(self._snapshot_top - float(pos))
            try:
                self.quick.requestUpdate()
            except RuntimeError:
                self.abort()

        def note_terminal(self, pos):
            try:
                self._terminal_pos = float(pos)
            except (TypeError, ValueError):
                self._terminal_pos = float(self.area.verticalScrollBar().value())

        def _frame_swapped(self):
            if not self.active or not self._frame_driven:
                return

            # The previous request contained the final position. It is now on
            # screen, so the existing freeze-underlay proxy can commit the real
            # scrollbar underneath it and reveal the QWidget page safely.
            if self._finish_after_swap:
                self._finish_after_swap = False
                pos = self._terminal_pos
                proxy = self._motion_proxy
                self._handoff_from_swap = True
                try:
                    if proxy is not None:
                        proxy.end(pos)
                    else:
                        self.end(pos)
                finally:
                    self._handoff_from_swap = False
                return

            motion = self._motion
            if motion is None:
                return

            # This is the central change: integrate only after an actual Quick
            # presentation. The ordinary millisecond/vblank ticker still exists
            # as _Momentum's fallback and for every non-target surface, but its
            # callbacks are ignored while this target Quick surface owns motion.
            self._driving_frame = True
            try:
                old_tick(motion)
            finally:
                self._driving_frame = False

            if self._terminal_pos is not None:
                self.present(self._terminal_pos)
                self._finish_after_swap = True
                try:
                    self.quick.requestUpdate()
                except (AttributeError, RuntimeError):
                    proxy = self._motion_proxy
                    if proxy is not None:
                        proxy.end(self._terminal_pos)
                    else:
                        self.end(self._terminal_pos)
                return

            pos = motion._pos if motion._pos is not None else motion._bar.value()
            self.present(pos)

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
            self._frame_driven = False
            self._motion = None
            self._motion_proxy = None
            self._terminal_pos = None
            self._finish_after_swap = False

        def end(self, pos=None):
            if not self.active:
                return
            # During the frameSwapped handoff this exact final position was
            # already presented. Do not schedule another frame just before hide.
            if pos is not None and not self._handoff_from_swap:
                self.present(pos)
            self._reveal_qwidget()

        def abort(self):
            if not self.active:
                return
            self._finish_after_swap = False
            self._terminal_pos = None
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

    def _inner(surface):
        candidate = getattr(surface, "_original", surface)
        return candidate if isinstance(candidate, _QuickScrollSurface) else None

    def start(self):
        result = old_start(self)
        surface = _surface(self)
        if surface is not None:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.begin(pos)
            inner = _inner(surface)
            if inner is not None and inner.active:
                inner.bind_motion(self, surface)
        return result

    def tick(self):
        surface = _surface(self)
        inner = _inner(surface)
        if (surface is not None and surface.active and inner is not None
                and inner._frame_driven):
            # QQuickWindow.frameSwapped owns this motion while the affected
            # page overlay is visible. Running old_tick here as well would put
            # the two independent clocks back on the same position.
            return None

        result = old_tick(self)
        if surface is not None and surface.active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.present(pos)
        return result

    def stop(self):
        surface = _surface(self)
        inner = _inner(surface)

        # Called by old_tick when the frame-driven integrator reaches its tail.
        # Stop the fallback timer now, but leave the Quick cover up. The current
        # float is still available here; old_tick sets _pos=None immediately
        # after this call, so remember it for the final presented frame.
        if (surface is not None and surface.active and inner is not None
                and inner._frame_driven and inner._driving_frame):
            pos = self._pos if self._pos is not None else self._bar.value()
            inner.note_terminal(pos)
            return old_stop(self)

        result = old_stop(self)
        if surface is not None and surface.active:
            pos = self._pos if self._pos is not None else self._bar.value()
            surface.end(pos)
        return result

    w._Momentum._start_ticking = start
    w._Momentum._tick = tick
    w._Momentum._stop_ticking = stop
