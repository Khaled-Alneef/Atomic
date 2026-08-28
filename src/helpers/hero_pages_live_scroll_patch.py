"""Keep Home and tracker sections on live widget scrolling.

Movies is the owner's known-perfect control. Home and the tracker views contain
nested/lazy scroll surfaces where a page-wide Qt Quick snapshot is the wrong
owner: it can cover live content and make the first wheel gesture pay for a
synchronous snapshot.

Detach those surfaces from the compositor after construction. Tracker sections
such as Saved, Schedule and History are built lazily when _set_tab() runs, so
repeat the same detach after every section switch as well. The existing
_Momentum implementation then falls back to its normal native-refresh live
QWidget path automatically.

This is intentionally page-local. Other scroll_area() users keep the GPU path,
and the confirmed startup-DPR fix remains independent.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_TARGETS = {"windows.home", "windows.tracker"}
_INSTALLED = False
_PATCHED = set()


def _disable_page_compositors(page):
    from PyQt6.QtWidgets import QAbstractScrollArea

    try:
        areas = page.findChildren(QAbstractScrollArea)
    except RuntimeError:
        return

    for area in areas:
        surface = getattr(area, "_atomic_compositor", None)
        if surface is None:
            continue
        try:
            # If an overlay somehow exists already, reveal the live widgets
            # immediately before detaching it.
            if getattr(surface, "_active", False):
                motion = getattr(surface, "_motion", None)
                pos = float(getattr(
                    surface, "_visual", area.verticalScrollBar().value()))
                try:
                    if motion is not None:
                        if hasattr(motion, "_pos"):
                            motion._pos = pos
                        if getattr(surface, "_decoupled_wheel", False):
                            surface.commit_decoupled(pos)
                    surface._finish_end()
                except (RuntimeError, AttributeError):
                    pass

            bar = area.verticalScrollBar()
            # This is the switch used by motion_patch._surface_for(). Once it
            # is gone, patched _Momentum methods call their captured original
            # implementations instead of starting the Quick snapshot.
            if hasattr(bar, "_atomic_motion_surface"):
                try:
                    delattr(bar, "_atomic_motion_surface")
                except (AttributeError, RuntimeError):
                    bar._atomic_motion_surface = None

            try:
                surface.container.hide()
            except (RuntimeError, AttributeError):
                pass
            area._atomic_compositor = None
        except RuntimeError:
            continue


def _patch_home(module):
    key = "windows.home"
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    cls = getattr(module, "HomePage", None)
    if cls is None:
        return
    old_init = cls.__init__

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _disable_page_compositors(self)

    cls.__init__ = init


def _patch_tracker(module):
    key = "windows.tracker"
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    # Saved, Schedule, History, Discover and the category views all belong to
    # TrackerPage. Some of their scroll areas do not exist until _set_tab()
    # builds that section, so patch the shared base rather than only the
    # DiscoverPage subclass.
    cls = getattr(module, "TrackerPage", None)
    if cls is None:
        return

    old_init = cls.__init__
    old_set_tab = cls._set_tab

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _disable_page_compositors(self)

    def set_tab(self, key, *args, **kwargs):
        result = old_set_tab(self, key, *args, **kwargs)
        # Saved's grid is lazy, Schedule is rebuilt on every visit, and
        # History/category views can also create fresh scroll_area() instances.
        # Detach any compositor created by that build before the user can wheel.
        _disable_page_compositors(self)
        return result

    cls.__init__ = init
    cls._set_tab = set_tab


def _patch(module):
    name = getattr(module, "__name__", "")
    if name == "windows.home":
        _patch_home(module)
    elif name == "windows.tracker":
        _patch_tracker(module)


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
        if fullname not in _TARGETS:
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
    for name in tuple(_TARGETS):
        module = sys.modules.get(name)
        if module is not None:
            _patch(module)
    sys.meta_path.insert(0, _Finder())
