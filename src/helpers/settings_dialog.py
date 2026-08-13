"""Settings window: a category sidebar (mirroring the main app window's
own sidebar) on the left, the selected category's controls on the right.

General: Windows-startup toggle and which sections show up in the main
sidebar (Anime, Reading, Series, Games, Apps, Websites can each be hidden
without losing their saved data). Anime & Series: which app Anime entries
open in (Stremio or Crunchyroll), the connected Stremio account and/or
AniList username used to pull in real watch progress for either. Reading:
the list of manga/manhwa/manhua reading sites the Reading page can search
and open to, plus an optional music/ambience URL.
"""

import threading

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from . import app_settings, manga_sites, nav_config, startup, stremio, theme
from .widgets import scroll_area

CATEGORIES = ["General", "Anime & Series", "Reading"]


class _StremioLoginSignals(QObject):
    done = Signal(str, str, str)  # email, auth key, error message (one is always "")


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

        self.stack = QStackedWidget()
        self.stack.addWidget(scroll_area(self._build_general_page()))
        self.stack.addWidget(scroll_area(self._build_anime_page()))
        self.stack.addWidget(scroll_area(self._build_reading_page()))
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

        form.addWidget(QLabel("Startup", objectName="SectionTitle"))
        self.startup_check = QCheckBox("Launch on Windows startup")
        self.startup_check.setChecked(startup.is_enabled())
        self.startup_check.toggled.connect(self._toggle_startup)
        form.addWidget(self.startup_check)

        hint = QLabel("Starts Atomic automatically when you sign in to Windows.", objectName="Muted")
        hint.setWordWrap(True)
        form.addWidget(hint)

        form.addSpacing(24)
        form.addWidget(QLabel("Sidebar Sections", objectName="SectionTitle"))
        sections_hint = QLabel(
            "Choose which sections show up in the main sidebar. Hidden "
            "sections keep their saved entries - toggle one back on any "
            "time to bring it back.",
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

        form.addStretch()
        return page

    def _toggle_section_visibility(self, page_name, visible):
        hidden = set(app_settings.get_hidden_sections())
        if visible:
            hidden.discard(page_name)
        else:
            hidden.add(page_name)
        app_settings.set_hidden_sections(hidden)
        main_window = self.parent()
        if main_window is not None and hasattr(main_window, "_refresh_nav_list"):
            main_window._refresh_nav_list()

    # ------------------------------------------------------------------
    def _build_anime_page(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Anime Opens In", objectName="SectionTitle"))
        self.anime_provider_box = QComboBox()
        self.anime_provider_box.addItem("Stremio", "stremio")
        self.anime_provider_box.addItem("Crunchyroll", "crunchyroll")
        current_provider = app_settings.get_anime_provider()
        idx = self.anime_provider_box.findData(current_provider)
        if idx >= 0:
            self.anime_provider_box.setCurrentIndex(idx)
        self.anime_provider_box.currentIndexChanged.connect(self._save_anime_provider)
        form.addWidget(self.anime_provider_box)

        anime_provider_hint = QLabel(
            "Which app an Anime entry opens on double-click. Stremio gets a "
            "direct deep link straight to the title; Crunchyroll doesn't - "
            "its own search isn't something this app can query safely (it's "
            "behind bot protection this app won't try to bypass), so it "
            "opens the title's Crunchyroll page via a best-effort redirect "
            "instead (occasionally a one-click \"you're being redirected\" "
            "page rather than landing there directly). Either way, "
            "suggestions/covers while adding an entry still come from "
            "Stremio's public metadata, and the auto-filled Last Season/"
            "Episode below comes from your connected Stremio account "
            "and/or AniList username further down - both work no matter "
            "which app you actually watch in.",
            objectName="Muted",
        )
        anime_provider_hint.setWordWrap(True)
        form.addWidget(anime_provider_hint)

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
    def _refresh_sites(self):
        self.sites_list.clear()
        for site in manga_sites.list_sites():
            item = QListWidgetItem(f"{site['name']}  —  {site['base_url']}")
            item.setData(Qt.ItemDataRole.UserRole, site["id"])
            self.sites_list.addItem(item)

    def _selected_site_id(self):
        items = self.sites_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _add_site(self):
        dialog = MangaSiteForm(self)
        if dialog.result_data:
            manga_sites.add_site(*dialog.result_data)
            self._refresh_sites()

    def _edit_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            QMessageBox.information(self, "Reading Websites", "Select a website first.")
            return
        dialog = MangaSiteForm(self, manga_sites.get_site(site_id))
        if dialog.result_data:
            manga_sites.update_site(site_id, *dialog.result_data)
            self._refresh_sites()

    def _remove_site(self):
        site_id = self._selected_site_id()
        if not site_id:
            QMessageBox.information(self, "Reading Websites", "Select a website first.")
            return
        site = manga_sites.get_site(site_id)
        if QMessageBox.question(self, "Remove Website", f"Remove '{site['name']}'?") == QMessageBox.StandardButton.Yes:
            manga_sites.remove_site(site_id)
            self._refresh_sites()

    def _toggle_startup(self, checked):
        try:
            startup.set_enabled(checked)
        except OSError as exc:
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not checked)
            self.startup_check.blockSignals(False)
            QMessageBox.critical(self, "Settings", f"Couldn't update startup setting:\n{exc}")

    def _save_anime_provider(self):
        app_settings.set_anime_provider(self.anime_provider_box.currentData())

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

    def _save_anilist_username(self):
        app_settings.set_anilist_username(self.anilist_username_edit.text().strip())

    def _save_manga_music_url(self):
        app_settings.set_manga_music_url(self.manga_music_edit.text().strip())


class MangaSiteForm(QDialog):
    """Add/edit one manga website: just a name + base URL. `result_data`
    is a (name, base_url) tuple after a successful Save, else None."""

    def __init__(self, parent, site=None):
        super().__init__(parent)
        self.result_data = None
        self.setWindowTitle("Edit Website" if site else "Add Website")
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
        self.url_edit = QLineEdit(site["base_url"] if site else "")
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
            QMessageBox.warning(self, "Reading Websites", "Name and URL are required.")
            return
        self.result_data = (name, url)
        self.accept()
