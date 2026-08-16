"""Settings window: a category sidebar (mirroring the main app window's
own sidebar) on the left, the selected category's controls on the right.

General: Windows-startup toggle (plus whether that sign-in launch opens
full screen), and which sections show up in the main
sidebar (Anime, Reading, Series, Games, Apps, Websites can each be hidden
without losing their saved data). Anime & Series: the list of Video
Websites Anime entries can be set to open on (Stremio is always
available as a built-in option; Crunchyroll and any others are
addable/editable, the same way Reading sites work), the connected
Stremio account and/or AniList username used to pull in real watch
progress. Reading: the list of manga/manhwa/manhua reading sites the
Reading page can search and open to, plus an optional music/ambience URL.
Games: each game launcher's install directory, so the Games page can
bulk-import every game it finds there (see helpers.launchers) instead of
adding each one by hand. Data: wipe one content category's saved entries
at a time, or uninstall the app entirely (every saved file plus the app
itself).
"""

import threading

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import (
    anime_sites, app_settings, crunchyroll, launchers, manga_sites, nav_config,
    startup, storage, stremio, theme, uninstall, updater,
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


class _CrunchyrollSignals(QObject):
    done = Signal(str, str, str, str)  # email, refresh token, account id, error


class _SiteProbeSignals(QObject):
    done = Signal(str, str)  # which list ("reading"/"video"), site name


# What probe_site's verdicts mean to someone looking at the list. The
# distinction that matters: a site with a known engine opens straight to
# a title's own page, everything else only ever opens that site's search.
_RESOLVES_LABELS = {
    "engine": "opens title pages",
    "generic": "opens title pages (read off its search page)",
    "streaming": "opens title pages",
    "search-only": "search links only - no title pages",
    "unreachable": "didn't answer when checked",
    "unknown": "couldn't be checked",
    "checking": "checking...",
}


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

        self._crunchyroll_signals = _CrunchyrollSignals()
        self._crunchyroll_signals.done.connect(self._on_crunchyroll_login_done)

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
        self._refresh_crunchyroll_account()
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
            "Updates come straight from the project's GitHub repository - no "
            "reinstalling, and your saved entries are left alone.",
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
            "Only applies to that sign-in launch - opening Atomic yourself "
            "still starts it maximized, and F11 or Escape leaves full "
            "screen either way.",
            objectName="Muted",
        )
        fullscreen_hint.setWordWrap(True)
        form.addWidget(fullscreen_hint)
        self._sync_fullscreen_startup_check()

        form.addSpacing(24)
        form.addWidget(QLabel("Sections", objectName="SectionTitle"))
        sections_hint = QLabel(
            "Choose which sections show up in the app. Hidden sections "
            "keep their saved entries - toggle one back on any time to "
            "bring it back.",
            objectName="Muted",
        )
        sections_hint.setWordWrap(True)
        form.addWidget(sections_hint)

        hidden = set(app_settings.get_hidden_sections())
        self.section_checks = {}
        for name, page_name in nav_config.ordered_nav_items():
            cb = QCheckBox(name)
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
            "Off by default, where unticking a section above only drops "
            "its sidebar entry and Home still previews it. Tick this to "
            "leave hidden sections off Home as well - their preview row, "
            "quick list, and any \"Continue\" slides all go with them.",
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
            "Which app/site an Anime entry can be set to open on double-"
            "click, picked per-entry (Add/Edit Entry > Video Website) the "
            "same way Reading Websites work for Manga. \"Stremio\" is "
            "always available and gets a direct deep link straight to the "
            "title; Crunchyroll and anything else you add here open a "
            "search on that site instead, since their search isn't "
            "something this app can query safely (behind bot protection "
            "this app won't try to bypass). Either way, suggestions/"
            "covers while adding an entry still come from Stremio's "
            "public metadata, and the auto-filled Last Season/Episode "
            "below comes from your connected Stremio account and/or "
            "AniList username further down - both work no matter which "
            "site the entry actually opens on.",
            objectName="Muted",
        )
        video_sites_hint.setWordWrap(True)
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
        check_video_site_btn.setToolTip("Search this site for a known title to see "
                                        "whether it opens title pages or only search links")
        check_video_site_btn.clicked.connect(self._check_video_site)
        video_sites_btn_row.addWidget(check_video_site_btn)
        remove_video_site_btn = QPushButton("Remove", objectName="Danger")
        remove_video_site_btn.clicked.connect(self._remove_video_site)
        video_sites_btn_row.addWidget(remove_video_site_btn)
        form.addLayout(video_sites_btn_row)

        form.addSpacing(24)
        form.addWidget(QLabel("Stremio Account", objectName="SectionTitle"))

        self.stremio_account_status = QLabel("", objectName="Muted")
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
            "Lets Anime/Series entries auto-fill with your real watch "
            "progress (not just the newest episode out) when you pick a "
            "Stremio match, for anything already in your Stremio library. "
            "Your password is sent once to Stremio's own sign-in and "
            "never stored - only the resulting session stays saved.",
            objectName="Muted",
        )
        stremio_account_hint.setWordWrap(True)
        form.addWidget(stremio_account_hint)

        form.addSpacing(24)
        form.addWidget(QLabel("Crunchyroll Account", objectName="SectionTitle"))

        self.crunchyroll_status = QLabel("", objectName="Muted")
        self.crunchyroll_status.setWordWrap(True)
        form.addWidget(self.crunchyroll_status)

        self.crunchyroll_email_edit = QLineEdit()
        self.crunchyroll_email_edit.setPlaceholderText("Email")
        form.addWidget(self.crunchyroll_email_edit)

        self.crunchyroll_password_edit = QLineEdit()
        self.crunchyroll_password_edit.setPlaceholderText("Password")
        self.crunchyroll_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addWidget(self.crunchyroll_password_edit)

        crunchyroll_btn_row = QHBoxLayout()
        self.crunchyroll_connect_btn = QPushButton("Sign In")
        self.crunchyroll_connect_btn.clicked.connect(self._connect_crunchyroll)
        crunchyroll_btn_row.addWidget(self.crunchyroll_connect_btn)
        self.crunchyroll_disconnect_btn = QPushButton("Disconnect", objectName="Danger")
        self.crunchyroll_disconnect_btn.clicked.connect(self._disconnect_crunchyroll)
        crunchyroll_btn_row.addWidget(self.crunchyroll_disconnect_btn)
        form.addLayout(crunchyroll_btn_row)

        crunchyroll_hint = QLabel(
            "Reads your real Crunchyroll progress from Crunchyroll itself, "
            "for entries whose Video Website is Crunchyroll - so they no "
            "longer borrow a number from Stremio or wait on AniList. Your "
            "password is sent once to Crunchyroll's own sign-in and never "
            "stored; only the session token is saved.\n\n"
            "Crunchyroll publishes no public API and issues no key to other "
            "apps, so signing in needs a client credential set in "
            "settings.json (crunchyroll_client_id / crunchyroll_client_secret). "
            "Without one this will say so rather than fail vaguely. Using it "
            "is against Crunchyroll's terms of service.",
            objectName="Muted",
        )
        crunchyroll_hint.setWordWrap(True)
        form.addWidget(crunchyroll_hint)

        form.addSpacing(24)
        form.addWidget(QLabel("AniList Username", objectName="SectionTitle"))
        self.anilist_username_edit = QLineEdit(app_settings.get_anilist_username())
        self.anilist_username_edit.setPlaceholderText("Your AniList username")
        self.anilist_username_edit.editingFinished.connect(self._save_anilist_username)
        form.addWidget(self.anilist_username_edit)

        anilist_hint = QLabel(
            "A second, independent source for real Anime progress - tried "
            "for Anime (not Series/Reading) if a Stremio match doesn't "
            "have your progress, so it also covers Crunchyroll-provider "
            "entries. Just your public username, no login: your AniList "
            "profile's list visibility has to be set to public for this "
            "to see anything. Leave blank to skip.",
            objectName="Muted",
        )
        anilist_hint.setWordWrap(True)
        form.addWidget(anilist_hint)

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
            "Sites the Reading page searches for direct links and can open "
            "entries straight to, the way Stremio does for Anime/Series. "
            "Most manga/manhwa/manhua sites work automatically - just add "
            "the name and URL.",
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
        check_site_btn.setToolTip("Search this site for a known title to see "
                                  "whether it opens title pages or only search links")
        check_site_btn.clicked.connect(self._check_site)
        sites_btn_row.addWidget(check_site_btn)
        remove_site_btn = QPushButton("Remove", objectName="Danger")
        remove_site_btn.clicked.connect(self._remove_site)
        sites_btn_row.addWidget(remove_site_btn)
        form.addLayout(sites_btn_row)

        form.addSpacing(24)
        form.addWidget(QLabel("Reading Music URL", objectName="SectionTitle"))
        self.manga_music_edit = QLineEdit(app_settings.get_manga_music_url())
        self.manga_music_edit.setPlaceholderText("https://example.com/lofi-playlist")
        self.manga_music_edit.editingFinished.connect(self._save_manga_music_url)
        form.addWidget(self.manga_music_edit)

        manga_music_hint = QLabel(
            "Opened alongside a reading page whenever you double-click an "
            "entry - handy for a music/ambience site to play while you "
            "read. Leave blank to skip.",
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
            "Point at each launcher's install folder - for Steam, the "
            r"folder containing steamapps (e.g. G:\Steam); for the "
            "others, the folder each game gets its own subfolder under - "
            "and every game found there is added to the Games page "
            "automatically, the moment you set or change it. Games "
            "already there (matched by path) are never duplicated, so "
            "it's safe to re-check any time new games are installed. "
            "Leave blank to skip a launcher.",
            objectName="Muted",
        )
        hint.setWordWrap(True)
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
            "Check one or more categories, then Clear Selected to wipe "
            "just their saved entries. Site lists (Video/Reading Websites) "
            "and the rest of Settings are untouched. This cannot be undone.",
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
            "Deletes every saved Atomic file on this PC - all entries, "
            "site lists, and settings - and removes the app itself. This "
            "cannot be undone, and the app closes immediately after.",
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
        entry opened a search page."""
        label = f"{site['name']}  —  {site['base_url']}"
        state = "checking" if site["id"] in self._probing_sites else site.get("resolves")
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
            pass
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
        email, auth_key = app_settings.get_stremio_auth()
        connected = bool(auth_key)
        self.stremio_account_status.setText(f"Connected as {email}" if connected else "Not connected")
        self.stremio_email_edit.setVisible(not connected)
        self.stremio_password_edit.setVisible(not connected)
        self.stremio_connect_btn.setVisible(not connected)
        self.stremio_disconnect_btn.setVisible(connected)

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
        app_settings.set_stremio_auth(email, auth_key)
        self.stremio_email_edit.clear()
        self._refresh_stremio_account()

    def _disconnect_stremio(self):
        app_settings.clear_stremio_auth()
        self._refresh_stremio_account()

    def _refresh_crunchyroll_account(self):
        session = app_settings.get_crunchyroll_session()
        connected = bool(session)
        self.crunchyroll_status.setText(
            f"Connected as {session['email']}" if connected else "Not connected")
        self.crunchyroll_email_edit.setVisible(not connected)
        self.crunchyroll_password_edit.setVisible(not connected)
        self.crunchyroll_connect_btn.setVisible(not connected)
        self.crunchyroll_disconnect_btn.setVisible(connected)

    def _connect_crunchyroll(self):
        email = self.crunchyroll_email_edit.text().strip()
        password = self.crunchyroll_password_edit.text()
        if not email or not password:
            QMessageBox.warning(self, "Crunchyroll Account",
                                "Email and password are required.")
            return
        self.crunchyroll_connect_btn.setEnabled(False)
        self.crunchyroll_status.setText("Signing in...")
        threading.Thread(target=self._crunchyroll_login_worker,
                         args=(email, password), daemon=True).start()

    def _crunchyroll_login_worker(self, email, password):
        # Must never raise - a dead thread leaves "Signing in..." forever.
        # The password stays in this frame and is never handed to the
        # signal, so it cannot reach anything that persists.
        try:
            session = crunchyroll.login(email, password)
            self._crunchyroll_signals.done.emit(
                email, session.get("refresh_token", ""),
                str(session.get("account_id", "")), "")
        except Exception as exc:
            self._crunchyroll_signals.done.emit("", "", "", str(exc))

    def _on_crunchyroll_login_done(self, email, refresh_token, account_id, error):
        self.crunchyroll_connect_btn.setEnabled(True)
        self.crunchyroll_password_edit.clear()
        if error:
            self.crunchyroll_status.setText("Not connected")
            QMessageBox.critical(self, "Crunchyroll Account",
                                 f"Couldn't sign in:\n{error}")
            return
        app_settings.set_crunchyroll_session(email, refresh_token, account_id)
        crunchyroll.forget_cached_history()
        self.crunchyroll_email_edit.clear()
        self._refresh_crunchyroll_account()

    def _disconnect_crunchyroll(self):
        app_settings.clear_crunchyroll_session()
        self._refresh_crunchyroll_account()

    def _save_anilist_username(self):
        app_settings.set_anilist_username(self.anilist_username_edit.text().strip())

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
