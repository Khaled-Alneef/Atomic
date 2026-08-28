"""High-refresh precision fixes for Atomic's painted poster grid.

The 165 Hz monitor is already clean.  On the 240 Hz / 125% monitor the motion
model can be perfectly smooth while cached poster rasters still *look* like they
shake: drawing a card at an arbitrary logical fraction places it between real
physical pixels, so its edge is resampled differently on consecutive frames.

Keep the motion position itself floating-point.  Below 200 Hz retain the exact
fractional path that is already proven.  At 200 Hz and up, align only the drawn
card positions to the nearest physical pixel.  The existing high-refresh clock
support remains in place as a separate safeguard against millisecond-timer beat.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys


_TARGET = "helpers.poster_grid"
_INSTALLED = False
_PATCHED = False
_HIGH_HZ_TICKER = None


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QRectF

    def stable_cell_viewport_rect(self, index, offset):
        # Every visible card in one paint receives the same offset.  Resolve
        # the screen/DPR and quantize it once for that frame rather than doing
        # a QScreen lookup for every cell in a 4.17 ms 240 Hz budget.
        source = float(offset)
        if getattr(self, "_atomic_visual_source_offset", None) == source:
            visual = self._atomic_visual_offset
        else:
            visual = source
            try:
                screen = self.screen()
                rate = float(screen.refreshRate()) if screen is not None else 0.0
            except Exception:
                rate = 0.0
            if rate >= 200.0:
                try:
                    dpr = float(self.devicePixelRatioF() or 1.0)
                except Exception:
                    dpr = 1.0
                visual = round(source * dpr) / dpr
            self._atomic_visual_source_offset = source
            self._atomic_visual_offset = visual
        rect = QRectF(self.cell_rect(index))
        rect.moveTop(rect.top() - visual)
        return rect

    module.PosterGrid._cell_viewport_rect = stable_cell_viewport_rect

    old_hold = module.PosterGrid._hold_vblank
    old_release = module.PosterGrid._release_vblank

    def high_hz_ticker():
        global _HIGH_HZ_TICKER
        if sys.platform != "win32":
            return None
        if _HIGH_HZ_TICKER is None:
            try:
                _HIGH_HZ_TICKER = module.widgets_module._VBlankTicker()
            except Exception:
                return None
        ticker = _HIGH_HZ_TICKER
        if getattr(ticker, "failed", False):
            return None
        return ticker

    def hold_vblank(self):
        try:
            screen = self.screen()
            rate = float(screen.refreshRate()) if screen is not None else 0.0
        except Exception:
            rate = 0.0
        if rate < 200.0:
            return old_hold(self)
        if self._vblank_on:
            return True
        ticker = high_hz_ticker()
        if ticker is None:
            return old_hold(self)
        if getattr(ticker, "clock", None) is None:
            return old_hold(self)
        try:
            ticker.tick.connect(self._on_vblank)
            ticker.acquire()
            self._vblank_on = True
            self._atomic_high_hz_ticker = ticker
            return True
        except Exception:
            return old_hold(self)

    def release_vblank(self):
        ticker = getattr(self, "_atomic_high_hz_ticker", None)
        if ticker is None:
            return old_release(self)
        try:
            ticker.tick.disconnect(self._on_vblank)
            ticker.release()
        except Exception:
            pass
        self._atomic_high_hz_ticker = None
        self._vblank_on = False

    module.PosterGrid._hold_vblank = hold_vblank
    module.PosterGrid._release_vblank = release_vblank


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
