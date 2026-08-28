"""Preserve Home/Discover/History/Schedule fixes with the 0825ee3 raster scroller.

The later hero_pages_live_scroll_patch mixed two separate concerns:
1) useful page-specific work reduction for Home/Discover and lazy Tracker tabs;
2) detaching the then-current Qt Quick compositor.

Development now deliberately uses the raster compositor from 0825ee3, so only
(1) belongs here. This module keeps the page fixes without touching, deleting,
or replacing any _atomic_motion_surface created by motion_patch.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_TARGETS = {"windows.home", "windows.tracker"}
_INSTALLED = False
_PATCHED = set()
_VISUAL_IDLE_RETRY_MS = 48


def _motion_active() -> bool:
    try:
        from . import widgets
        return bool(widgets.momentum_active())
    except Exception:
        return False


def _defer_visual_until_idle(owner, key, callback):
    """Coalesce visual updates so image arrivals do not compete with scrolling."""
    try:
        pending = getattr(owner, "_atomic_deferred_scroll_visuals", None)
        if pending is None:
            pending = {}
            owner._atomic_deferred_scroll_visuals = pending
        pending[key] = callback
        if getattr(owner, "_atomic_deferred_scroll_visuals_queued", False):
            return
        owner._atomic_deferred_scroll_visuals_queued = True

        from PyQt6.QtCore import QTimer

        def flush():
            try:
                if _motion_active():
                    QTimer.singleShot(_VISUAL_IDLE_RETRY_MS, flush)
                    return
                queued = dict(getattr(owner, "_atomic_deferred_scroll_visuals", {}))
                owner._atomic_deferred_scroll_visuals = {}
                owner._atomic_deferred_scroll_visuals_queued = False
                for work in queued.values():
                    try:
                        work()
                    except RuntimeError:
                        pass
            except RuntimeError:
                pass

        QTimer.singleShot(_VISUAL_IDLE_RETRY_MS, flush)
    except RuntimeError:
        pass


def _mark_raster_tabs(page):
    """Record/verify lazy Tracker tabs without altering their raster surfaces."""
    try:
        from PyQt6.QtWidgets import QAbstractScrollArea
        for area in page.findChildren(QAbstractScrollArea):
            surface = getattr(area.verticalScrollBar(), "_atomic_motion_surface", None)
            # No mutation: this is intentionally only a diagnostic marker. The
            # 0825ee3 motion_patch owns the surface and must remain authoritative.
            if surface is not None:
                area._atomic_raster_scroll_verified = True
    except RuntimeError:
        pass


def _patch_home(module):
    key = "windows.home"
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    cls = getattr(module, "HomePage", None)
    if cls is None:
        return

    old_init = cls.__init__
    old_backdrop = getattr(cls, "_on_hero_backdrop", None)
    old_overlay = getattr(cls, "_on_hero_overlay", None)

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _mark_raster_tabs(self)

    cls.__init__ = init

    # Keep the measured Home fix: warm hero covers on a worker instead of doing
    # the first expensive decode on the GUI thread.
    if hasattr(cls, "_warm_next_hero_cover"):
        def warm_next_hero_cover(self):
            queue = getattr(self, "_hero_warm_queue", None)
            if not queue:
                return
            entry = queue.pop(0)
            path = entry.get("cover_path")
            if path:
                try:
                    module.threading.Thread(
                        target=module.images.warm,
                        args=(path, tuple(module.HERO_COVER_SIZE)),
                        daemon=True,
                    ).start()
                except Exception:
                    pass
            if queue:
                try:
                    module.QTimer.singleShot(
                        module.HERO_COVER_WARM_MS, self._warm_next_hero_cover)
                except RuntimeError:
                    pass

        cls._warm_next_hero_cover = warm_next_hero_cover

    if callable(old_backdrop):
        def hero_backdrop(self, entry_id, path):
            if _motion_active():
                _defer_visual_until_idle(
                    self, ("hero-backdrop", entry_id),
                    lambda s=self, i=entry_id, p=path: old_backdrop(s, i, p))
                return
            return old_backdrop(self, entry_id, path)
        cls._on_hero_backdrop = hero_backdrop

    if callable(old_overlay):
        def hero_overlay(self, entry_id, logo_path, hide_title):
            if _motion_active():
                _defer_visual_until_idle(
                    self, ("hero-overlay", entry_id),
                    lambda s=self, i=entry_id, p=logo_path, h=hide_title:
                    old_overlay(s, i, p, h))
                return
            return old_overlay(self, entry_id, logo_path, hide_title)
        cls._on_hero_overlay = hero_overlay


def _patch_tracker(module):
    key = "windows.tracker"
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    cls = getattr(module, "TrackerPage", None)
    if cls is None:
        return

    old_init = cls.__init__
    old_set_tab = cls._set_tab
    old_poster = getattr(cls, "_on_discover_poster", None)

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _mark_raster_tabs(self)

    def set_tab(self, key, *args, **kwargs):
        result = old_set_tab(self, key, *args, **kwargs)
        # Schedule, History, Saved and category views build/rebuild lazily. Keep
        # the post-build hook from the later fix, but never detach their raster
        # compositor. This ensures newly-created sections are covered too.
        _mark_raster_tabs(self)
        return result

    cls.__init__ = init
    cls._set_tab = set_tab

    # Keep Discover's poster-swap coalescing while the vertical page is moving.
    if callable(old_poster):
        def discover_poster(self, kind, index, path, resolved_url, stamp):
            if (getattr(self, "_active_tab", None) == "discover"
                    and _motion_active()):
                _defer_visual_until_idle(
                    self, ("discover-poster", kind, index),
                    lambda s=self, k=kind, i=index, p=path,
                           r=resolved_url, st=stamp:
                    old_poster(s, k, i, p, r, st))
                return
            return old_poster(self, kind, index, path, resolved_url, stamp)
        cls._on_discover_poster = discover_poster


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
