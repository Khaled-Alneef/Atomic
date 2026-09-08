"""Restore the exact 1.10.139 player top bar and fix chapter unread boundaries.

Player core windows/player.py has not changed since 1.10.139; later helper
patches promoted/re-styled/repositioned the top bar. Restore the original core
top-bar methods at the final patch boundary so its parenting, hit testing,
hover/click behaviour and geometry are exactly the known-good 1.10.139 path.
Playback/startup methods are deliberately untouched.

Reading progress is contiguous by chapter number. Marking chapter N unread
clears N and every newer chapter and leaves the stored boundary at the largest
chapter below N, regardless of newest->oldest visual row order.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_INSTALLED = False
_PATCHED = set()


def _find_core(function, module_name, function_name, seen=None):
    if seen is None:
        seen = set()
    if not callable(function) or id(function) in seen:
        return None
    seen.add(id(function))
    if (getattr(function, "__module__", None) == module_name
            and getattr(function, "__name__", None) == function_name):
        return function
    for cell in getattr(function, "__closure__", ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value):
            found = _find_core(value, module_name, function_name, seen)
            if found is not None:
                return found
    original = getattr(function, "_atomic_original", None)
    if callable(original):
        return _find_core(original, module_name, function_name, seen)
    return None


def _restore_player_topbar(module):
    key = ("player-topbar-139", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.PlayerPage

    # windows/player.py is byte-for-byte unchanged from the 1.10.139 baseline.
    # These are precisely the methods changed by the later top-bar experiments.
    # Putting their original definitions back means the bar is once again the
    # normal 1.10.139 child/native control surface; no Qt.Tool promotion, mask,
    # follower, z-order timer, or post-build restyling remains in the hot path.
    for name in ("__init__", "_build_top_bar", "_layout_overlays",
                 "_wake_controls", "_pointer_in", "_widget_rect", "_veil"):
        current = getattr(Page, name, None)
        core = _find_core(current, "windows.player", name)
        if core is not None:
            setattr(Page, name, core)


def _patch_details(module):
    key = ("details-unread", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.DetailsPage

    def chapter_menu(self, event, number):
        if number is None:
            return
        try:
            from PyQt6.QtWidgets import QMenu
            from windows.reader import chapter_number
            from windows.tracker import correct_progress
            from helpers import history
        except ImportError:
            return

        values = sorted({float(v) for v in
                         (chapter_number(c) for c in self._chapters)
                         if v is not None})
        if not values:
            return
        number = float(number)
        last = float(self._last_read() or 0.0)
        already = bool((last and number <= last)
                       or history.chapter_key(number) in self._history_marks)

        menu = QMenu(self)
        mark = menu.addAction("Mark as Unread" if already else "Mark as Read")
        menu.addSeparator()
        mark_all = menu.addAction("Mark All as Read")
        clear_all = menu.addAction("Mark All as Unread")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is None:
            return

        if chosen is mark:
            if already:
                # N and EVERY newer chapter become unread. Do not derive this
                # from the row index: the list is newest->oldest and providers
                # may contain gaps/duplicates, while progress is numeric.
                affected = [v for v in values if v >= number]
                older = [v for v in values if v < number]
                target = max(older) if older else 0.0
                read = False
            else:
                affected = [v for v in values if v <= number]
                target = number
                read = True
        elif chosen is mark_all:
            affected = values
            target = max(values)
            read = True
        elif chosen is clear_all:
            affected = values
            target = 0.0
            read = False
        else:
            return

        self._mark_history([history.chapter_key(v) for v in affected], read)
        if self.entry.get("id") and not correct_progress(self.entry, chapter=target):
            module.show_toast(self, "Could Not Save That")
            return
        self._fill_rows()

    Page._chapter_menu = chapter_menu


def _patch_reader(module):
    key = ("reader-unread", id(module))
    if key in _PATCHED:
        return
    _PATCHED.add(key)
    Page = module.ReaderPage
    old_mark = Page._mark_chapter

    def mark(self, index, finished):
        if finished or not (0 <= index < len(self.chapters)):
            return old_mark(self, index, finished)
        number = module.chapter_number(self.chapters[index])
        if number is None:
            return old_mark(self, index, finished)

        values = sorted({float(v) for v in
                         (module.chapter_number(c) for c in self.chapters)
                         if v is not None})
        number = float(number)
        affected = [v for v in values if v >= number]
        older = [v for v in values if v < number]
        target = max(older) if older else 0.0

        module._mark_history(self.entry, affected, False)
        if (not module.correct_progress(self.entry, chapter=target)
                and self.entry.get("id")):
            module.show_toast(self, "Could Not Save That Chapter")
            return
        current = (self.chapter_index
                   if 0 <= self.chapter_index < len(self.chapters)
                   else self._row_for_number(target))
        self._list_view.set_chapters(
            self.chapters, current, self._last_read_number())
        self._sync_controls()

    Page._mark_chapter = mark


class _DetailsLoader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        _patch_details(module)


class _DetailsFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "windows.details":
            return None
        # Ask the standard PathFinder directly so this finder cannot recurse.
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or isinstance(spec.loader, _DetailsLoader):
            return spec
        spec.loader = _DetailsLoader(spec.loader)
        return spec


def _chain():
    from . import requested_fixes_patch as requested

    previous_player = requested._patch_player
    def player(module):
        previous_player(module)
        _restore_player_topbar(module)
    requested._patch_player = player

    loaded = sys.modules.get("windows.player")
    if loaded is not None:
        _restore_player_topbar(loaded)

    # Reader already has a shared lazy hook; chain the numeric unread boundary
    # after the existing newest->oldest presentation patch.
    previous_reader = requested._patch_reader
    def reader_patch(module):
        previous_reader(module)
        _patch_reader(module)
    requested._patch_reader = reader_patch

    reader = sys.modules.get("windows.reader")
    if reader is not None:
        _patch_reader(reader)

    # Details may load after helpers bootstrap, so patch immediately when it is
    # already present and otherwise install one narrow one-shot loader wrapper.
    details = sys.modules.get("windows.details")
    if details is not None:
        _patch_details(details)
    elif not any(isinstance(f, _DetailsFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _DetailsFinder())


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _chain()
