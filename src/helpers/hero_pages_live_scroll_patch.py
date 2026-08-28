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

The important part of "detach" is that the native Qt Quick child window is
actually destroyed, not merely hidden. A hidden createWindowContainer still
owns a native QQuickWindow/D3D surface; if it was created on the 2K/DPR>1
monitor Windows has to migrate that larger native child when the top-level
window crosses screens. That is wasted work on these pages because their Quick
path is permanently disabled.

Home/Discover are also the two live pages that receive artwork asynchronously
while the user may already be scrolling. Keep that visual work off the active
scroll frame: Home's expensive hero-cover prewarm runs through images.warm on a
worker, and hero/poster swaps that arrive during a glide are coalesced until the
glide is idle. The data still arrives immediately; only the repaint waits.

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
_VISUAL_IDLE_RETRY_MS = 48


def _live_motion_active() -> bool:
    try:
        from . import widgets
        return bool(widgets.momentum_active())
    except Exception:
        return False


def _defer_visual_until_idle(owner, key, callback):
    """Coalesce one visual update while a live page is scrolling.

    A 48ms retry is deliberately much slower than the frame clock: waiting for
    idle must not become another timer competing with a 240Hz glide. Repeated
    updates for the same logical visual replace one another, so a burst of cover
    arrivals becomes one final paint when motion stops.
    """
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
                if _live_motion_active():
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


def _destroy_surface(surface):
    """Dispose the unused native Quick child owned by a detached scroll area."""
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

    # QWidget.createWindowContainer owns the QWindow once embedded. Deleting
    # the container therefore releases both the native child HWND and the
    # QQuickWindow/D3D resources. Do not separately delete the QQuickWindow.
    try:
        container = getattr(surface, "container", None)
        if container is not None:
            container.hide()
            container.deleteLater()
    except (RuntimeError, AttributeError):
        pass


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

            area._atomic_compositor = None
            _destroy_surface(surface)
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
    old_backdrop = getattr(cls, "_on_hero_backdrop", None)
    old_overlay = getattr(cls, "_on_hero_overlay", None)

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        _disable_page_compositors(self)

    cls.__init__ = init

    # The source method intentionally warms one hero cover every 120ms, but it
    # used thumbnail_or_avatar on the GUI thread and its own measurement records
    # a first decode at 21-26ms. images.warm is the thread-safe Pillow half of
    # exactly that operation; after it lands, the later GUI request only has the
    # cheap QPixmap conversion left.
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
                        daemon=True).start()
                except Exception:
                    pass
            if queue:
                try:
                    module.QTimer.singleShot(module.HERO_COVER_WARM_MS,
                                             self._warm_next_hero_cover)
                except RuntimeError:
                    pass

        cls._warm_next_hero_cover = warm_next_hero_cover

    if callable(old_backdrop):
        def hero_backdrop(self, entry_id, path):
            if _live_motion_active():
                _defer_visual_until_idle(
                    self, ("hero-backdrop", entry_id),
                    lambda s=self, i=entry_id, p=path: old_backdrop(s, i, p))
                return
            return old_backdrop(self, entry_id, path)

        cls._on_hero_backdrop = hero_backdrop

    if callable(old_overlay):
        def hero_overlay(self, entry_id, logo_path, hide_title):
            if _live_motion_active():
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

    # Saved, Schedule, History, Discover and the category views all belong to
    # TrackerPage. Some of their scroll areas do not exist until _set_tab()
    # builds that section, so patch the shared base rather than only the
    # DiscoverPage subclass.
    cls = getattr(module, "TrackerPage", None)
    if cls is None:
        return

    old_init = cls.__init__
    old_set_tab = cls._set_tab
    old_poster = getattr(cls, "_on_discover_poster", None)

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

    # Discover already decodes/cuts poster art on workers. The last ~0.1ms
    # conversion and setPixmap are cheap individually, but dozens can land while
    # the vertical page is gliding and each invalidates a painted PosterStrip.
    # Coalesce those visible swaps until the glide is idle; saved/category tabs
    # keep their existing behavior.
    if callable(old_poster):
        def discover_poster(self, kind, index, path, resolved_url, stamp):
            if (getattr(self, "_active_tab", None) == "discover"
                    and _live_motion_active()):
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