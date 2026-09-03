"""Games page: a small local launcher. Add a game's executable/shortcut
once (its icon is picked up automatically), then launch it with one click.
Shown as a poster-style grid, same as Anime/Reading/Series - right-click a
card for Edit/Delete, sort by name/date/last played, or reorder by
dragging a card onto the slot you want it in (which switches the sort to
Custom Order as the drag begins).
"""

import copy
import os
import threading
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPushButton, QVBoxLayout, QWidget,
)

from helpers import (app_settings, game_art, game_launch, images, launchers,
                     lookup_pool, media_grid, storage, theme)
from helpers.widgets import (
    Card, CardDragReorder, GlassPage, GridSelection, confirm,
    defer_grid_rebuild, finish_toast, frameless_dialog, inform, scroll_area,
    search_field, show_toast, show_undo_toast, smooth_combo,
    use_hover_cursor,
)
from windows.link_grid import (
    CARD_MARGINS, POSTER_ART_SIZE, POSTER_CARD_WIDTH, CardTextLabel,
    poster_grid_columns,
)

# **Development A/B switch, 27 August 2026 - not a shipping feature.**
# ATOMIC_VIRTUAL_GRID=1 swaps this page's QGridLayout-of-Card-widgets for
# the virtualized model/view grid in helpers/media_grid, on the same data,
# in the same panel, so the two can be compared on one machine without a
# rebuild. Unset (the default) is the untouched widget grid, byte for byte.
# See helpers/media_grid's docstring for what the comparison measured.
VIRTUAL_GRID = os.environ.get("ATOMIC_VIRTUAL_GRID") == "1"

DATA_FILE = "games.json"
# Poster tiles now, the Movies & Series card's own size (the owner's
# ask) - the art comes from Steam's store (helpers/game_art), and a
# game with none keeps the letter avatar: ImageOps.fit would stretch a
# 32px shell icon across a 160x216 tile as mush.
CARD_COVER_SIZE = POSTER_ART_SIZE
FILE_FILTER = "Games (*.exe *.lnk *.url);;All files (*.*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*.*)"
SORT_OPTIONS = ["Custom Order", "Name (A-Z)", "Date Added (Newest)", "Last Played"]

# Shared with Settings' per-launcher auto-import - one place doing icon
# extraction/caching for games, regardless of which UI triggered it.
_extract_and_cache_icon = launchers.extract_and_cache_icon


class _ScanSignals(QObject):
    done = Signal(list)  # [{"name", "path", "launcher"}, ...] found by the background scan


class _CoverSignals(QObject):
    # game id -> local Steam cover path, back from the lookup pool so
    # the storage write and the repaint stay on the UI thread.
    ready = Signal(str, str)


class GamesPage(GridSelection, GlassPage):
    # What the selection bar and its messages call these - see
    # widgets.GridSelection, which Apps/Websites share.
    SELECTION_NOUN = ("game", "games")

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app

        self.games = storage.load(DATA_FILE, [])
        self._migrate_and_backfill()
        self._init_selection()

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
        self.sort_box = smooth_combo(QComboBox())
        use_hover_cursor(self.sort_box)
        self.sort_box.addItems(SORT_OPTIONS)
        self.sort_box.currentTextChanged.connect(self._refresh_grid)
        top_row.addWidget(self.sort_box)
        # No drag hint here any more: it named a right-click Move Up/Down
        # that no longer exists, and dragging is how every page reorders.
        top_row.addStretch()
        # Debounced rather than filtering on every keystroke: each redraw
        # rebuilds every card from scratch (pages hold no state - see
        # .claude/rules/ui.md), so typing six characters would otherwise
        # rebuild the whole grid six times. Kept now that the field lives
        # in the window's bar - `refresh_filter` starts it.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._refresh_grid)
        top_row.addWidget(self._build_select_button(
            "Pick several games and delete them at once"))
        layout.addLayout(top_row)

        layout.addWidget(self._build_selection_bar())

        self.grid_body = QWidget()
        self.grid_layout = QGridLayout(self.grid_body)
        self.grid_layout.setSpacing(14)
        # **Centred, not left-hugging - the same thing poster_grid does.**
        # The owner's ask, 28 August 2026: "make the games and apps and
        # webs grid in the mid like the movies". The column count here is
        # a fixed 8 or 9 (link_grid.poster_grid_columns), so on a wide
        # window the row is narrower than the area it sits in and every
        # pixel of the difference used to land on the right, which reads
        # as the page leaning left. poster_grid._left_margin solves the
        # same problem by halving the slack; a QGridLayout does it with
        # the alignment, and the last partial row still fills from the
        # left inside the centred block exactly as the poster grids' does.
        self.grid_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._virtual = None
        if VIRTUAL_GRID:
            # The grid_body/grid_layout above are still built and still
            # own the drag-reorder helper - they are simply not shown.
            # Keeping them means every other method on this page
            # (_refresh_grid's teardown, the selection map, the cover
            # labels) stays valid without a second code path.
            self._virtual = media_grid.VirtualMediaGrid(
                CARD_COVER_SIZE, POSTER_CARD_WIDTH, ground=theme.PANEL_FILL)
            self._virtual.setModel(media_grid.MediaGridModel(
                media_grid.MediaFields(id="id", title="name", cover="cover")))
            self._virtual.card_clicked.connect(self._on_virtual_clicked)
            self._virtual.card_right_clicked.connect(self._on_virtual_menu)
            self._virtual.needs_cover.connect(self._on_virtual_needs_cover)
            layout.addWidget(self._virtual, stretch=1)
        else:
            layout.addWidget(scroll_area(self.grid_body, ground=theme.PANEL_FILL),
                             stretch=1)

        self._drag_reorder = CardDragReorder(
            self.grid_body, self._begin_custom_order, self._drop_reorder)

        # game id -> the drawn card's cover label, so an arriving Steam
        # cover swaps one pixmap instead of rebuilding the grid under
        # the pointer. Nothing in it outlives the redraw that filled it.
        self._cover_labels = {}
        self._cover_signals = _CoverSignals()
        self._cover_signals.ready.connect(self._on_cover_ready)

        self._refresh_grid()
        self._backfill_covers()

    # ------------------------------------------------------------------
    def _backfill_covers(self):
        """Fetch a Steam poster for every game that has none - the
        poster tiles need portrait art no .exe icon can supply, and
        helpers/game_art is the measured, matched source for it. One
        lookup per game on the shared bounded pool; game_art caches both
        hits and authoritative misses on disk, so later loads cost a
        stat, not a request."""
        for game in self.games:
            # Resolved by name too, not only as written: the owner's
            # covers were imported from a source run and name the source
            # tree's cache (helpers/art_paths).
            if images.resolve_art_path(game.get("cover")):
                continue
            lookup_pool.submit(self._cover_worker, game.get("id"),
                               game.get("name") or "", game.get("path"))

    def _cover_worker(self, game_id, name, install_path):
        # Never raises - an exception here kills the pool worker thread.
        try:
            path = game_art.fetch_cover(name, install_path=install_path)
        except Exception:
            path = None
        if path and game_id:
            self._cover_signals.ready.emit(game_id, str(path))

    def _on_cover_ready(self, game_id, path):
        game = next((g for g in self.games if g.get("id") == game_id), None)
        if game is None:
            return
        game["cover"] = path
        # One field on one entry - Home and Settings hold their own
        # copies of this file (see _mutate for the defect a whole-list
        # write caused).
        storage.update_entry(DATA_FILE, game_id, {"cover": path})
        drawn = self._cover_labels.get(game_id)
        if drawn is not None:
            try:
                drawn.setPixmap(images.thumbnail_or_avatar(
                    path, game.get("name") or "", CARD_COVER_SIZE))
            except RuntimeError:
                pass    # the grid rebuilt; the new card already asked

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
        # Games imported before launch commands existed still hold only a
        # path; this gives them their launcher's own way in without the
        # user re-importing a library they already have.
        self.games = launchers.backfill_launch_commands(self.games)

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
        # **A game added now gets its cover now**, 28 August 2026 (the
        # owner: "make the image of the games appear immediately when
        # its added, not when I change the page then go back"). The
        # backfill used to run only from __init__, so a game added to a
        # page already open had nothing looking for its poster until
        # that page was rebuilt - leaving the .exe icon on the tile and
        # making a page change look like the thing that fetched it.
        # Every game already holding a cover file is skipped in a stat,
        # so re-running this per mutation costs one loop over ten
        # entries, not ten lookups.
        self._backfill_covers()

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
        """What the one search field in the window's title bar currently
        says, lowercased.

        It used to be this page's own box. There is no page box any more
        (the owner's ask, 25 August 2026: one bar that searches
        everything, and remove the others), so the answer comes from the
        window - `main.MainWindow.page_filter_text`. The seam is
        deliberately this method and nothing else: every grid on the page
        already funnelled through it, so the field moving out of the page
        changed one line rather than every caller."""
        window = self.window()
        getter = getattr(window, "page_filter_text", None)
        if not callable(getter):
            return ""
        return getter()

    def refresh_filter(self):
        """Redraw against the field's current text. Called by the window
        as it is typed into.

        Debounced through the same timer the page's own box used, and
        for the same measured reason: a redraw rebuilds every card from
        scratch, so six characters would otherwise rebuild the grid six
        times."""
        timer = getattr(self, "_search_timer", None)
        if timer is not None:
            timer.start()

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
        if self._virtual is not None:
            games = self._visible_games()
            self._prune_selection({g.get("id") for g in games})
            # set_items is the one legitimate model reset - a new search
            # or a new sort really is a different list. An arriving cover
            # goes through set_pixmap instead and touches one row.
            self._virtual.set_items(games)
            return
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # hide() first - the same trap link_grid, downloads_page
                # and the tracker's own grid each record: a deleteLater'd
                # widget is still a *visible* child at its old geometry
                # until the event loop gets to it, so the outgoing tiles
                # paint over the incoming ones. Invisible here, where two
                # copies of the same poster overlay each other; on the
                # tracker, where a bordered button sits in the row, the
                # label was drawn twice.
                widget.hide()
                widget.deleteLater()
        self._clear_selection_cards()
        # Emptied with the cards it names, same reason as the selection
        # map above.
        self._cover_labels = {}

        # Dragging is off while a search is narrowing the grid: a drop
        # writes the order that is on screen (see _begin_custom_order),
        # and that isn't the whole order while cards are hidden.
        narrowed = bool(self._search_query())

        games = self._visible_games()
        # Before the early return below as well as the draw: a search
        # that matches nothing must still drop the selection it hid.
        self._prune_selection({g.get("id") for g in games})
        if not games:
            message = (f"Nothing here matches '{self._search_query()}'."
                       if narrowed else "No games yet - click '+' to add one.")
            self.grid_layout.addWidget(QLabel(message, objectName="Muted"), 0, 0)
            return

        columns = poster_grid_columns(self)
        for index, game in enumerate(games):
            # Dragging is off while selecting as well as while the grid is
            # narrowed: both a drag and a pick want the same left press.
            card = self._build_card(game, draggable=not narrowed and not self._select_mode)
            self.grid_layout.addWidget(card, index // columns, index % columns)

    def relayout_for_sidebar(self):
        """See LinkGridPage.relayout_for_sidebar - same grid, same reason."""
        self._refresh_grid()

    def _build_card(self, game, draggable=True):
        # No matte any more: a plain #Card is the frameless tile now
        # (theme.py) - icon and name floating on the ground, box only on
        # hover - the same Harbor language the poster grids and Home use.
        card = Card(hoverable=True)
        card.setFixedWidth(POSTER_CARD_WIDTH)
        card.setToolTip(game["name"])
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.setContentsMargins(*CARD_MARGINS)

        # The Steam poster when one has been resolved, the letter avatar
        # until (or unless) one is - not the extracted icon, which at
        # poster size renders as a 32px shell icon stretched to mush.
        cover = QLabel()
        cover.setFixedSize(*CARD_COVER_SIZE)
        cover.setPixmap(images.thumbnail_or_avatar(
            images.resolve_art_path(game.get("cover")), game["name"],
            CARD_COVER_SIZE))
        layout.addWidget(cover, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._cover_labels[game.get("id")] = cover

        name = CardTextLabel(game["name"], width=POSTER_CARD_WIDTH
                             - CARD_MARGINS[0] - CARD_MARGINS[2])
        name.setObjectName("CardTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)

        if self._select_mode:
            # After the layout's own widgets, so the mark stacks above
            # them rather than under the icon.
            self._attach_selection_badge(card, game.get("id"))
            card.clicked.connect(lambda g_id=game.get("id"): self._toggle_selected(g_id))
        else:
            card.clicked.connect(lambda g=game: self._launch(g))
        card.rightClicked.connect(lambda event, g=game: self._show_context_menu(event, g))
        if draggable:
            self._drag_reorder.attach(card, game.get("id"))
        return card

    # -- virtual-grid callbacks (development A/B, see VIRTUAL_GRID) --------
    def _on_virtual_clicked(self, game):
        if self._select_mode:
            self._toggle_selected(game.get("id"))
        else:
            self._launch(game)

    def _on_virtual_menu(self, game, global_pos):
        """The delegate hands back a screen position, not a QMouseEvent.

        _show_context_menu only ever wanted `event.globalPosition()`, so a
        shim with that one method keeps the existing menu code unchanged
        rather than forking it - the business logic must not learn which
        renderer it is being called from."""
        class _Where:
            def __init__(self, point):
                self._point = point

            def globalPosition(self):
                return self

            def toPoint(self):
                return self._point

        self._show_context_menu(_Where(global_pos), game)

    def _on_virtual_needs_cover(self, row, game):
        """A visible card has no poster yet.

        Deferred to an idle turn by the view (COVER_ASK_PER_TURN) and
        answered here on the UI thread, because a QPixmap cannot be
        constructed off it - the pool below is used by the widget grid
        only to *find* a cover file, never to build the pixmap.

        Still a clear improvement on the widget grid, which calls the
        same function synchronously for **every** entry while building
        the page, on screen or not: at 1000 games that is 1000 decodes
        inside `_refresh_grid`, and it is most of the 465ms that path
        measured. Here it is one decode per card that is actually looked
        at, off the paint path.
        """
        path = images.resolve_art_path(game.get("cover"))
        if not path or self._virtual is None:
            return
        pixmap = images.thumbnail_or_avatar(
            path, game.get("name") or "", CARD_COVER_SIZE)
        # By id, not by row: a re-sort while this was queued would leave
        # the row number naming a different game, and the poster would be
        # painted onto the wrong card.
        self._virtual.set_pixmap_for_id(game.get("id"), pixmap)

    def _show_context_menu(self, event, game):
        menu = QMenu(self)
        menu.addAction("Launch", lambda: self._launch(game))
        menu.addAction("Edit", lambda: self._edit(game))
        # The second way into selection mode, from the card the user is
        # already pointing at - the toolbar button alone means noticing a
        # word at the far end of the row before knowing to look for it.
        # Same entry point the tracker pages carry.
        if not self._select_mode:
            menu.addAction("Select", lambda: self._set_select_mode(True, game.get("id")))
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
            inform(self, "Import from Launchers",
                   "Add at least one launcher's install directory in "
                   "Settings > Games first.")
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
        # Refresh is both directions, 28 August 2026 ("make the games
        # when I refresh removes the uninstalled games"): what the
        # launchers have gained is added, and what is no longer on disk
        # stops taking up a tile that cannot be launched. Pruning runs
        # after the import so a game that has merely *moved* is added
        # back at its new path in the same pass, rather than
        # disappearing until the next refresh.
        added = launchers.import_scanned_games(found)
        removed = launchers.prune_uninstalled_games()
        if added or removed:
            self.games = storage.load(DATA_FILE, [])
            self._refresh_grid()
            # Imported games arrive with a .exe icon and no poster; ask
            # for one straight away rather than on the next page build.
            self._backfill_covers()
        toast, self._scan_toast = self._scan_toast, None
        message = launchers.import_result_message(added)
        pruned = launchers.prune_result_message(removed)
        if pruned:
            # "No New Games Found" plus a removal reads as a contradiction;
            # when something was removed that is the news.
            message = f"{message}, {pruned}" if added else pruned
        finish_toast(toast, self, message)

    def _launch(self, game):
        try:
            game_launch.run(game)
        except OSError as exc:
            inform(self, "Games", f"Couldn't launch this game:\n{exc}")
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
            "launch": game.get("launch"), "launcher": game.get("launcher"),
        })
        self._refresh_grid()


    def _remove(self, game):
        if not confirm(self, "Remove Game",
                       f"Remove '{game['name']}' from the list?"):
            return

        # Copied whole before the removal - _mutate re-reads the file into
        # fresh dicts and the card holding this one is torn down, so this
        # is the only surviving copy of the record undo has to restore
        # (icon path and added_at included, not just the name and path).
        removed = copy.deepcopy(game)
        removed_at = []

        def apply_change(games):
            idx = self._index_of(games, game)
            if idx is not None:
                games.pop(idx)
                # Its place in the saved list, so undo under Custom Order
                # puts it back where it was rather than at the end.
                removed_at.append(idx)

        self._mutate(apply_change)
        show_undo_toast(self, f"Removed '{removed['name']}' - Click to Undo",
                        lambda: self._restore(removed, removed_at),
                        on_redo=lambda: self._delete_ids([removed.get("id")]))

    def _restore(self, game, removed_at):
        def apply_change(games):
            # Clamped: the file is re-read here, and Settings' launcher
            # import (or Clear Data) can have changed its length since.
            index = removed_at[0] if removed_at else len(games)
            games.insert(min(index, len(games)), game)

        self._mutate(apply_change)
        return f"Restored '{game['name']}'"


class EditGameForm(QDialog):
    def __init__(self, parent, game, on_save):
        super().__init__(parent)
        self.game = game
        self.on_save = on_save
        self.icon_path = game.get("icon")

        self.setWindowTitle("Edit Game")
        # 410 tall, up from the framed 380: the panel carries its own
        # heading now, where the native title bar used to.
        self.setFixedSize(420, 410)

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

        frameless_dialog(self, title=self.windowTitle())
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
            inform(self, "Games", "Couldn't detect an icon for this path.")

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
            inform(self, "Games", "Name and path can't be empty.")
            return
        if Path(path) != Path(self.game.get("path") or ""):
            # Pointing the entry somewhere else has to drop the command
            # resolved for where it used to be, or Save would look like
            # it worked while every launch still started the old game.
            self.game["launch"] = None
            self.game["launcher"] = None
        self.game.update(name=name, path=path, icon=self.icon_path)
        self.on_save()
        self.accept()
