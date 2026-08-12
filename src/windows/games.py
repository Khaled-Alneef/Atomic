"""Games page: a small local launcher. Add a game's executable/shortcut
once (its icon is picked up automatically), then launch it with one click.
Reorder manually by dragging (or Move Up/Down), or sort by name/date/last
played."""

import subprocess
import uuid
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout,
)

from helpers import icon_extract, images, storage, theme
from helpers.widgets import GlassPage

DATA_FILE = "games.json"
ICON_SIZE = (34, 34)
FILE_FILTER = "Games (*.exe *.lnk *.url);;All files (*.*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*.*)"
SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Played"]


def _extract_and_cache_icon(path):
    img = icon_extract.extract_icon(path, size=64)
    if img is None:
        return None
    dest = images.CACHE_DIR / f"game_{uuid.uuid4().hex}.png"
    try:
        img.save(dest)
        return str(dest)
    except Exception:
        return None


class GamesPage(GlassPage):
    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        self.games = storage.load(DATA_FILE, [])
        self._migrate_and_backfill()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Games", objectName="PanelTitle"))

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_list)
        top_row.addWidget(self.sort_box)
        hint = QLabel("(drag rows, or use Move Up/Down, while sorted as Custom Order)", objectName="Muted")
        top_row.addWidget(hint)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(*ICON_SIZE))
        self.tree.itemDoubleClicked.connect(lambda *_: self._launch_selected())
        self.tree.model().rowsMoved.connect(self._on_reorder)
        layout.addWidget(self.tree, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("+ Add Game", objectName="Accent")
        add_btn.clicked.connect(self._add_game)
        btn_row.addWidget(add_btn)
        launch_btn = QPushButton("Launch")
        launch_btn.clicked.connect(self._launch_selected)
        btn_row.addWidget(launch_btn)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_selected)
        btn_row.addWidget(edit_btn)
        up_btn = QPushButton("↑", objectName="Small")
        up_btn.setFixedWidth(36)
        up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(up_btn)
        down_btn = QPushButton("↓", objectName="Small")
        down_btn.setFixedWidth(36)
        down_btn.clicked.connect(lambda: self._move_selected(1))
        btn_row.addWidget(down_btn)
        btn_row.addStretch()
        remove_btn = QPushButton("Remove", objectName="Danger")
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(remove_btn)
        layout.addLayout(btn_row)

        self._refresh_list()

    # ------------------------------------------------------------------
    def _migrate_and_backfill(self):
        changed = False
        for game in self.games:
            if "added_at" not in game:
                game["added_at"] = storage.now_iso()
                changed = True
            if "last_played" not in game:
                game["last_played"] = None
                changed = True
            if not game.get("icon") and game.get("path") and Path(game["path"]).exists():
                icon_path = _extract_and_cache_icon(game["path"])
                if icon_path:
                    game["icon"] = icon_path
                    changed = True
        if changed:
            storage.save(DATA_FILE, self.games)

    def _sorted_games(self):
        mode = self.sort_box.currentText()
        if mode == "Name (A-Z)":
            return sorted(self.games, key=lambda g: g["name"].lower())
        if mode == "Date Added (Newest)":
            return sorted(self.games, key=lambda g: g.get("added_at") or "", reverse=True)
        if mode == "Last Played":
            return sorted(self.games, key=lambda g: g.get("last_played") or "", reverse=True)
        return self.games

    def _refresh_list(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        custom = self.sort_box.currentText() == "Custom Order"
        self.tree.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove if custom
            else QAbstractItemView.DragDropMode.NoDragDrop
        )
        for game in self._sorted_games():
            item = QTreeWidgetItem([f"  {game['name']}"])
            pixmap = images.thumbnail_or_avatar(game.get("icon"), game["name"], ICON_SIZE)
            item.setIcon(0, QIcon(pixmap))
            item.setData(0, Qt.ItemDataRole.UserRole, game["id"])
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)

    def _on_reorder(self, *_args):
        visible_ids = [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                       for i in range(self.tree.topLevelItemCount())]
        by_id = {g["id"]: g for g in self.games}
        self.games = [by_id[i] for i in visible_ids if i in by_id]
        storage.save(DATA_FILE, self.games)

    # ------------------------------------------------------------------
    def _add_game(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a game executable or shortcut", "", FILE_FILTER)
        if not path:
            return
        name = Path(path).stem
        icon_path = _extract_and_cache_icon(path)
        self.games.append({
            "id": str(uuid.uuid4()), "name": name, "path": path, "icon": icon_path,
            "added_at": storage.now_iso(), "last_played": None,
        })
        storage.save(DATA_FILE, self.games)
        self._refresh_list()

    def _selected_index(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        game_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        return next((i for i, g in enumerate(self.games) if g["id"] == game_id), None)

    def _launch_selected(self):
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Games", "Select a game first.")
            return
        game = self.games[idx]
        try:
            subprocess.Popen([game["path"]], shell=True, cwd=str(Path(game["path"]).parent))
            game["last_played"] = storage.now_iso()
            storage.save(DATA_FILE, self.games)
            if self.sort_box.currentText() == "Last Played":
                self._refresh_list()
        except OSError as exc:
            QMessageBox.critical(self, "Games", f"Couldn't launch this game:\n{exc}")

    def _edit_selected(self):
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Games", "Select a game first.")
            return
        EditGameForm(self, self.games[idx], on_save=self._on_edit_save)

    def _on_edit_save(self):
        storage.save(DATA_FILE, self.games)
        self._refresh_list()

    def _move_selected(self, delta):
        if self.sort_box.currentText() != "Custom Order":
            QMessageBox.information(self, "Games", "Switch Sort to 'Custom Order' to reorder manually.")
            return
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Games", "Select a game first.")
            return
        new_idx = idx + delta
        if 0 <= new_idx < len(self.games):
            self.games[idx], self.games[new_idx] = self.games[new_idx], self.games[idx]
            storage.save(DATA_FILE, self.games)
            self._refresh_list()

    def _remove_selected(self):
        idx = self._selected_index()
        if idx is None:
            QMessageBox.information(self, "Games", "Select a game first.")
            return
        game = self.games[idx]
        if QMessageBox.question(self, "Remove Game", f"Remove '{game['name']}' from the list?") == QMessageBox.StandardButton.Yes:
            del self.games[idx]
            storage.save(DATA_FILE, self.games)
            self._refresh_list()


class EditGameForm(QDialog):
    def __init__(self, parent, game, on_save):
        super().__init__(parent)
        self.game = game
        self.on_save = on_save
        self.icon_path = game.get("icon")

        self.setWindowTitle("Edit Game")
        self.setFixedSize(420, 380)
        theme.apply_dark_titlebar(self)

        form = QVBoxLayout(self)
        form.setContentsMargins(24, 20, 24, 16)
        form.setSpacing(4)

        form.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(game["name"])
        form.addWidget(self.name_edit)

        form.addSpacing(10)
        form.addWidget(QLabel("Path"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(game["path"])
        path_row.addWidget(self.path_edit, stretch=1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_path)
        path_row.addWidget(browse_btn)
        form.addLayout(path_row)

        form.addSpacing(10)
        form.addWidget(QLabel("Icon"))
        icon_row = QHBoxLayout()
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(56, 56)
        icon_row.addWidget(self.preview_label)
        auto_btn = QPushButton("Auto-Detect")
        auto_btn.clicked.connect(self._auto_detect_icon)
        icon_row.addWidget(auto_btn)
        choose_btn = QPushButton("Choose Image...")
        choose_btn.clicked.connect(self._choose_image)
        icon_row.addWidget(choose_btn)
        icon_row.addStretch()
        form.addLayout(icon_row)
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

    def _browse_path(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a game executable or shortcut", "", FILE_FILTER)
        if path:
            self.path_edit.setText(path)

    def _auto_detect_icon(self):
        icon_path = _extract_and_cache_icon(self.path_edit.text())
        if icon_path:
            self.icon_path = icon_path
            self._refresh_preview()
        else:
            QMessageBox.information(self, "Games", "Couldn't detect an icon for this path.")

    def _choose_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose an image", "", IMAGE_FILTER)
        if path:
            self.icon_path = path
            self._refresh_preview()

    def _refresh_preview(self):
        name = self.name_edit.text() or "?"
        self.preview_label.setPixmap(images.thumbnail_or_avatar(self.icon_path, name, (56, 56)))

    def _save(self):
        name = self.name_edit.text().strip()
        path = self.path_edit.text().strip()
        if not name or not path:
            QMessageBox.warning(self, "Games", "Name and path can't be empty.")
            return
        self.game.update(name=name, path=path, icon=self.icon_path)
        self.on_save()
        self.accept()
