"""Shared base for the Websites and Apps pages: a customizable grid of
icon+name cards (right-click for Edit/Delete, click to open) where each
entry can launch up to 3 URLs/apps together at once."""

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

from helpers import images, storage, theme
from helpers.widgets import Card, GlassPage, scroll_area

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*.*)"
EXE_FILTER = "Executable / Shortcut (*.exe *.lnk);;All files (*.*)"
MAX_TARGETS = 3

CARD_WIDTH = 108
THUMB_SIZE = (44, 44)
GRID_COLS = 7


def open_link_entry(parent, entry, label="Links"):
    """Launch every target (site/app) attached to a Websites/Apps entry.
    Shared by LinkGridPage and the Home dashboard's preview lists."""
    for t in entry.get("targets", []):
        if not t.get("target"):
            continue
        if t["type"] == "app":
            try:
                subprocess.Popen([t["target"]], shell=True,
                                  cwd=str(Path(t["target"]).parent))
            except OSError as exc:
                QMessageBox.critical(parent, label, f"Couldn't launch '{t['target']}':\n{exc}")
        else:
            webbrowser.open(t["target"])


def _migrate_entry(entry):
    """Older saves stored a single type/target pair directly on the
    entry; fold that into the new `targets` list so existing data keeps
    working."""
    if "targets" not in entry:
        entry["targets"] = [{"type": entry.pop("type", "site"), "target": entry.pop("target", "")}]
    return entry


class LinkGridPage(GlassPage):
    """Base for a customizable icon+name grid of sites/apps. Subclasses
    just set DATA_FILE/TITLE/SUBTITLE/DEFAULT_ENTRIES."""

    DATA_FILE = "links.json"
    TITLE = "Links"
    SUBTITLE = ""
    DEFAULT_ENTRIES = []

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

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(10)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        panel_layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._refresh_grid()

    # ------------------------------------------------------------------
    def _refresh_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not self.entries:
            empty = QLabel(f"No {self.TITLE.lower()} yet - click '+' to create one.",
                            objectName="Muted")
            self.grid_layout.addWidget(empty, 0, 0)
            return

        for index, entry in enumerate(self.entries):
            card = self._build_card(entry)
            self.grid_layout.addWidget(card, index // GRID_COLS, index % GRID_COLS)

    def _build_card(self, entry):
        card = Card(hoverable=True)
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
        return card

    def _show_context_menu(self, event, entry):
        menu = QMenu(self)
        menu.addAction("Edit", lambda: self._open_edit_form(entry))
        menu.addAction("Delete", lambda: self._remove_entry(entry))
        menu.exec(event.globalPosition().toPoint())

    # ------------------------------------------------------------------
    def _open_entry(self, entry):
        open_link_entry(self, entry, self.TITLE)

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
            self.entries.append(entry)
        storage.save(self.DATA_FILE, self.entries)
        self._refresh_grid()


class TargetRow(QWidget):
    """One Type + URL/path field, used up to MAX_TARGETS times per entry.

    `on_target_changed` (if given) fires with this row's `get()` result
    whenever the target actually changes - used by the first row to
    auto-fetch the entry's icon instead of making the user pick one."""

    def __init__(self, label_text, parent=None, on_target_changed=None):
        super().__init__(parent)
        self._on_target_changed = on_target_changed
        self._last_notified = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(label_text, objectName="Muted"))

        row = QHBoxLayout()
        row.setSpacing(6)
        self.type_box = QComboBox()
        self.type_box.addItems(["Website", "App"])
        self.type_box.setFixedWidth(90)
        self.type_box.currentTextChanged.connect(self._update_browse)
        self.type_box.currentTextChanged.connect(self._notify_change)
        row.addWidget(self.type_box)

        self.target_edit = QLineEdit()
        self.target_edit.editingFinished.connect(self._notify_change)
        row.addWidget(self.target_edit, stretch=1)

        self.browse_btn = QPushButton("...", objectName="Small")
        self.browse_btn.setFixedWidth(36)
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)

        layout.addLayout(row)
        self._update_browse()

    def _update_browse(self):
        self.browse_btn.setVisible(self.type_box.currentText() == "App")

    def _notify_change(self):
        if not self._on_target_changed:
            return
        data = self.get()
        key = (data["type"], data["target"]) if data else None
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
        return {"type": "app" if self.type_box.currentText() == "App" else "site", "target": target}

    def set(self, data):
        if not data:
            return
        self.type_box.setCurrentText("App" if data.get("type") == "app" else "Website")
        self.target_edit.setText(data.get("target", ""))
        self._update_browse()


class _IconSignals(QObject):
    ready = Signal(str, str)  # target (identity check), local path ("" = failed)


class EntryForm(QDialog):
    def __init__(self, parent, entry, on_save):
        super().__init__(parent)
        self.on_save = on_save
        self.entry = entry
        self.is_new = entry is None
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

        form.addSpacing(10)
        form.addWidget(QLabel("Opens (up to 3 URLs/apps at once)"))
        existing_targets = entry.get("targets", []) if entry else []
        labels = ["Target 1", "Target 2 (optional)", "Target 3 (optional)"]
        self.rows = []
        for i in range(MAX_TARGETS):
            row = TargetRow(labels[i], on_target_changed=self._on_primary_target_changed if i == 0 else None)
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
