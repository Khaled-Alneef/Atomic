"""Shared base for the Websites and Apps pages: a customizable grid of
icon+name cards (right-click for Edit/Delete, click to open) where each
entry can launch up to 3 targets together at once - URLs for Websites,
executables/shortcuts for Apps (each page is single-purpose via its own
TARGET_KIND, not a per-target Website/App choice)."""

import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGraphicsOpacityEffect,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from helpers import child_process, images, storage, theme
from helpers.widgets import (
    Card, CardDragReorder, GlassPage, defer_grid_rebuild, scroll_area,
)

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*.*)"
EXE_FILTER = "Executable / Shortcut (*.exe *.lnk);;All files (*.*)"
MAX_TARGETS = 3

CARD_WIDTH = 120
THUMB_SIZE = (44, 44)
GRID_COLS = 13

# Shared by Apps, Websites and Games so the three grids stay identical
# and CARD_TEXT_WIDTH below can't drift out of step with the margins it
# is derived from.
CARD_MARGINS = (8, 10, 8, 10)
# The width a card's text actually gets on screen: the fixed card width
# less the card layout's own left/right margins. The QSS border is not
# subtracted - measured, a 120px card lays its contents out across 104px.
CARD_TEXT_WIDTH = CARD_WIDTH - CARD_MARGINS[0] - CARD_MARGINS[2]

# Same shape as Games' SORT_OPTIONS ("Last Played" here is "Last Used" -
# whenever an entry's targets were last opened, see _open_entry).
SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Used"]


class CardTextLabel(QLabel):
    """A word-wrapped line of text on a card, sized honestly for the
    width it will actually be given.

    A plain wrapped QLabel is not, and that clipped the second line of
    every long card name on Apps, Websites and Games. Two Qt behaviours
    combine to do it:

    * `QLabel.sizeHint()` for a wrapped label is a heuristic - it picks a
      wrap width it thinks looks balanced rather than the one it will be
      laid out at, and reports the height *that* width needs. Measured on
      "A Really Long Missing Application Name": a sizeHint wide enough
      for two lines, in a card that only ever offers 104px, where the
      same text needs three.
    * A QBoxLayout with an alignment set (these cards centre their
      contents) lays itself out inside `alignmentRect`, which clamps the
      layout's *width* to what the card has - but keeps the height the
      too-wide sizeHint asked for. So the label is narrowed without ever
      being asked how tall it now needs to be.

    Fixing the width and answering sizeHint from `heightForWidth` at that
    same width removes both halves: the layout cannot narrow it further,
    and the height it reports is the height the text really occupies.
    Deliberately lazy rather than measured in `__init__` - the fonts here
    come from QSS (#CardTitle's weight, the badge's 8pt), which is not
    applied to a widget until it is polished, some time after it is
    built."""

    def __init__(self, text, width=CARD_TEXT_WIDTH, parent=None):
        super().__init__(text, parent)
        self._text_width = width
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.setFixedWidth(width)

    def sizeHint(self):
        return QSize(self._text_width, self.heightForWidth(self._text_width))

    def minimumSizeHint(self):
        # Same answer as sizeHint: QLabel's own minimumSizeHint for
        # wrapped text is another heuristic, and a minimum shorter than
        # the real height is all a grid row needs to squeeze the last
        # line back off the card.
        return self.sizeHint()


def open_link_entry(parent, entry, label="Links"):
    """Launch every target (site/app) attached to a Websites/Apps entry.
    Shared by LinkGridPage and the Home dashboard's preview lists."""
    for t in entry.get("targets", []):
        if not t.get("target"):
            continue
        if t["type"] == "app":
            try:
                subprocess.Popen([t["target"]], shell=True,
                                  cwd=str(Path(t["target"]).parent),
                                  env=child_process.clean_env(),
                                  creationflags=child_process.flags())
            except OSError as exc:
                QMessageBox.critical(parent, label, f"Couldn't launch '{t['target']}':\n{exc}")
        else:
            webbrowser.open(t["target"])


# A stat on a local disk is microseconds, but a path on a disconnected
# network share or an unplugged drive can block for seconds - and this
# grid is rebuilt from scratch on every visit, every sort change and
# every debounced search keystroke, so one uninstalled app on a dead
# drive would otherwise be paid for again on each redraw. Answers are
# reused for a few seconds: long enough that typing a query costs one
# check per path instead of one per rebuild, short enough that
# installing or removing a program while Atomic is open shows up on the
# next visit rather than needing a restart.
_EXISTS_TTL = 5.0
_exists_cache = {}


def _target_exists(path: str) -> bool:
    now = time.monotonic()
    cached = _exists_cache.get(path)
    if cached is not None and now - cached[0] < _EXISTS_TTL:
        return cached[1]
    # Path.exists() answers False rather than raising for a path Windows
    # can't even parse (illegal characters, a drive letter that isn't
    # mounted), which is the answer wanted here anyway.
    exists = Path(path).exists()
    _exists_cache[path] = (now, exists)
    return exists


def missing_app_targets(entry) -> list:
    """The entry's app targets whose path no longer exists - an
    uninstalled or moved program.

    App targets only. A "site" target is a URL, and asking the same
    question of one means a network probe with all of probe_site's
    deadline discipline; that is deliberately out of scope (roadmap
    #16), which is also why this needs no per-page special-casing -
    Websites entries carry no "app" target, so they simply never
    match."""
    return [t["target"] for t in entry.get("targets", [])
            if t.get("type") == "app" and t.get("target")
            and not _target_exists(t["target"])]


def _migrate_entry(entry):
    """Older saves stored a single type/target pair directly on the
    entry; fold that into the new `targets` list so existing data keeps
    working. Also backfills added_at/last_used (added once sorting/Move
    Up-Down was added, matching Games) for entries saved before either
    existed.

    Returns True when it actually changed the entry, so the caller can
    tell a real migration (which has to be written) from the ordinary
    case of already-current data (which must not be)."""
    changed = False
    if "targets" not in entry:
        entry["targets"] = [{"type": entry.pop("type", "site"), "target": entry.pop("target", "")}]
        changed = True
    if not entry.get("id"):
        # Entries saved before ids existed have none, and everything that
        # touches one entry rather than the whole list works by id -
        # update_entry, and now drag-to-reorder, which would otherwise
        # match every id-less card against every other one.
        entry["id"] = str(uuid.uuid4())
        changed = True
    if "added_at" not in entry:
        entry["added_at"] = storage.now_iso()
        changed = True
    if "last_used" not in entry:
        entry["last_used"] = None
        changed = True
    return changed


def _migrate_entries(entries):
    """(migrated list, did anything change). Applied to whatever was just
    read off disk - including inside _mutate, because a list re-read there
    can be older than the one this page was built from (Settings > Clear
    Data writes these files too), and an un-migrated entry has no id for
    update_entry or drag-reorder to work by."""
    changed = False
    for entry in entries:
        changed |= _migrate_entry(entry)
    return list(entries), changed


class LinkGridPage(GlassPage):
    """Base for a customizable icon+name grid of sites/apps. Subclasses
    just set DATA_FILE/TITLE/DEFAULT_ENTRIES/TARGET_KIND.

    TARGET_KIND fixes what every target on this page's entries is - "site"
    (a URL, opened in the browser) or "app" (an executable/shortcut,
    launched directly) - so the Add/Edit form doesn't need a per-target
    Website/App choice; Websites only ever adds URLs, Apps only ever adds
    executables.
    """

    DATA_FILE = "links.json"
    TITLE = "Links"
    DEFAULT_ENTRIES = []
    TARGET_KIND = "site"

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        entries = storage.load(self.DATA_FILE, None)
        fresh = entries is None
        if fresh:
            entries = self.DEFAULT_ENTRIES
        self.entries, migrated = _migrate_entries(entries)
        # Only written when the migration actually changed something, or
        # there was no file yet. This used to write the whole list back on
        # every single visit to the page - a whole-list save off a
        # just-loaded snapshot is harmless in itself, but it meant the one
        # save shape this file is being fixed for ran every time the user
        # clicked Apps or Websites in the sidebar.
        if fresh or migrated:
            storage.save(self.DATA_FILE, self.entries)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 20, 24, 24)
        panel_layout.setSpacing(14)

        # Title only. The subtitle under it ("Sites you open all the time")
        # described what the page obviously was, and no other page carries
        # one - the mechanism goes with it rather than sitting unused.
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(QLabel(self.TITLE, objectName="PanelTitle"))
        header.addLayout(title_box)
        header.addStretch()
        add_btn = QPushButton("+", objectName="AccentIcon")
        add_btn.setFixedSize(40, 40)
        add_btn.setToolTip("Add")
        add_btn.clicked.connect(self._open_add_form)
        header.addWidget(add_btn, alignment=Qt.AlignmentFlag.AlignTop)
        panel_layout.addLayout(header)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        # No drag hint here any more: it named a right-click Move Up/Down
        # that no longer exists, and dragging is how every page reorders.
        top_row.addStretch()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(f"Search {self.TITLE.lower()}...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(220)
        # Debounced rather than filtering on every keystroke: each redraw
        # rebuilds every card from scratch (pages hold no state - see
        # .claude/rules/ui.md), so typing six characters would otherwise
        # rebuild the whole grid six times. Same 150ms as the tracker
        # pages, which this is the extension of.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh_grid)
        self.search_box.textChanged.connect(lambda _text: self._search_timer.start())
        top_row.addWidget(self.search_box)
        panel_layout.addLayout(top_row)

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(10)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        panel_layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._drag_reorder = CardDragReorder(
            self.grid_body, self._begin_custom_order, self._drop_reorder)

        self._refresh_grid()

    # ------------------------------------------------------------------
    def _begin_custom_order(self):
        """A drag has started, so this page is in Custom Order from here
        on - see GamesPage._begin_custom_order, same reasoning: any other
        sort is re-applied on the next redraw and would undo the drop.

        The order on screen is saved as the custom one before the switch,
        so the switch itself moves nothing, and the dropdown is set with
        its signal blocked because a rebuild here would delete the card
        being dragged."""
        if self.sort_box.currentText() == "Custom Order":
            return
        order = [e.get("id") for e in self._sorted_entries()]
        storage.apply_custom_order(self.DATA_FILE, order)
        # In place, not reloaded: the cards on screen hold these dicts.
        storage.order_by_ids(self.entries, order)
        self.sort_box.blockSignals(True)
        self.sort_box.setCurrentText("Custom Order")
        self.sort_box.blockSignals(False)

    def _drop_reorder(self, moved_id, target_id):
        if not storage.move_entry(self.DATA_FILE, moved_id, target_id):
            return
        storage.move_in_list(self.entries, moved_id, target_id)
        defer_grid_rebuild(self._refresh_grid)

    # ------------------------------------------------------------------
    def _sorted_entries(self):
        mode = self.sort_box.currentText()
        if mode == "Name (A-Z)":
            return sorted(self.entries, key=lambda e: e["name"].lower())
        if mode == "Date Added (Newest)":
            return sorted(self.entries, key=lambda e: e.get("added_at") or "", reverse=True)
        if mode == "Last Used":
            return sorted(self.entries, key=lambda e: e.get("last_used") or "", reverse=True)
        return self.entries

    def _search_query(self) -> str:
        # getattr because _refresh_grid can run before the box exists on a
        # page still being built.
        box = getattr(self, "search_box", None)
        return box.text().strip().lower() if box else ""

    def _visible_entries(self):
        """What the grid draws: the sorted list narrowed by the search box.

        Deliberately not folded into _sorted_entries - that one is what
        _begin_custom_order writes out as the custom order, and it has to
        stay the whole list even if a query were somehow active."""
        query = self._search_query()
        entries = self._sorted_entries()
        if not query:
            return entries
        # Plain case-insensitive substring, not a fuzzy match: same
        # reasoning as the tracker pages - the user is looking for a name
        # they know is there, and near-misses quietly included make a
        # short query look like it failed to filter at all.
        return [e for e in entries if query in (e.get("name") or "").lower()]

    def _refresh_grid(self, *_args):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # deleteLater() alone leaves the widget visible (just no
                # longer laid out) until Qt gets around to the deferred
                # delete - hide() immediately so a leftover "no entries
                # yet" placeholder doesn't linger behind the first card
                # added right after it.
                widget.hide()
                widget.deleteLater()

        # Dragging is off while a search is narrowing the grid: a drop
        # writes the order that is on screen (see _begin_custom_order),
        # and that isn't the whole order while cards are hidden.
        narrowed = bool(self._search_query())

        entries = self._visible_entries()
        if not entries:
            message = (f"Nothing here matches '{self.search_box.text().strip()}'."
                       if narrowed
                       else f"No {self.TITLE.lower()} yet - click '+' to create one.")
            self.grid_layout.addWidget(QLabel(message, objectName="Muted"), 0, 0)
            return

        for index, entry in enumerate(entries):
            card = self._build_card(entry, draggable=not narrowed)
            self.grid_layout.addWidget(card, index // GRID_COLS, index % GRID_COLS)

    def _build_card(self, entry, draggable=True):
        card = Card(hoverable=True, matte=True)
        card.setFixedWidth(CARD_WIDTH)
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.setContentsMargins(*CARD_MARGINS)

        missing = missing_app_targets(entry)

        icon = QLabel()
        icon.setFixedSize(*THUMB_SIZE)
        icon.setPixmap(images.thumbnail_or_avatar(entry.get("image"), entry["name"], THUMB_SIZE))
        if missing:
            # Dimmed rather than swapped for a warning glyph: the icon is
            # how a card is picked out of a grid at a glance, and losing
            # it would make the row harder to read, not clearer. The
            # badge below is what actually states the verdict.
            faded = QGraphicsOpacityEffect(icon)
            faded.setOpacity(0.3)
            icon.setGraphicsEffect(faded)
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        name = CardTextLabel(entry["name"])
        name.setObjectName("CardTitle")
        layout.addWidget(name)

        if missing:
            total = len([t for t in entry.get("targets", []) if t.get("target")])
            # "Not found" only when nothing on this card can launch; an
            # entry that opens three programs and lost one still works,
            # and saying otherwise would be wrong rather than cautious.
            text = ("Not found" if len(missing) >= total
                    else f"{len(missing)} of {total} not found")
            badge = CardTextLabel(text)
            badge.setStyleSheet(
                f"color: {theme.DANGER}; font-weight: 700; font-size: 8pt; background: transparent;")
            layout.addWidget(badge)
            # The card itself carries the paths - the badge has room for
            # a verdict, not for a path, and "which one?" is the next
            # question on an entry that launches more than one.
            card.setToolTip("No longer on disk:\n" + "\n".join(missing))

        card.clicked.connect(lambda en=entry: self._open_entry(en))
        card.rightClicked.connect(lambda event, en=entry: self._show_context_menu(event, en))
        if draggable:
            self._drag_reorder.attach(card, entry.get("id"))
        return card

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_edit_form(entry))
        # No Move Up/Move Down: these cards reorder by dragging, same as
        # every other page, and the menu items only appeared under one sort
        # mode - a second, worse way to do the same thing.
        menu.addAction("Delete", lambda: self._remove_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    # ------------------------------------------------------------------
    def _mutate(self, apply_change):
        """Apply a change to the saved list and redraw - GamesPage._mutate,
        same reasoning and same shape.

        Re-reads the file and works on that rather than saving this page's
        own `self.entries`: Home holds its own copy of apps.json and
        websites.json, and Settings > Clear Data can empty either one
        while this page sits open behind the dialog (main.py's
        refresh_current_page names that exact race). Writing back the
        snapshot this page was built from would roll all of that back -
        which is how reordering one game once erased a whole batch of
        freshly imported ones."""
        self.entries, _ = _migrate_entries(storage.load(self.DATA_FILE, []))
        apply_change(self.entries)
        storage.save(self.DATA_FILE, self.entries)
        self._refresh_grid()

    @staticmethod
    def _index_of(entries, entry):
        return next((i for i, e in enumerate(entries) if e.get("id") == entry.get("id")), None)

    def _open_entry(self, entry):
        open_link_entry(self, entry, self.TITLE)
        entry["last_used"] = storage.now_iso()
        # One field on one entry, so no whole-list write and no redraw of
        # cards the user is still looking at.
        storage.update_entry(self.DATA_FILE, entry.get("id"), {"last_used": entry["last_used"]})
        if self.sort_box.currentText() == "Last Used":
            self._refresh_grid()

    def _remove_entry(self, entry):
        if QMessageBox.question(self, "Remove", f"Remove '{entry['name']}'?") != QMessageBox.StandardButton.Yes:
            return

        def apply_change(entries):
            idx = self._index_of(entries, entry)
            if idx is not None:
                entries.pop(idx)

        self._mutate(apply_change)

    def _open_add_form(self):
        EntryForm(self, None, on_save=self._on_form_save)

    def _open_edit_form(self, entry):
        EntryForm(self, entry, on_save=self._on_form_save)

    def _on_form_save(self, entry, is_new):
        if is_new:
            entry["added_at"] = storage.now_iso()
            entry["last_used"] = None
            self._mutate(lambda entries: entries.append(entry))
            return
        # An edit touches only this entry's own fields; update_entry
        # merges them into the file as it stands now. The form has
        # already applied them to the dict the cards hold, so the page
        # only needs the redraw.
        storage.update_entry(self.DATA_FILE, entry.get("id"), {
            "name": entry["name"], "targets": entry["targets"], "image": entry.get("image"),
        })
        self._refresh_grid()


class TargetRow(QWidget):
    """One URL/path field, used up to MAX_TARGETS times per entry. `kind`
    ("site" or "app", fixed by the owning page's TARGET_KIND) decides the
    placeholder text and whether a Browse... button is shown - there's no
    per-row type choice anymore, each page is single-purpose.

    `on_target_changed` (if given) fires with this row's `get()` result
    whenever the target actually changes - used by the first row to
    auto-fetch the entry's icon instead of making the user pick one."""

    def __init__(self, label_text, kind, parent=None, on_target_changed=None):
        super().__init__(parent)
        self.kind = kind
        self._on_target_changed = on_target_changed
        self._last_notified = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(label_text, objectName="Muted"))

        row = QHBoxLayout()
        row.setSpacing(6)

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(
            "Path to .exe/.lnk" if kind == "app" else "https://example.com"
        )
        self.target_edit.editingFinished.connect(self._notify_change)
        row.addWidget(self.target_edit, stretch=1)

        self.browse_btn = QPushButton("...", objectName="Small")
        self.browse_btn.setFixedWidth(36)
        self.browse_btn.clicked.connect(self._browse)
        self.browse_btn.setVisible(kind == "app")
        row.addWidget(self.browse_btn)

        layout.addLayout(row)

    def _notify_change(self):
        if not self._on_target_changed:
            return
        data = self.get()
        key = data["target"] if data else None
        if key == self._last_notified:
            return
        self._last_notified = key
        self._on_target_changed(data)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select an executable or shortcut", "", EXE_FILTER)
        if path:
            self.target_edit.setText(path)
            self._notify_change()

    def get(self):
        target = self.target_edit.text().strip()
        if not target:
            return None
        return {"type": self.kind, "target": target}

    def set(self, data):
        if not data:
            return
        self.target_edit.setText(data.get("target", ""))


class _IconSignals(QObject):
    ready = Signal(str, str)  # target (identity check), local path ("" = failed)


class EntryForm(QDialog):
    def __init__(self, parent, entry, on_save):
        super().__init__(parent)
        self.on_save = on_save
        self.entry = entry
        self.is_new = entry is None
        self.target_kind = getattr(parent, "TARGET_KIND", "site")
        self.image_path = entry.get("image") if entry else None
        # Only auto-fetch an icon while there isn't one yet - once an
        # entry has an image (auto-fetched or manually chosen), further
        # target edits don't silently replace it.
        self.auto_image = self.image_path is None
        self._icon_signals = _IconSignals()
        self._icon_signals.ready.connect(self._on_site_icon_ready)

        self.setWindowTitle("Edit Entry" if entry else "Add Entry")
        self.setFixedSize(420, 560)
        theme.apply_dark_titlebar(self)

        form = QVBoxLayout(self)
        form.setContentsMargins(24, 20, 24, 16)
        form.setSpacing(4)

        form.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(entry["name"] if entry else "")
        form.addWidget(self.name_edit)

        kind_label = "apps" if self.target_kind == "app" else "URLs"
        form.addSpacing(10)
        form.addWidget(QLabel(f"Opens (up to {MAX_TARGETS} {kind_label} at once)"))
        existing_targets = entry.get("targets", []) if entry else []
        labels = ["Target 1", "Target 2 (optional)", "Target 3 (optional)"]
        self.rows = []
        for i in range(MAX_TARGETS):
            row = TargetRow(labels[i], self.target_kind,
                             on_target_changed=self._on_primary_target_changed if i == 0 else None)
            if i < len(existing_targets):
                row.set(existing_targets[i])
            form.addWidget(row)
            self.rows.append(row)

        form.addSpacing(10)
        form.addWidget(QLabel("Image (auto-detected from Target 1 - override below if needed)"))
        image_row = QHBoxLayout()
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(56, 56)
        image_row.addWidget(self.preview_label)
        choose_btn = QPushButton("Choose Image...")
        choose_btn.clicked.connect(self._choose_image)
        image_row.addWidget(choose_btn)
        image_row.addStretch()
        form.addLayout(image_row)
        self._refresh_preview()

        form.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save", objectName="Accent")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        form.addLayout(btn_row)

        self.exec()

    def _on_primary_target_changed(self, target_data):
        if not self.auto_image or not target_data:
            return
        kind, target = target_data["type"], target_data["target"]
        if kind == "app":
            path = images.extract_app_icon(target)
            if path:
                self.image_path = str(path)
                self._refresh_preview()
        else:
            threading.Thread(target=self._fetch_site_icon_worker, args=(target,), daemon=True).start()

    def _fetch_site_icon_worker(self, target):
        # Must never raise: an uncaught exception here would kill the
        # background thread silently.
        try:
            path = images.fetch_site_icon(target)
        except Exception:
            path = None
        self._icon_signals.ready.emit(target, str(path) if path else "")

    def _on_site_icon_ready(self, target, path):
        current = self.rows[0].get()
        if not self.auto_image or not path or not current or current["target"] != target:
            return  # stale - the target changed again before this returned
        self.image_path = path
        self._refresh_preview()

    def _choose_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose an image", "", IMAGE_FILTER)
        if path:
            self.image_path = path
            self.auto_image = False
            self._refresh_preview()

    def _refresh_preview(self):
        name = self.name_edit.text() or "?"
        self.preview_label.setPixmap(images.thumbnail_or_avatar(self.image_path, name, (56, 56)))

    def _save(self):
        name = self.name_edit.text().strip()
        targets = [t for t in (row.get() for row in self.rows) if t]
        if not name or not targets:
            QMessageBox.warning(self, "Links", "Name and at least one URL/app are required.")
            return

        if self.is_new:
            self.entry = {"id": str(uuid.uuid4())}
        self.entry.update(name=name, targets=targets, image=self.image_path)
        self.on_save(self.entry, self.is_new)
        self.accept()
