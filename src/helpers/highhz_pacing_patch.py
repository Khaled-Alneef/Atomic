"""240 Hz pacing fixes for Atomic's compositor-backed scroll surfaces.

The 165 Hz monitor is already clean and is deliberately left alone.  On the
240 Hz display, two details in the experimental Qt Quick bridge can turn a
perfectly smooth scene-graph transform into uneven motion:

* one frame requested the next Quick render twice (present() and frameSwapped),
* wheel physics used callback arrival time, so a late Python/GUI callback became
  a double-size visual step on the next 4.17 ms presentation.

At >=200 Hz, schedule exactly one next Quick frame and advance wheel physics by
one display interval per presented frame.  The model remains floating point;
there is no device-pixel snapping.  The painted PosterGrid uses the same fixed
frame interval and keeps fractional card placement.  Lower refresh rates keep
the repository's existing behaviour exactly.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import time

from . import motion_patch as _motion_patch


_TARGETS = {"helpers.widgets", "helpers.poster_grid"}
_INSTALLED = False
_WIDGETS_PATCHED = False
_POSTER_PATCHED = False


def _surface_high_hz(surface) -> bool:
    try:
        screen = surface.viewport.screen()
        return screen is not None and float(screen.refreshRate()) >= 200.0
    except Exception:
        return False


def _patch_widgets(module):
    global _WIDGETS_PATCHED
    if _WIDGETS_PATCHED:
        return
    _WIDGETS_PATCHED = True

    # motion_patch owns the Quick compositor and must be in place first.
    _motion_patch._patch_widgets(module)

    base_tick = module._Momentum._tick

    def paced_tick(self):
        surface = getattr(self._bar, "_atomic_motion_surface", None)
        if (surface is not None
                and getattr(surface, "_active", False)
                and not getattr(surface, "_ending_after_swap", False)
                and getattr(self, "_follow", None) is None
                and getattr(self, "_pos", None) is not None
                and _surface_high_hz(surface)):
            # frameSwapped is already the presentation boundary.  Do not use
            # the queued callback's wall-clock lateness as motion distance: one
            # late callback would otherwise turn into a visibly larger step on
            # a 240 Hz panel.  Feed the existing integrator exactly one screen
            # interval; missed callbacks therefore slow time instead of making
            # the picture jump, which is the preferable failure mode for
            # motion clarity.
            try:
                frame = float(module.screen_frame_s(surface.viewport) or 0.0)
            except Exception:
                frame = 0.0
            if 0.0 < frame <= 0.0055:
                stamp = time.monotonic()
                self._phase = stamp
                self._last = stamp - frame
        return base_tick(self)

    module._Momentum._tick = paced_tick

    original_scroll_area = module.scroll_area

    def paced_scroll_area(*args, **kwargs):
        area = original_scroll_area(*args, **kwargs)
        surface = getattr(area, "_atomic_compositor", None)
        if surface is None or getattr(surface, "_atomic_single_request", False):
            return area
        surface._atomic_single_request = True
        original_present = surface.present

        def single_request_present(pos):
            if not _surface_high_hz(surface):
                return original_present(pos)
            if not surface._active:
                return
            surface._visual = float(pos)
            if not surface._contains(surface._visual):
                surface._build_cache(surface._visual)
            surface._draw(surface._visual)
            # Do NOT call quick.update() here at 240 Hz. _frame_swapped() does
            # that once after the motion step.  The previous code called it in
            # both places, so each presented frame queued the next render twice.

        surface.present = single_request_present
        return area

    module.scroll_area = paced_scroll_area


def _patch_poster(module):
    global _POSTER_PATCHED
    if _POSTER_PATCHED:
        return
    _POSTER_PATCHED = True

    from PyQt6.QtCore import QRectF

    base_step = module.FrameMotion.step

    def paced_step(self, now=None):
        frame = float(getattr(self, "frame_s", 0.0) or 0.0)
        if (now is None and 0.0 < frame <= 0.0055
                and getattr(self, "_last", None) is not None):
            # PosterGrid computes position inside paintEvent. At 240 Hz use one
            # exact display interval for that paint rather than the timer's
            # arrival jitter. Its scheduler already targets the same refresh
            # grid, so this changes spacing, not total travel or wheel feel.
            now = self._last + frame
        return base_step(self, now)

    module.FrameMotion.step = paced_step

    def fractional_cell_viewport_rect(self, index, offset):
        # Keep the motion model's sub-pixel precision. The physical-pixel
        # quantization experiment reduced smoothness and is intentionally gone.
        rect = QRectF(self.cell_rect(index))
        rect.moveTop(rect.top() - float(offset))
        return rect

    module.PosterGrid._cell_viewport_rect = fractional_cell_viewport_rect


class _Loader(importlib.abc.Loader):
    def __init__(self, fullname, wrapped):
        self._fullname = fullname
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        if self._fullname == "helpers.widgets":
            _patch_widgets(module)
        else:
            _patch_poster(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(fullname, spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    widgets = sys.modules.get("helpers.widgets")
    if widgets is not None:
        _patch_widgets(widgets)
    poster = sys.modules.get("helpers.poster_grid")
    if poster is not None:
        _patch_poster(poster)
    sys.meta_path.insert(0, _Finder())
