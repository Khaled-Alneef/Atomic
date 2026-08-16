"""Settings window: a category sidebar (mirroring the main app window's
own sidebar) on the left, the selected category's controls on the right.

General: Windows-startup toggle (plus whether that sign-in launch opens
full screen), and which sections show up in the main
sidebar (Anime, Reading, Series, Games, Apps, Websites can each be hidden
without losing their saved data). Anime & Series: the list of Video
Websites Anime entries can be set to open on (Stremio is always
available as a built-in option; Crunchyroll and any others are
addable/editable, the same way Reading sites work) and the connected
Stremio account used to pull in real watch progress. Reading: the list of manga/manhwa/manhua reading sites the
Reading page can search and open to, plus an optional music/ambience URL.
Games: each game launcher's install directory, so the Games page can
bulk-import every game it finds there (see helpers.launchers) instead of
adding each one by hand. Data: wipe one content category's saved entries
at a time, or uninstall the app entirely (every saved file plus the app
itself).
"""

import sys
import threading

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import (
    anime_sites, app_settings, launchers, manga_sites, nav_config, startup,
    storage, stremio, theme, uninstall, updater,
)
from .widgets import finish_toast, scroll_area, show_toast

CATEGORIES = ["General", "Anime & Series", "Reading", "Games", "Data"]

# (display name, data file, predicate). A predicate of None means "clear
# the whole file" (Series/Games/Apps/Websites each hold only their own
# entries) - Anime and Reading share tracker.json, so those two instead
# filter out just the matching type(s) and keep the rest. Mirrors
# windows.tracker.MANGA_TYPES, duplicated here rather than imported to
# avoid a helpers -> windows dependency for one 3-value tuple.
CLEAR_CATEGORIES = [
    ("Anime", "tracker.json", lambda e: e.get("type") == "Anime"),
    ("Reading", "tracker.json", lambda e: e.get("type") in ("Manga", "Manhwa", "Manhua")),
    ("Series", "series.json", None),
    ("Games", "games.json", None),
    ("Apps", "apps.json", None),
    ("Websites", "websites.json", None),
]


class _StremioLoginSignals(QObject):
    done = Signal(str, str, str)  # email, auth key, error message (one is always "")


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

# Set once the user signs in again from this dialog, to stop the marker
# below - which is sticky for the life of the process - from calling a
# brand new session rejected. Module level for the same reason as the set
# above: the dialog is rebuilt on every open, the fact is not.
_STREMIO_SIGNED_IN_HERE = False


def _stremio_sign_in_rejected() -> bool:
    """Whether the tracker's last progress sync was turned away by
    Stremio, meaning the saved session is dead and nothing is syncing.

    Read, never measured: asking Stremio whether the key still works
    would put a network request behind merely opening this dialog. The
    tracker has already asked, on every page arrival, and records the
    answer - windows.tracker._auth_warning_shown goes True the first time
    a bulk sync comes back REASON_STREMIO_AUTH_FAILED (see
    TrackerPage._warn_stremio_auth_once). That marker is once-per-run by
    design and never resets, so this reads exactly as often as the
    tracker's own toast says it: a dead key stays dead until reconnected.

    sys.modules rather than an import, on purpose - helpers must not
    depend on windows (see CLEAR_CATEGORIES above for the same call), and
    a run in which no tracker page was ever built then reads as "nothing
    known" rather than dragging the whole page module in to find out."""
    if _STREMIO_SIGNED_IN_HERE:
        return False
    tracker = sys.modules.get("windows.tracker")
    return bool(tracker is not None and getattr(tracker, "_auth_warning_shown", False))


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
        theme.apply_dark_titlebar(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
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

        self._login_signals = _StremioLoginSignals()
        self._login_signals.done.connect(self._on_stremio_login_done)

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
        self.stack.addWidget(scroll_area(self._build_general_page()))
        self.stack.addWidget(scroll_area(self._build_anime_page()))
        self.stack.addWidget(scroll_area(self._build_reading_page()))
        self.stack.addWidget(scroll_area(self._build_games_page()))
        self.stack.addWidget(scroll_area(self._build_data_page()))
        content_col.addWidget(self.stack, stretch=1)
        self.category_list.setCurrentRow(0)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        content_col.addLayout(btn_row)

        content_wrap = QWidget()
        content_wrap.setLayout(content_col)
        body.addWidget(content_wrap, stretch=1)

        self._refresh_stremio_account()
        self._refresh_video_sites()
        self._refresh_sites()

        self.exec()

    # ------------------------------------------------------------------
    def _build_category_sidebar(self):
        sidebar = QWidget(objectName="Sidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(4)

        self.category_list = QListWidget(objectName="NavList")
        self.category_list.setFrameShape(QFrame.Shape.NoFrame)
        self.category_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.category_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.category_list.setSpacing(2)
        for name in CATEGORIES:
            self.category_list.addItem(QListWidgetItem(f"  {name}"))
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        layout.addWidget(self.category_list)
        layout.addStretch()

        return sidebar

    def _on_category_changed(self, row):
        if row >= 0:
            self.stack.setCurrentIndex(row)

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

        form.addSpacing(24)
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
    def _build_anime_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Video Websites", objectName="SectionTitle"))
        video_sites_hint = QLabel(
            "Where entries open on double-click, chosen per entry in Add/Edit.",
            objectName="Muted",
        )
        video_sites_hint.setWordWrap(True)
        video_sites_hint.setToolTip(
            "Stremio is always available and opens the title directly. Crunchyroll "
            "and Netflix open the title's own page too, found through public "
            "databases. Anything else you add depends on its own search - Check "
            "says which. Suggestions, covers and watch progress come from Stremio "
            "either way, whichever site an entry opens on.")
        form.addWidget(video_sites_hint)

        self.video_sites_list = QListWidget()
        self.video_sites_list.setMinimumHeight(120)
        self.video_sites_list.itemDoubleClicked.connect(self._edit_video_site)
        form.addWidget(self.video_sites_list)

        video_sites_btn_row = QHBoxLayout()
        add_video_site_btn = QPushButton("Add...")
        add_video_site_btn.clicked.connect(self._add_video_site)
        video_sites_btn_row.addWidget(add_video_site_btn)
        edit_video_site_btn = QPushButton("Edit...")
        edit_video_site_btn.clicked.connect(self._edit_video_site)
        video_sites_btn_row.addWidget(edit_video_site_btn)
        check_video_site_btn = QPushButton("Check")
        check_video_site_btn.setToolTip(
            "Searches this site for a title it should have, then says which "
            "of the verdicts below you would get by opening an entry here.")
        check_video_site_btn.clicked.connect(self._check_video_site)
        video_sites_btn_row.addWidget(check_video_site_btn)
        remove_video_site_btn = QPushButton("Remove", objectName="Danger")
        remove_video_site_btn.clicked.connect(self._remove_video_site)
        video_sites_btn_row.addWidget(remove_video_site_btn)
        form.addLayout(video_sites_btn_row)
        form.addWidget(_verdict_legend("anime/movies/series"))

        form.addSpacing(24)
        form.addWidget(QLabel("Stremio Account", objectName="SectionTitle"))

        self.stremio_account_status = QLabel("", objectName="Muted")
        # Wraps because the rejected-session wording is longer than one
        # line at this width; kept to two rendered lines like every other
        # hint on this page (measured at the dialog's 920px, 688px pane).
        self.stremio_account_status.setWordWrap(True)
        form.addWidget(self.stremio_account_status)

        self.stremio_email_edit = QLineEdit()
        self.stremio_email_edit.setPlaceholderText("Email")
        form.addWidget(self.stremio_email_edit)

        self.stremio_password_edit = QLineEdit()
        self.stremio_password_edit.setPlaceholderText("Password")
        self.stremio_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addWidget(self.stremio_password_edit)

        stremio_account_btn_row = QHBoxLayout()
        self.stremio_connect_btn = QPushButton("Sign In")
        self.stremio_connect_btn.clicked.connect(self._connect_stremio)
        stremio_account_btn_row.addWidget(self.stremio_connect_btn)
        self.stremio_disconnect_btn = QPushButton("Disconnect", objectName="Danger")
        self.stremio_disconnect_btn.clicked.connect(self._disconnect_stremio)
        stremio_account_btn_row.addWidget(self.stremio_disconnect_btn)
        form.addLayout(stremio_account_btn_row)

        stremio_account_hint = QLabel(
            "Fills in how far you have watched. Your password is never stored.",
            objectName="Muted",
        )
        stremio_account_hint.setWordWrap(True)
        form.addWidget(stremio_account_hint)

        form.addSpacing(24)
        progress_note = QLabel(
            "Only Stremio can report watch progress - nobody else publishes it.",
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
            found = [{**g, "launcher": key} for g in launchers.scan_launcher(path, subpath, label)]
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
    def _build_data_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

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
            cb = QCheckBox(name)
            cb.toggled.connect(self._sync_clear_select_all)
            form.addWidget(cb)
            self.clear_checks.append(cb)

        clear_btn = QPushButton("Clear Selected", objectName="Danger")
        clear_btn.clicked.connect(self._clear_checked_categories)
        form.addWidget(clear_btn)

        form.addSpacing(28)
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
            QMessageBox.information(self, "Clear Data", "Check at least one category first.")
            return
        names = ", ".join(name for name, _file, _predicate in checked)
        if QMessageBox.question(
            self, "Clear Data", f"Clear all {names} entries? This cannot be undone."
        ) != QMessageBox.StandardButton.Yes:
            return

        # Anime and Reading share tracker.json - load/save it once for
        # both instead of the second clear stomping the first's result.
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
        QMessageBox.information(self, "Clear Data", f"{names} cleared.")

    def _uninstall(self):
        confirm = QMessageBox.warning(
            self, "Uninstall Atomic",
            "This permanently deletes every saved Atomic file on this PC "
            "(all Anime/Reading/Series/Games/Apps/Websites entries, site "
            "lists, and settings) and removes the app itself. This cannot "
            "be undone.\n\nThe app will close immediately. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
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
        if which == "reading":
            self._refresh_sites()
        else:
            self._refresh_video_sites()
        threading.Thread(target=self._probe_site_worker,
                         args=(which, site_id, dict(site)), daemon=True).start()

    def _probe_site_worker(self, which, site_id, site):
        # Must never raise - a dead thread would leave the row stuck on
        # "checking..." forever.
        module = manga_sites if which == "reading" else anime_sites
        try:
            verdict = module.probe_site(site)
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
            pass
        else:
            _CHECKED_THIS_RUN.add(site_id)
        self._site_probe_signals.done.emit(which, site_id)

    def _on_site_probed(self, which, site_id):
        self._probing_sites.discard(site_id)
        if which == "reading":
            self._refresh_sites()
        else:
            self._refresh_video_sites()

    def _refresh_sites(self):
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
            QMessageBox.information(self, "Reading Websites", "Select a website first.")
            return
        dialog = SiteForm(self, "Website", manga_sites.get_site(site_id))
        if dialog.result_data:
            manga_sites.update_site(site_id, *dialog.result_data)
            self._refresh_sites()
            # Re-checked, not kept: the URL may be the thing that changed.
            self._probe_site_async("reading", site_id)

    def _check_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            QMessageBox.information(self, "Reading Websites", "Select a website first.")
            return
        self._probe_site_async("reading", site_id)

    def _remove_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            QMessageBox.information(self, "Reading Websites", "Select a website first.")
            return
        site = manga_sites.get_site(site_id)
        if QMessageBox.question(self, "Remove Website", f"Remove '{site['name']}'?") == QMessageBox.StandardButton.Yes:
            manga_sites.remove_site(site_id)
            self._refresh_sites()

    # ------------------------------------------------------------------
    def _refresh_video_sites(self):
        self.video_sites_list.clear()
        item = QListWidgetItem("Stremio  —  built-in, always available")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.video_sites_list.addItem(item)
        for site in anime_sites.list_sites():
            item = QListWidgetItem(self._site_label(site))
            item.setData(Qt.ItemDataRole.UserRole, site["id"])
            self.video_sites_list.addItem(item)

    def _selected_video_site_id(self):
        items = self.video_sites_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _add_video_site(self):
        # Same plain base URL as a Reading Website now - the per-site
        # search pattern is worked out in anime_sites, not typed here.
        dialog = SiteForm(self, "Video Website")
        if dialog.result_data:
            site = anime_sites.add_site(*dialog.result_data)
            self._refresh_video_sites()
            self._probe_site_async("video", site["id"])

    def _edit_video_site(self):
        site_id = self._selected_video_site_id()
        if not site_id:
            QMessageBox.information(self, "Video Websites", "Select a website first.")
            return
        dialog = SiteForm(self, "Video Website", anime_sites.get_site(site_id))
        if dialog.result_data:
            anime_sites.update_site(site_id, *dialog.result_data)
            self._refresh_video_sites()
            # Re-checked, not kept: the URL may be the thing that changed.
            self._probe_site_async("video", site_id)

    def _check_video_site(self):
        site_id = self._selected_video_site_id()
        if not site_id:
            QMessageBox.information(self, "Video Websites", "Select a website first.")
            return
        self._probe_site_async("video", site_id)

    def _remove_video_site(self):
        site_id = self._selected_video_site_id()
        if not site_id:
            QMessageBox.information(self, "Video Websites", "Select a website first.")
            return
        site = anime_sites.get_site(site_id)
        if QMessageBox.question(self, "Remove Website", f"Remove '{site['name']}'?") == QMessageBox.StandardButton.Yes:
            anime_sites.remove_site(site_id)
            self._refresh_video_sites()

    def _toggle_startup(self, checked):
        try:
            startup.set_enabled(checked)
        except OSError as exc:
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not checked)
            self.startup_check.blockSignals(False)
            QMessageBox.critical(self, "Settings", f"Couldn't update startup setting:\n{exc}")
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

    def _refresh_stremio_account(self):
        """Three states, not two. "Connected as X" used to be shown for
        the whole life of a saved key, including after Stremio had begun
        refusing it - the account page said connected while every sync
        silently returned nothing, which is the one place the user would
        come to fix it. The third state is read off the tracker's last
        attempt (see _stremio_sign_in_rejected), never re-measured."""
        email, auth_key = app_settings.get_stremio_auth()
        connected = bool(auth_key)
        rejected = connected and _stremio_sign_in_rejected()
        if not connected:
            self.stremio_account_status.setText("Not connected")
        elif rejected:
            self.stremio_account_status.setText(
                f"Connected as {email} — but Stremio is refusing this sign-in, "
                "so no watch progress is syncing. Sign in again below.")
        else:
            self.stremio_account_status.setText(f"Connected as {email}")
        # Muted grey is right for a status nobody needs to act on and
        # wrong for this one; the colour comes from theme so it follows
        # the palette rather than pinning a literal here.
        self.stremio_account_status.setStyleSheet(
            f"color: {theme.WARNING}; background: transparent;" if rejected else "")
        # The sign-in fields come back for a rejected session: telling
        # someone to sign in again while hiding the form behind Disconnect
        # is the same defect one step further on. Disconnect stays too -
        # there is still a stored key, and clearing it is a valid answer.
        self.stremio_email_edit.setVisible(not connected or rejected)
        self.stremio_password_edit.setVisible(not connected or rejected)
        self.stremio_connect_btn.setVisible(not connected or rejected)
        self.stremio_disconnect_btn.setVisible(connected)
        if rejected and not self.stremio_email_edit.text():
            self.stremio_email_edit.setText(email)

    def _connect_stremio(self):
        email = self.stremio_email_edit.text().strip()
        password = self.stremio_password_edit.text()
        if not email or not password:
            QMessageBox.warning(self, "Stremio Account", "Email and password are required.")
            return
        self.stremio_connect_btn.setEnabled(False)
        self.stremio_account_status.setText("Signing in...")
        threading.Thread(target=self._stremio_login_worker, args=(email, password), daemon=True).start()

    def _stremio_login_worker(self, email, password):
        try:
            auth_key = stremio.login(email, password)
            self._login_signals.done.emit(email, auth_key, "")
        except Exception as exc:
            self._login_signals.done.emit("", "", str(exc))

    def _on_stremio_login_done(self, email, auth_key, error):
        self.stremio_connect_btn.setEnabled(True)
        self.stremio_password_edit.clear()
        if error:
            self.stremio_account_status.setText("Not connected")
            QMessageBox.critical(self, "Stremio Account", f"Couldn't sign in:\n{error}")
            return
        global _STREMIO_SIGNED_IN_HERE
        app_settings.set_stremio_auth(email, auth_key)
        # Stremio just issued this key, so whatever the tracker's earlier
        # attempt found is about a key that no longer exists. Without
        # this, the marker's once-per-run stickiness would leave a fresh
        # sign-in reading as rejected until the app restarted.
        _STREMIO_SIGNED_IN_HERE = True
        self.stremio_email_edit.clear()
        self._refresh_stremio_account()

    def _disconnect_stremio(self):
        global _STREMIO_SIGNED_IN_HERE
        app_settings.clear_stremio_auth()
        # Nothing stored, nothing rejected - and the next sign-in from
        # here would otherwise inherit the old verdict.
        _STREMIO_SIGNED_IN_HERE = True
        self.stremio_email_edit.clear()
        self._refresh_stremio_account()

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
        self.setFixedSize(360, 210)
        theme.apply_dark_titlebar(self)

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

        self.exec()

    def _save(self):
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        if not name or not url:
            QMessageBox.warning(self, "Websites", "Name and URL are required.")
            return
        self.result_data = (name, url)
        self.accept()
