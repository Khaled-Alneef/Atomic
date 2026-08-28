"""YouTube-style visual rows for Atomic's global search suggestions.

The base global search already has the right search/data/opening behavior.  This
patch changes only presentation: every result becomes a tall horizontal row
with artwork on the left and title/metadata on the right.  Discover rows already
carry poster URLs, but the base UI intentionally rendered them text-only; those
posters are now fetched through Atomic's existing cover queue and disk cache so
network work never blocks typing or the UI thread.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

from PyQt6.QtCore import QObject, QSize, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

_TARGET = "helpers.global_search"
_INSTALLED = False
_PATCHED = False

THUMB_W = 72
THUMB_H = 96
ROW_HEIGHT = 108
VISIBLE_ROWS = 6
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


def _cover_worker(module, query, token, url):
    path = ""
    try:
        from helpers import images
        found = images.download(url, timeout=8)
        if found:
            path = str(found)
    except Exception:
        module.logs.exception("Global search: suggestion artwork failed")
    finally:
        _visual_signals.ready.emit(query, token, path)


def _make_row(module, title, subtitle, path=""):
    """One thumbnail-left suggestion row.  The container ignores mouse
    events so QListWidget remains the click/selection owner."""
    from helpers import images

    row = QWidget()
    row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    outer = QHBoxLayout(row)
    outer.setContentsMargins(8, 6, 10, 6)
    outer.setSpacing(12)

    thumb = QLabel()
    thumb.setFixedSize(THUMB_W, THUMB_H)
    thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    thumb.setStyleSheet(
        f"background:{module.theme.SURFACE_HOVER}; border-radius:{max(4, module.theme.RADIUS - 2)}px;"
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

    text = QWidget()
    text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    texts = QVBoxLayout(text)
    texts.setContentsMargins(0, 0, 0, 0)
    texts.setSpacing(5)
    texts.addStretch(1)

    title_label = QLabel(title)
    title_label.setWordWrap(True)
    title_label.setMaximumHeight(46)
    title_label.setStyleSheet(
        f"color:{module.theme.TEXT}; font-weight:600; font-size:14px; background:transparent;"
    )
    texts.addWidget(title_label)

    meta = QLabel(subtitle)
    meta.setStyleSheet(
        f"color:{module.theme.TEXT_MUTED}; font-size:12px; background:transparent;"
    )
    texts.addWidget(meta)
    texts.addStretch(1)

    outer.addWidget(text, 1)
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
        self.results.setIconSize(QSize(0, 0))
        self.results.setSpacing(2)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _visual_signals.ready.connect(self._on_visual_thumb_ready)

    def add_visual_item(self, item, title, subtitle, path=""):
        item.setSizeHint(QSize(0, ROW_HEIGHT))
        self.results.addItem(item)
        self.results.setItemWidget(item, _make_row(module, title, subtitle, path))

    def set_query(self, query: str):
        self._query = query
        self.results.clear()
        self._discover_rows = []
        self._rows = module.collect(query)

        for title, page, label, entry in self._rows:
            item = module.QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, (page, entry))
            # YouTube-style: content image at left, then the thing it is and
            # where it comes from, instead of a tiny icon embedded in text.
            subtitle = f"{label}  •  Saved"
            add_visual_item(self, item, title, subtitle, _local_art_path(entry))

        if self._rows:
            self.results.setCurrentRow(0)

        if query.strip():
            module.lookup_pool.submit_latest("global-search", module.discover_worker, query)
        self._relayout(searching=bool(query.strip()))
        return bool(self._rows)

    def on_discover_ready(self, query, rows):
        if query != self._query:
            return
        self._discover_rows = list(rows or [])

        from helpers import images
        for index, row in enumerate(self._discover_rows):
            title = row.get("title", "")
            bits = [row.get("type") or "", str(row.get("year") or ""), "Discover"]
            subtitle = "  •  ".join(bit for bit in bits if bit)
            item = module.QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, (module._DISCOVER, row))

            poster = str(row.get("poster") or "")
            token = f"{query}\x1f{index}\x1f{poster}"
            item.setData(_TOKEN_ROLE, token)

            path = ""
            if poster:
                try:
                    cached = images.cache_path_for_url(poster)
                    if cached.is_file():
                        path = str(cached)
                except Exception:
                    path = ""

            add_visual_item(self, item, title, subtitle, path)

            if poster and not path:
                module.lookup_pool.submit_cover(_cover_worker, module, query, token, poster)

        if not self._rows and self._discover_rows:
            self.results.setCurrentRow(0)
        self._relayout(searching=False)

    def on_visual_thumb_ready(self, query, token, path):
        if query != self._query or not path:
            return
        for index in range(self.results.count()):
            item = self.results.item(index)
            if item.data(_TOKEN_ROLE) == token:
                _set_row_art(module, self.results.itemWidget(item), path)
                return

    def relayout(self, searching):
        count = self.results.count()
        self.results.setVisible(count > 0)
        self.empty.setVisible(count == 0)
        if count == 0:
            self.empty.setText(
                "Searching…" if searching
                else f"Nothing matches '{self._query.strip()}'."
            )
        # Keep the dropdown a suggestion list rather than a page-sized wall.
        # More matches stay available by scrolling, like YouTube's suggestion
        # surfaces, while six full visual rows remain visible at once.
        total = sum(self.results.sizeHintForRow(i) for i in range(count))
        visible = min(total, ROW_HEIGHT * VISIBLE_ROWS) if count else 1
        self.results.setFixedHeight(max(1, visible) + 8)
        self.adjustSize()
        self.place()

    def move_selection(self, delta):
        if self.results.count() <= 0:
            return
        current = self.results.currentRow()
        if current < 0:
            current = 0
        row = max(0, min(current + delta, self.results.count() - 1))
        self.results.setCurrentRow(row)
        self.results.scrollToItem(self.results.item(row))

    def close_event(self, event):
        try:
            _visual_signals.ready.disconnect(self._on_visual_thumb_ready)
        except (TypeError, RuntimeError):
            pass
        old_close(self, event)

    Search.__init__ = init
    Search._atomic_add_visual_item = add_visual_item
    Search.set_query = set_query
    Search._on_discover_ready = on_discover_ready
    Search._on_visual_thumb_ready = on_visual_thumb_ready
    Search._relayout = relayout
    Search.move_selection = move_selection
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
