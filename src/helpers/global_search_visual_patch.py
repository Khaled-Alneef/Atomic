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
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
        if obj is not self._anchor:
            return False
        if event.type() == QEvent.Type.FocusOut:
            """**A press anywhere else leaves the search box.** The owner,
            4 September 2026: "the global search box, when I click
            anywhere outside it make it leave the search box".

            Photographed on the frozen build that day: with "solo" in the
            field and the panel up, a click on a schedule row opened that
            title's details page *and left the panel standing over it*
            with the query still in the box. The panel's own
            mousePressEvent is the only thing that closed it, and that
            fires only for a press inside the panel's own window - which
            is the rounded list and nothing else, so "outside" was never
            reachable. The pages underneath are a WebView2 native child
            besides, and Qt is never told about a press there at all.

            Focus answers both: the panel is WA_ShowWithoutActivating, so
            the field keeps focus for as long as it is being typed into,
            and anything that takes a press - a Qt widget or the native
            page - takes the focus with it. A dropped focus is therefore
            exactly "the user is somewhere else now".

            `_exit_search_field` lives in the polish patch, which is the
            module that owns leaving this field; imported here rather
            than duplicated, and its own deferral (the clear happens on
            the next tick) is what keeps this safe inside a focus
            event.
            """
            reason = event.reason()
            if reason in (Qt.FocusReason.PopupFocusReason,
                          Qt.FocusReason.ActiveWindowFocusReason):
                # The suggestion panel showing, or the window being
                # deactivated by something outside Atomic. Neither is the
                # user choosing to be elsewhere in the app.
                return False
            try:
                from helpers import global_search_list_polish_patch as polish
                polish._exit_search_field(self._panel)
            except Exception:
                try:
                    self._panel.close()
                except RuntimeError:
                    pass
            return False
        if event.type() != QEvent.Type.KeyPress:
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


class _OutsideFilter(QObject):
    """Any press anywhere in the app, except on the field or the panel,
    leaves the search.

    **The owner, 4 September 2026: "when I click anywhere even if it is
    not a button or card make it leave the search bar!"**

    Two routes already existed and between them they still left a hole.
    The field's own focus answers a press on a Qt widget that *takes*
    focus, and app.js's `pagepress` answers one anywhere in a web page
    (web_pages._leave_search). Photographed that day: typing "solo" and
    pressing the empty part of the sidebar, below Settings, left the
    panel standing and the word in the box - because that strip is a
    plain QWidget with no focus policy, so the line edit never lost
    focus and there was no page to report the press.

    An application filter sees the press whatever it lands on. It is
    installed only while a panel is open and removed with it, so an
    ordinary click in the app costs nothing.

    Where the press landed is asked of `widgetAt`, which hit-tests the
    real widget tree - never arithmetic on global coordinates, which is
    the mixed-DPI trap .claude/rules/ui.md records.

    **And it belongs to the field, not to the panel.** The owner, 4
    September 2026: "the search bar does not exit when I click anywhere
    except there is something written inside it!!". The first version of
    this was created in the panel's __init__ and died with it - and the
    panel only exists while there is a query, so a field that was focused
    and *empty* had nothing watching for the press. Owned by the title
    bar it is installed once, for the window's life, and does nothing at
    all until the field has focus or a panel is up.
    """

    def __init__(self, bar):
        super().__init__(bar)
        self._bar = bar

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Type.MouseButtonPress:
                return False
            field = self._bar.search
            window = self._bar.window()
            panel = getattr(window, "_search_panel", None) if window else None
            if panel is None and not field.hasFocus():
                return False          # nothing to leave
            node = QApplication.widgetAt(QCursor.pos())
            while node is not None:
                if node is field or (panel is not None and node is panel):
                    return False      # the field, or the list itself
                node = node.parent()
            if panel is not None:
                from helpers import global_search_list_polish_patch as polish
                polish._exit_search_field(panel)
            else:
                field.clear()
                field.clearFocus()
        except (RuntimeError, AttributeError, ImportError):
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


# How many faces the suggestion panel offers. Small on purpose: the
# panel is a list the owner scans while typing, and a name is useful
# there as a door, not as a browse.
CAST_SUGGESTIONS = 3


def add_face_row(panel, module, query, row, add_item):
    """Draw one cast row into `panel`'s list. Returns whether it did.

    **Shared on purpose, and the sharing is the fix.** The first version
    of this lived inside this module's own `on_discover_ready` - and
    helpers/global_search_list_polish_patch *replaces* that method
    outright, so it never ran. Caught on a screenshot of the frozen
    build (3 September 2026): the face drew, through the generic title
    path, wearing "Watch - Person" as its meta and carrying a
    ("discover", ...) payload that would have sent a click to a details
    page for a person. That is the trap .claude/rules/testing.md
    records - "a wrapper that replaced the patched function outright, so
    the fix had never run" - so there is one implementation and both
    callers use it.

    `add_item` is the caller's own row-adder, because the two patches
    reach it differently (one has the function in scope, the other only
    the method).
    """
    title = str(row.get("title") or "").strip()
    key = ("cast", title.lower())
    if not title or key in panel._seen:
        return True                   # a face, and already shown
    panel._seen.add(key)
    item = QListWidgetItem(title)
    item.setData(Qt.ItemDataRole.UserRole, ("cast", title, row))
    # Not through _meta_text: that prefixes every line with "Watch" or
    # "Read", which is the one thing a face is neither of. "Cast" and
    # what they are known for is the whole line.
    meta = "  \u2022  ".join(
        bit for bit in ("Cast", str(row.get("note") or "")) if bit)
    add_item(item, title, meta, "")
    poster = str(row.get("poster") or "")
    if poster:
        token = f"{query}\x1fcast\x1f{title}\x1f{poster}"
        item.setData(_TOKEN_ROLE, token)
        module.lookup_pool.submit_cover(
            _cover_worker, module, query, token, poster, title, "person", "")
    return True


# One lock for the snapshot _emit takes: the four provider threads write
# into `found` under their own lock, and a partial emit must not read a
# list while one of them is replacing it.
_EMIT_LOCK = threading.Lock()


def _search_worker(module, query):
    """Fetch Watch + Read suggestions in parallel, then emit one mixed list."""
    found = {"series": [], "movie": [], "reading": [], "cast": []}
    lock = _EMIT_LOCK
    done = threading.Event()
    # **The first provider back is worth showing on its own.** The owner,
    # 4 September 2026: "the search results and the search suggestions
    # takes too long to appear". Measured on the frozen build that day,
    # typing "solo": 2.2 to 2.7 seconds of "Searching..." with nothing at
    # all under it, then every row at once - because this waited on all
    # four before emitting anything, and one provider alone measured
    # 0.08s to 5.46s across four queries. Rule 7's answer is to show what
    # has landed and fill the rest in, which is what streams.find_streams
    # already does with on_partial.
    first = threading.Event()
    remaining = 4

    def finish(kind, rows):
        nonlocal remaining
        with lock:
            found[kind] = list(rows or [])
            remaining -= 1
            if remaining <= 0:
                done.set()
        first.set()

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

    def cast():
        """**Cast in the suggestions too.** The owner, 3 September 2026:
        "add cast for discover page and the search page also with anime,
        series and etc... also to the search suggestion, and take their
        images from IMDB or TMDB or any API". TMDB is the API - it is
        the only one of the two with a public person endpoint, and this
        app already carries a read token for artwork - and
        helpers.people is where the app asks it (the cast chips on the
        details page have gone through it since 2 September).

        A fourth worker rather than a fourth round trip: it is one call,
        measured at 0.21s, and running it after the other three would
        add its whole cost to a panel the owner is watching fill.
        """
        rows = []
        try:
            from helpers import people
            rows = people.search(query, CAST_SUGGESTIONS) or []
        except Exception:
            module.logs.exception("Global search: cast suggestions failed")
        finish("cast", rows)

    threading.Thread(target=cast, daemon=True,
                     name="global-search-cast").start()
    threading.Thread(target=video, args=("series",), daemon=True,
                     name="global-search-series").start()
    threading.Thread(target=video, args=("movie",), daemon=True,
                     name="global-search-movie").start()
    threading.Thread(target=reading, daemon=True,
                     name="global-search-reading").start()

    # Each provider already has its own bounded request timeout.  The outer
    # guard only prevents a broken provider from holding this latest-query job.
    #
    # Two emits, not four: one when the first provider lands so the panel
    # stops being empty, one when they are all in. The panel appends and
    # de-duplicates on its own `_seen` set (on_discover_ready), so a
    # second emit carrying the same rows plus more only adds the more.
    first.wait(22.0)
    if not done.is_set():
        _emit(module, query, found)
    done.wait(22.0)
    _emit(module, query, found)


def _emit(module, query, found):
    """Whatever the four providers hold right now, in the panel's order."""
    with _EMIT_LOCK:
        found = {key: list(value) for key, value in found.items()}
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

    # **Faces last, and not sorted in with the titles.** A person is not
    # a result of the same kind - clicking one opens their filmography
    # rather than a title - so mixing them into the title ordering would
    # put "Alan Ritchson" between two shows called Reacher.
    faces = []
    for original in found["cast"]:
        row = dict(original)
        row["_atomic_entry_type"] = "Person"
        faces.append(row)

    needle = query.strip().lower()
    rows.sort(key=lambda row: (
        not str(row.get("title") or "").lower().startswith(needle),
        str(row.get("title") or "").lower(),
        0 if _route_for_entry_type(row.get("_atomic_entry_type")) == "series" else 1,
    ))
    # **Faces first in the outside half, and that was a screenshot.**
    # Appended after the titles they came out below the fold of a panel
    # that holds about seven rows - photographed on the frozen build, 3
    # September 2026, typing "reacher": seven titles and no sign of Alan
    # Ritchson. There are at most CAST_SUGGESTIONS of them and a name is
    # the most precise thing a name can match, so they lead. The saved
    # half is still above both (add_saved runs first), which is the rule
    # that has not changed: what he already has, then what exists.
    module._signals.ready.emit(query, faces + rows[:MAX_REMOTE_RESULTS])


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
            if entry_type == "Person":
                add_face_row(self, module, query, row,
                             lambda i, t, m, path="": add_item(self, i, t, m, path))
                continue
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

            if payload[0] == "cast":
                # A face opens that name's own page, the same surface a
                # cast chip on the details page opens.
                from windows import web_reader
                web_reader.open_cast_browse(self._window, str(payload[1]))
                return
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
        # Installed once, for the window's life - see _OutsideFilter for
        # why it cannot belong to the panel.
        app = QApplication.instance()
        if app is not None:
            self._atomic_leave_search = _OutsideFilter(self)
            app.installEventFilter(self._atomic_leave_search)

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
