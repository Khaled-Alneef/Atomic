"""Search-suggestion list polish only.

This patch intentionally touches only GlobalSearch's visual suggestion surface:
- per-pixel, slower wheel travel;
- a genuinely rounded app-theme panel/background;
- stronger teal animated hover plus the app's pointing-hand cursor;
- background-click dismissal that also exits the title-bar search field;
- one reliable remote-art delivery path for Watch + Read suggestions.

No other app scroll surface is modified.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractItemView, QListWidgetItem, QStyledItemDelegate

_INSTALLED = False
_NOTCH_SCALE = 0.50
_HOVER_MS = 140


class _ArtSignals(QObject):
    ready = Signal(str, str, str)  # query, row token, cached path or ""


_art_signals = _ArtSignals()


def _with_alpha(colour, amount: float):
    c = QColor(colour)
    c.setAlphaF(max(0.0, min(1.0, c.alphaF() * float(amount))))
    return c


class _SuggestionDelegate(QStyledItemDelegate):
    """Paint Atomic's animated teal hover plate behind each item widget."""

    def __init__(self, view, theme):
        super().__init__(view)
        self._view = view
        self._theme = theme
        # ACCENT_SOFT is intentionally subtle on cards/nav. Search suggestions
        # need a clearer teal affordance, so lift it toward the app's teal
        # accent while keeping it in the exact same palette family.
        self._hover_fill = theme.mix(theme.ACCENT_SOFT, theme.ACCENT, 0.42)
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
        rect = QRectF(option.rect).adjusted(3.0, 1.5, -3.0, -1.5)
        painter.setBrush(_with_alpha(self._hover_fill, amount))
        painter.setPen(QPen(_with_alpha(self._theme.ACCENT_HOVER, amount), 1.0))
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


def _poster_url(row):
    if not isinstance(row, dict):
        return ""
    for key in ("poster", "cover_url", "cover", "image"):
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def _art_worker(module, query, token, url, logical_size,
                title="", kind="", imdb_id=""):
    """Resolve + prewarm one suggestion image, then return it to Qt.

    cover_fetch.resolve rather than a bare download: **the reading
    sites' search cards usually carry no cover at all** (measured 30
    August 2026 - 8 of 8 Mangalek rows, empty cover_url), so a Read
    suggestion had nothing to fetch and stayed the grey tile forever -
    the owner's "the watch and read images do not load in the search
    suggestion list". resolve() is the same guarded catalogue chain the
    tracker cards use (MangaDex/AniList for reading and anime, TMDB by
    IMDb id for video), so a coverless row gets its card's own art, and
    a poster URL that refuses still falls through to a second source."""
    path = ""
    try:
        from helpers import cover_fetch, images

        found = cover_fetch.resolve(url, imdb_id=imdb_id, title=title,
                                    kind=kind)
        if found:
            path = str(found)
            # Pillow-only decode/crop here means the UI callback normally only
            # has the cheap QPixmap conversion left to do.
            try:
                images.warm(path, tuple(logical_size))
            except Exception:
                pass
    except Exception:
        module.logs.exception("Global search: suggestion artwork failed")
    finally:
        _art_signals.ready.emit(query, token, path)


def _seed_from_cache(search, query):
    """Fill the panel from the discover cache before the network answers.

    **The instant half existed and was never running.** The owner, 4
    September 2026: "the search results and the search suggestions takes
    too long to appear". `global_search.set_query` has called
    `_cached_outside` since it was written - its own comment says "the
    panel is never empty while the network thinks" - but *this* module
    replaces `set_query` outright, and so does the visual patch before
    it, and neither replacement carried that line. So the cache was dead
    code and every keystroke waited on the socket.

    That is `.claude/rules/ui.md`'s "the second patch wins", in the one
    place it costs seconds: measured on the frozen build that day, typing
    "solo" showed "Searching..." and nothing else for **2.2 to 2.7
    seconds**, while `_cached_outside` for the same query answers in
    **0.00s** off a file this machine already holds.

    Seeded through `_on_discover_ready` rather than a second row builder,
    so a cached row is built exactly like a live one and the panel's own
    `_seen` set drops it when the real answer repeats it. The searching
    state is put back afterwards, because that method ends by declaring
    the search settled and it is not - the network half is still out.
    """
    if not str(query or "").strip():
        return
    try:
        from helpers import global_search as _gs
        rows = _gs._cached_outside(query) or []
    except Exception:
        return
    if not rows:
        return
    try:
        search._on_discover_ready(query, rows)
        if query == getattr(search, "_query", None):
            search._visual_status.setText("Searching…")
            search._relayout(searching=True)
    except (AttributeError, RuntimeError):
        pass


def _exit_search_field(search):
    """Close suggestions and leave the persistent search field cleanly."""
    anchor = getattr(search, "_anchor", None)
    window = getattr(search, "_window", None)
    try:
        search.close()
    except RuntimeError:
        pass

    # Defer clearing until the mouse event has fully unwound. Clearing emits
    # textChanged, which also owns panel teardown; doing both while the dialog
    # is handling its own press event risks deleting the receiver mid-handler.
    def finish():
        if anchor is not None:
            try:
                anchor.clear()
                anchor.clearFocus()
            except RuntimeError:
                pass
        if window is not None:
            try:
                page = getattr(window, "_current_page", None)
                if page is not None:
                    page.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass

    QTimer.singleShot(0, finish)


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from helpers import global_search, theme
    from helpers import global_search_visual_patch as visual
    from helpers import widgets

    Search = global_search.GlobalSearch
    old_init = Search.__init__
    old_set_query = Search.set_query
    old_close = Search.closeEvent
    old_mouse_press = Search.mousePressEvent

    def init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        view = getattr(self, "_visual_list", None)
        panel = getattr(self, "_visual_content", None)
        if view is None or panel is None:
            return

        # Search suggestions ONLY: finer pixels and half the ordinary Atomic
        # wheel distance. The installed _SmoothWheel reads notch_scale
        # dynamically, so this changes no other list or page.
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.notch_scale = _NOTCH_SCALE
        # **And in pixels, because nothing reads notch_scale any more.**
        # It was widgets._SmoothWheel that applied it, and that no longer
        # takes the wheel at all (the owner asked for plain scrolling
        # everywhere, 1 September 2026) - so this list went from half the
        # app's notch to Qt's own three lines, which is faster than it
        # has ever been. His "decrease the speed of search suggestion
        # scrolling" is that regression. Qt moves three single-steps per
        # notch, so the step is a third of the distance wanted.
        try:
            step = max(4, int(round(120.0 * _NOTCH_SCALE / 3.0)))
            view.verticalScrollBar().setSingleStep(step)
        except Exception:
            pass

        # The rounded surface belongs to the real outer container, not the
        # QListWidget viewport. A styled scroll viewport is rectangular and was
        # the sharp-corner slab visible behind the rounded list frame.
        panel.setObjectName("UnifiedSearchPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel.setStyleSheet(
            f"QWidget#UnifiedSearchPanel {{"
            f" background: {theme.PANEL_FILL};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px;"
            f"}}"
        )
        view.setStyleSheet(
            "QListWidget#UnifiedSearchList {"
            " background: transparent; border: none; outline: 0; padding: 4px;"
            "}"
            "QListWidget#UnifiedSearchList::item {"
            " background: transparent; border: none; padding: 0px;"
            "}"
        )
        view.viewport().setStyleSheet("background: transparent; border: none;")

        self._atomic_suggestion_hover = _SuggestionHover(self, view, theme, widgets)
        _art_signals.ready.connect(self._atomic_polish_art_ready)

    def set_query(self, query):
        hover = getattr(self, "_atomic_suggestion_hover", None)
        if hover is not None:
            hover.reset()
        result = old_set_query(self, query)
        _seed_from_cache(self, query)
        return result

    def on_discover_ready(self, query, rows):
        """Insert remote rows and own their image delivery exactly once."""
        if query != getattr(self, "_query", None):
            return

        self._discover_rows = list(rows or [])
        from helpers import images

        for row in self._discover_rows:
            title = str(row.get("title") or "").strip()
            entry_type = str(
                row.get("_atomic_entry_type") or row.get("type") or "Series"
            )
            # A face is not a title: it opens that name's own page, so
            # it carries a different payload and a different meta line.
            # visual.add_face_row is the one implementation of it, and
            # it lives there because *this* method replaces the one in
            # that module - see its docstring.
            if entry_type == "Person":
                from helpers import global_search as _gs
                visual.add_face_row(self, _gs, query, row,
                                    self._atomic_add_visual_item)
                continue
            route = visual._route_for_entry_type(entry_type)
            key = (route, title.lower())
            if not title or key in self._seen:
                continue

            meta = visual._meta_text(
                entry_type,
                row.get("year") or "",
                row.get("imdbRating") or "",
                "",
            )
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, ("discover", entry_type, row))

            poster = _poster_url(row)
            token = f"{query}\x1f{route}\x1f{title}\x1f{poster}"
            item.setData(visual._TOKEN_ROLE, token)

            path = ""
            existing = row.get("cover_path")
            cached = images.cache_path_for_url(poster) if poster else None
            if existing:
                path = str(existing)
            elif cached is not None and cached.is_file():
                path = str(cached)

            self._atomic_add_visual_item(item, title, meta, path)
            self._seen.add(key)

            # _make_row may delete a corrupt cached image while decoding it.
            # Re-check the cache after insertion and queue exactly one fresh
            # resolve when there is no valid file left. **Whether or not the
            # row carries a poster URL** - a reading row usually does not
            # (see _art_worker), and the catalogue fallback exists for
            # exactly that case.
            cached = images.cache_path_for_url(poster) if poster else None
            if cached is None or not cached.is_file():
                kind = ("reading" if route == "manga"
                        else ("anime" if entry_type.strip().lower() == "anime"
                              else "video"))
                global_search.lookup_pool.submit_cover(
                    _art_worker,
                    global_search,
                    query,
                    token,
                    poster,
                    (visual.THUMB_W, visual.THUMB_H),
                    title,
                    kind,
                    str(row.get("imdb_id") or ""),
                )

        count = self._visual_list.count()
        self._visual_status.setText(
            "" if count else "No suggestions — press Enter to search Discover."
        )
        if count and self._visual_list.currentRow() < 0:
            self._visual_list.setCurrentRow(0)
        self._relayout(searching=False)

    def art_ready(self, query, token, path):
        if query != getattr(self, "_query", None) or not path:
            return
        for index in range(self._visual_list.count()):
            item = self._visual_list.item(index)
            if item.data(visual._TOKEN_ROLE) == token:
                visual._set_row_art(
                    global_search, self._visual_list.itemWidget(item), path
                )
                return

    def mouse_press(self, event):
        panel = getattr(self, "_visual_content", None)
        try:
            point = event.position().toPoint()
        except AttributeError:
            point = event.pos()

        # A press on the dimmed background, not on the rounded panel, dismisses
        # the whole search mode. Clicks inside the list keep their normal direct
        # Watch/Read open behavior.
        if panel is not None and panel.geometry().contains(point):
            return old_mouse_press(self, event)

        event.accept()
        _exit_search_field(self)

    def close_event(self, event):
        hover = getattr(self, "_atomic_suggestion_hover", None)
        if hover is not None:
            hover.detach()
        try:
            _art_signals.ready.disconnect(self._atomic_polish_art_ready)
        except (TypeError, RuntimeError):
            pass
        return old_close(self, event)

    Search.__init__ = init
    Search.set_query = set_query
    Search._on_discover_ready = on_discover_ready
    Search._atomic_polish_art_ready = art_ready
    Search.mousePressEvent = mouse_press
    Search.closeEvent = close_event
