"""Make chapter-row context menus reliable across the whole row.

The details page already has the same reading context actions as the episode
list, but only the parent Card emits rightClicked. Child controls inside chapter
rows (play button, labels/badges) can consume the right mouse press first, so the
menu appears to be missing depending on where the user clicks.

Forward right-click presses from chapter-row descendants to the existing
DetailsPage._chapter_menu callback. Episode behavior, playback and scrolling are
untouched.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

_INSTALLED = False
_PATCHED = set()


def _patch_details(module):
    key = id(module)
    if key in _PATCHED:
        return
    _PATCHED.add(key)

    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QWidget

    Page = module.DetailsPage
    old_row_card = Page._row_card

    class _ChapterRightClickForwarder(QObject):
        def __init__(self, card, callback):
            super().__init__(card)
            self.card = card
            self.callback = callback

        def eventFilter(self, watched, event):
            try:
                if (event.type() == QEvent.Type.MouseButtonPress
                        and event.button() == Qt.MouseButton.RightButton):
                    # The parent Card's existing signal handles clicks that land
                    # directly on the card. This filter exists only on child
                    # widgets, so one physical click opens exactly one menu.
                    self.callback(event)
                    return True
            except (AttributeError, RuntimeError):
                pass
            return False

    def row_card(self, title, date_text, badge, on_click, on_menu=None,
                 variant="chapter", still_url=None):
        card = old_row_card(self, title, date_text, badge, on_click,
                            on_menu=on_menu, variant=variant,
                            still_url=still_url)
        if variant != "chapter" or on_menu is None or card is None:
            return card

        try:
            forwarder = _ChapterRightClickForwarder(card, on_menu)
            # Keep a strong Python reference as well as QObject parentage; PyQt
            # wrappers can otherwise be collected while the C++ object lives.
            card._atomic_chapter_context_forwarder_154 = forwarder
            for child in card.findChildren(QWidget):
                child.installEventFilter(forwarder)
        except (AttributeError, RuntimeError):
            pass
        return card

    Page._row_card = row_card


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self.wrapped, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self.wrapped.exec_module(module)
        _patch_details(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "windows.details":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or isinstance(spec.loader, _Loader):
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    loaded = sys.modules.get("windows.details")
    if loaded is not None:
        _patch_details(loaded)
        return

    # Install once and only for the details page; no global mouse behavior is
    # changed.
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
