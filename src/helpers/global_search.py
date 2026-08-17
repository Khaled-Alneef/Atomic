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

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import (
    QDialog, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
)

from . import storage, theme
from .widgets import show_toast

# The app's keyboard map. Listed in Settings under Keybinds - it lived in
# this panel first, which put a wall of grey text under the field every
# time the panel opened, in front of someone who had just proved they
# knew the shortcut. Kept here because this is where the keys are
# defined-adjacent, and Settings imports it rather than repeating it.
# Alt+Left/Right and F11 predate this and sit where a browser puts them.
SHORTCUTS = (
    ("Ctrl+K", "search everything"),
    ("Ctrl+F", "search this page"),
    ("Ctrl+N", "add something"),
    ("Ctrl+Z", "undo the last removal"),
    ("Ctrl+Y", "redo it"),
    ("Ctrl+1-9", "jump to a sidebar page"),
    ("Ctrl+,", "settings"),
    ("Esc", "clear a search"),
)

PANEL_WIDTH = 620
PANEL_TOP_FRACTION = 0.18
MAX_RESULTS = 8

# Which file holds what, and which page each entry belongs to. Anime and
# Reading share tracker.json, so that one is split by the entry's own
# type rather than by file - the same rule home.PAGE_FOR_TYPE uses.
_MANGA_TYPES = ("Manga", "Manhwa", "Manhua")
_SOURCES = (
    ("tracker.json", None, "title"),
    ("series.json", "series", "title"),
    ("games.json", "games", "name"),
    ("apps.json", "apps", "name"),
    ("websites.json", "websites", "name"),
)
_PAGE_LABELS = {
    "anime": "Anime", "manga": "Reading", "series": "Movies & Series",
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
            entry_page = page
            if entry_page is None:
                entry_page = "manga" if entry.get("type") in _MANGA_TYPES else "anime"
            results.append((title, entry_page,
                            _PAGE_LABELS.get(entry_page, entry_page), entry))
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.results = QListWidget()
        self.results.itemActivated.connect(self._open)
        self.results.itemClicked.connect(self._open)
        layout.addWidget(self.results)

        self.empty = QLabel("", objectName="Muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.setStyleSheet(
            f"QDialog {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};"
            f" border-radius: 10px; }}")
        self._rows = []

    def set_query(self, query: str):
        """Show what `query` finds. Returns whether anything was found -
        the caller decides whether an empty panel is worth showing."""
        self.results.clear()
        self._rows = collect(query)
        for title, page, label, entry in self._rows:
            item = QListWidgetItem(f"{title}    ·    {label}")
            item.setData(Qt.ItemDataRole.UserRole, (page, entry))
            self.results.addItem(item)
        if self._rows:
            self.results.setCurrentRow(0)
        self.results.setVisible(bool(self._rows))
        self.empty.setVisible(not self._rows)
        self.empty.setText("" if self._rows else f"Nothing matches '{query.strip()}'.")
        self.results.setFixedHeight(
            max(1, len(self._rows)) * (self.results.sizeHintForRow(0) if self._rows else 1) + 8)
        self.adjustSize()
        self.place()
        return bool(self._rows)

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

    def _open(self, item):
        page_name, entry = item.data(Qt.ItemDataRole.UserRole)
        self.close()
        open_entry(self._window, page_name, entry)


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

    if page_name in ("anime", "manga", "series"):
        open_tracker_entry(parent, entry)
    elif page_name == "games":
        _open_game(parent, entry)
    else:
        open_link_entry(parent, entry)


def _open_game(parent, game):
    """A game has no shared open helper - GamesPage._launch is a method
    on the page, and the page may not be built. This is its body without
    the redraw, which is the part that needs a page."""
    from helpers import child_process
    from pathlib import Path
    import subprocess

    path = game.get("path")
    if not path or not Path(path).exists():
        show_toast(parent, f"Can't Open '{game.get('name')}' - Not Found on This PC", 5000)
        return
    try:
        subprocess.Popen([path], shell=True, cwd=str(Path(path).parent),
                         env=child_process.clean_env(),
                         creationflags=child_process.flags())
    except OSError as exc:
        show_toast(parent, f"Couldn't Open '{game.get('name')}' - "
                           f"{getattr(exc, 'strerror', None) or 'It Wouldn\'t Start'}", 5000)
        return
    storage.update_entry("games.json", game.get("id"), {"last_played": storage.now_iso()})
