"""Keep live QWidget motion phase-locked on very high refresh displays.

The Qt Quick compositor has its own 200+ Hz presentation loop, but
motion_patch also replaced helpers.widgets.present_frame_s globally with the
raw screen refresh. That made *live QWidget* surfaces try to repaint at every
240 Hz refresh too. On a 2K/125% panel that is much more work than the same
logical UI at 1080p and missed frames become uneven steps.

Restore the original exact-divider policy for live QWidget motion: 240 Hz uses
120 Hz (every second refresh), 165/144 Hz use every refresh, and lower rates are
unchanged. The decoupled Quick wheel path remains 240 Hz because motion_patch
explicitly drives it from screen_frame_s().

SideScroller also had a _Momentum object but its horizontal scrollbar drag was
still raw Qt mouse-sample stepping. Attach a horizontal version of the existing
paced thumb-drag idea: Qt keeps press/release ownership, while mouse moves feed
targets into the row's existing _Momentum.follow().
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys

from . import motion_patch as _motion_patch

_TARGET = "helpers.widgets"
_INSTALLED = False
_PATCHED = False


def _patch_widgets(w):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # Ensure motion_patch has installed the compositor before we narrow its
    # global pacing override back to live QWidget surfaces.
    _motion_patch._patch_widgets(w)

    def live_present_frame_s(widget=None) -> float:
        frame = w.screen_frame_s(widget)
        if frame <= 0.0:
            return 0.0
        target = float(getattr(w, "MOTION_MAX_HZ", 120.0) or 120.0)
        override = os.environ.get("ATOMIC_PRESENT_HZ")
        if override:
            try:
                wanted = float(override)
                if wanted > 0.0:
                    target = wanted
            except ValueError:
                pass
        rate = 1.0 / frame
        divider = max(1, int(round(rate / target)))
        return frame * divider

    # Lambdas already stored by _Momentum resolve this module global at call
    # time, so existing/future live surfaces pick this up without rebuilding.
    w.present_frame_s = live_present_frame_s

    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QStyle, QStyleOptionSlider

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
                    # Qt must see the press so it keeps sliderDown/mouse grab.
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
                # Event-filter exceptions are fatal in PyQt; horizontal drag
                # smoothness is never allowed to become a crash path.
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
            # Leave the row on Qt's native drag if setup ever fails.
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
