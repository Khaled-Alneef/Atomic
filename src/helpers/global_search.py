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

Choosing a result navigates to that entry's own page and puts the title
in that page's search box, so the card is on screen with nothing else
around it. Deliberately not a second way to open an entry: every page
already knows how to open its own, and a global list that launched
things itself would be a second implementation of six different open
behaviours.
"""

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout,
)

from . import storage, theme

# The keyboard map, shown here because this panel is the one place every
# user opens with a shortcut - a list of shortcuts nobody can find is not
# a feature. Kept to the ones worth memorising; Alt+Left/Right and F11
# predate this and are where a browser puts them anyway.
SHORTCUTS = (
    ("Ctrl+K", "search everything"),
    ("Ctrl+F", "search this page"),
    ("Ctrl+N", "add something"),
    ("Ctrl+Z", "undo the last removal"),
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
    [(title, page name, page label), ...].

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
            results.append((title, entry_page, _PAGE_LABELS.get(entry_page, entry_page)))
    # Titles that *start* with what was typed first - typing "one" should
    # reach One Piece before The World After The End.
    results.sort(key=lambda row: (not row[0].lower().startswith(query), row[0].lower()))
    return results[:MAX_RESULTS]


class GlobalSearch(QDialog):
    """The panel itself. Built fresh each time it is opened, so it never
    holds a stale copy of any page's data."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.field = QLineEdit()
        self.field.setPlaceholderText("Search everything...")
        self.field.textChanged.connect(self._refresh)
        layout.addWidget(self.field)

        self.results = QListWidget()
        self.results.itemActivated.connect(self._open)
        self.results.itemClicked.connect(self._open)
        layout.addWidget(self.results)

        self.empty = QLabel("", objectName="Muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.hints = QLabel(
            "   ".join(f"<b>{keys}</b> {what}" for keys, what in SHORTCUTS),
            objectName="Muted")
        self.hints.setWordWrap(True)
        layout.addWidget(self.hints)

        self.setStyleSheet(
            f"QDialog {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};"
            f" border-radius: 10px; }}")
        self.field.installEventFilter(self)
        self._refresh("")
        self._place()

    def _place(self):
        """Sized and positioned against the window's own geometry, which
        is already in global coordinates - see the module docstring for
        why not mapToGlobal, and where the numbers come from."""
        frame = self._window.geometry()
        width = min(PANEL_WIDTH, int(frame.width() * 0.9))
        self.setFixedWidth(width)
        self.adjustSize()
        x = frame.x() + (frame.width() - width) // 2
        y = frame.y() + int(frame.height() * PANEL_TOP_FRACTION)
        self.move(x, y)

    def _refresh(self, _text=None):
        query = self.field.text()
        self.results.clear()
        rows = collect(query)
        for title, page, label in rows:
            item = QListWidgetItem(f"{title}    ·    {label}")
            item.setData(Qt.ItemDataRole.UserRole, (page, title))
            self.results.addItem(item)
        if rows:
            self.results.setCurrentRow(0)
        # The list is hidden rather than left empty, so the panel is a
        # single field until there is something to show under it.
        self.results.setVisible(bool(rows))
        # The shortcut list is what an empty panel says instead of
        # nothing - the moment anything is typed it gets out of the way.
        self.hints.setVisible(not query.strip())
        self.empty.setVisible(bool(query.strip()) and not rows)
        self.empty.setText(f"Nothing matches '{query.strip()}'." if not rows else "")
        self.results.setFixedHeight(
            max(1, len(rows)) * (self.results.sizeHintForRow(0) if rows else 1) + 8)
        self.adjustSize()
        self._place_vertically()

    def _place_vertically(self):
        frame = self._window.geometry()
        self.move(self.x(), frame.y() + int(frame.height() * PANEL_TOP_FRACTION))

    def eventFilter(self, obj, event):
        # Down/Up from the field move through the results without the
        # focus leaving what is being typed - the behaviour every app
        # this borrows from has.
        if obj is self.field and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.results.isVisible():
                row = self.results.currentRow() + (1 if event.key() == Qt.Key.Key_Down else -1)
                self.results.setCurrentRow(max(0, min(row, self.results.count() - 1)))
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.results.currentItem()
                if item is not None:
                    self._open(item)
                return True
        return super().eventFilter(obj, event)

    def _open(self, item):
        page_name, title = item.data(Qt.ItemDataRole.UserRole)
        self.close()
        self._window.reveal_entry(page_name, title)
