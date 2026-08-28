"""Stabilize raster content on the 240 Hz / HiDPI compositor path.

The Qt Quick scroll layer moves one cached QWidget raster, which is the right
architecture for motion clarity.  At 125% DPI, however, a free fractional
logical Y is also a fractional *physical* pixel.  Linear filtering then changes
poster/card edge coverage on every 240 Hz frame and the grid reads as if it is
shaking even though the motion position itself is smooth.

Keep the physics as a float.  Only at >=200 Hz, align the displayed texture to
the nearest real device pixel and disable filtering for that already-aligned
blit.  Lower-refresh paths are left exactly as they are because the owner's
165 Hz monitor is already clean.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from . import motion_patch as _motion_patch


_TARGET = "helpers.widgets"
_INSTALLED = False
_PATCHED = False


def _high_refresh(surface) -> bool:
    try:
        screen = surface.viewport.screen()
        return screen is not None and float(screen.refreshRate()) >= 200.0
    except Exception:
        return False


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # motion_patch owns the compositor class inside its patch function rather
    # than exporting it.  Wrap the factory that returns each scroll area and
    # tune the concrete compositor instance it creates.
    original_scroll_area = module.scroll_area

    def stable_scroll_area(*args, **kwargs):
        area = original_scroll_area(*args, **kwargs)
        surface = getattr(area, "_atomic_compositor", None)
        if surface is None or getattr(surface, "_atomic_device_aligned", False):
            return area
        surface._atomic_device_aligned = True
        original_draw = surface._draw

        def aligned_draw(pos):
            if not _high_refresh(surface):
                try:
                    surface.texture.setSmooth(True)
                except Exception:
                    pass
                return original_draw(pos)

            if surface._cache_height <= 0:
                return
            offset = float(pos) - surface._cache_top
            try:
                dpr = float(surface.viewport.devicePixelRatioF() or 1.0)
            except Exception:
                dpr = 1.0
            # Align the *visual* transform, not the model.  At the owner's
            # 125% 2K display this is a 0.8-logical-pixel grid, so it preserves
            # substantially finer motion than integer logical positioning while
            # preventing a poster edge from being filtered differently on each
            # frame.
            offset = round(offset * dpr) / dpr
            try:
                surface.texture.setSmooth(False)
            except Exception:
                pass
            surface.texture.setY(-offset)

        surface._draw = aligned_draw
        return area

    module.scroll_area = stable_scroll_area


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        # This finder sits in front of motion_patch's finder.  Apply the base
        # compositor explicitly, then add only the visual alignment above.
        _motion_patch._patch_widgets(module)
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
        _motion_patch._patch_widgets(module)
        _patch(module)
        return
    sys.meta_path.insert(0, _Finder())
