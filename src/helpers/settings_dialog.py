"""Settings window: a category sidebar (mirroring the main app window's
own sidebar) on the left, the selected category's controls on the right.

General: Windows-startup toggle (plus whether that sign-in launch opens
full screen), and which sections show up in the main
sidebar (Anime, Reading, Series, Games, Apps, Websites can each be hidden
without losing their saved data). Watching: the resolution the in-app
player starts on (anime, films and series play inside the app now, so
there is no per-entry "where does this open" list here any more, and the
Stremio account sign-in is gone at the owner's ask - see
_build_anime_page).
Reading: the list of manga/manhwa/manhua reading sites the
Reading page can search and open to, plus an optional music/ambience URL.
Games: each game launcher's install directory, so the Games page can
bulk-import every game it finds there (see helpers.launchers) instead of
adding each one by hand. Data: take a zip backup of everything saved and
restore one again, wipe one content category's saved entries at a time,
or uninstall the app entirely (every saved file plus the app itself).
"""

import json
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import (
    anime_sites, app_settings, global_search, launchers, logs, lookup_pool,
    manga_sites, nav_config, startup, storage, theme, uninstall,
    updater,
)
from .widgets import (confirm, finish_toast, frameless_dialog, inform,
                      scroll_area, show_toast)

# "Watching", not "Anime & Series": that name predates films being tracked,
# and the page's settings serve all three media. The
# literal "Anime, Movies & Series" was measured against this
# sidebar and does not fit - it needs 192px of the list's 182px and elides
# to "Anime, Movies & Seri..." (Segoe UI 10.5 at 125% scaling; "Anime,
# Films & Series" clears it by 3px, which is no margin at all on a
# different font or scale factor). "Watching" needs 104px, cannot read as
# excluding films, and pairs with "Reading" one row below - the two things
# the tracker does.
CATEGORIES = ["General", "Preferences", "Watching", "Reading", "Games",
              "API Keys", "Data", "Keybinds", "Uninstall"]

# Uninstall is a category rather than a section at the bottom of Data,
# and it is the one row drawn in the danger colour: it deletes every
# saved file and removes the app, and it should not sit one careless
# scroll below the buttons that wipe a single category.
DANGER_CATEGORY = "Uninstall"

# Width of the key-combination column under Keybinds, so every
# description starts at the same x rather than stepping with the
# combination's length. Measured, not guessed: with each key in its own
# frame the longest ("Ctrl+1-9") wants 111px against the 92 this was
# when the keys were one plain string, and a fixed width narrower than
# its content clips rather than wraps. 124 leaves margin for a wider
# font or scale factor.
KEYBIND_COLUMN_WIDTH = 124

# The API-key page's two fixed columns, so every field starts and ends at
# the same x rather than stepping with the service's name. Sized off the
# longest label in app_settings.API_KEYS ("TMDB (The Movie Database)")
# and off "Not set", both with room for a wider font or scale factor.
API_KEY_LABEL_WIDTH = 190
API_KEY_STATE_WIDTH = 56

# Slack added to the measured height of the category rows: the selected
# row's QSS border draws at the very edge of its box, and without a few
# pixels spare the list clips its own bottom border off - the same margin
# main.NavListWidget keeps, for the same reason.
CATEGORY_LIST_PADDING = 12

# How each stored resolution reads in the Watching page's dropdown. Same
# order as app_settings.RESOLUTION_CHOICES, which is the order the player
# ranks in - highest first, "best" last because it is not a resolution
# but an instruction. "2160p" is spelled out as 4K as well: the sources
# are labelled 2160p and the person choosing thinks in 4K.
RESOLUTION_LABELS = {
    "2160p": "4K (2160p)",
    "1080p": "1080p (recommended)",
    "720p": "720p",
    "480p": "480p",
    "best": "Best available",
}

# (display name, data file, predicate). A predicate of None means "clear
# the whole file" (Series/Games/Apps/Websites each hold only their own
# entries) - Anime and Reading share tracker.json, so those two instead
# filter out just the matching type(s) and keep the rest. Mirrors
# windows.tracker.MANGA_TYPES, duplicated here rather than imported to
# avoid a helpers -> windows dependency for one 3-value tuple.
# **Rebuilt 21 August 2026 to match where the app actually keeps things.**
# It had drifted, and silently: "Anime" cleared `tracker.json` for
# entries typed Anime, but anime moved into `series.json` when the two
# watch pages merged (main._merge_anime_into_series) - so ticking Anime
# cleared *nothing at all* (measured: 0 entries), while ticking "Series"
# wiped every anime with it (measured: all 6). A destructive control
# that does nothing, beside one that does more than it says, is the
# worst pair of the two.
#
# So the three watch kinds are named separately and keyed off the type
# they are really stored under, reading is the whole of tracker.json
# (which holds nothing else since the merge), and the three files the
# app has grown since - history, downloads and the catalogue cache - are
# offered instead of being unclearable.
# **"Saved" leads every entry category** - the owner's ask, 23 August
# 2026. "Anime" beside "Watch & Read History" reads as though it clears
# anime *itself*; what it actually clears is the anime he has saved. The
# three rows below the entry categories keep their own names, because
# they are not saved entries and prefixing them would be a lie.
CLEAR_CATEGORIES = [
    ("Saved Anime", "series.json", lambda e: e.get("type") == "Anime"),
    ("Saved Series", "series.json", lambda e: e.get("type") == "Series"),
    ("Saved Movies", "series.json", lambda e: e.get("type") == "Movie"),
    ("Saved Reading", "tracker.json", None),
    ("Saved Games", "games.json", None),
    ("Saved Apps", "apps.json", None),
    ("Saved Websites", "websites.json", None),
    # Not entries, but the three other things this app accumulates and
    # that someone clearing data plainly means to include.
    ("Watch & Read History", "history.json", None),
    ("Download Queue", "downloads.json", None),
    ("Cached Discover Results", "discover_cache.json", None),
]


# What a backup is made of: the live JSON files at the top of DATA_DIR.
# Deliberately not the whole folder - image_cache/ measured 19MB of
# re-downloadable covers on the owner's own install, atomic.log is a
# diagnostic rather than data, and ui_assets/checkmark.png is written by
# theme.py at startup. storage's own .bak/.tmp/.corrupt copies are left
# out as well: a backup is a snapshot of the data, not of the recovery
# copies sitting behind it. This glob already excludes all of them - a
# "tracker.json.bak" ends in .bak, not .json.
_BACKUP_GLOB = "*.json"

# Nothing Atomic saves is remotely near this (settings.json is under 1KB,
# the largest - tracker.json - about 10KB). It's here so a zip claiming a
# gigabyte-sized member is refused *before* being read into memory rather
# than after, since the file being restored is one the user picked off
# disk and nothing guarantees this app wrote it.
_MAX_BACKUP_MEMBER_BYTES = 32 * 1024 * 1024


class _BackupError(Exception):
    """A backup that must not be restored, carrying the reason in words
    the owner can act on. Every message says nothing was changed, because
    that is the point of raising instead of writing: this is only ever
    raised before the first byte is written."""


def _read_backup(path: Path) -> dict:
    """Every data file in the archive, parsed, keyed by filename - or
    _BackupError naming what is wrong with it.

    The whole archive is read and parsed here, before the caller writes
    anything at all. A restore that validated file-by-file as it went
    would half-overwrite real data on a truncated zip and leave the rest
    behind, which is the pre-1.4 data-loss incident with a wider blast
    radius: an unreadable file read back as "empty", and the next save
    made that true. A backup is either wholly restorable or refused.

    Members are matched the way `load` reads a saved file - utf-8-sig,
    since a BOM is invisible in an editor and is exactly what cost a
    stored setting once - and a member name has to be a plain filename:
    a zip is free to carry "..\\..\\anything.json", and joining that onto
    DATA_DIR would write outside it."""
    try:
        with zipfile.ZipFile(path) as archive:
            # Checks every member's CRC, which is what catches a zip that
            # opened cleanly but was truncated or corrupted mid-member.
            damaged = archive.testzip()
            if damaged is not None:
                raise _BackupError(
                    f"This backup is damaged - {damaged} did not read back "
                    f"correctly. Nothing was changed.")

            members = [info for info in archive.infolist()
                       if not info.is_dir()
                       and info.filename.lower().endswith(".json")
                       and info.filename == Path(info.filename).name]
            if not members:
                raise _BackupError(
                    "There is no Atomic data in this zip. Pick a backup made "
                    "with Back Up Data. Nothing was changed.")

            restored = {}
            for info in members:
                if info.file_size > _MAX_BACKUP_MEMBER_BYTES:
                    raise _BackupError(
                        f"{info.filename} in this backup is far too large to "
                        f"be Atomic data. Nothing was changed.")
                try:
                    data = json.loads(archive.read(info).decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    raise _BackupError(
                        f"{info.filename} in this backup is unreadable, so the "
                        f"backup can't be trusted. Nothing was changed.")
                if not isinstance(data, (list, dict)):
                    # Every file this app writes is a list of entries or a
                    # settings object. A bare number restored into
                    # tracker.json would parse and then break every page
                    # that reads it.
                    raise _BackupError(
                        f"{info.filename} in this backup isn't in Atomic's "
                        f"format. Nothing was changed.")
                restored[info.filename] = data
            return restored
    except _BackupError:
        raise
    except Exception:
        # Deliberately everything, not (BadZipFile, OSError): a zip whose
        # central directory is intact but whose compressed bytes are
        # truncated fails inside zlib, and `zlib.error` is neither of
        # those. Measured - a 60%-truncated copy of a real backup raised
        # zlib.error "invalid distances set" straight out of testzip(),
        # and an exception escaping a Qt slot takes the process with it
        # (pre-1.4 item #5). Anything this file can throw means the same
        # thing to the user either way: don't restore from it.
        logs.exception(f"Could not read the backup at {path}")
        raise _BackupError(
            "This file isn't a backup Atomic can read - it may be damaged or "
            "only partly downloaded. Nothing was changed.")


class _SiteProbeSignals(QObject):
    done = Signal(str, str)  # which list ("reading"/"video"), site name


# A verdict per row, in the owner's own words - they wrote these after
# reading the previous two attempts. The first described the resolver
# ("opens title pages", "search links only") and had to be explained; the
# second explained itself on every row, which said the same thing five
# times over. A row now carries the verdict alone and _VERDICT_LEGEND
# below says what each one means, once, under the buttons.
#
# engine/streaming and generic all land on the entry's own page, but
# generic gets there by reading the site's own search results, which is
# the first thing to break when a site is redesigned - hence "may break"
# rather than a fifth way of saying "works". Collapsing it into
# "Works perfectly" would hide the case that fails first.
_RESOLVES_LABELS = {
    "engine": "Works perfectly",
    "generic": "Works, but may break",
    "streaming": "Works perfectly",
    "search-only": "Works, but directed to search page",
    "unreachable": "Site not responding",
    "unknown": "Check failed",
    "checking": "checking...",
}

# The same verdicts spelled out once, under the buttons, rather than
# repeated on every row - a row says which verdict, this says what the
# verdict means. {kind} is the medium the list is for, so it reads as
# the thing being tracked there rather than as "content".
_VERDICT_LEGEND = (
    "<b>Works perfectly</b>: you'll be taken to the added {kind} page.<br>"
    "<b>Works, but may break</b>: same, but the page is found through the "
    "site's own search - it stops working when that changes.<br>"
    "<b>Works, but directed to search page</b>: you'll be taken to the "
    "site's {kind} search page and pick it yourself.<br>"
    "<b>Site not responding</b>: nothing answered - the site may be down.<br>"
    "<b>Check failed</b>: the check didn't finish - try Check again."
)

# Site ids whose verdict was actually measured during this run of the
# app. A verdict is a measurement of a remote site at one moment, not a
# property of the site, and nothing on the record says when it was taken
# - so a check run weeks ago read as current forever. The stored
# "resolves" field is still written (probe_site is unchanged) and still
# read; it is just not shown until this run measured it again. Module
# level rather than on SettingsDialog because the dialog is rebuilt on
# every open and the lifetime wanted is the process, not the dialog.
_CHECKED_THIS_RUN = set()

# How many of the user's own titles one Check asks a site for. Three:
# enough that a site missing one particular series still gets a fair
# hearing, few enough that a site answering nothing is still bounded
# (see probe_site's deadline).
_PROBE_TITLE_LIMIT = 3

# The Stremio account sign-in that used to live on the Watching page is
# gone entirely, at the owner's ask. A session saved before the removal
# keeps working - app_settings still holds and serves the auth key and
# the tracker still syncs with it - there is simply no UI here to add or
# replace one any more.


def _key_caps(keys: str) -> QWidget:
    """"Ctrl+K" as two framed keys with a plus between them.

    Split on "+" rather than parsed: every combination in SHORTCUTS is
    plus-separated and no key in it is itself a plus, so a parser would
    be code with nothing to decide. "Ctrl+," survives it - the comma is
    the second half - and "Ctrl+1-9" keeps its range on one cap, which
    is what it is: one key, any of nine.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    for index, key in enumerate(keys.split("+")):
        if index:
            row.addWidget(QLabel("+", objectName="KeyPlus"))
        row.addWidget(QLabel(key, objectName="KeyCap"))
    # Left-aligned in a fixed-width column: without this the caps spread
    # across the whole column and stop lining up with the row above.
    row.addStretch()
    return holder


def _verdict_legend(kind: str) -> QLabel:
    label = QLabel(_VERDICT_LEGEND.format(kind=kind), objectName="Muted")
    label.setWordWrap(True)
    return label


class _LauncherImportSignals(QObject):
    done = Signal(str, int)  # launcher key, number of games added


class _UpdateSignals(QObject):
    # The check and the download both run off the UI thread; these carry
    # their results back onto it.
    checked = Signal(object, str)      # update dict (or None), error message
    progress = Signal(int, int)        # bytes received, total
    downloaded = Signal(object, str)   # downloaded Path (or None), error message


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(920, 640)
        self.setMinimumSize(760, 560)

        outer = QVBoxLayout(self)
        # 1px, not 0: the dialog is a frameless rounded panel now and
        # paints its own 1px border at the window edge - a flush child
        # (the sidebar) would sit on top of that line.
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, stretch=1)

        body.addWidget(self._build_category_sidebar())

        content_col = QVBoxLayout()
        content_col.setContentsMargins(28, 24, 28, 20)
        content_col.setSpacing(14)
        content_col.addWidget(QLabel("Settings", objectName="PanelTitle"))

        self._site_probe_signals = _SiteProbeSignals()
        self._site_probe_signals.done.connect(self._on_site_probed)
        # Sites currently being checked, so the list can say so instead of
        # showing nothing for the ~10s a probe can take.
        self._probing_sites = set()

        self._launcher_import_signals = _LauncherImportSignals()
        self._launcher_import_signals.done.connect(self._on_launcher_import_done)
        # The "Scanning..." toast each in-progress launcher import is
        # waiting to report into, keyed by launcher.
        self._scan_toasts = {}

        # The update found by the last check, kept so the same button can
        # go on to install it without checking again.
        self._pending_update = None
        self._update_signals = _UpdateSignals()
        self._update_signals.checked.connect(self._on_update_checked)
        self._update_signals.progress.connect(self._on_update_progress)
        self._update_signals.downloaded.connect(self._on_update_downloaded)

        self.stack = QStackedWidget()
        # Same order as CATEGORIES - the stack is indexed by the
        # sidebar's row, so the two lists are one list in two places.
        #
        # **Built on first visit, not all nine up front** (22 August 2026,
        # the owner: "the settings btn takes ~1 sec to show the settings
        # window, make it < 200 ms"). Measured against the real main
        # window over the owner's own data, click to the dialog's first
        # paint: 355-376ms, and nothing in it was one slow call. Every
        # cost was simply *proportional to how many widgets existed*, so
        # the same 300ms was being paid nine times over for eight pages
        # nobody was looking at:
        #
        #     scroll_area x9                     60ms
        #     stack.addWidget x9                 54ms
        #     content_wrap.setLayout             47ms   (reparents the lot)
        #     frameless_dialog                   50ms   (setWindowFlag)
        #     the nine _build_* methods          40ms
        #
        # The last line is the only one that looks like page building;
        # the rest is Qt walking the tree those pages made - an empty
        # frameless QDialog of this size builds in **0.2ms**, which is
        # what says the cost is the tree and not the dialog.
        #
        # After: **44-57ms** click to first paint, three consecutive
        # opens, same harness. A category's first visit then costs
        # 9-59ms (worst: API Keys, 41 widgets), measured in both
        # directions through the list so a per-tab cost could be told
        # from a first-switch one; a second visit costs nothing.
        self._page_builders = [
            self._build_general_page,
            self._build_preferences_page,
            self._build_anime_page,
            self._build_reading_page,
            self._build_games_page,
            self._build_api_keys_page,
            self._build_data_page,
            self._build_keybinds_page,
            self._build_uninstall_page,
        ]
        self._built_pages = set()
        for _ in self._page_builders:
            # #Bare, so an unbuilt slot paints nothing: the app
            # stylesheet's plain QWidget rule is an opaque BG fill, and
            # these stand where a transparent QScrollArea used to.
            holder = QWidget(objectName="Bare")
            slot = QVBoxLayout(holder)
            slot.setContentsMargins(0, 0, 0, 0)
            self.stack.addWidget(holder)
        content_col.addWidget(self.stack, stretch=1)
        # Builds row 0 on the way through _on_category_changed, so the
        # page that is about to be on screen is the one page that exists.
        self.category_list.setCurrentRow(0)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        content_col.addLayout(btn_row)

        # Bare, or the app stylesheet's opaque QWidget fill paints this
        # wrapper's square corners over the frameless panel's rounded
        # right edge (measured: both right corners came back alpha 255).
        content_wrap = QWidget(objectName="Bare")
        content_wrap.setLayout(content_col)
        body.addWidget(content_wrap, stretch=1)

        # The sites list used to be filled here. It is filled by
        # _build_reading_page instead now - that page may not exist yet,
        # and a list nobody has built has nothing to fill.

        # No title heading: the content column already opens with its
        # own "Settings" PanelTitle.
        frameless_dialog(self)
        self.exec()

    # ------------------------------------------------------------------
    def _build_category_sidebar(self):
        sidebar = QWidget(objectName="Sidebar")
        sidebar.setFixedWidth(210)
        # The shared #Sidebar rule rounds only the right corners (in the
        # main window its left edge is the screen edge). Here the left
        # edge is the dialog's rounded corner, and the sidebar's square
        # corners would paint over the transparent rounding - so this
        # copy rounds its left corners too. Merged property-by-property
        # with the app rule, so the gradient and right radii stay.
        sidebar.setStyleSheet(
            f"QWidget#Sidebar {{"
            f" border-top-left-radius: {theme.RADIUS_LG}px;"
            f" border-bottom-left-radius: {theme.RADIUS_LG}px; }}")
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(4)

        self.category_list = QListWidget(objectName="NavList")
        self.category_list.setFrameShape(QFrame.Shape.NoFrame)
        self.category_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.category_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # **No auto-scroll to the clicked row** - the owner's ask, 23
        # August 2026: "when I click in the sidebar of the settings on
        # Uninstall the list scrolls down!! do not make it do that!
        # (there is no need for scroll in the settings sidebar)".
        #
        # Hiding the scrollbars does not stop a QListWidget scrolling:
        # selecting a row calls scrollTo(EnsureVisible) on it, so if the
        # viewport is even a pixel short of the last row - which is
        # exactly the bottom row, Uninstall - clicking it slides the
        # whole list up to reveal it, and with no scrollbar there is no
        # way back down. The list is already sized to hold every row
        # (below), so there is nothing it should ever need to scroll to.
        self.category_list.setAutoScroll(False)
        self.category_list.setSpacing(2)
        for name in CATEGORIES:
            item = QListWidgetItem(f"  {name}")
            self.category_list.addItem(item)
            if name == DANGER_CATEGORY:
                # Painted by a label sitting on the row, not by the item's
                # own foreground brush. The brush was the obvious way and
                # it does nothing here: the nav list's QSS sets a colour
                # on ::item, and a stylesheet colour beats the model's
                # ForegroundRole - measured, the row still drew at
                # #9d9db1 with theme.DANGER set on it. A child widget's
                # own stylesheet is the one thing that wins, and a
                # transparent background leaves the row's hover and
                # selection painting untouched underneath.
                label = QLabel(item.text())
                label.setStyleSheet(
                    f"color: {theme.DANGER}; background: transparent;")
                item.setSizeHint(self.category_list.item(0).sizeHint())
                item.setText("")
                self.category_list.setItemWidget(item, label)
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        # Tall enough for every row, measured rather than left to Qt.
        # QListWidget's own sizeHint is bounded however many items it
        # holds - it asked for 192px while eight rows needed 453 - and
        # this list has its scrollbars switched off, so the rows past the
        # end were not merely out of view but unreachable. There is
        # nothing to scroll: the sidebar has the room, it just was not
        # being given to the list.
        rows = self.category_list.count()
        row_height = self.category_list.sizeHintForRow(0) if rows else 0
        spacing = self.category_list.spacing()
        self.category_list.setFixedHeight(
            rows * row_height + spacing * (rows + 1) + CATEGORY_LIST_PADDING)
        layout.addWidget(self.category_list)
        layout.addStretch()

        return sidebar

    def _on_category_changed(self, row):
        if row >= 0:
            self._ensure_page(row)
            self.stack.setCurrentIndex(row)

    def _ensure_page(self, row):
        """Build one category's page, the first time it is asked for.

        Before setCurrentIndex, deliberately: the page is added while its
        holder is still the hidden one, so Qt lays it out once rather
        than laying out and then showing. A row is recorded as built
        before the builder runs, so a builder that raises leaves an empty
        page rather than being retried on every click."""
        if row in self._built_pages:
            return
        self._built_pages.add(row)
        self.stack.widget(row).layout().addWidget(
            scroll_area(self._page_builders[row]()))

    # ------------------------------------------------------------------
    def _build_general_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Version", objectName="SectionTitle"))
        version_row = QHBoxLayout()
        version_row.addWidget(QLabel(f"Atomic {updater.APP_VERSION}"))
        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.clicked.connect(self._on_update_clicked)
        version_row.addWidget(self.update_btn)
        version_row.addStretch()
        form.addLayout(version_row)

        self.update_status = QLabel(
            "Downloads and replaces the app itself. Your entries are left "
            "alone.",
            objectName="Muted",
        )
        self.update_status.setWordWrap(True)
        form.addWidget(self.update_status)

        form.addSpacing(24)
        form.addWidget(QLabel("Startup", objectName="SectionTitle"))
        self.startup_check = QCheckBox("Launch on Windows startup")
        self.startup_check.setChecked(startup.is_enabled())
        self.startup_check.toggled.connect(self._toggle_startup)
        form.addWidget(self.startup_check)

        hint = QLabel("Starts Atomic automatically when you sign in to Windows.", objectName="Muted")
        hint.setWordWrap(True)
        form.addWidget(hint)

        self.fullscreen_startup_check = QCheckBox("Fullscreen mode when launch on startup")
        self.fullscreen_startup_check.setChecked(app_settings.get_fullscreen_on_startup())
        self.fullscreen_startup_check.toggled.connect(self._toggle_fullscreen_on_startup)
        form.addWidget(self.fullscreen_startup_check)

        fullscreen_hint = QLabel(
            "Only that sign-in launch. F11 leaves full screen either way.",
            objectName="Muted",
        )
        fullscreen_hint.setWordWrap(True)
        form.addWidget(fullscreen_hint)
        self._sync_fullscreen_startup_check()

        form.addStretch()
        return page

    def _build_preferences_page(self):
        """How the app is laid out for this person - which sections exist
        and whether Home mirrors that. Split out of General, which had
        become "everything that is not a website list"."""
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Sections", objectName="SectionTitle"))
        sections_hint = QLabel(
            "Which sections show in the sidebar. Hidden ones keep their entries.",
            objectName="Muted",
        )
        sections_hint.setWordWrap(True)
        form.addWidget(sections_hint)

        hidden = set(app_settings.get_hidden_sections())
        self.section_checks = {}
        for name, page_name in nav_config.ordered_nav_items():
            # "&&", not the name as stored: QCheckBox reads a single "&"
            # as a mnemonic marker and swallows it, so "Movies & Series"
            # drew as "Movies  Series" with a hole where the ampersand
            # should be. Doubling it escapes it, and is undone on screen.
            cb = QCheckBox(name.replace("&", "&&"))
            cb.setChecked(page_name not in hidden)
            cb.toggled.connect(lambda checked, p=page_name: self._toggle_section_visibility(p, checked))
            form.addWidget(cb)
            self.section_checks[page_name] = cb

        form.addSpacing(12)
        self.hide_from_home_check = QCheckBox("Hide them from the Home page too")
        self.hide_from_home_check.setChecked(app_settings.get_hide_sections_from_home())
        self.hide_from_home_check.toggled.connect(self._toggle_hide_from_home)
        form.addWidget(self.hide_from_home_check)

        home_hint = QLabel(
            "Also keeps hidden sections off the Home page.",
            objectName="Muted",
        )
        home_hint.setWordWrap(True)
        form.addWidget(home_hint)

        form.addStretch()
        return page

    def _build_keybinds_page(self):
        """The keyboard map. Read-only - these are fixed, and saying so
        is cheaper than a rebinding UI nobody asked for."""
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Keybinds", objectName="SectionTitle"))
        keys_hint = QLabel("What the keyboard does. These are fixed.",
                           objectName="Muted")
        keys_hint.setWordWrap(True)
        form.addWidget(keys_hint)
        for keys, what in global_search.SHORTCUTS:
            row = QHBoxLayout()
            row.setSpacing(8)
            # Fixed width so the descriptions line up in a column
            # instead of stepping in and out with the length of each
            # combination.
            caps = _key_caps(keys)
            caps.setFixedWidth(KEYBIND_COLUMN_WIDTH)
            row.addWidget(caps)
            row.addWidget(QLabel(what, objectName="Muted"), stretch=1)
            form.addLayout(row)

        form.addStretch()
        return page

    # ---- Updates ------------------------------------------------------
    def _on_update_clicked(self):
        """One button for the whole flow: check, then - once something has
        been found - download and install it."""
        if self._pending_update is not None:
            self._start_update_download()
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking...")
        self.update_status.setText("Asking GitHub for the latest release...")
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            found = updater.check_for_update()
            error = ""
        except updater.UpdateError as exc:
            found, error = None, str(exc)
        except Exception as exc:                      # never kill the thread
            found, error = None, f"Couldn't check for updates: {exc}"
        self._update_signals.checked.emit(found, error)

    def _on_update_checked(self, found, error):
        self.update_btn.setEnabled(True)
        if error:
            self.update_btn.setText("Check for Updates")
            self.update_status.setText(error)
            return
        if not found:
            self.update_btn.setText("Check for Updates")
            self.update_status.setText(
                f"Atomic {updater.APP_VERSION} is the latest version.")
            return

        self._pending_update = found
        size_mb = (found.get("size") or 0) / (1024 * 1024)
        self.update_btn.setText(f"Install {found['tag']}")
        self.update_status.setText(
            f"Version {found['version']} is available ({size_mb:.0f} MB). "
            "Installing closes Atomic and reopens it on the new version; "
            "your saved entries are untouched.")

    def _start_update_download(self):
        if not updater.is_frozen():
            self.update_status.setText(
                "This is running from source, so there is no executable to "
                "replace - use git to update instead.")
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Downloading...")
        threading.Thread(target=self._download_update_worker, daemon=True).start()

    def _download_update_worker(self):
        try:
            path = updater.download_update(
                self._pending_update,
                progress=lambda done, total: self._update_signals.progress.emit(done, total))
            error = ""
        except updater.UpdateError as exc:
            path, error = None, str(exc)
        except Exception as exc:
            path, error = None, f"Couldn't download the update: {exc}"
        self._update_signals.downloaded.emit(path, error)

    def _on_update_progress(self, received, total):
        if total:
            self.update_status.setText(
                f"Downloading... {received / (1024 * 1024):.0f} of "
                f"{total / (1024 * 1024):.0f} MB")
        else:
            self.update_status.setText(
                f"Downloading... {received / (1024 * 1024):.0f} MB")

    def _on_update_downloaded(self, path, error):
        if error or path is None:
            self.update_btn.setEnabled(True)
            self.update_btn.setText(f"Install {self._pending_update['tag']}")
            self.update_status.setText(error or "The download failed.")
            return

        self.update_status.setText("Verified. Restarting into the new version...")
        # Recorded before the swap is handed off, because this build is
        # the only one that knows what version it is - the replacement
        # starts with no memory of what it replaced, and reads this to
        # show what changed (helpers.whats_new). Written even though the
        # swap might still fail: a leftover marker only costs one
        # summary that lists nothing and is discarded unread, whereas
        # writing it after apply_update risks the process being gone.
        try:
            app_settings.set_updated_from(updater.APP_VERSION)
        except Exception:
            pass
        try:
            updater.apply_update(path)
        except updater.UpdateError as exc:
            self.update_btn.setEnabled(True)
            self.update_btn.setText(f"Install {self._pending_update['tag']}")
            self.update_status.setText(str(exc))
            return
        # The swap only happens once this process lets go of the exe, so
        # closing is part of installing rather than something after it.
        QApplication.quit()

    def _toggle_section_visibility(self, page_name, visible):
        hidden = set(app_settings.get_hidden_sections())
        if visible:
            hidden.discard(page_name)
        else:
            hidden.add(page_name)
        app_settings.set_hidden_sections(hidden)
        self._apply_section_visibility()

    def _toggle_hide_from_home(self, enabled):
        app_settings.set_hide_sections_from_home(enabled)
        self._apply_section_visibility()

    def _apply_section_visibility(self):
        """Rebuild the sidebar and the page behind this dialog, so a
        toggle lands immediately rather than on the next restart. The
        page is redrawn as well as the sidebar because the toggle can
        now change what Home shows, and Home is very often what's
        sitting behind Settings when this gets changed."""
        main_window = self.parent()
        if main_window is None:
            return
        if hasattr(main_window, "_refresh_nav_list"):
            main_window._refresh_nav_list()
        if hasattr(main_window, "refresh_current_page"):
            main_window.refresh_current_page()

    # ------------------------------------------------------------------
    # The "Watching" category (see CATEGORIES) - the method keeps its
    # older name because the page it builds is unchanged; what it holds
    # always served films and series as well as anime.
    def _build_anime_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        # No Video Websites list here any more - no Stremio/Netflix/
        # Crunchyroll to choose between, and nothing to add to the choice.
        # Video plays inside Atomic now, so where an entry "opens" stopped
        # being a setting. The saved sites file and every entry's site_id
        # are untouched: anime_sites.streaming_provider still reads them
        # to tell the player that a Netflix or Crunchyroll entry is DRM
        # and cannot be played. This is a removal from the interface, not
        # from the data.

        form.addWidget(QLabel("Playback", objectName="SectionTitle"))
        resolution_row = QHBoxLayout()
        resolution_row.addWidget(QLabel("Default resolution"))
        self.resolution_combo = QComboBox()
        for value in app_settings.RESOLUTION_CHOICES:
            self.resolution_combo.addItem(RESOLUTION_LABELS.get(value, value), value)
        current = app_settings.get_preferred_resolution()
        index = self.resolution_combo.findData(current)
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
        # currentIndexChanged, not activated: the two behave the same for
        # a click, and this one also fires for a keyboard change, which
        # `activated` misses.
        self.resolution_combo.currentIndexChanged.connect(self._save_resolution)
        resolution_row.addWidget(self.resolution_combo)
        resolution_row.addStretch()
        form.addLayout(resolution_row)

        resolution_hint = QLabel(
            "Which quality the player starts on when a title offers several. "
            "It falls back to the nearest available one.",
            objectName="Muted",
        )
        resolution_hint.setWordWrap(True)
        # The detail that does not fit two lines - and the reason the
        # default is not "best".
        resolution_hint.setToolTip(
            "4K is picked from a much smaller swarm and moves far larger "
            "pieces: one measured here advertised 313 seeders and served "
            "nothing at all inside a minute, while 1080p started instantly.")
        form.addWidget(resolution_hint)

        form.addSpacing(10)
        self.auto_pick_check = QCheckBox("Auto choose source to play")
        self.auto_pick_check.setChecked(app_settings.get_auto_pick_source())
        self.auto_pick_check.toggled.connect(app_settings.set_auto_pick_source)
        form.addWidget(self.auto_pick_check)
        auto_pick_hint = QLabel(
            "Pressing an episode starts the best source at your preferred "
            "resolution right away. Turn off to pick from the source list "
            "each time - the source and resolution can still be changed "
            "inside the player either way.",
            objectName="Muted",
        )
        auto_pick_hint.setWordWrap(True)
        form.addWidget(auto_pick_hint)

        form.addSpacing(20)
        form.addWidget(QLabel("Episode List", objectName="SectionTitle"))
        self.blur_stills_check = QCheckBox("Blur episode images")
        self.blur_stills_check.setChecked(app_settings.get_blur_episode_stills())
        self.blur_stills_check.toggled.connect(
            app_settings.set_blur_episode_stills)
        form.addWidget(self.blur_stills_check)
        blur_hint = QLabel(
            "Episode rows show a picture of the episode. Turn this on to "
            "soften them, so a still cannot give away what happens. Takes "
            "effect the next time a title's page is opened.",
            objectName="Muted",
        )
        blur_hint.setWordWrap(True)
        form.addWidget(blur_hint)

        form.addSpacing(24)
        # The Stremio Account sign-in that lived here (email/password,
        # Sign In/Disconnect) is removed entirely at the owner's ask -
        # see the module-level note near _PROBE_TITLE_LIMIT. The progress
        # note below survives it: it describes the in-app player, which
        # is the only thing recording progress now.
        progress_note = QLabel(
            "Playing an episode here records it on the entry by itself. "
            "Progress only ever moves forward - rewatching an old episode "
            "leaves the number where it is.",
            objectName="Muted",
        )
        progress_note.setWordWrap(True)
        form.addWidget(progress_note)

        form.addStretch()
        return page

    # ------------------------------------------------------------------
    def _build_reading_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Reading Websites", objectName="SectionTitle"))
        sites_hint = QLabel(
            "Sites reading entries are searched on and opened to. Add name and URL.",
            objectName="Muted",
        )
        sites_hint.setWordWrap(True)
        form.addWidget(sites_hint)

        self.sites_list = QListWidget()
        self.sites_list.setMinimumHeight(160)
        self.sites_list.itemDoubleClicked.connect(self._edit_site)
        form.addWidget(self.sites_list, stretch=1)
        # Filled here rather than from __init__: this page builds on
        # first visit, so this is the moment the list exists.
        self._refresh_sites()

        sites_btn_row = QHBoxLayout()
        add_site_btn = QPushButton("Add...")
        add_site_btn.clicked.connect(self._add_site)
        sites_btn_row.addWidget(add_site_btn)
        edit_site_btn = QPushButton("Edit...")
        edit_site_btn.clicked.connect(self._edit_site)
        sites_btn_row.addWidget(edit_site_btn)
        check_site_btn = QPushButton("Check")
        check_site_btn.setToolTip(
            "Searches this site for a title it should have, then says which "
            "of the verdicts below you would get by opening an entry here.")
        check_site_btn.clicked.connect(self._check_site)
        sites_btn_row.addWidget(check_site_btn)
        check_all_sites_btn = QPushButton("Check All")
        check_all_sites_btn.setToolTip("Check every site in this list. Verdicts clear "
                                       "when Atomic restarts, so this is how to fill "
                                       "them back in.")
        check_all_sites_btn.clicked.connect(lambda: self._check_all_sites("reading"))
        sites_btn_row.addWidget(check_all_sites_btn)
        remove_site_btn = QPushButton("Remove", objectName="Danger")
        remove_site_btn.clicked.connect(self._remove_site)
        sites_btn_row.addWidget(remove_site_btn)
        form.addLayout(sites_btn_row)
        form.addWidget(_verdict_legend("reading"))

        form.addSpacing(24)
        form.addWidget(QLabel("Reading Music URL", objectName="SectionTitle"))
        self.manga_music_edit = QLineEdit(app_settings.get_manga_music_url())
        self.manga_music_edit.setPlaceholderText("https://example.com/lofi-playlist")
        self.manga_music_edit.editingFinished.connect(self._save_manga_music_url)
        form.addWidget(self.manga_music_edit)

        manga_music_hint = QLabel(
            "Opens alongside a reading entry, for music while you read. "
            "Leave blank to skip.",
            objectName="Muted",
        )
        manga_music_hint.setWordWrap(True)
        form.addWidget(manga_music_hint)

        form.addStretch()
        return page

    # ------------------------------------------------------------------
    def _build_games_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Game Launcher Directories", objectName="SectionTitle"))
        hint = QLabel(
            "Point at each launcher's folder - Steam's is the one with steamapps.",
            objectName="Muted",
        )
        hint.setWordWrap(True)
        # The detail that no longer fits the two-line hint: it imports
        # on the spot, and re-checking later is safe.
        hint.setToolTip("Every game found there is added to the Games page as soon as you "
                        "set this. Games already listed are matched by path and never "
                        "added twice, so it is safe to set again after installing more.")
        form.addWidget(hint)

        self.launcher_dir_edits = {}
        saved_dirs = app_settings.get_launcher_dirs()
        for key, label, _subpath in launchers.LAUNCHERS:
            form.addSpacing(12)
            form.addWidget(QLabel(label))
            row = QHBoxLayout()
            edit = QLineEdit(saved_dirs.get(key, ""))
            edit.setPlaceholderText(f"e.g. G:\\{label}")
            edit.editingFinished.connect(lambda k=key, e=edit: self._save_and_import_launcher_dir(k, e.text().strip()))
            row.addWidget(edit, stretch=1)
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(lambda checked=False, k=key, e=edit: self._browse_launcher_dir(k, e))
            row.addWidget(browse_btn)
            form.addLayout(row)
            self.launcher_dir_edits[key] = edit

        form.addStretch()
        return page

    def _browse_launcher_dir(self, key, edit):
        path = QFileDialog.getExistingDirectory(self, "Select install directory")
        if path:
            edit.setText(path)
            self._save_and_import_launcher_dir(key, path)

    def _save_and_import_launcher_dir(self, key, path):
        app_settings.set_launcher_dir(key, path)
        if not path:
            return
        self._scan_toasts[key] = show_toast(self, "Scanning...", duration_ms=None)
        threading.Thread(target=self._import_launcher_worker, args=(key, path), daemon=True).start()

    def _import_launcher_worker(self, key, path):
        label, subpath = next(((l, s) for k, l, s in launchers.LAUNCHERS if k == key), (None, None))
        try:
            found = [{**g, "launcher": key} for g in launchers.scan_launcher(path, subpath, label, key)]
            added = launchers.import_scanned_games(found)
        except Exception:
            added = 0
        self._launcher_import_signals.done.emit(key, added)

    def _on_launcher_import_done(self, key, added):
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "refresh_current_page"):
            main_window.refresh_current_page()
        label = next((l for k, l, _s in launchers.LAUNCHERS if k == key), key)
        # Same wording as the Games page's own Import button, prefixed
        # with which launcher this was - several can be scanning at once
        # here, one per directory the user fills in.
        finish_toast(self._scan_toasts.pop(key, None), self,
                     f"{label}: {launchers.import_result_message(added)}")

    # ------------------------------------------------------------------
    def _build_api_keys_page(self):
        """One field per key in app_settings.API_KEYS.

        Its own category rather than a section tacked onto Watching:
        these keys serve three different features (artwork, subtitle
        sources, translation) and there was previously **nowhere at all**
        to put them - the table existed, every reader of it existed, and
        the only way to set one was to hand-edit settings.json.

        Drawn from the table, not written out by hand, so adding a source
        is a row in app_settings and nothing here."""
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        intro = QLabel(
            "Keys are stored on this machine only, in settings.json, and are "
            "never sent anywhere except to the service they belong to. "
            "Anything without a key stays off and says so rather than "
            "failing quietly.",
            objectName="Muted",
        )
        intro.setWordWrap(True)
        form.addWidget(intro)

        self.api_key_edits = {}
        self.api_key_states = {}
        for heading, names in app_settings.API_KEY_GROUPS:
            form.addSpacing(18)
            form.addWidget(QLabel(heading, objectName="SectionTitle"))
            for name in names:
                label, unlocks = app_settings.API_KEYS.get(name, (name, ""))
                row = QHBoxLayout()
                caption = QLabel(label)
                caption.setFixedWidth(API_KEY_LABEL_WIDTH)
                row.addWidget(caption)

                edit = QLineEdit(app_settings.get_api_key(name))
                # Password echo by default: this dialog gets opened with
                # somebody watching often enough, and a key on screen is
                # a key on screen. The Show tick below reveals all of
                # them at once for the one job that needs it - checking
                # a paste went in whole.
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setPlaceholderText(f"Paste your {label} key")
                edit.editingFinished.connect(
                    lambda n=name: self._save_api_key(n))
                row.addWidget(edit, stretch=1)

                state = QLabel("", objectName="Muted")
                state.setFixedWidth(API_KEY_STATE_WIDTH)
                row.addWidget(state)
                form.addLayout(row)

                hint_text = (f"{unlocks} · "
                             f"{app_settings.API_KEY_HELP.get(name, '')}")
                # The same "Get a key" link the first-run wizard carries,
                # from the same URL table, so the two never drift apart.
                url = app_settings.API_KEY_URLS.get(name, "")
                if url:
                    hint_text = (f'<a href="{url}" style="color: '
                                 f'{theme.ACCENT};">Get a key ↗</a>'
                                 f" · {hint_text}")
                hint = QLabel(hint_text, objectName="Muted")
                hint.setWordWrap(True)
                if url:
                    hint.setOpenExternalLinks(True)
                    hint.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextBrowserInteraction)
                hint.setContentsMargins(API_KEY_LABEL_WIDTH + 8, 0, 0, 4)
                form.addWidget(hint)

                self.api_key_edits[name] = edit
                self.api_key_states[name] = state

        form.addSpacing(18)
        show_keys = QCheckBox("Show keys")
        show_keys.toggled.connect(self._toggle_api_key_echo)
        form.addWidget(show_keys)

        # No debrid line here any more - the row is gone from
        # app_settings.API_KEYS at the owner's ask and every build uses the
        # bundled token, so describing a field that no longer exists would
        # be the only place in the app still advertising the choice.
        note = QLabel(
            "TMDB already has a key built into this build - only paste one "
            "here if logos stop loading. Subtitles need at least one source "
            "key; AI translation is what covers a title nobody has published "
            "Arabic for, and one AI key is enough.",
            objectName="Muted",
        )
        note.setWordWrap(True)
        form.addWidget(note)

        self._refresh_api_key_states()
        form.addStretch()
        return page

    def _toggle_api_key_echo(self, shown):
        mode = (QLineEdit.EchoMode.Normal if shown
                else QLineEdit.EchoMode.Password)
        for edit in self.api_key_edits.values():
            edit.setEchoMode(mode)

    def _save_api_key(self, name):
        edit = self.api_key_edits.get(name)
        if edit is None:
            return
        app_settings.set_api_key(name, edit.text().strip())
        self._refresh_api_key_states()

    def _refresh_api_key_states(self):
        for name, state in self.api_key_states.items():
            configured = bool(app_settings.get_api_key(name))
            state.setText("Set" if configured else "Not set")
            state.setStyleSheet(
                f"color: {theme.SUCCESS if configured else theme.TEXT_DIM};"
                f" background: transparent;")

    # ------------------------------------------------------------------
    def _build_data_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        # First on the page, ahead of the two destructive sections: taking
        # a copy is the thing to do *before* clearing something, and
        # restoring is what you look for after clearing the wrong one.
        form.addWidget(QLabel("Backup", objectName="SectionTitle"))
        backup_hint = QLabel(
            "Saves everything you have added to one zip file you choose.",
            objectName="Muted",
        )
        backup_hint.setWordWrap(True)
        # The detail that doesn't fit the two-line hint.
        backup_hint.setToolTip(
            "Every entry, site list and setting goes in. Covers and the log "
            "file don't - they are rebuilt on their own and would make the "
            "file large. Keep it somewhere else if it is meant to survive "
            "this PC.")
        form.addWidget(backup_hint)

        backup_btn_row = QHBoxLayout()
        backup_btn = QPushButton("Back Up Data...")
        backup_btn.clicked.connect(self._backup_data)
        backup_btn_row.addWidget(backup_btn)
        restore_btn = QPushButton("Restore Data...")
        restore_btn.setToolTip(
            "Replaces what is saved now with the contents of a backup. You "
            "are asked to confirm first, and a backup that won't read is "
            "refused before anything is changed.")
        restore_btn.clicked.connect(self._restore_data)
        backup_btn_row.addWidget(restore_btn)
        backup_btn_row.addStretch()
        form.addLayout(backup_btn_row)

        restore_hint = QLabel(
            "Restoring replaces what is saved now with the backup's contents.",
            objectName="Muted",
        )
        restore_hint.setWordWrap(True)
        form.addWidget(restore_hint)

        form.addSpacing(28)
        form.addWidget(QLabel("Clear Data", objectName="SectionTitle"))
        clear_hint = QLabel(
            "Wipes the ticked categories' entries. This cannot be undone.",
            objectName="Muted",
        )
        clear_hint.setWordWrap(True)
        form.addWidget(clear_hint)

        self.clear_select_all = QCheckBox("Select All")
        self.clear_select_all.toggled.connect(self._toggle_all_clear_checks)
        form.addWidget(self.clear_select_all)
        form.addSpacing(4)

        self.clear_checks = []
        for name, _file, _predicate in CLEAR_CATEGORIES:
            # `&&`, not `&`: Qt reads a single ampersand in a button or
            # checkbox label as a mnemonic accelerator and swallows it,
            # so "Watch & Read History" was drawn as "Watch  Read
            # History" with a hole where the ampersand should be - the
            # owner's screenshot. The table keeps the real string,
            # because it is also printed in the "... cleared." toast,
            # where an escaped one would show through.
            cb = QCheckBox(name.replace("&", "&&"))
            cb.toggled.connect(self._sync_clear_select_all)
            form.addWidget(cb)
            self.clear_checks.append(cb)

        clear_btn = QPushButton("Clear Selected", objectName="Danger")
        clear_btn.clicked.connect(self._clear_checked_categories)
        form.addWidget(clear_btn)

        form.addStretch()
        return page

    def _backup_data(self):
        """Zip every saved data file to a location the user picks.

        Not threaded, unlike the launcher import next door: the whole of
        DATA_DIR's JSON measured about 23KB on the owner's install, so the
        zip is written well inside a frame and a background thread would
        only add a way for the result to arrive after this dialog is
        gone."""
        files = sorted(p for p in storage.DATA_DIR.glob(_BACKUP_GLOB) if p.is_file())
        if not files:
            inform(self, "Back Up Data",
                   "There is nothing saved to back up yet.")
            return

        suggested = str(Path.home() / f"Atomic Backup {datetime.now():%Y-%m-%d}.zip")
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Back Up Data", suggested, "Zip archive (*.zip)")
        if not path:
            return

        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for data_file in files:
                    # arcname is the bare filename on purpose: a restore
                    # matches members against DATA_DIR by name, and a
                    # stored path would put the file somewhere else.
                    archive.write(data_file, data_file.name)
        except (OSError, zipfile.BadZipFile):
            logs.exception(f"Could not write a backup to {path}")
            # A dialog rather than a toast: a backup the user believes
            # they have and doesn't is the failure this whole feature is
            # meant to prevent.
            inform(self, "Back Up Data",
                   "Could not write the backup there. Try another folder - a "
                   "system folder or a full drive will refuse.")
            return

        show_toast(self, f"Backed Up {len(files)} Files")

    def _restore_data(self):
        """Replace saved data with an archive's contents, all of it or
        none of it.

        Order is the whole safety here: read and validate every member
        (_read_backup), then confirm, then write. Each file goes through
        storage.save, so each one leaves the .bak the rest of the app
        relies on - a restore is mechanically a batch of saves, and this
        deliberately doesn't grow its own overwrite handling beside it."""
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Restore Data", str(Path.home()), "Zip archive (*.zip)")
        if not path:
            return

        try:
            restored = _read_backup(Path(path))
        except _BackupError as exc:
            inform(self, "Restore Data", str(exc))
            return

        names = ", ".join(sorted(restored))
        # Defaulting to No, like Uninstall and unlike Clear Data: this one
        # overwrites files the user did not name, and the accidental
        # Return keypress must not be the one that does it.
        if not confirm(
                self, "Restore Data",
                f"Replace what is saved now with this backup?\n\n{names}\n\n"
                f"Everything currently in those files is overwritten. This "
                f"cannot be undone.",
                danger=True, default_no=True):
            return

        for name, data in sorted(restored.items()):
            storage.save(name, data)

        main_window = self.parent()
        # Sidebar and page first - a restored settings.json can change
        # which sections exist, and a restored tracker.json changes what
        # the page behind this dialog is showing.
        self._apply_section_visibility()
        # Anchored to the main window, not to this dialog: the dialog is
        # closing on the next line and its toast would go with it.
        show_toast(main_window or self, f"Restored {len(restored)} Files")
        # Closing is part of restoring, not something after it. Every
        # control in this dialog was built from the settings.json that has
        # just been replaced, so leaving it open means the next toggle
        # writes a pre-restore value straight back over what was restored.
        # The dialog is rebuilt from saved state on every open anyway.
        self.accept()

    def _build_uninstall_page(self):
        """On its own, at the bottom, in red. It was a section under Clear
        Data - one scroll below the buttons that wipe a single category,
        which is too close for the one control that removes everything
        including the app."""
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Uninstall", objectName="SectionTitle"))
        uninstall_hint = QLabel(
            "Deletes every entry, site list and setting, then removes the "
            "app. This cannot be undone.",
            objectName="Muted",
        )
        uninstall_hint.setWordWrap(True)
        form.addWidget(uninstall_hint)
        uninstall_btn = QPushButton("Uninstall", objectName="Danger")
        uninstall_btn.clicked.connect(self._uninstall)
        form.addWidget(uninstall_btn)

        form.addStretch()
        return page

    def _toggle_all_clear_checks(self, checked):
        for cb in self.clear_checks:
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

    def _sync_clear_select_all(self, _checked):
        # Keep "Select All" reflecting reality when the user (un)checks
        # categories by hand, instead of it silently going stale.
        all_checked = all(cb.isChecked() for cb in self.clear_checks)
        self.clear_select_all.blockSignals(True)
        self.clear_select_all.setChecked(all_checked)
        self.clear_select_all.blockSignals(False)

    def _clear_checked_categories(self):
        checked = [CLEAR_CATEGORIES[i] for i, cb in enumerate(self.clear_checks) if cb.isChecked()]
        if not checked:
            inform(self, "Clear Data", "Check at least one category first.")
            return
        names = ", ".join(name for name, _file, _predicate in checked)
        if not confirm(self, "Clear Data",
                       f"Clear all {names} entries? This cannot be undone."):
            return

        # The three watch kinds share series.json - load/save it once for
        # all of them instead of the second clear stomping the first's
        # result.
        by_file = {}
        for name, data_file, predicate in checked:
            by_file.setdefault(data_file, []).append(predicate)

        for data_file, predicates in by_file.items():
            if any(p is None for p in predicates):
                storage.save(data_file, [])
                continue
            entries = storage.load(data_file, [])
            storage.save(data_file, [e for e in entries if not any(p(e) for p in predicates)])

        for cb in self.clear_checks:
            cb.setChecked(False)

        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "refresh_current_page"):
            main_window.refresh_current_page()
        inform(self, "Clear Data", f"{names} cleared.")

    def _uninstall(self):
        if not confirm(
                self, "Uninstall Atomic",
                "This permanently deletes every saved Atomic file on this PC "
                "(all Anime/Series/Movies/Reading/Games/Apps/Websites entries, "
                "your watch and read history, the download queue, site "
                "lists, and settings) and removes the app itself. This cannot "
                "be undone.\n\nThe app will close immediately. Continue?",
                danger=True, default_no=True):
            return
        uninstall.run()
        QApplication.instance().quit()

    # ------------------------------------------------------------------
    def _site_label(self, site) -> str:
        """One row of a websites list, with what the site can actually
        do. Without this the only way to learn that a site never resolves
        to title pages was to use it for a while and notice that every
        entry opened a search page.

        A verdict shows only while it is this run's own measurement (see
        _CHECKED_THIS_RUN) - a stale one is a claim the app cannot stand
        behind after a restart."""
        label = f"{site['name']}  —  {site['base_url']}"
        if site["id"] in self._probing_sites:
            state = "checking"
        elif site["id"] in _CHECKED_THIS_RUN:
            state = site.get("resolves")
        else:
            state = None
        note = _RESOLVES_LABELS.get(state)
        return f"{label}   ·   {note}" if note else label

    def _probe_site_async(self, which: str, site_id: str):
        """Check what a site resolves to, in the background - it makes
        real requests and takes seconds."""
        module = manga_sites if which == "reading" else anime_sites
        site = module.get_site(site_id)
        if not site:
            return
        self._probing_sites.add(site_id)
        # Reading is the only list on screen now (the Video Websites one
        # is gone), so it is the only one with a row to repaint.
        if which == "reading":
            self._refresh_sites()
        # Pooled, not a thread of its own: Check All fires one of these
        # per configured site, and a bare thread each is the shape that
        # once put 651 simultaneous connections on this user's network
        # (.claude/rules/integrations.md).
        #
        # submit_watched, not submit: the shared queue is drained by
        # three tracker pages' worth of page-load backfill, and a Check
        # pressed after visiting one of them sat behind all of it.
        # Crunchyroll made that plain - its verdict is decided from a
        # table with no request at all, and the row still never filled
        # in, because the job had not started yet.
        lookup_pool.submit_watched(self._probe_site_worker, which, site_id, dict(site),
                                   self._probe_titles(which, site_id))

    def _probe_titles(self, which: str, site_id: str) -> list:
        """Titles to check a site with: ones the user actually tracks,
        preferring any already pointed at this very site, since those are
        certain to exist there.

        A single fixed title was the whole bug behind "directed to search
        page" on sites that resolve fine - three of the four reading
        sites here are Arabic scanlation sites that do not carry "One
        Piece" under that name, so the probe found nothing and blamed the
        site. Read off disk rather than from a page: Settings opens over
        whichever page is showing, and none of them may be a tracker one.

        Reading entries are "everything in tracker.json that isn't
        Anime" - that file holds Anime and the reading types, and
        films/series live in series.json - which avoids restating the
        list of reading types (it belongs to windows.tracker, and helpers
        must not import from windows)."""
        tracked = storage.load("tracker.json", [])
        if which == "reading":
            entries = [e for e in tracked if e.get("type") != "Anime"]
        else:
            entries = [e for e in tracked if e.get("type") == "Anime"]
            entries += storage.load("series.json", [])
        entries.sort(key=lambda e: e.get("site_id") != site_id)
        titles = []
        for entry in entries:
            title = (entry.get("title") or "").strip()
            if title and title not in titles:
                titles.append(title)
            if len(titles) >= _PROBE_TITLE_LIMIT:
                break
        return titles

    def _probe_site_worker(self, which, site_id, site, titles=None):
        # Must never raise - a dead thread would leave the row stuck on
        # "checking..." forever.
        module = manga_sites if which == "reading" else anime_sites
        try:
            verdict = module.probe_site(site, titles=titles)
        except Exception:
            verdict = module.RESOLVES_UNKNOWN
        try:
            module.record_resolution(site_id, verdict)
        except Exception:
            # Only a verdict that reached the record is shown - if the
            # write failed, what is on disk is some older run's answer,
            # which is exactly what is being kept off the screen. set.add
            # off the UI thread is fine (atomic); the redraw itself still
            # goes through the signal below.
            #
            # Logged rather than passed over in silence: swallowing it is
            # why Check All looked like it simply skipped Crunchyroll for
            # so long - two probes finishing at once collided in
            # storage.save (fixed there), and the row's blankness was the
            # only symptom anywhere.
            logs.exception(f"Could not record the check result for {site.get('name')}")
        else:
            _CHECKED_THIS_RUN.add(site_id)
        self._site_probe_signals.done.emit(which, site_id)

    def _on_site_probed(self, which, site_id):
        self._probing_sites.discard(site_id)
        if which == "reading":
            self._refresh_sites()

    def _refresh_sites(self):
        # The Reading page builds on first visit, and a probe started
        # before that (Add Website, or one still running from an earlier
        # visit) reports back through _on_site_probed regardless. An
        # AttributeError raised in a Qt slot takes the whole process down
        # (planning.md, defect #5) - so ask whether the list exists
        # rather than assuming it does. Nothing is lost: the page fills
        # itself from disk when it is finally built.
        if getattr(self, "sites_list", None) is None:
            return
        self.sites_list.clear()
        for site in manga_sites.list_sites():
            item = QListWidgetItem(self._site_label(site))
            item.setData(Qt.ItemDataRole.UserRole, site["id"])
            self.sites_list.addItem(item)

    def _selected_site_id(self):
        items = self.sites_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _add_site(self):
        dialog = SiteForm(self, "Website")
        if dialog.result_data:
            site = manga_sites.add_site(*dialog.result_data)
            self._refresh_sites()
            self._probe_site_async("reading", site["id"])

    def _edit_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            inform(self, "Reading Websites", "Select a website first.")
            return
        dialog = SiteForm(self, "Website", manga_sites.get_site(site_id))
        if dialog.result_data:
            manga_sites.update_site(site_id, *dialog.result_data)
            self._refresh_sites()
            # Re-checked, not kept: the URL may be the thing that changed.
            self._probe_site_async("reading", site_id)

    def _check_all_sites(self, which: str):
        """Re-probe every site in one list.

        Worth having because a verdict now only shows while the run that
        measured it is still going (see _CHECKED_THIS_RUN), so after a
        restart the whole list is blank and clicking Check once per site
        is the only way back. Already-running probes are skipped rather
        than queued twice."""
        module = manga_sites if which == "reading" else anime_sites
        sites = [site for site in module.list_sites()
                 if site["id"] not in self._probing_sites]
        for site in sites:
            self._probe_site_async(which, site["id"])

    def _check_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            inform(self, "Reading Websites", "Select a website first.")
            return
        self._probe_site_async("reading", site_id)

    def _remove_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            inform(self, "Reading Websites", "Select a website first.")
            return
        site = manga_sites.get_site(site_id)
        if confirm(self, "Remove Website", f"Remove '{site['name']}'?"):
            manga_sites.remove_site(site_id)
            self._refresh_sites()

    # The Add/Edit/Check/Remove set for Video Websites lived here. It went
    # with the list itself: there is nothing to add a video site *to* any
    # more. anime_sites keeps its saved sites and its whole API - entries
    # still carry site_id, and streaming_provider is what tells the player
    # a Netflix or Crunchyroll entry is DRM - it simply has no editor.

    def _toggle_startup(self, checked):
        try:
            startup.set_enabled(checked)
        except OSError as exc:
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not checked)
            self.startup_check.blockSignals(False)
            inform(self, "Settings", f"Couldn't update startup setting:\n{exc}")
        # Whichever way that went, the fullscreen option follows the
        # checkbox's *actual* state - including the rolled-back one above.
        self._sync_fullscreen_startup_check()

    def _sync_fullscreen_startup_check(self):
        """"Fullscreen mode when launch on startup" only means anything
        while there *is* a startup launch, so it greys out with the
        toggle above it rather than sitting there ticked and inert.

        Its saved value is left alone when it greys out: turning startup
        off and back on shouldn't quietly lose the choice, and nothing
        reads the setting unless the app was started by Windows anyway
        (see main.main)."""
        enabled = self.startup_check.isChecked()
        self.fullscreen_startup_check.setEnabled(enabled)
        self.fullscreen_startup_check.setToolTip(
            "" if enabled else "Turn on \"Launch on Windows startup\" first.")

    def _toggle_fullscreen_on_startup(self, checked):
        app_settings.set_fullscreen_on_startup(checked)

    def _save_resolution(self, index):
        value = self.resolution_combo.itemData(index)
        if value:
            app_settings.set_preferred_resolution(value)

    def _save_manga_music_url(self):
        app_settings.set_manga_music_url(self.manga_music_edit.text().strip())


class SiteForm(QDialog):
    """Add/edit one site: just a name + URL - shared by Reading Websites
    and Video Websites, which both store a plain base URL now (video
    sites used to want a hand-typed "/search?q=" prefix here; the search
    pattern is derived per site in anime_sites instead). `result_data`
    is a (name, url) tuple after a successful Save, else None."""

    def __init__(self, parent, kind, site=None):
        super().__init__(parent)
        self.result_data = None
        self.setWindowTitle(f"Edit {kind}" if site else f"Add {kind}")
        # 240 tall, up from the 210 the natively-framed version needed:
        # the panel now carries its own heading where the title bar was.
        self.setFixedSize(360, 240)

        form = QVBoxLayout(self)
        form.setContentsMargins(20, 18, 20, 16)
        form.setSpacing(6)

        form.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(site["name"] if site else "")
        form.addWidget(self.name_edit)

        form.addSpacing(8)
        form.addWidget(QLabel("Website URL"))
        self.url_edit = QLineEdit(site.get("base_url", "") if site else "")
        self.url_edit.setPlaceholderText("https://example.com/")
        form.addWidget(self.url_edit)

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

    def _save(self):
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name or not url:
            inform(self, "Websites", "Name and URL are required.")
            return
        self.result_data = (name, url)
        self.accept()
