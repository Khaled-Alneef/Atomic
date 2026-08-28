"""Interaction fixes for hero pages with nested scrollers.

Movies is the reference path: the control being moved is also the surface the
user sees. Home/Discover are different because a vertical Qt Quick snapshot can
cover live horizontal scrollers. This patch fixes only those ownership edges:

* a wheel during the vertical page transition ends the transition and is cloned
  directly to the visible vertical scroll area, so the first notch is not lost;
* pressing a horizontal scrollbar immediately lands/stops any active outer
  vertical Quick snapshot before Qt begins the drag, so its cards remain live.

No scroll physics are changed here.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from . import hero_scroll_patch as _hero_scroll_patch

_TARGET = "windows.home"
_INSTALLED = False
_PATCHED = False


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    # This finder is installed after hero_scroll_patch and therefore wins the
    # meta-path lookup for windows.home. Apply the older hero patch explicitly
    # so its backdrop/fade deferral is preserved before adding the interaction
    # fixes below.
    _hero_scroll_patch._patch_home(module)

    widgets = sys.modules.get("helpers.widgets")
    if widgets is None:
        return

    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication, QAbstractScrollArea, QScrollBar, QWidget

    old_slide_wheel = getattr(widgets.PageSlide, "wheelEvent", None)

    def _vertical_scroll_owner(slide, global_pos):
        try:
            window = slide.window()
            areas = window.findChildren(QAbstractScrollArea)
        except RuntimeError:
            return None

        candidates = []
        for area in areas:
            try:
                if not area.isVisible():
                    continue
                bar = area.verticalScrollBar()
                if bar is None or bar.maximum() <= bar.minimum():
                    continue
                viewport = area.viewport()
                local = viewport.mapFromGlobal(global_pos)
                if not viewport.rect().contains(local):
                    continue
                priority = 0 if getattr(area, "_atomic_compositor", None) is not None else 1
                area_size = viewport.width() * viewport.height()
                candidates.append((priority, -area_size, area))
            except RuntimeError:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda row: (row[0], row[1]))
        return candidates[0][2]

    def page_slide_wheel(self, event):
        if getattr(self, "_axis", "y") != "y":
            if old_slide_wheel is not None:
                return old_slide_wheel(self, event)
            event.ignore()
            return

        global_pos = event.globalPosition()
        pixel = event.pixelDelta()
        angle = event.angleDelta()
        buttons = event.buttons()
        modifiers = event.modifiers()
        phase = event.phase()
        inverted = event.inverted()

        try:
            self.stop()
        except RuntimeError:
            event.ignore()
            return

        owner = _vertical_scroll_owner(self, global_pos.toPoint())
        if owner is None:
            event.accept()
            return

        try:
            viewport = owner.viewport()
            local = QPointF(viewport.mapFromGlobal(global_pos.toPoint()))
            clone = QWheelEvent(
                local, global_pos, pixel, angle, buttons, modifiers,
                phase, inverted)
            QApplication.sendEvent(viewport, clone)
            event.setAccepted(clone.isAccepted())
        except (RuntimeError, TypeError):
            if old_slide_wheel is not None:
                return old_slide_wheel(self, event)
            event.ignore()

    widgets.PageSlide.wheelEvent = page_slide_wheel

    old_bar_press = QScrollBar.mousePressEvent

    def _outer_surface(bar):
        try:
            node = bar.parentWidget()
        except RuntimeError:
            return None
        seen = set()
        while isinstance(node, QWidget) and id(node) not in seen:
            seen.add(id(node))
            surface = getattr(node, "_atomic_compositor", None)
            if surface is not None:
                return surface
            try:
                node = node.parentWidget()
            except RuntimeError:
                break
        return None

    def _reveal_live(surface):
        if surface is None or not getattr(surface, "_active", False):
            return
        motion = getattr(surface, "_motion", None)
        pos = float(getattr(surface, "_visual", 0.0))
        try:
            if motion is not None:
                if hasattr(motion, "_vel"):
                    motion._vel = 0.0
                if hasattr(motion, "_pending"):
                    motion._pending = 0.0
                if hasattr(motion, "_follow"):
                    motion._follow = None
                if hasattr(motion, "_pos"):
                    motion._pos = pos
                motion._stop_ticking()
        except (RuntimeError, AttributeError):
            pass

        try:
            if getattr(surface, "_active", False):
                surface._finish_end()
        except (RuntimeError, AttributeError):
            pass

    def horizontal_press(self, event):
        if self.orientation() == Qt.Orientation.Horizontal:
            _reveal_live(_outer_surface(self))
        return old_bar_press(self, event)

    QScrollBar.mousePressEvent = horizontal_press


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
