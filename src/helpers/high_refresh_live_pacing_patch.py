"""Keep high-refresh live-widget pacing local and non-invasive.

Home and Discover are intentionally detached from the page-wide Quick compositor
because their hero + nested horizontal rows must remain live and interactive.
They therefore use the real QWidget path. Their motion follows the active
monitor's native cadence, including 240 Hz; holding each position for two
physical refreshes on a 240 Hz panel produced an obvious repeated-frame cadence.

Reader and every other live surface also follow the monitor cadence directly.
An explicit ATOMIC_PRESENT_HZ override remains available for diagnostics only.

Home/Discover also contain decorative SmoothTween animations. A tween callback
can resize/repaint widgets on the GUI thread while a wheel glide is active.
During an active Home/Discover glide those decorative callbacks are paused, with
elapsed animation time shifted forward on resume so nothing jumps or
fast-forwards. Scroll motion always wins the frame budget; decoration continues
exactly where it left off afterward.

On 200+ Hz displays only, Home/Discover's vertical wheel motion also gets a very
small amount of extra tail: friction 46 instead of the app-wide 50. The wheel
still travels exactly the same distance per notch because _Momentum scales the
impulse by friction; the lower value only spreads that distance over a slightly
longer glide. The confirmed-smooth 160 Hz path and every other page remain on 50.

SideScroller also has a _Momentum object but its horizontal scrollbar drag was
still raw Qt mouse-sample stepping. Attach a horizontal version of the existing
paced thumb-drag idea: Qt keeps press/release ownership, while mouse moves feed
targets into the row's existing _Momentum.follow().
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
import time

from . import motion_patch as _motion_patch

_TARGET = "helpers.widgets"
_INSTALLED = False
_PATCHED = False
_HIGH_REFRESH_HZ = 200.0
_DEFAULT_WHEEL_FRICTION = 50.0
_HIGH_REFRESH_WHEEL_FRICTION = 46.0


def _is_native_live_page(widget) -> bool:
    """Whether `widget` belongs to Home or Discover.

    Use class/module names rather than importing windows.home/tracker here: this
    patch is installed while those modules may still be importing, and pulling
    either one in from helpers would create a circular-import startup hazard.
    Parent walking is cheap (a handful of QWidget ancestors) and runs only when
    a motion/tween tick asks whether it belongs to one of these two pages.
    """
    node = widget
    for _ in range(16):
        if node is None:
            break
        try:
            cls = type(node)
            name = cls.__name__
            module = cls.__module__
            if ((module == "windows.home" and name == "HomePage")
                    or (module == "windows.tracker" and name == "DiscoverPage")):
                return True
            node = node.parentWidget()
        except (AttributeError, RuntimeError):
            break
    return False


def _patch_widgets(w):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # Ensure motion_patch has installed the compositor before we narrow its
    # global pacing override back to live QWidget surfaces.
    _motion_patch._patch_widgets(w)

    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QStyle, QStyleOptionSlider

    def live_present_frame_s(widget=None) -> float:
        frame = w.screen_frame_s(widget)
        if frame <= 0.0:
            return 0.0
        rate = 1.0 / frame

        # Home/Discover used to be forced to an exact /2 divider on 200+ Hz
        # panels. On a 240 Hz display that guarantees a repeated A,A,B,B motion
        # cadence. The later background-work reductions make the native path
        # cheaper now, so let these pages present at the monitor cadence again.
        if widget is not None and _is_native_live_page(widget):
            return frame

        # Reader and every other ordinary live surface also follow the screen
        # natively unless an explicit diagnostic override was requested.
        override = os.environ.get("ATOMIC_PRESENT_HZ")
        if override:
            try:
                wanted = float(override)
                if wanted > 0.0:
                    divider = max(1, int(round(rate / wanted)))
                    return frame * divider
            except ValueError:
                pass
        return frame

    # Lambdas already stored by _Momentum resolve this module global at call
    # time, so existing/future live surfaces pick this up without rebuilding.
    w.present_frame_s = live_present_frame_s

    # Tiny, high-refresh-only wheel tail for the two live pages. Do this at kick
    # time rather than construction time so dragging the same window between the
    # 160 Hz and 240 Hz monitors immediately adopts the correct profile. Keep
    # horizontal SideScroller motion untouched; the request is for vertical
    # page-wheel drift only.
    old_momentum_kick = w._Momentum.kick

    def live_page_momentum_kick(self, distance_px, direction):
        try:
            bar = getattr(self, "_bar", None)
            if (bar is not None
                    and bar.orientation() == Qt.Orientation.Vertical
                    and _is_native_live_page(bar)):
                screen = bar.screen()
                rate = float(screen.refreshRate()) if screen is not None else 0.0
                self.FRICTION = (_HIGH_REFRESH_WHEEL_FRICTION
                                 if rate >= _HIGH_REFRESH_HZ
                                 else _DEFAULT_WHEEL_FRICTION)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return old_momentum_kick(self, distance_px, direction)

    w._Momentum.kick = live_page_momentum_kick

    # Decorative tweens are useful, but not while the same GUI thread is trying
    # to feed a live high-refresh scroll. Pause only tweens whose QObject owner
    # lives under Home/Discover, and shift their elapsed-time origin on resume.
    old_tween_tick = w.SmoothTween._tick
    old_tween_start = w.SmoothTween.start

    def quiet_tween_start(self, *args, **kwargs):
        self._atomic_scroll_pause_at = None
        return old_tween_start(self, *args, **kwargs)

    def quiet_tween_tick(self):
        try:
            owner = self.parent()
            gliding = bool(w.momentum_active() and _is_native_live_page(owner))
        except (AttributeError, RuntimeError):
            gliding = False

        if gliding:
            if getattr(self, "_atomic_scroll_pause_at", None) is None:
                self._atomic_scroll_pause_at = time.monotonic()
            return

        paused_at = getattr(self, "_atomic_scroll_pause_at", None)
        if paused_at is not None:
            try:
                self._started_at += max(0.0, time.monotonic() - paused_at)
            except (AttributeError, TypeError):
                pass
            self._atomic_scroll_pause_at = None

        return old_tween_tick(self)

    w.SmoothTween.start = quiet_tween_start
    w.SmoothTween._tick = quiet_tween_tick

    class _HorizontalBarDrag(QObject):
        _EVENTS = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                   QEvent.Type.MouseButtonRelease,
                   QEvent.Type.MouseButtonDblClick)

        def __init__(self, owner, bar, motion):
            super().__init__(owner)
            self._bar = bar
            self._motion = motion
            self._drag_from = None
            bar.installEventFilter(self)

        @staticmethod
        def _thumb_rect(bar):
            option = QStyleOptionSlider()
            option.initFrom(bar)
            option.orientation = bar.orientation()
            option.minimum, option.maximum = bar.minimum(), bar.maximum()
            option.sliderPosition = option.sliderValue = bar.value()
            option.pageStep, option.singleStep = bar.pageStep(), bar.singleStep()
            return bar.style().subControlRect(
                QStyle.ComplexControl.CC_ScrollBar, option,
                QStyle.SubControl.SC_ScrollBarSlider, bar)

        @staticmethod
        def _groove_span(bar, thumb):
            option = QStyleOptionSlider()
            option.initFrom(bar)
            option.orientation = bar.orientation()
            groove = bar.style().subControlRect(
                QStyle.ComplexControl.CC_ScrollBar, option,
                QStyle.SubControl.SC_ScrollBarGroove, bar)
            return max(1, groove.width() - thumb.width()), groove

        def eventFilter(self, obj, event):
            if obj is not self._bar or event.type() not in self._EVENTS:
                return False
            try:
                kind = event.type()
                if kind == QEvent.Type.MouseButtonPress:
                    self._drag_from = None
                    if event.button() == Qt.MouseButton.LeftButton:
                        self._motion.cancel()
                        thumb = self._thumb_rect(obj)
                        where = event.position()
                        if thumb.contains(int(where.x()), int(where.y())):
                            span, groove = self._groove_span(obj, thumb)
                            self._drag_from = (where.x() - thumb.x(),
                                               groove.x(), span)
                    return False

                if kind == QEvent.Type.MouseButtonRelease:
                    if self._drag_from is not None:
                        self._drag_from = None
                        self._motion.finish_follow()
                    return False

                if kind == QEvent.Type.MouseMove and self._drag_from is not None:
                    grab, groove_left, span = self._drag_from
                    left = event.position().x() - grab - groove_left
                    low, high = obj.minimum(), obj.maximum()
                    target = low + (high - low) * (left / float(span))
                    self._motion.follow(target)
                    return True
            except Exception:
                try:
                    w.logs.exception("horizontal scrollbar drag failed")
                except Exception:
                    pass
            return False

    old_side_init = w.SideScroller.__init__

    def side_init(self, *args, **kwargs):
        old_side_init(self, *args, **kwargs)
        try:
            self._atomic_horizontal_drag = _HorizontalBarDrag(
                self, self._bar, self._motion)
        except Exception:
            self._atomic_horizontal_drag = None

    w.SideScroller.__init__ = side_init


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch_widgets(module)


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
        _patch_widgets(module)
        return
    sys.meta_path.insert(0, _Finder())
