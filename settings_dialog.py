"""Small Settings popup: Windows-startup toggle, which app Anime entries
open in (Stremio or Crunchyroll), the connected Stremio account and/or
AniList username used to pull in real watch progress for either, and the
list of manga/manhwa/manhua reading sites the Reading page can search
and open to."""

import threading

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

import app_settings
import manga_sites
import startup
import theme
import stremio
from widgets import scroll_area


class _StremioLoginSignals(QObject):
    done = Signal(str, str, str)  # email, auth key, error message (one is always "")


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(440, 640)
        theme.apply_dark_titlebar(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(10)
        outer.addWidget(QLabel("Settings", objectName="PanelTitle"))

        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 10, 0)
        form.setSpacing(6)
        outer.addWidget(scroll_area(body), stretch=1)

        self.startup_check = QCheckBox("Launch on Windows startup")
        self.startup_check.setChecked(startup.is_enabled())
        self.startup_check.toggled.connect(self._toggle_startup)
        form.addWidget(self.startup_check)

        hint = QLabel("Starts Atomic automatically when you sign in to Windows.", objectName="Muted")
        hint.setWordWrap(True)
        form.addWidget(hint)

        form.addSpacing(18)
        form.addWidget(QLabel("Anime Opens In"))
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
            "its search isn't something this app can query safely (it's "
            "behind bot protection this app won't try to bypass), so it "
            "opens a Crunchyroll search for the title instead. Either way, "
            "suggestions/covers while adding an entry still come from "
            "Stremio's public metadata, and the auto-filled Last Season/"
            "Episode below comes from your connected Stremio account "
            "and/or AniList username further down - both work no matter "
            "which app you actually watch in.",
            objectName="Muted",
        )
        anime_provider_hint.setWordWrap(True)
        form.addWidget(anime_provider_hint)

        form.addSpacing(18)
        form.addWidget(QLabel("Stremio Account"))
        self._login_signals = _StremioLoginSignals()
        self._login_signals.done.connect(self._on_stremio_login_done)

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
        self._refresh_stremio_account()

        form.addSpacing(18)
        form.addWidget(QLabel("AniList Username"))
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

        form.addSpacing(18)
        form.addWidget(QLabel("Reading Websites"))
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
        self.sites_list.setFixedHeight(120)
        self.sites_list.itemDoubleClicked.connect(self._edit_site)
        form.addWidget(self.sites_list)

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
        self._refresh_sites()

        form.addSpacing(18)
        form.addWidget(QLabel("Reading Music URL"))
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

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self.exec()

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
