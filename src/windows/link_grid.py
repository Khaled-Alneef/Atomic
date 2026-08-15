"""Shared base for the Websites and Apps pages: a customizable grid of
icon+name cards (right-click for Edit/Delete, click to open) where each
entry can launch up to 3 targets together at once - URLs for Websites,
executables/shortcuts for Apps (each page is single-purpose via its own
TARGET_KIND, not a per-target Website/App choice)."""

import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
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

# Same shape as Games' SORT_OPTIONS ("Last Played" here is "Last Used" -
# whenever an entry's targets were last opened, see _open_entry).
SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Used"]


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


def _migrate_entry(entry):
    """Older saves stored a single type/target pair directly on the
    entry; fold that into the new `targets` list so existing data keeps
    working. Also backfills added_at/last_used (added once sorting/Move
    Up-Down was added, matching Games) for entries saved before either
    existed."""
    if "targets" not in entry:
        entry["targets"] = [{"type": entry.pop("type", "site"), "target": entry.pop("target", "")}]
    if not entry.get("id"):
        # Entries saved before ids existed have none, and everything that
        # touches one entry rather than the whole list works by id -
        # update_entry, and now drag-to-reorder, which would otherwise
        # match every id-less card against every other one.
        entry["id"] = str(uuid.uuid4())
    if "added_at" not in entry:
        entry["added_at"] = storage.now_iso()
    if "last_used" not in entry:
        entry["last_used"] = None
    return entry


class LinkGridPage(GlassPage):
    """Base for a customizable icon+name grid of sites/apps. Subclasses
    just set DATA_FILE/TITLE/SUBTITLE/DEFAULT_ENTRIES/TARGET_KIND.

    TARGET_KIND fixes what every target on this page's entries is - "site"
    (a URL, opened in the browser) or "app" (an executable/shortcut,
    launched directly) - so the Add/Edit form doesn't need a per-target
    Website/App choice; Websites only ever adds URLs, Apps only ever adds
    executables.
    """

    DATA_FILE = "links.json"
    TITLE = "Links"
    SUBTITLE = ""
    DEFAULT_ENTRIES = []
    TARGET_KIND = "site"

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        entries = storage.load(self.DATA_FILE, None)
        if entries is None:
            entries = self.DEFAULT_ENTRIES
        self.entries = [_migrate_entry(e) for e in entries]
        storage.save(self.DATA_FILE, self.entries)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 20, 24, 24)
        panel_layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(QLabel(self.TITLE, objectName="PanelTitle"))
        if self.SUBTITLE:
            title_box.addWidget(QLabel(self.SUBTITLE, objectName="PanelSubtitle"))
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
        hint = QLabel("(drag a card to reorder, or right-click it for Move Up/Down)", objectName="Muted")
        top_row.addWidget(hint)
        top_row.addStretch()
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

        if not self.entries:
            empty = QLabel(f"No {self.TITLE.lower()} yet - click '+' to create one.",
                            objectName="Muted")
            self.grid_layout.addWidget(empty, 0, 0)
            return

        for index, entry in enumerate(self._sorted_entries()):
            card = self._build_card(entry)
            self.grid_layout.addWidget(card, index // GRID_COLS, index % GRID_COLS)

    def _build_card(self, entry):
        card = Card(hoverable=True, matte=True)
        card.setFixedWidth(CARD_WIDTH)
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.setContentsMargins(8, 10, 8, 10)

        icon = QLabel()
        icon.setFixedSize(*THUMB_SIZE)
        icon.setPixmap(images.thumbnail_or_avatar(entry.get("image"), entry["name"], THUMB_SIZE))
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        name = QLabel(entry["name"], objectName="CardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        name.setWordWrap(True)
        layout.addWidget(name)

        card.clicked.connect(lambda en=entry: self._open_entry(en))
        card.rightClicked.connect(lambda event, en=entry: self._show_context_menu(event, en))
        self._drag_reorder.attach(card, entry.get("id"))
        return card

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_edit_form(entry))
        if self.sort_box.currentText() == "Custom Order":
            menu.addAction("Move Up", lambda: self._move_entry(entry, -1))
            menu.addAction("Move Down", lambda: self._move_entry(entry, 1))
        menu.addAction("Delete", lambda: self._remove_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    # ------------------------------------------------------------------
    def _open_entry(self, entry):
        open_link_entry(self, entry, self.TITLE)
        entry["last_used"] = storage.now_iso()
        storage.save(self.DATA_FILE, self.entries)
        if self.sort_box.currentText() == "Last Used":
            self._refresh_grid()

    def _move_entry(self, entry, delta):
        if self.sort_box.currentText() != "Custom Order":
            QMessageBox.information(self, self.TITLE, "Switch Sort to 'Custom Order' to reorder manually.")
            return
        idx = self.entries.index(entry)
        new_idx = idx + delta
        if 0 <= new_idx < len(self.entries):
            self.entries[idx], self.entries[new_idx] = self.entries[new_idx], self.entries[idx]
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()

    def _remove_entry(self, entry):
        if QMessageBox.question(self, "Remove", f"Remove '{entry['name']}'?") == QMessageBox.StandardButton.Yes:
            self.entries.remove(entry)
            storage.save(self.DATA_FILE, self.entries)
            self._refresh_grid()

    def _open_add_form(self):
        EntryForm(self, None, on_save=self._on_form_save)

    def _open_edit_form(self, entry):
        EntryForm(self, entry, on_save=self._on_form_save)

    def _on_form_save(self, entry, is_new):
        if is_new:
            entry["added_at"] = storage.now_iso()
            entry["last_used"] = None
            self.entries.append(entry)
        storage.save(self.DATA_FILE, self.entries)
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
