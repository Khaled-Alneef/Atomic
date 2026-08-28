"""Preserve fractional motion in Atomic's painted poster surfaces.

PosterGrid already owns card rendering and integrates its scroll position as a
float inside paintEvent. Keep that precision through the final viewport-rect
conversion so Home/Discover/category cards move as one painted surface instead
of holding on integer rows at high refresh rates.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_TARGET = "helpers.poster_grid"
_INSTALLED = False
_PATCHED = False


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from PyQt6.QtCore import QRectF

    def fractional_cell_viewport_rect(self, index, offset):
        rect = QRectF(self.cell_rect(index))
        rect.moveTop(rect.top() - float(offset))
        return rect

    module.PosterGrid._cell_viewport_rect = fractional_cell_viewport_rect


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
