"""Search-suggestion list polish only.

This patch intentionally touches only GlobalSearch's visual suggestion QListWidget:
- per-pixel, slower wheel travel;
- app-theme frame/background;
- Movies-style ACCENT_SOFT + ACCENT hover with a short fade;
- pointing-hand cursor only while the pointer is over a real suggestion row.

No other app scroll surface is modified.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QRectF
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractItemView, QStyledItemDelegate

_INSTALLED = False
_NOTCH_SCALE = 0.50
_HOVER_MS = 140


def _with_alpha(colour, amount: float):
    c = QColor(colour)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * float(amount))))
    return c


class _SuggestionDelegate(QStyledItemDelegate):
    """Paint only Atomic's animated hover plate behind each item widget."""

    def __init__(self, view, theme):
        super().__init__(view)
        self._view = view
        self._theme = theme
        self._hover_row = -1
        self._previous_row = -1
        self._mix = 1.0

    def set_transition(self, previous_row: int, hover_row: int, mix: float):
        self._previous_row = int(previous_row)
        self._hover_row = int(hover_row)
        self._mix = max(0.0, min(1.0, float(mix)))

    def _amount_for(self, row: int) -> float:
        if row == self._hover_row:
            return self._mix
        if row == self._previous_row:
            return 1.0 - self._mix
        return 0.0

    def paint(self, painter, option, index):
        amount = self._amount_for(index.row())
        if amount <= 0.001:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(2.5, 1.5, -2.5, -1.5)
        painter.setBrush(_with_alpha(self._theme.ACCENT_SOFT, amount))
        painter.setPen(QPen(_with_alpha(self._theme.ACCENT, amount), 1.0))
        painter.drawRoundedRect(rect, self._theme.RADIUS, self._theme.RADIUS)
        painter.restore()


class _SuggestionHover(QObject):
    """Hover/cursor state for one suggestion viewport."""

    def __init__(self, search, view, theme, widgets):
        super().__init__(view)
        self._search = search
        self._view = view
        self._viewport = view.viewport()
        self._theme = theme
        self._widgets = widgets
        self._delegate = _SuggestionDelegate(view, theme)
        self._view.setItemDelegate(self._delegate)

        self._hover_row = -1
        self._previous_row = -1
        self._mix = 1.0
        self._holding_cursor = False
        self._disabled = False
        self._tween = widgets.SmoothTween(view, self._on_tween, _HOVER_MS)

        self._viewport.setMouseTracking(True)
        self._viewport.installEventFilter(self)

    def _row_at(self, pos):
        index = self._view.indexAt(pos)
        return index.row() if index.isValid() else -1

    def _set_cursor(self, over_item: bool):
        if over_item and not self._holding_cursor:
            self._widgets.hold_hover_cursor(self._viewport)
            self._holding_cursor = True
        elif not over_item and self._holding_cursor:
            self._widgets.release_hover_cursor(self._viewport)
            self._holding_cursor = False

    def _move_to(self, row: int):
        self._disabled = False
        row = int(row)
        if row == self._hover_row:
            self._set_cursor(row >= 0)
            return

        self._previous_row = self._hover_row
        self._hover_row = row
        self._mix = 0.0
        self._delegate.set_transition(self._previous_row, self._hover_row, 0.0)
        self._viewport.update()
        self._set_cursor(row >= 0)
        self._tween.start(0.0, 1.0, _HOVER_MS)

    def _on_tween(self, value):
        if self._disabled:
            return
        self._mix = float(value)
        self._delegate.set_transition(self._previous_row, self._hover_row, self._mix)
        self._viewport.update()
        if self._mix >= 0.999:
            self._previous_row = -1
            self._delegate.set_transition(-1, self._hover_row, 1.0)

    def reset(self):
        self._disabled = True
        self._previous_row = -1
        self._hover_row = -1
        self._mix = 1.0
        self._delegate.set_transition(-1, -1, 1.0)
        self._set_cursor(False)
        self._viewport.update()

    def eventFilter(self, obj, event):
        if obj is not self._viewport:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            try:
                self._move_to(self._row_at(event.position().toPoint()))
            except (AttributeError, RuntimeError):
                self._move_to(-1)
        elif kind == QEvent.Type.Leave:
            self._move_to(-1)
        return False

    def detach(self):
        self.reset()
        try:
            self._viewport.removeEventFilter(self)
        except RuntimeError:
            pass


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from helpers import global_search, theme
    from helpers import widgets

    Search = global_search.GlobalSearch
    old_init = Search.__init__
    old_set_query = Search.set_query
    old_close = Search.closeEvent

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        view = getattr(self, "_visual_list", None)
        if view is None:
            return

        # Search suggestions only: finer pixels and half the ordinary Atomic
        # wheel distance. The already-installed _SmoothWheel reads notch_scale
        # dynamically, so this changes no other list or page.
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.notch_scale = _NOTCH_SCALE

        # Replace the black/transparent list edge with Atomic's own panel
        # surface and border tokens. The dimmed overlay behind it is unchanged.
        view.setStyleSheet(
            f"QListWidget#UnifiedSearchList {{"
            f" background: {theme.PANEL_FILL};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px;"
            f" outline: 0; padding: 4px;"
            f"}}"
            f"QListWidget#UnifiedSearchList::item {{"
            f" background: transparent; border: none; padding: 0px;"
            f"}}"
        )

        self._atomic_suggestion_hover = _SuggestionHover(self, view, theme, widgets)

    def set_query(self, query):
        hover = getattr(self, "_atomic_suggestion_hover", None)
        if hover is not None:
            hover.reset()
        return old_set_query(self, query)

    def close_event(self, event):
        hover = getattr(self, "_atomic_suggestion_hover", None)
        if hover is not None:
            hover.detach()
        return old_close(self, event)

    Search.__init__ = init
    Search.set_query = set_query
    Search.closeEvent = close_event
