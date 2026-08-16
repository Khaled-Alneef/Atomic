"""Games page: a small local launcher. Add a game's executable/shortcut
once (its icon is picked up automatically), then launch it with one click.
Shown as a poster-style grid, same as Anime/Reading/Series - right-click a
card for Edit/Delete, sort by name/date/last played, or reorder by
dragging a card onto the slot you want it in (which switches the sort to
Custom Order as the drag begins).
"""

import subprocess
import threading
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from helpers import app_settings, child_process, images, launchers, storage, theme
from helpers.widgets import (
    Card, CardDragReorder, GlassPage, defer_grid_rebuild, finish_toast,
    scroll_area, show_toast,
)
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
        # The "Scanning..." toast waiting to be told what was found, while
        # an import is in progress.
        self._scan_toast = None

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
        # No drag hint here any more: it named a right-click Move Up/Down
        # that no longer exists, and dragging is how every page reorders.
        top_row.addStretch()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search games...")
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
        layout.addLayout(top_row)

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(14)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(scroll_area(self.grid_body), stretch=1)

        self._drag_reorder = CardDragReorder(
            self.grid_body, self._begin_custom_order, self._drop_reorder)

        self._refresh_grid()

    # ------------------------------------------------------------------
    def _begin_custom_order(self):
        """A drag has started. Whatever the Sort box said, this page is
        in Custom Order from here on - any other mode is re-applied on the
        next redraw and would drop the card back where it started.

        The order already on screen is written out as the custom one
        first, so nothing moves at the moment of the switch: only the
        drag that follows changes the order. The dropdown is then set
        with its signal blocked, since the grid would only be rebuilt
        into the arrangement it is already showing - and rebuilding it
        here would delete the card currently being dragged."""
        if self.sort_box.currentText() == "Custom Order":
            return
        order = [g.get("id") for g in self._sorted_games()]
        storage.apply_custom_order(DATA_FILE, order)
        # This page's own copy follows, in place: the cards built from it
        # hold references to these very dicts, so it is reordered rather
        # than reloaded.
        storage.order_by_ids(self.games, order)
        self.sort_box.blockSignals(True)
        self.sort_box.setCurrentText("Custom Order")
        self.sort_box.blockSignals(False)

    def _drop_reorder(self, moved_id, target_id):
        if not storage.move_entry(DATA_FILE, moved_id, target_id):
            return
        storage.move_in_list(self.games, moved_id, target_id)
        defer_grid_rebuild(self._refresh_grid)

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

    def _search_query(self) -> str:
        # getattr because _refresh_grid can run before the box exists on a
        # page still being built.
        box = getattr(self, "search_box", None)
        return box.text().strip().lower() if box else ""

    def _visible_games(self):
        """What the grid draws: the sorted list narrowed by the search box.

        Deliberately not folded into _sorted_games - that one is what
        _begin_custom_order writes out as the custom order, and it has to
        stay the whole list even if a query were somehow active."""
        query = self._search_query()
        games = self._sorted_games()
        if not query:
            return games
        # Plain case-insensitive substring, not a fuzzy match: same
        # reasoning as the tracker pages - the user is looking for a name
        # they know is there, and near-misses quietly included make a
        # short query look like it failed to filter at all.
        return [g for g in games if query in (g.get("name") or "").lower()]

    # ------------------------------------------------------------------
    def _refresh_grid(self, *_args):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        # Dragging is off while a search is narrowing the grid: a drop
        # writes the order that is on screen (see _begin_custom_order),
        # and that isn't the whole order while cards are hidden.
        narrowed = bool(self._search_query())

        games = self._visible_games()
        if not games:
            message = (f"Nothing here matches '{self.search_box.text().strip()}'."
                       if narrowed else "No games yet - click '+' to add one.")
            self.grid_layout.addWidget(QLabel(message, objectName="Muted"), 0, 0)
            return

        for index, game in enumerate(games):
            card = self._build_card(game, draggable=not narrowed)
            self.grid_layout.addWidget(card, index // GRID_COLS, index % GRID_COLS)

    def _build_card(self, game, draggable=True):
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
        if draggable:
            self._drag_reorder.attach(card, game.get("id"))
        return card

    def _show_context_menu(self, event, game):
        menu = QMenu(self)
        menu.addAction("Launch", lambda: self._launch(game))
        menu.addAction("Edit", lambda: self._edit(game))
        # No Move Up/Move Down: dragging a card reorders it on every page,
        # and these only appeared under one sort mode anyway.
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
        if self._scan_toast is not None:
            return  # a scan is already running - let it finish
        # Walking several launcher directories takes a moment, and it all
        # happens on a background thread, so the button needs to say it is
        # working; the same toast is then handed the result (see
        # _on_scan_done) rather than interrupting with a dialog to
        # dismiss for something that needs no decision.
        self._scan_toast = show_toast(self, "Scanning...", duration_ms=None)
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
        toast, self._scan_toast = self._scan_toast, None
        finish_toast(toast, self, launchers.import_result_message(added))

    def _launch(self, game):
        try:
            subprocess.Popen([game["path"]], shell=True, cwd=str(Path(game["path"]).parent),
                             env=child_process.clean_env(),
                             creationflags=child_process.flags())
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
