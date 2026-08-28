"""Harbor-style visual overlay for Atomic's global search.

The base search owns the query plumbing and the "open this entry" behavior.
This patch changes the suggestion surface to match the Harbor reference:
a dimmed, wide overlay under the persistent search field, two media columns,
poster-left rows, and Enter meaning "run this query in Discover" rather than
"open whichever suggestion happens to be highlighted".
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import threading

from PyQt6.QtCore import QObject, QPoint, QSize, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QFont
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

_TARGET = "helpers.global_search"
_INSTALLED = False
_PATCHED = False

THUMB_W = 56
THUMB_H = 82
ROW_HEIGHT = 94
MAX_PER_SECTION = 6
CONTENT_MAX_W = 980
CONTENT_MIN_W = 720
CONTENT_SIDE_MARGIN = 48
COLUMN_GAP = 42
_TOKEN_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class _VisualSignals(QObject):
    ready = Signal(str, str, str)  # query, row token, cached path or ""


_visual_signals = _VisualSignals()


def _local_art_path(entry):
    for key in ("cover_path", "cover", "art", "image", "icon", "icon_path"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _kind_for_saved(entry):
    return "movie" if str(entry.get("type") or "").strip().lower() == "movie" else "series"


def _meta_text(year="", rating="", suffix=""):
    bits = []
    if year:
        bits.append(str(year))
    if rating:
        bits.append(f"★ {rating}")
    if suffix:
        bits.append(str(suffix))
    return "   •   ".join(bits)


def _cover_worker(module, query, token, url):
    path = ""
    try:
        from helpers import images

        found = images.download(url, timeout=8)
        if found:
            path = str(found)
    except Exception:
        module.logs.exception("Global search: Harbor suggestion artwork failed")
    finally:
        _visual_signals.ready.emit(query, token, path)


def _harbor_worker(module, query):
    """Fetch Movies and Series concurrently without blocking app shutdown.

    lookup_pool's latest worker is already daemonized.  These two children are
    daemon threads too (not ThreadPoolExecutor workers), so a slow catalog can
    never become an interpreter-exit join.  Parallel lookup also prevents a
    slow Series request from holding Movies off-screen, or vice versa.
    """
    found = {"movie": [], "series": []}
    lock = threading.Lock()
    done = threading.Event()
    remaining = 2

    def one(kind):
        nonlocal remaining
        rows = []
        try:
            rows = module.discover.discover_video(
                kind, query, limit=MAX_PER_SECTION
            ) or []
        except Exception:
            module.logs.exception(f"Global search: {kind} suggestions failed")
        with lock:
            found[kind] = list(rows)
            remaining -= 1
            if remaining <= 0:
                done.set()

    for kind in ("movie", "series"):
        threading.Thread(
            target=one,
            args=(kind,),
            name=f"global-search-{kind}",
            daemon=True,
        ).start()

    # discover.py already bounds each request.  This outer wait is only a guard
    # against a provider path violating its own timeout.
    done.wait(22.0)
    rows = list(found["movie"]) + list(found["series"])
    module._signals.ready.emit(query, rows)


def _make_header(module, text):
    label = QLabel(text)
    label.setStyleSheet(
        f"color:{module.theme.TEXT_DIM}; background:transparent; "
        "font-size:10px; font-weight:600;"
    )
    font = QFont(label.font())
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
    label.setFont(font)
    return label


def _make_list(owner):
    view = QListWidget()
    view.setObjectName("HarborSearchList")
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    view.setSpacing(2)
    view.itemActivated.connect(owner._open)
    view.itemClicked.connect(owner._open)
    return view


def _make_row(module, title, meta, path=""):
    """Poster-left row matching Harbor's search result density."""
    from helpers import images

    row = QWidget()
    row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    outer = QHBoxLayout(row)
    outer.setContentsMargins(0, 5, 8, 5)
    outer.setSpacing(12)

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
        f"color:{module.theme.TEXT_MUTED}; background:transparent; "
        "font-size:11px;"
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
            path,
            getattr(widget, "_atomic_search_title", ""),
            (THUMB_W, THUMB_H),
        )
        label = getattr(widget, "_atomic_search_thumb", None)
        if label is not None and not pix.isNull():
            label.setPixmap(pix)
            label.setStyleSheet("background:transparent; border:none;")
    except (RuntimeError, AttributeError):
        pass
    except Exception:
        module.logs.exception("Global search: could not paint Harbor artwork")


def _patch(module):
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    Search = module.GlobalSearch
    old_init = Search.__init__
    old_close = Search.closeEvent

    def init(self, window, anchor=None):
        old_init(self, window, anchor=anchor)

        # The old compact list still owns nothing we need visually; keep it
        # hidden so its base wiring/data helpers remain available without
        # painting a second suggestion surface behind this one.
        self.results.hide()
        self.empty.hide()

        base = self.layout()
        base.setContentsMargins(0, 0, 0, 0)
        base.setSpacing(0)

        self.setObjectName("HarborSearchOverlay")
        self.setStyleSheet(
            "QDialog#HarborSearchOverlay {"
            " background-color: rgba(5, 7, 12, 222);"
            " border: none; border-radius: 0px;"
            "}"
            "QListWidget#HarborSearchList {"
            " background: transparent; border: none; outline: 0;"
            "}"
            "QListWidget#HarborSearchList::item {"
            " background: transparent; border: none; padding: 0px;"
            "}"
            "QListWidget#HarborSearchList::item:selected,"
            "QListWidget#HarborSearchList::item:hover {"
            " background: transparent; border: none;"
            "}"
        )

        self._harbor_content = QWidget()
        self._harbor_content.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, False
        )
        content = QVBoxLayout(self._harbor_content)
        content.setContentsMargins(0, 14, 0, 0)
        content.setSpacing(10)

        columns_widget = QWidget()
        columns = QHBoxLayout(columns_widget)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(COLUMN_GAP)

        self._harbor_sections = {}
        for kind, heading in (("movie", "MOVIES"), ("series", "SERIES")):
            column = QWidget()
            layout = QVBoxLayout(column)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            header = _make_header(module, heading)
            view = _make_list(self)
            layout.addWidget(header)
            layout.addWidget(view)
            columns.addWidget(column, 1)
            self._harbor_sections[kind] = {
                "column": column,
                "header": header,
                "view": view,
            }

        content.addWidget(columns_widget)

        self._harbor_status = QLabel("")
        self._harbor_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._harbor_status.setStyleSheet(
            f"color:{module.theme.TEXT_MUTED}; background:transparent; "
            "font-size:12px;"
        )
        content.addWidget(self._harbor_status)

        base.addWidget(
            self._harbor_content,
            0,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        )

        self._harbor_seen = {"movie": set(), "series": set()}
        self._harbor_query_active = False
        _visual_signals.ready.connect(self._on_visual_thumb_ready)

    def add_item(self, kind, item, title, meta, path=""):
        section = self._harbor_sections[kind]
        view = section["view"]
        if view.count() >= MAX_PER_SECTION:
            return False
        item.setSizeHint(QSize(0, ROW_HEIGHT))
        view.addItem(item)
        view.setItemWidget(item, _make_row(module, title, meta, path))
        return True

    def add_saved_matches(self, query):
        # Keep media the user already owns at the head of the matching Harbor
        # column.  Non-media entries deliberately stay out of this surface:
        # Enter now means Discover, and this overlay is the Discover preview.
        for title, page, _label, entry in module.collect(query):
            if page != "series":
                continue
            kind = _kind_for_saved(entry)
            key = str(title).strip().lower()
            if not key or key in self._harbor_seen[kind]:
                continue
            year = entry.get("year") or entry.get("release_year") or ""
            meta = _meta_text(year, "", "Saved")
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, (page, entry))
            if add_item(self, kind, item, title, meta, _local_art_path(entry)):
                self._harbor_seen[kind].add(key)

    def set_query(self, query: str):
        query = str(query or "")
        self._query = query
        self._rows = []
        self._discover_rows = []
        self._harbor_seen = {"movie": set(), "series": set()}
        self._harbor_query_active = bool(query.strip())

        for section in self._harbor_sections.values():
            section["view"].clear()

        if not query.strip():
            self._harbor_status.setText("")
            self._relayout(searching=False)
            return False

        add_saved_matches(self, query)
        have_local = any(
            section["view"].count() for section in self._harbor_sections.values()
        )
        self._harbor_status.setText("" if have_local else "Searching…")

        # Same latest-query discipline as the base implementation, but the
        # worker itself now asks Movies and Series in parallel.
        module.lookup_pool.submit_latest(
            "global-search", _harbor_worker, module, query
        )
        self._relayout(searching=True)
        return True

    def on_discover_ready(self, query, rows):
        if query != self._query:
            return

        self._discover_rows = list(rows or [])
        from helpers import images

        for row in self._discover_rows:
            row_type = str(row.get("type") or "").strip().lower()
            if row_type == "movie":
                kind = "movie"
            elif row_type in ("series", "anime"):
                kind = "series"
            else:
                continue

            title = str(row.get("title") or "").strip()
            key = title.lower()
            if not title or key in self._harbor_seen[kind]:
                continue

            year = str(row.get("year") or "").strip()
            rating = str(row.get("imdbRating") or "").strip()
            meta = _meta_text(year, rating, "")
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, (module._DISCOVER, row))

            poster = str(row.get("poster") or "")
            token = f"{query}\x1f{kind}\x1f{title}\x1f{poster}"
            item.setData(_TOKEN_ROLE, token)

            path = ""
            if poster:
                try:
                    cached = images.cache_path_for_url(poster)
                    if cached.is_file():
                        path = str(cached)
                except Exception:
                    path = ""

            if not add_item(self, kind, item, title, meta, path):
                continue

            self._harbor_seen[kind].add(key)
            if poster and not path:
                module.lookup_pool.submit_cover(
                    _cover_worker, module, query, token, poster
                )

        total = sum(
            section["view"].count() for section in self._harbor_sections.values()
        )
        self._harbor_status.setText(
            "" if total else "No suggestions — press Enter to search Discover."
        )
        self._relayout(searching=False)

    def on_visual_thumb_ready(self, query, token, path):
        if query != self._query or not path:
            return
        for section in self._harbor_sections.values():
            view = section["view"]
            for index in range(view.count()):
                item = view.item(index)
                if item.data(_TOKEN_ROLE) == token:
                    _set_row_art(module, view.itemWidget(item), path)
                    return

    def relayout(self, searching):
        movie_count = self._harbor_sections["movie"]["view"].count()
        series_count = self._harbor_sections["series"]["view"].count()
        visible_rows = max(1, min(MAX_PER_SECTION, max(movie_count, series_count)))
        list_h = visible_rows * ROW_HEIGHT + 4
        for section in self._harbor_sections.values():
            section["view"].setFixedHeight(list_h)

        status_h = 24 if self._harbor_status.text() else 0
        self._harbor_status.setVisible(status_h > 0)
        self._harbor_content.setFixedHeight(14 + 24 + 8 + list_h + status_h + 12)
        self.place()

    def place(self):
        frame = self._window.geometry()

        if self._anchor is not None and self._anchor.isVisible():
            point = self._anchor.mapTo(self._window, QPoint(0, 0))
            top = frame.y() + point.y() + self._anchor.height() + 6
        else:
            top = frame.y() + int(frame.height() * 0.14)

        bottom = frame.y() + frame.height()
        height = max(1, bottom - top)
        self.setGeometry(frame.x(), top, frame.width(), height)

        usable = max(320, frame.width() - CONTENT_SIDE_MARGIN * 2)
        width = min(CONTENT_MAX_W, max(CONTENT_MIN_W, int(frame.width() * 0.68)))
        width = min(width, usable)
        self._harbor_content.setFixedWidth(width)

    def move_selection(self, delta):
        # Up/down remains useful for visual orientation, but Enter is *not*
        # selection activation anymore (see open_current below).
        views = [
            self._harbor_sections["movie"]["view"],
            self._harbor_sections["series"]["view"],
        ]
        active = next((v for v in views if v.currentRow() >= 0), None)
        if active is None:
            active = next((v for v in views if v.count()), None)
            if active is not None:
                active.setCurrentRow(0)
            return
        if not active.count():
            return
        row = max(0, min(active.currentRow() + delta, active.count() - 1))
        active.setCurrentRow(row)
        active.scrollToItem(active.item(row))

    def open_current(self):
        # Harbor behavior requested by the owner: typing a query and pressing
        # Enter is a full search, not an implicit click on row zero.
        query = str(self._query or "").strip()
        if not query:
            return
        self.close()
        module.open_discover(self._window, query, {})

    def close_event(self, event):
        try:
            _visual_signals.ready.disconnect(self._on_visual_thumb_ready)
        except (TypeError, RuntimeError):
            pass
        old_close(self, event)

    Search.__init__ = init
    Search._atomic_add_harbor_item = add_item
    Search.set_query = set_query
    Search._on_discover_ready = on_discover_ready
    Search._on_visual_thumb_ready = on_visual_thumb_ready
    Search._relayout = relayout
    Search.place = place
    Search.move_selection = move_selection
    Search.open_current = open_current
    Search.closeEvent = close_event


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
