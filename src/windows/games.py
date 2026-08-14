"""Games page: a small local launcher. Add a game's executable/shortcut
once (its icon is picked up automatically), then launch it with one click.
Shown as a poster-style grid, same as Anime/Reading/Series - right-click a
card for Edit/Move Up/Move Down/Delete, sort by name/date/last played, or
reorder manually (Move Up/Down) while sorted as Custom Order.
"""

import subprocess
import threading
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from helpers import app_settings, images, launchers, storage, theme
from helpers.widgets import Card, GlassPage, scroll_area, show_toast
from windows.link_grid import CARD_WIDTH, GRID_COLS, THUMB_SIZE

DATA_FILE = "games.json"
CARD_ICON_SIZE = THUMB_SIZE  # match the Apps/Websites card image size
FILE_FILTER = "Games (*.exe *.lnk *.url);;All files (*.*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*.*)"
SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Played"]

# Shared with Settings' per-launcher auto-import - one place doing icon
# extraction/caching for games, regardless of which UI triggered it.
_extract_and_cache_icon = launchers.extract_and_cache_icon


class _ScanSignals(QObject):
    done = Signal(list)  # [{"name", "path", "launcher"}, ...] found by the background scan


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
        layout.setSpacing(14)

        self._scan_signals = _ScanSignals()
        self._scan_signals.done.connect(self._on_scan_done)

        header = QHBoxLayout()
        header.addWidget(QLabel("Games", objectName="PanelTitle"))
        header.addStretch()
        import_btn = QPushButton("⟳", objectName="AccentIcon")
        import_btn.setFixedSize(40, 40)
        import_btn.setToolTip("Import from Launchers")
        import_btn.clicked.connect(self._import_from_launchers)
        header.addWidget(import_btn)
        add_btn = QPushButton("+", objectName="AccentIcon")
        add_btn.setFixedSize(40, 40)
        add_btn.setToolTip("Add Game")
        add_btn.clicked.connect(self._add_game)
        header.addWidget(add_btn)
        layout.addLayout(header)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Sort:"))
        self.sort_box = QComboBox()
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        hint = QLabel("(right-click a game for Move Up/Down while sorted as Custom Order)", objectName="Muted")
        top_row.addWidget(hint)
        top_row.addStretch()
        layout.addLayout(top_row)

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(14)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._refresh_grid()

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
        if changed:
            storage.save(DATA_FILE, self.games)
        self.games = launchers.backfill_missing_icons(self.games)

    def _mutate(self, apply_change):
        """Apply a change to the saved games list and redraw.

        Re-reads the file first and works on that, rather than saving
        this page's own `self.games`: Home holds its own copy of the
        same list, Settings can import launcher games into it while this
        page sits open behind the dialog, and the icon backfill writes
        to it too. Saving a snapshot taken when this page was built
        would roll all of that back - which is what made game icons
        (and, worse, whole imported games) disappear after something as
        unrelated as moving one entry down the list."""
        self.games = storage.load(DATA_FILE, [])
        apply_change(self.games)
        storage.save(DATA_FILE, self.games)
        self._refresh_grid()

    @staticmethod
    def _index_of(games, game):
        return next((i for i, g in enumerate(games) if g.get("id") == game.get("id")), None)

    def _sorted_games(self):
        mode = self.sort_box.currentText()
        if mode == "Name (A-Z)":
            return sorted(self.games, key=lambda g: g["name"].lower())
        if mode == "Date Added (Newest)":
            return sorted(self.games, key=lambda g: g.get("added_at") or "", reverse=True)
        if mode == "Last Played":
            return sorted(self.games, key=lambda g: g.get("last_played") or "", reverse=True)
        return self.games

    # ------------------------------------------------------------------
    def _refresh_grid(self, *_args):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        games = self._sorted_games()
        if not games:
            empty = QLabel("No games yet - click '+' to add one.", objectName="Muted")
            self.grid_layout.addWidget(empty, 0, 0)
            return

        for index, game in enumerate(games):
            card = self._build_card(game)
            self.grid_layout.addWidget(card, index // GRID_COLS, index % GRID_COLS)

    def _build_card(self, game):
        card = Card(hoverable=True, matte=True)
        card.setFixedWidth(CARD_WIDTH)
        card.setToolTip(game["name"])
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.setContentsMargins(8, 10, 8, 10)

        icon = QLabel()
        icon.setFixedSize(*CARD_ICON_SIZE)
        icon.setPixmap(images.thumbnail_or_avatar(game.get("icon"), game["name"], CARD_ICON_SIZE))
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        name = QLabel(game["name"], objectName="CardTitle")
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)

        card.clicked.connect(lambda g=game: self._launch(g))
        card.rightClicked.connect(lambda event, g=game: self._show_context_menu(event, g))
        return card

    def _show_context_menu(self, event, game):
        menu = QMenu(self)
        menu.addAction("Launch", lambda: self._launch(game))
        menu.addAction("Edit", lambda: self._edit(game))
        if self.sort_box.currentText() == "Custom Order":
            menu.addAction("Move Up", lambda: self._move(game, -1))
            menu.addAction("Move Down", lambda: self._move(game, 1))
        menu.addAction("Delete", lambda: self._remove(game))
        menu.exec(event.globalPosition().toPoint())

    # ------------------------------------------------------------------
    def _add_game(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select game executables or shortcuts", "", FILE_FILTER)
        if not paths:
            return
        added_at = storage.now_iso()
        new_games = [{
            "id": str(uuid.uuid4()), "name": Path(path).stem, "path": path,
            "icon": _extract_and_cache_icon(path), "added_at": added_at, "last_played": None,
        } for path in paths]
        self._mutate(lambda games: games.extend(new_games))

    def _import_from_launchers(self):
        dirs = app_settings.get_launcher_dirs()
        if not any(dirs.values()):
            QMessageBox.information(
                self, "Import from Launchers",
                "Add at least one launcher's install directory in Settings > Games first.")
            return
        show_toast(self, "Scanning...")
        threading.Thread(target=self._scan_worker, args=(dirs,), daemon=True).start()

    def _scan_worker(self, dirs):
        try:
            found = launchers.scan_all(dirs)
        except Exception:
            found = []
        self._scan_signals.done.emit(found)

    def _on_scan_done(self, found):
        added = launchers.import_scanned_games(found)
        if added:
            self.games = storage.load(DATA_FILE, [])
            self._refresh_grid()
        QMessageBox.information(
            self, "Import from Launchers",
            f"Added {added} new game(s)." if added else "No new games found.")

    def _launch(self, game):
        try:
            subprocess.Popen([game["path"]], shell=True, cwd=str(Path(game["path"]).parent))
        except OSError as exc:
            QMessageBox.critical(self, "Games", f"Couldn't launch this game:\n{exc}")
            return
        game["last_played"] = storage.now_iso()
        storage.update_entry(DATA_FILE, game.get("id"), {"last_played": game["last_played"]})
        if self.sort_box.currentText() == "Last Played":
            self._refresh_grid()

    def _edit(self, game):
        EditGameForm(self, game, on_save=lambda: self._on_edit_save(game))

    def _on_edit_save(self, game):
        storage.update_entry(DATA_FILE, game.get("id"), {
            "name": game["name"], "path": game["path"], "icon": game.get("icon"),
        })
        self._refresh_grid()

    def _move(self, game, delta):
        if self.sort_box.currentText() != "Custom Order":
            QMessageBox.information(self, "Games", "Switch Sort to 'Custom Order' to reorder manually.")
            return

        def apply_change(games):
            idx = self._index_of(games, game)
            new_idx = idx + delta if idx is not None else None
            if new_idx is not None and 0 <= new_idx < len(games):
                games[idx], games[new_idx] = games[new_idx], games[idx]

        self._mutate(apply_change)

    def _remove(self, game):
        if QMessageBox.question(self, "Remove Game", f"Remove '{game['name']}' from the list?") != QMessageBox.StandardButton.Yes:
            return

        def apply_change(games):
            idx = self._index_of(games, game)
            if idx is not None:
                games.pop(idx)

        self._mutate(apply_change)


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
        save_btn.setDefault(True)
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
