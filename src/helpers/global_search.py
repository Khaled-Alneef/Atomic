"""One search across every page, opened with Ctrl+K.

Where it sits, and why, since the owner asked for that specifically:
every application that has this - VS Code's Quick Open, Spotlight,
Raycast, Slack, Notion - puts it in the same place, and it is not the
middle of the screen. A panel of fixed width, centred horizontally, its
top set at roughly a fifth of the window's height, with the results
growing downwards from the field. Two reasons that shape won: the field
lands under the eye rather than under the hand, and the results have
room to grow without the panel jumping around as they do.

So: 620px wide (VS Code's Quick Open is ~600, Raycast ~750 - wide enough
for a title and its page label, narrow enough that the eye doesn't have
to travel), capped at 90% of the window on a small window, top at 18% of
the window height, and the list capped at eight results so the panel
never grows past the window it belongs to.

Positioned from `window.geometry()`, never `mapToGlobal` - that returns
coordinates divided by the *other* screen's scale factor on a mixed-DPI
pair, which is how this app's toasts once landed 200px off
(.claude/rules/ui.md).

Choosing a result opens it - the anime in Stremio, the game, the app,
the site - rather than walking to its page and leaving it to be clicked
again. Nothing here knows *how* to open anything: it hands the entry to
the same function the entry's own page uses, so there is one open
behaviour per kind of thing and this is not a second one.
"""

from PyQt6.QtCore import QObject, QPoint, QSize, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
)

from . import discover, logs, lookup_pool, storage, theme
from .widgets import show_toast, smooth_scrolling

# The app's keyboard map. Listed in Settings under Keybinds - it lived in
# this panel first, which put a wall of grey text under the field every
# time the panel opened, in front of someone who had just proved they
# knew the shortcut. Kept here because this is where the keys are
# defined-adjacent, and Settings imports it rather than repeating it.
# Alt+Left/Right and F11 predate this and sit where a browser puts them.
SHORTCUTS = (
    ("Ctrl+K", "Search everything"),
    ("Ctrl+F", "Search this page"),
    ("Ctrl+N", "Add something"),
    ("Ctrl+Z", "Undo the last action"),
    ("Ctrl+Y", "Redo the last action"),
    ("Ctrl+1-9", "Jump to a sidebar page"),
    ("Ctrl+,", "Settings"),
    ("Esc", "Exit search"),
)

PANEL_WIDTH = 620
PANEL_TOP_FRACTION = 0.18
MAX_RESULTS = 8

# A poster is 2:3, so a 44px-tall thumbnail is about 29 wide - enough to
# recognise a cover at a glance without turning the panel into a grid.
THUMB_HEIGHT = 44
# How many outside results ride under the owned ones. Deliberately few:
# they are the answer to "do I have this?" coming back "no, but this
# exists", not a browse - the Discover page is the browse.
MAX_DISCOVER_RESULTS = 5

# Marks a row as coming from outside rather than from a file the
# app owns. An object rather than a string so it can never collide
# with a real page name.
_DISCOVER = object()


class _DiscoverSignals(QObject):
    """Carries an outside lookup back to the UI thread.

    **Module level, not owned by the panel.** The panel is
    WA_DeleteOnClose and a search outlives the keystroke that started it,
    so a signals object parented to the panel would be freed while a
    worker still held it - and emitting on a freed C++ object takes the
    process with it. One object for the app's lifetime, with the query
    carried alongside so a late answer to an abandoned query can be
    dropped rather than shown."""

    ready = Signal(str, list)


_signals = _DiscoverSignals()

# Scaled covers, keyed by (path, ratio). The panel rebuilds its rows on
# every keystroke and the same handful of covers come back each time.
_thumbs = {}


def _thumbnail(entry) -> QIcon:
    """The entry's own cover, read off disk.

    **Never `cover_url`.** This is a dropdown that has to be on screen
    inside a keystroke (CLAUDE.md rule 7), and a URL is a download per
    row. An entry with no local cover gets no picture at all rather than
    a placeholder - a column of identical grey blocks reads worse than a
    clean list of titles.

    Cut at the screen's devicePixelRatio and tagged with it, or the
    thumbnails blur on any non-100% display (.claude/rules/ui.md)."""
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    ratio = float(screen.devicePixelRatio()) if screen else 1.0
    # In the order a kind prefers its own art: a saved title's cached
    # cover, a game's poster then its launcher icon, an app's artwork
    # then its exe icon, a site's icon. Measured against the real files,
    # 26 August 2026 - these are the keys the four pages actually write,
    # and the first version guessed three that no page has ever used.
    for key in ("cover_path", "cover", "art", "image", "icon"):
        path = entry.get(key)
        if not path:
            continue
        cached = _thumbs.get((path, ratio))
        if cached is None:
            source = QPixmap(str(path))
            if source.isNull():
                continue
            scaled = source.scaledToHeight(
                max(1, round(THUMB_HEIGHT * ratio)),
                Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(scaled.height() / float(THUMB_HEIGHT))
            cached = QIcon(scaled)
            _thumbs[(path, ratio)] = cached
        return cached
    return QIcon()


def discover_worker(query: str):
    """Ask the outside sources what `query` finds, off the UI thread.

    Never raises and always emits, even empty: the panel counts on an
    answer to know the section is settled, and a worker that dies
    silently leaves it waiting forever (.claude/rules/integrations.md).
    """
    rows = []
    try:
        for kind in ("series", "movie"):
            rows += discover.discover_video(kind, query, limit=3) or []
        rows += discover.discover_reading(query, limit=3) or []
    except Exception:
        logs.exception("Global search: the Discover lookup failed")
    finally:
        _signals.ready.emit(query, rows[:MAX_DISCOVER_RESULTS])

# Which file holds what, and which page each entry belongs to. Since
# the Anime merge (main._merge_anime_into_series), tracker.json is the
# reading file and series.json holds everything watched.
_SOURCES = (
    ("tracker.json", "manga", "title"),
    ("series.json", "series", "title"),
    ("games.json", "games", "name"),
    ("apps.json", "apps", "name"),
    ("websites.json", "websites", "name"),
)
_PAGE_LABELS = {
    "manga": "Read", "series": "Watch",
    "games": "Games", "apps": "Apps", "websites": "Websites",
}


def collect(query: str):
    """Everything whose name contains `query`, as
    [(title, page name, page label, entry), ...] - the entry included
    because choosing a result opens it, and opening needs the record
    rather than its name.

    Read fresh on every search rather than cached: this opens over
    whichever page is showing, and that page may have just added,
    renamed or removed the thing being looked for."""
    query = (query or "").strip().lower()
    if not query:
        return []
    results = []
    for filename, page, key in _SOURCES:
        for entry in storage.load(filename, []) or []:
            title = entry.get(key) or ""
            if query not in title.lower():
                continue
            results.append((title, page, _PAGE_LABELS.get(page, page), entry))
    # Titles that *start* with what was typed first - typing "one" should
    # reach One Piece before The World After The End.
    results.sort(key=lambda row: (not row[0].lower().startswith(query), row[0].lower()))
    return results[:MAX_RESULTS]


class GlobalSearch(QDialog):
    """The results, under whichever field is driving them.

    No field of its own any more. There was one, and with the search box
    on Home it meant two fields on screen a few pixels apart, one of them
    holding what had been typed into the other. What is typed lives in
    the page's field; this shows what it found.

    Frameless and non-modal rather than a Popup: a Popup grabs the
    keyboard, which would take focus off the very field being typed into.
    """

    # Emitted whenever this panel closes, including the close it does to
    # itself when a result is clicked. Whoever opened it keeps a
    # reference, and this deletes itself on close - so without this the
    # page is left pointing at a deleted object. Shipped bug: opening a
    # result *by clicking it* left Home holding the dead panel, and every
    # later keystroke in the search field raised on it, so no suggestions
    # appeared again until the page happened to be rebuilt. Pressing
    # Enter never showed it, because that path closes the panel from the
    # page's own side.
    closed = Signal()

    def __init__(self, window, anchor=None):
        super().__init__(window)
        self._window = window
        # The field driving this, if there is one - the panel hangs
        # beneath it. Without one it falls back to the window's own
        # geometry (see _panel_position).
        self._anchor = anchor
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        # Shown without taking focus, so the field being typed into keeps
        # it and every keystroke still arrives there.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Translucent, so the border-radius below leaves genuinely
        # transparent corners rather than square slabs of window - the
        # same treatment widgets.frameless_dialog gives the app's modal
        # dialogs (this panel keeps its own flags: it must not grab
        # focus, and dragging a suggestion list would be wrong).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.results = QListWidget()
        smooth_scrolling(self.results)     # see widgets.ScrollBarDrag
        self.results.setIconSize(QSize(THUMB_HEIGHT, THUMB_HEIGHT))
        self.results.itemActivated.connect(self._open)
        self.results.itemClicked.connect(self._open)
        layout.addWidget(self.results)

        self.empty = QLabel("", objectName="Muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.setStyleSheet(
            f"QDialog {{ background: {theme.PANEL_FILL}; border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS}px; }}")
        self._rows = []
        # The query these rows belong to, so a Discover answer that
        # arrives after the user has typed on can be dropped instead of
        # appended under the wrong search.
        self._query = ""
        self._discover_rows = []
        _signals.ready.connect(self._on_discover_ready)

    def set_query(self, query: str):
        """Show what `query` finds. Returns whether anything was found -
        the caller decides whether an empty panel is worth showing.

        Two halves, in this order and never merged: what the user
        already has, then what exists outside. The owner asked for one
        bar that searches everything (25 August 2026), and "everything"
        stops being useful the moment a title you own has to be picked
        out of a list of ones you do not."""
        self._query = query
        self.results.clear()
        self._discover_rows = []
        self._rows = collect(query)
        for title, page, label, entry in self._rows:
            item = QListWidgetItem(f"{title}    ·    {label}")
            item.setIcon(_thumbnail(entry))
            item.setData(Qt.ItemDataRole.UserRole, (page, entry))
            self.results.addItem(item)
        if self._rows:
            self.results.setCurrentRow(0)
        # Fired even when the library already answered: "I have this, and
        # there are two more seasons of it" is the useful answer.
        # submit_latest, not submit - every debounced keystroke but the
        # last is stale before it lands, and the shared queue is drained
        # by page-load backfill this must not queue behind
        # (.claude/rules/integrations.md).
        if query.strip():
            lookup_pool.submit_latest("global-search", discover_worker, query)
        self._relayout(searching=bool(query.strip()))
        return bool(self._rows)

    def _on_discover_ready(self, query, rows):
        """Outside results, appended under the owned ones."""
        if query != self._query:
            return          # the user typed on; this answers a dead query
        self._discover_rows = list(rows or [])
        for row in self._discover_rows:
            bits = [row.get("type") or "", str(row.get("year") or "")]
            label = "  ".join(bit for bit in bits if bit)
            item = QListWidgetItem(
                f"{row.get('title', '')}    ·    {label}  —  Discover")
            item.setData(Qt.ItemDataRole.UserRole, (_DISCOVER, row))
            self.results.addItem(item)
        if not self._rows and self._discover_rows:
            self.results.setCurrentRow(0)
        self._relayout(searching=False)

    def _relayout(self, searching):
        """Size the panel to whatever it is currently showing.

        Row heights vary now that owned rows carry a cover and Discover
        rows do not, so this adds the rows up rather than multiplying by
        the first one's hint - which under-measured the panel by the
        difference and clipped the last row."""
        count = self.results.count()
        self.results.setVisible(count > 0)
        self.empty.setVisible(count == 0)
        if count == 0:
            self.empty.setText(
                "Searching…" if searching
                else f"Nothing matches '{self._query.strip()}'.")
        total = sum(self.results.sizeHintForRow(i) for i in range(count))
        self.results.setFixedHeight(max(1, total) + 8)
        self.adjustSize()
        self.place()

    def move_selection(self, delta):
        if not self._rows:
            return
        row = self.results.currentRow() + delta
        self.results.setCurrentRow(max(0, min(row, self.results.count() - 1)))

    def open_current(self):
        item = self.results.currentItem()
        if item is not None:
            self._open(item)

    def place(self):
        frame = self._window.geometry()
        width = min(PANEL_WIDTH, int(frame.width() * 0.9))
        if self._anchor is not None:
            width = max(width, self._anchor.width())
        self.setFixedWidth(width)
        self.move(self._panel_position(width))

    def _panel_position(self, width):
        """Under the field driving it, or a fifth of the way down the
        window when nothing is.

        Both from `geometry()`, which is already global - never
        `mapToGlobal`, which returns coordinates divided by the other
        screen's scale factor on a mixed-DPI pair."""
        frame = self._window.geometry()
        if self._anchor is not None and self._anchor.isVisible():
            top_left = self._anchor.mapTo(self._window, QPoint(0, 0))
            return QPoint(frame.x() + top_left.x() + (self._anchor.width() - width) // 2,
                          frame.y() + top_left.y() + self._anchor.height() + 6)
        return QPoint(frame.x() + (frame.width() - width) // 2,
                      frame.y() + int(frame.height() * PANEL_TOP_FRACTION))

    def closeEvent(self, event):
        # Disconnected by hand: _signals lives for the life of the app
        # (see _DiscoverSignals), so a connection left behind would keep
        # calling a slot on a panel Qt has already deleted.
        try:
            _signals.ready.disconnect(self._on_discover_ready)
        except TypeError:
            pass
        self.closed.emit()
        super().closeEvent(event)

    def _open(self, item):
        page_name, entry = item.data(Qt.ItemDataRole.UserRole)
        self.close()
        if page_name is _DISCOVER:
            # Hand it to the Discover page rather than opening it here.
            # Adding a title is that page's flow - it resolves artwork,
            # picks a medium and writes the entry - and a second way in
            # would be a second behaviour to keep in step, which is the
            # trap `open_entry` below exists to avoid.
            open_discover(self._window, self._query, entry)
            return
        open_entry(self._window, page_name, entry)


def open_discover(window, query, row):
    """Show an outside result on the Discover page.

    Deliberately not "add it from here". Adding a title is the Discover
    page's own flow - it resolves artwork, decides a medium and writes
    the entry - and a second way in would be a second behaviour to keep
    in step with the first. This puts the user in front of the same row
    on the page that knows what to do with it."""
    try:
        window.navigate_to("discover", animate=False)
        page = getattr(window, "_current_page", None)
        start = getattr(page, "start_search", None)
        if callable(start):
            start(query)
    except Exception:
        logs.exception("Could not open the Discover page for a result")
        show_toast(window, "Could Not Open Discover")


def open_entry(parent, page_name, entry):
    """Open one search result.

    Imported here rather than at module scope: helpers must not depend on
    windows (the same reason settings_dialog reaches for a page module
    through sys.modules), and these three functions are the ones the
    pages themselves use - so an entry opened from here behaves exactly
    as it does from its own card, including the toasts it raises when a
    target is missing."""
    from windows.link_grid import open_link_entry
    from windows.tracker import open_tracker_entry

    if page_name in ("manga", "series"):
        # The details page, not an immediate resume: someone searching a
        # title by name is looking it up, the same intent as clicking a
        # card's body - resuming playback is the cover button's job.
        open_tracker_entry(parent, entry, resume=False)
    elif page_name == "games":
        _open_game(parent, entry)
    else:
        open_link_entry(parent, entry)


def _open_game(parent, game):
    """A game has no shared open helper - GamesPage._launch is a method
    on the page, and the page may not be built. This is its body without
    the redraw, which is the part that needs a page."""
    from pathlib import Path

    from helpers import game_launch

    path = game.get("path")
    if not path or not Path(path).exists():
        show_toast(parent, f"Can't Open '{game.get('name')}' - Not Found on This PC", 5000)
        return
    try:
        game_launch.run(game)
    except OSError as exc:
        show_toast(parent, f"Couldn't Open '{game.get('name')}' - "
                           f"{getattr(exc, 'strerror', None) or 'It Wouldn\'t Start'}", 5000)
        return
    storage.update_entry("games.json", game.get("id"), {"last_played": storage.now_iso()})
