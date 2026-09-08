"""Global-search interaction fixes.

The suggestion surface is a separate non-activating Qt Tool window.  Two things
follow from that:

* a click on the page behind it can be delivered straight to MainWindow and
  never reach GlobalSearch.mousePressEvent;
* because GlobalSearch is positioned in global coordinates, dragging the main
  window does not guarantee that the Tool window is repositioned with it.

An application event filter is the correct seam because it sees mouse presses
and main-window move/resize events regardless of which top-level Qt window got
them.  It is installed only while a GlobalSearch instance exists.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QWidget

_INSTALLED = False


class _SearchInteractionFilter(QObject):
    def __init__(self, search):
        super().__init__(search)
        self._search = search
        self._app = QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)

    @staticmethod
    def _contains(widget, global_pos) -> bool:
        if widget is None:
            return False
        try:
            if not widget.isVisible():
                return False
            top_left = widget.mapToGlobal(QPoint(0, 0))
            rect = widget.rect()
            rect.moveTopLeft(top_left)
            return rect.contains(global_pos)
        except (AttributeError, RuntimeError):
            return False

    def _reposition(self):
        try:
            if self._search.isVisible():
                self._search.place()
                self._search.raise_()
        except (AttributeError, RuntimeError):
            pass

    def eventFilter(self, obj, event):
        search = self._search
        kind = event.type()

        # Keep the separate Tool window physically attached to Atomic while the
        # main window is dragged/resized, including a drag onto another monitor.
        try:
            window = getattr(search, "_window", None)
            if obj is window and kind in (
                QEvent.Type.Move,
                QEvent.Type.Resize,
                QEvent.Type.WindowStateChange,
            ):
                self._reposition()
                return False
        except RuntimeError:
            return False

        if kind != QEvent.Type.MouseButtonPress:
            return False
        try:
            if not search.isVisible():
                return False
        except RuntimeError:
            return False

        try:
            global_pos = event.globalPosition().toPoint()
        except (AttributeError, RuntimeError):
            global_pos = QCursor.pos()

        # These are the only two regions that mean "stay in search": the field
        # itself and the actual suggestion list (scrollbar included).  A click
        # anywhere else exits the search bar completely, as Escape does.
        if self._contains(getattr(search, "_anchor", None), global_pos):
            return False
        if self._contains(getattr(search, "_visual_list", None), global_pos):
            return False

        from helpers import global_search_list_polish_patch as polish
        polish._exit_search_field(search)

        # If the click belonged to the overlay being closed, consume it so Qt
        # does not continue dispatching into a WA_DeleteOnClose hierarchy.  If
        # it belongs to the underlying app, let that click do what the user
        # intended after search gets out of the way.
        try:
            if obj is search or (
                isinstance(obj, QWidget) and search.isAncestorOf(obj)
            ):
                return True
        except RuntimeError:
            return True
        return False

    def detach(self):
        app, self._app = self._app, None
        if app is not None:
            try:
                app.removeEventFilter(self)
            except RuntimeError:
                pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from helpers import global_search

    Search = global_search.GlobalSearch
    old_init = Search.__init__
    old_close = Search.closeEvent

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self._atomic_search_interaction_filter = _SearchInteractionFilter(self)

    def close_event(self, event):
        watcher = getattr(self, "_atomic_search_interaction_filter", None)
        if watcher is not None:
            watcher.detach()
            self._atomic_search_interaction_filter = None
        return old_close(self, event)

    Search.__init__ = init
    Search.closeEvent = close_event
