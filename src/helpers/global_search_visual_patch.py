"""One mixed, scrollable visual list for Atomic's global search.

The persistent title-bar search keeps its existing Enter behavior: Enter runs the
full query on Discover.  Typing additionally opens this Harbor-like dimmed
suggestion surface.  Watch and Read matches share one list, each row carries its
artwork, and clicking a row navigates to the matching Watch/Read page and opens
the title's normal episode/chapter details list.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
import threading

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

_SEARCH_TARGET = "helpers.global_search"
_CHROME_TARGET = "helpers.window_chrome"
_INSTALLED = False
_SEARCH_PATCHED = False
_CHROME_PATCHED = False

THUMB_W = 56
THUMB_H = 82
ROW_HEIGHT = 94
VISIBLE_ROWS = 7
MAX_REMOTE_RESULTS = 24
SEARCH_EACH = 10
CONTENT_MAX_W = 820
CONTENT_MIN_W = 620
CONTENT_SIDE_MARGIN = 40
_TOKEN_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class _VisualSignals(QObject):
    ready = Signal(str, str, str)  # query, row token, cached path or ""


_visual_signals = _VisualSignals()


class _AnchorFilter(QObject):
    """Keep the field focused while allowing keyboard list navigation.

    Up/Down moves the highlighted suggestion.  Enter/Escape only close the
    surface and then fall through to MainWindow, where Enter already means
    "full Discover search" and Escape already means "leave search".
    """

    def __init__(self, panel, anchor):
        super().__init__(panel)
        self._panel = panel
        self._anchor = anchor

    def eventFilter(self, obj, event):
        if obj is not self._anchor or event.type() != QEvent.Type.KeyPress:
            return False
        key = event.key()
        if key == Qt.Key.Key_Down:
            self._panel.move_selection(1)
            return True
        if key == Qt.Key.Key_Up:
            self._panel.move_selection(-1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            try:
                self._panel.close()
            except RuntimeError:
                pass
        return False


def _clear_panel_ref(window, panel):
    try:
        if getattr(window, "_search_panel", None) is panel:
            window._search_panel = None
    except RuntimeError:
        pass


def _close_window_panel(window):
    panel = getattr(window, "_search_panel", None)
    if panel is None:
        return
    try:
        panel.close()
    except RuntimeError:
        window._search_panel = None


def _show_from_titlebar(bar, text):
    query = str(text or "")
    try:
        window = bar.window()
    except RuntimeError:
        return
    if window is None or not hasattr(window, "_search_panel"):
        return

    if not query.strip():
        _close_window_panel(window)
        return

    panel = getattr(window, "_search_panel", None)
    if panel is not None:
        try:
            panel.set_query(query)
            panel.show()
            panel.raise_()
            panel.place()
            return
        except RuntimeError:
            window._search_panel = None

    try:
        from helpers import global_search

        panel = global_search.GlobalSearch(window, anchor=bar.search)
        window._search_panel = panel
        panel.closed.connect(lambda w=window, p=panel: _clear_panel_ref(w, p))
        panel.set_query(query)
        panel.show()
        panel.raise_()
        panel.place()
    except Exception:
        try:
            from helpers import logs

            logs.exception("Could not open global search suggestions")
        except Exception:
            pass


def _local_art_path(entry):
    for key in ("cover_path", "cover", "art", "image", "icon", "icon_path"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _route_for_entry_type(entry_type):
    return "manga" if str(entry_type or "").strip().lower() in (
        "manga", "manhwa", "manhua"
    ) else "series"


def _display_kind(entry_type):
    route = _route_for_entry_type(entry_type)
    return "Read" if route == "manga" else "Watch"


def _meta_text(entry_type, year="", rating="", suffix=""):
    bits = [_display_kind(entry_type), str(entry_type or "").strip()]
    if year:
        bits.append(str(year).strip())
    if rating:
        bits.append(f"★ {str(rating).strip()}")
    if suffix:
        bits.append(str(suffix))
    return "  •  ".join(bit for bit in bits if bit)


def _cover_worker(module, query, token, url, title="", kind="", imdb_id=""):
    """Resolve one suggestion's artwork, with every fallback the cards
    already have.

    cover_fetch.resolve, not a bare images.download - **the reading
    sites' search cards carry no cover at all** (measured 30 August
    2026: 8 of 8 Mangalek rows had an empty cover_url), so a Read
    suggestion had literally nothing to download and stayed the grey
    tile forever, which is the owner's "the watch and read images do
    not load in the search suggestion list". resolve() asks the same
    strictly-matched catalogues the tracker cards ask (MangaDex/AniList
    for reading and anime, TMDB by IMDb id for video), so a coverless
    row gets the art its card would have."""
    path = ""
    try:
        from helpers import cover_fetch

        found = cover_fetch.resolve(url, imdb_id=imdb_id, title=title,
                                    kind=kind)
        if found:
            path = str(found)
    except Exception:
        module.logs.exception("Global search: suggestion artwork failed")
    finally:
        _visual_signals.ready.emit(query, token, path)


def _cover_kind(entry_type, route):
    if route == "manga":
        return "reading"
    return "anime" if str(entry_type or "").strip().lower() == "anime" \
        else "video"


def _search_worker(module, query):
    """Fetch Watch + Read suggestions in parallel, then emit one mixed list."""
    found = {"series": [], "movie": [], "reading": []}
    lock = threading.Lock()
    done = threading.Event()
    remaining = 3

    def finish(kind, rows):
        nonlocal remaining
        with lock:
            found[kind] = list(rows or [])
            remaining -= 1
            if remaining <= 0:
                done.set()

    def video(kind):
        rows = []
        try:
            rows = module.discover.discover_video(kind, query, limit=SEARCH_EACH) or []
        except Exception:
            module.logs.exception(f"Global search: {kind} suggestions failed")
        finish(kind, rows)

    def reading():
        rows = []
        try:
            # Prefer the same site-aware reading search used by Tracker when it
            # exists, because its URL makes the chapter list open immediately.
            search = getattr(module.discover, "discover_reading_sites", None)
            if callable(search):
                rows = search(query=query, limit=SEARCH_EACH) or []
            else:
                rows = module.discover.discover_reading(query, limit=SEARCH_EACH) or []
        except Exception:
            module.logs.exception("Global search: reading suggestions failed")
        finish("reading", rows)

    threading.Thread(target=video, args=("series",), daemon=True,
                     name="global-search-series").start()
    threading.Thread(target=video, args=("movie",), daemon=True,
                     name="global-search-movie").start()
    threading.Thread(target=reading, daemon=True,
                     name="global-search-reading").start()

    # Each provider already has its own bounded request timeout.  The outer
    # guard only prevents a broken provider from holding this latest-query job.
    done.wait(22.0)

    rows = []
    for source in ("series", "movie", "reading"):
        for original in found[source]:
            row = dict(original)
            if source == "reading":
                entry_type = str(row.get("type") or "").strip()
                if entry_type.lower() not in ("manga", "manhwa", "manhua"):
                    entry_type = "Manga"
            elif source == "movie":
                entry_type = "Movie"
            else:
                entry_type = str(row.get("type") or "Series").strip() or "Series"
                if entry_type.lower() not in ("anime", "series"):
                    entry_type = "Series"
            row["_atomic_entry_type"] = entry_type
            rows.append(row)

    needle = query.strip().lower()
    rows.sort(key=lambda row: (
        not str(row.get("title") or "").lower().startswith(needle),
        str(row.get("title") or "").lower(),
        0 if _route_for_entry_type(row.get("_atomic_entry_type")) == "series" else 1,
    ))
    module._signals.ready.emit(query, rows[:MAX_REMOTE_RESULTS])


def _make_row(module, title, meta, path=""):
    from helpers import images

    row = QWidget()
    row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    outer = QHBoxLayout(row)
    outer.setContentsMargins(8, 5, 12, 5)
    outer.setSpacing(13)

    thumb = QLabel()
    thumb.setFixedSize(THUMB_W, THUMB_H)
    thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    thumb.setStyleSheet(
        f"background:{module.theme.SURFACE_HOVER}; "
        f"border-radius:{max(4, module.theme.RADIUS - 3)}px;"
    )
    if path:
        try:
            pix = images.thumbnail_or_avatar(path, title, (THUMB_W, THUMB_H))
            if not pix.isNull():
                thumb.setPixmap(pix)
                thumb.setStyleSheet("background:transparent; border:none;")
        except Exception:
            pass
    outer.addWidget(thumb, 0, Qt.AlignmentFlag.AlignVCenter)

    text_box = QWidget()
    text_box.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    texts = QVBoxLayout(text_box)
    texts.setContentsMargins(0, 0, 0, 0)
    texts.setSpacing(5)
    texts.addStretch(1)

    title_label = QLabel(title)
    title_label.setWordWrap(True)
    title_label.setMaximumHeight(42)
    title_label.setStyleSheet(
        f"color:{module.theme.TEXT}; background:transparent; "
        "font-size:13px; font-weight:600;"
    )
    texts.addWidget(title_label)

    meta_label = QLabel(meta)
    meta_label.setStyleSheet(
        f"color:{module.theme.TEXT_MUTED}; background:transparent; font-size:11px;"
    )
    texts.addWidget(meta_label)
    texts.addStretch(1)
    outer.addWidget(text_box, 1)

    row._atomic_search_thumb = thumb
    row._atomic_search_title = title
    return row


def _set_row_art(module, widget, path):
    if widget is None or not path:
        return
    try:
        from helpers import images

        pix = images.thumbnail_or_avatar(
            path, getattr(widget, "_atomic_search_title", ""), (THUMB_W, THUMB_H)
        )
        label = getattr(widget, "_atomic_search_thumb", None)
        if label is not None and not pix.isNull():
            label.setPixmap(pix)
            label.setStyleSheet("background:transparent; border:none;")
    except (RuntimeError, AttributeError):
        pass
    except Exception:
        module.logs.exception("Global search: could not paint suggestion artwork")


def _patch_search(module):
    global _SEARCH_PATCHED
    if _SEARCH_PATCHED:
        return
    _SEARCH_PATCHED = True

    Search = module.GlobalSearch
    old_init = Search.__init__
    old_close = Search.closeEvent

    def init(self, window, anchor=None):
        old_init(self, window, anchor=anchor)
        self.results.hide()
        self.empty.hide()

        base = self.layout()
        base.setContentsMargins(0, 0, 0, 0)
        base.setSpacing(0)

        self.setObjectName("UnifiedSearchOverlay")
        self.setStyleSheet(
            "QDialog#UnifiedSearchOverlay {"
            " background-color: rgba(5, 7, 12, 222);"
            " border: none; border-radius: 0px;"
            "}"
            "QListWidget#UnifiedSearchList {"
            " background: transparent; border: none; outline: 0;"
            "}"
            "QListWidget#UnifiedSearchList::item {"
            " background: transparent; border: none; padding: 0px;"
            " border-radius: 8px;"
            "}"
            f"QListWidget#UnifiedSearchList::item:selected,"
            f"QListWidget#UnifiedSearchList::item:hover {{"
            f" background: {module.theme.SURFACE_HOVER}; border: none;"
            "}"
        )

        self._visual_content = QWidget()
        content = QVBoxLayout(self._visual_content)
        content.setContentsMargins(0, 14, 0, 12)
        content.setSpacing(8)

        self._visual_list = QListWidget()
        self._visual_list.setObjectName("UnifiedSearchList")
        self._visual_list.setFrameShape(QFrame.Shape.NoFrame)
        self._visual_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._visual_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._visual_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._visual_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._visual_list.setSpacing(2)
        module.smooth_scrolling(self._visual_list)
        self._visual_list.itemActivated.connect(self._open)
        self._visual_list.itemClicked.connect(self._open)
        content.addWidget(self._visual_list)

        self._visual_status = QLabel("")
        self._visual_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._visual_status.setStyleSheet(
            f"color:{module.theme.TEXT_MUTED}; background:transparent; font-size:12px;"
        )
        content.addWidget(self._visual_status)

        # **Left, not HCenter** - the horizontal position is computed in
        # place() from the search field, and a centering flag would
        # silently override it. See place() for the measurement.
        base.addWidget(
            self._visual_content,
            0,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )

        self._seen = set()
        _visual_signals.ready.connect(self._on_visual_thumb_ready)
        self._anchor_filter = None
        if self._anchor is not None:
            self._anchor_filter = _AnchorFilter(self, self._anchor)
            self._anchor.installEventFilter(self._anchor_filter)

    def add_item(self, item, title, meta, path=""):
        item.setSizeHint(QSize(0, ROW_HEIGHT))
        self._visual_list.addItem(item)
        self._visual_list.setItemWidget(item, _make_row(module, title, meta, path))

    def add_saved(self, query):
        for title, page, _label, entry in module.collect(query):
            if page not in ("series", "manga"):
                continue
            entry_type = str(entry.get("type") or ("Manga" if page == "manga" else "Series"))
            route = "manga" if page == "manga" else "series"
            key = (route, title.strip().lower())
            if not title.strip() or key in self._seen:
                continue
            meta = _meta_text(
                entry_type,
                entry.get("year") or entry.get("release_year") or "",
                "",
                "Saved",
            )
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, ("saved", page, entry))
            art = _local_art_path(entry)
            if art and not os.path.exists(art):
                # A cover_path whose file the cache no longer holds is
                # the trap cover_fetch's docstring records - treat it as
                # absent so the fallback below can refill it.
                art = ""
            add_item(self, item, title, meta, art)
            self._seen.add(key)
            if not art:
                token = f"{query}\x1f{route}\x1f{title}\x1fsaved"
                item.setData(_TOKEN_ROLE, token)
                module.lookup_pool.submit_cover(
                    _cover_worker, module, query, token,
                    str(entry.get("cover_url") or ""), title,
                    _cover_kind(entry_type, route),
                    str(entry.get("imdb_id") or ""))

    def set_query(self, query: str):
        query = str(query or "")
        self._query = query
        self._rows = []
        self._discover_rows = []
        self._seen = set()
        self._visual_list.clear()

        if not query.strip():
            self._visual_status.setText("")
            self._relayout(searching=False)
            return False

        add_saved(self, query)
        self._visual_status.setText("Searching…")
        module.lookup_pool.submit_latest("global-search", _search_worker, module, query)
        self._relayout(searching=True)
        if self._visual_list.count():
            self._visual_list.setCurrentRow(0)
        return True

    def on_discover_ready(self, query, rows):
        if query != self._query:
            return
        self._discover_rows = list(rows or [])
        from helpers import images

        for row in self._discover_rows:
            title = str(row.get("title") or "").strip()
            entry_type = str(row.get("_atomic_entry_type") or row.get("type") or "Series")
            route = _route_for_entry_type(entry_type)
            key = (route, title.lower())
            if not title or key in self._seen:
                continue

            meta = _meta_text(
                entry_type,
                row.get("year") or "",
                row.get("imdbRating") or "",
                "",
            )
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, ("discover", entry_type, row))

            poster = str(row.get("poster") or row.get("cover_url") or "")
            token = f"{query}\x1f{route}\x1f{title}\x1f{poster}"
            item.setData(_TOKEN_ROLE, token)

            path = ""
            existing = row.get("cover_path")
            if existing:
                path = str(existing)
            elif poster:
                try:
                    cached = images.cache_path_for_url(poster)
                    if cached.is_file():
                        path = str(cached)
                except Exception:
                    path = ""

            add_item(self, item, title, meta, path)
            self._seen.add(key)
            # Whenever nothing is on disk yet - not only when a poster
            # URL exists. A reading row usually has NO url at all (see
            # _cover_worker), and the catalogue fallback is exactly for
            # that case.
            if not path:
                module.lookup_pool.submit_cover(
                    _cover_worker, module, query, token, poster,
                    title, _cover_kind(entry_type, route),
                    str(row.get("imdb_id") or ""))

        count = self._visual_list.count()
        self._visual_status.setText(
            "" if count else "No suggestions — press Enter to search Discover."
        )
        if count and self._visual_list.currentRow() < 0:
            self._visual_list.setCurrentRow(0)
        self._relayout(searching=False)

    def on_visual_thumb_ready(self, query, token, path):
        if query != self._query or not path:
            return
        for index in range(self._visual_list.count()):
            item = self._visual_list.item(index)
            if item.data(_TOKEN_ROLE) == token:
                _set_row_art(module, self._visual_list.itemWidget(item), path)
                return

    def relayout(self, searching):
        count = self._visual_list.count()
        visible_rows = max(1, min(VISIBLE_ROWS, count or 1))
        list_h = visible_rows * ROW_HEIGHT + 6
        self._visual_list.setFixedHeight(list_h)
        self._visual_list.setVisible(count > 0)

        status_text = self._visual_status.text()
        self._visual_status.setVisible(bool(status_text))
        status_h = 26 if status_text else 0
        self._visual_content.setFixedHeight(14 + list_h + status_h + 12)
        self.place()

    def place(self):
        frame = self._window.geometry()
        anchored = self._anchor is not None and self._anchor.isVisible()
        if anchored:
            point = self._anchor.mapTo(self._window, QPoint(0, 0))
            top = frame.y() + point.y() + self._anchor.height() + 6
        else:
            top = frame.y() + int(frame.height() * 0.14)

        bottom = frame.y() + frame.height()
        self.setGeometry(frame.x(), top, frame.width(), max(1, bottom - top))

        usable = max(320, frame.width() - CONTENT_SIDE_MARGIN * 2)
        width = min(CONTENT_MAX_W, max(CONTENT_MIN_W, int(frame.width() * 0.56)))
        width = min(width, usable)
        self._visual_content.setFixedWidth(width)

        # **Centred under the field, not under the window.** The overlay
        # spans the whole window (it carries the dim), and the content
        # used to be centred inside it with AlignHCenter - which is only
        # the same place when the search field happens to sit at the
        # window's centre. It does not: the field lives in the title bar
        # beside the window controls. Measured 30 August 2026 on the real
        # window, panel centre minus field centre:
        #
        #     1600px window   -4px    (looks aligned)
        #     full screen   -114px    (the owner's screenshot)
        #
        # so the bug only showed once the window got wide enough for the
        # two centres to separate. Clamped to the window so a field near
        # an edge cannot push the list off-screen.
        left = (frame.width() - width) // 2
        if anchored:
            point = self._anchor.mapTo(self._window, QPoint(0, 0))
            centred = point.x() + (self._anchor.width() - width) // 2
            limit = max(0, frame.width() - width - CONTENT_SIDE_MARGIN)
            left = max(min(centred, limit), min(CONTENT_SIDE_MARGIN, limit))
        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(max(0, left), 0, 0, 0)

    def move_selection(self, delta):
        count = self._visual_list.count()
        if count <= 0:
            return
        current = self._visual_list.currentRow()
        if current < 0:
            current = 0
        row = max(0, min(current + int(delta), count - 1))
        self._visual_list.setCurrentRow(row)
        self._visual_list.scrollToItem(self._visual_list.item(row))

    def open_item(self, item):
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or not payload:
            return
        self.close()
        try:
            from windows import tracker

            if payload[0] == "saved":
                _kind, page_name, entry = payload
                route = "manga" if page_name == "manga" else "series"
            elif payload[0] == "discover":
                _kind, entry_type, row = payload
                route = _route_for_entry_type(entry_type)
                entry = tracker.discover_entry(row, entry_type)
            else:
                return

            # Put the correct tracker page underneath the details overlay first,
            # so closing the episode/chapter list lands on Watch or Read rather
            # than on whichever unrelated page happened to own the search box.
            self._window.navigate_to(route, animate=False)
            parent = getattr(self._window, "_current_page", None) or self._window
            tracker.open_tracker_entry(parent, entry, resume=False)
        except Exception:
            module.logs.exception("Global search: could not open suggestion details")
            module.show_toast(self._window, "Could Not Open That Result")

    def open_current(self):
        # Enter belongs to MainWindow and intentionally goes to the normal
        # Discover search page.  Clicking a row is the direct episode/chapter
        # path above; these two actions must stay different.
        return

    def close_event(self, event):
        try:
            _visual_signals.ready.disconnect(self._on_visual_thumb_ready)
        except (TypeError, RuntimeError):
            pass
        try:
            if self._anchor is not None and self._anchor_filter is not None:
                self._anchor.removeEventFilter(self._anchor_filter)
        except RuntimeError:
            pass
        old_close(self, event)

    Search.__init__ = init
    Search._atomic_add_visual_item = add_item
    Search.set_query = set_query
    Search._on_discover_ready = on_discover_ready
    Search._on_visual_thumb_ready = on_visual_thumb_ready
    Search._relayout = relayout
    Search.place = place
    Search.move_selection = move_selection
    Search._open = open_item
    Search.open_current = open_current
    Search.closeEvent = close_event


def _patch_chrome(module):
    global _CHROME_PATCHED
    if _CHROME_PATCHED:
        return
    _CHROME_PATCHED = True

    TitleBar = module.TitleBar
    old_init = TitleBar.__init__

    def init(self, parent=None):
        old_init(self, parent)
        # Programmatic clearing on Escape must close the overlay too, hence
        # textChanged rather than textEdited.
        self.search.textChanged.connect(lambda text, bar=self: _show_from_titlebar(bar, text))

    TitleBar.__init__ = init


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped, fullname):
        self._wrapped = wrapped
        self._fullname = fullname

    def create_module(self, spec):
        creator = getattr(self._wrapped, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module):
        self._wrapped.exec_module(module)
        if self._fullname == _SEARCH_TARGET:
            _patch_search(module)
        elif self._fullname == _CHROME_TARGET:
            _patch_chrome(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in (_SEARCH_TARGET, _CHROME_TARGET):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _Loader(spec.loader, fullname)
        return spec


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    pending = False
    search_module = sys.modules.get(_SEARCH_TARGET)
    if search_module is not None:
        _patch_search(search_module)
    else:
        pending = True

    chrome_module = sys.modules.get(_CHROME_TARGET)
    if chrome_module is not None:
        _patch_chrome(chrome_module)
    else:
        pending = True

    if pending:
        sys.meta_path.insert(0, _Finder())
