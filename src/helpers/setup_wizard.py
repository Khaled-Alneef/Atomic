"""First-run setup: shown once, over the very first launch's window, to
offer the accounts, keys and preferences that already live in Settings.

Every field is optional and writes through app_settings (or the helper
that owns the value) the moment it changes - the wizard keeps no storage
of its own, so Settings later shows exactly what was entered here.
Finishing, skipping, closing the window, and simply being an existing
install all stamp the same flag (setup_completed_at), so the wizard can
never appear twice however it is left.
"""

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import app_settings, logs, startup, storage, theme
from .widgets import frameless_dialog, scroll_area, use_hover_cursor

# How long after main() the offer fires: enough for the window to have
# actually painted, so the dialog opens over a visible app rather than
# racing its first frame onto an empty desktop.
SHOW_DELAY_MS = 400

STEPS = 4

# The step pager's pills, matching Home's hero dashes.
DOT_HEIGHT = 6
DOT_WIDTH = 18
DOT_WIDTH_ACTIVE = 30
LOGO_HEIGHT = 96

# Same column width as Settings' API Keys page, for the same reason it
# is fixed there: every field starts at one x instead of stepping with
# the service's name.
LABEL_COLUMN_WIDTH = 190

# The saved files whose contents prove this profile is not a fresh
# install. tracker.json alone is not enough - someone who only tracks
# games or websites has never written it.
_ENTRY_FILES = ("tracker.json", "series.json", "games.json",
                "apps.json", "websites.json")

_ICON_PATH = (Path(__file__).resolve().parent.parent
              / "assets" / "atomic_icon.png")


def show_on_first_run(window):
    """main()'s one call: arm the offer, decided only when the timer
    fires. A timer so the dialog appears over a painted window, and armed
    by main() after whats_new's modal summary has returned so it cannot
    fire inside that dialog's nested event loop - the same trap
    schedule_update_check documents."""
    QTimer.singleShot(SHOW_DELAY_MS, lambda: _offer(window))


def _offer(window):
    """Show the wizard once ever, and only to a genuinely fresh install.

    Fails soft: a first launch that cannot decide must still open the
    app, so anything wrong here is logged and swallowed."""
    try:
        if app_settings.get_setup_completed_at():
            return
        if not _install_is_fresh():
            # An existing install updating into the build that introduced
            # this must never see a setup screen - stamp silently. The
            # stamp is also what keeps the check cheap: the file reads
            # below happen once, not on every launch.
            app_settings.set_setup_completed_at(storage.now_iso())
            return
        SetupWizard(window).exec()
    except Exception:
        logs.exception("Could not offer the first-run setup")


def _install_is_fresh() -> bool:
    """No user data anywhere: no setting ever saved and nothing tracked.

    has_run_before rather than "settings.json exists": by the time the
    timer fires, whats_new has already written last_seen_version into a
    genuinely first-run settings.json - has_run_before is the accessor
    that ignores exactly the markers a launch writes by itself."""
    if app_settings.has_run_before():
        return False
    return not any(storage.load(name, []) for name in _ENTRY_FILES)


def _resolution_labels() -> dict:
    """Settings' own display names for the resolution choices, imported
    so the two dropdowns cannot drift apart. Soft, and local rather than
    at module level: settings_dialog pulls in half of helpers, and the
    wizard would rather show the raw values than refuse to open."""
    try:
        from .settings_dialog import RESOLUTION_LABELS
        return RESOLUTION_LABELS
    except Exception:
        return {}


def _downloads_page():
    """windows.downloads_page, or None. Soft and local on purpose:
    helpers must not depend on windows at import time (settings_dialog
    records the same rule for one tuple), and losing the folder row is
    better than the wizard not opening."""
    try:
        from windows import downloads_page
        return downloads_page
    except Exception:
        return None


class SetupWizard(QDialog):
    """Four steps: welcome, API keys, preferences, done.

    No Stremio sign-in in here - the owner asked for it to stay gone
    (the same ask that removed it from Settings; see settings_dialog's
    own note). The account key the tracker reads is restored from a
    backup or set by other means, never typed into a first-run form.

    Constructed without exec() (like whats_new.UpdateSummaryDialog, and
    unlike SettingsDialog) so a test can build one offscreen and drive
    the buttons without a nested event loop - _offer is the caller that
    execs it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Up Atomic")
        # Wide enough that a key hint indented past the caption column
        # keeps a Settings-like measure (~440px) instead of wrapping to
        # four cramped lines at 640.
        self.resize(700, 620)
        self.setMinimumSize(600, 540)

        body = QVBoxLayout(self)
        body.setContentsMargins(28, 20, 28, 16)
        body.setSpacing(12)

        # Step indicator: one pill per step, the current one lit - the
        # same pager language as Home's hero dashes, which is the app's
        # only other "which of N am I on" control. It was a row of
        # bullet glyphs, which sized itself off the font rather than off
        # anything, and read as punctuation left in the layout.
        dots_row = QHBoxLayout()
        dots_row.setSpacing(6)
        dots_row.addStretch()
        self._dots = []
        for _ in range(STEPS):
            dot = QLabel("")
            dot.setFixedHeight(DOT_HEIGHT)
            dots_row.addWidget(dot)
            self._dots.append(dot)
        dots_row.addStretch()
        body.addLayout(dots_row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome_step())
        # The two middle steps scroll: the keys step lists seven key
        # fields and does not fit 600px on every scale factor.
        self.stack.addWidget(scroll_area(self._build_accounts_step()))
        self.stack.addWidget(scroll_area(self._build_preferences_step()))
        self.stack.addWidget(self._build_done_step())
        body.addWidget(self.stack, stretch=1)

        btn_row = QHBoxLayout()
        self.skip_btn = QPushButton("Skip for now")
        # Deliberately quiet next to Back/Next: skipping must be easy but
        # should not compete with the accent Finish. No QSS objectName
        # does "muted button", so the two colours are set here - from
        # theme, per the house rule.
        self.skip_btn.setStyleSheet(
            f"QPushButton {{ color: {theme.TEXT_DIM}; background: transparent;"
            f" border: none; }}"
            f"QPushButton:hover {{ color: {theme.TEXT}; }}")
        self.skip_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.skip_btn)
        btn_row.addStretch()

        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(lambda: self._go(self._step - 1))
        btn_row.addWidget(self.back_btn)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(lambda: self._go(self._step + 1))
        btn_row.addWidget(self.next_btn)
        self.finish_btn = QPushButton("Finish", objectName="Accent")
        self.finish_btn.clicked.connect(self._finish)
        btn_row.addWidget(self.finish_btn)
        body.addLayout(btn_row)

        for btn in (self.skip_btn, self.back_btn, self.next_btn,
                    self.finish_btn):
            use_hover_cursor(btn)

        # No title heading: every step opens with its own SectionTitle
        # ("Welcome to Atomic", ...), and the dots row sits above them.
        frameless_dialog(self)

        self._step = 0
        self._go(0)

    # ------------------------------------------------------------------
    def _go(self, step):
        self._step = max(0, min(STEPS - 1, step))
        self.stack.setCurrentIndex(self._step)
        for index, dot in enumerate(self._dots):
            active = index == self._step
            dot.setFixedWidth(DOT_WIDTH_ACTIVE if active else DOT_WIDTH)
            # Lit along its length, not top-down: at DOT_HEIGHT the
            # vertical ramp's lip is well under a pixel and does not
            # render at all (see theme.accent_gradient's note, and the
            # hero dashes, which hit this first).
            dot.setStyleSheet(
                f"background: {theme.accent_gradient(0, 0, 1, 0) if active else theme.SURFACE_ACTIVE};"
                f" border-radius: {DOT_HEIGHT // 2}px;")
        # Enabled rather than hidden: Back disappearing would shift Next
        # sideways under a pointer mid-click.
        self.back_btn.setEnabled(self._step > 0)
        last = self._step == STEPS - 1
        self.next_btn.setVisible(not last)
        self.finish_btn.setVisible(last)
        # On the last step Finish is the close; a second escape hatch
        # beside it would just be a dimmer copy of the same action.
        self._sync_skip()
        (self.finish_btn if last else self.next_btn).setDefault(True)

    def _has_tmdb_key(self) -> bool:
        """Whether a TMDB key has been given - typed into the wizard now,
        or already saved from an earlier run."""
        edit = self.api_key_edits.get("tmdb")
        if edit is not None and edit.text().strip():
            return True
        try:
            return bool(app_settings.get_api_key("tmdb"))
        except Exception:
            return False

    def _sync_skip(self):
        """**Skip appears only once a TMDB key has been entered** - the
        owner's ask, 23 August 2026: "in the setup window there was a skip
        for now btn, make it appear but after the user enters the TMDB key
        (TMDB KEY IS MUST)".

        Hidden rather than disabled: a greyed-out Skip invites a click and
        explains nothing, while a Skip that appears the moment the key
        lands reads as the key having been accepted. The window's own
        close button still exits, which is deliberate - this is a strong
        nudge toward the one key the app genuinely needs, not a trap with
        no way out."""
        last = self._step == STEPS - 1
        try:
            self.skip_btn.setVisible(not last and self._has_tmdb_key())
        except RuntimeError:
            pass

    def _finish(self):
        # editingFinished only fires on focus-out/Return, so a key typed
        # and immediately Finished-past would be lost without this sweep.
        # Only changed values are written - unconditionally writing all
        # seven would pad settings.json with empty keys.
        for name, edit in self.api_key_edits.items():
            value = edit.text().strip()
            if value != app_settings.get_api_key(name):
                app_settings.set_api_key(name, value)
        self.accept()

    def done(self, result):
        # Finish, Skip, Escape and the title-bar X all pass through here,
        # so however the wizard is left, it is left answered - it must
        # never greet the same install twice.
        try:
            app_settings.set_setup_completed_at(storage.now_iso())
        except Exception:
            logs.exception("Could not record that the setup wizard ran")
        super().done(result)

    # ------------------------------------------------------------------
    def _build_welcome_step(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(8, 8, 8, 8)
        col.setSpacing(10)
        col.addStretch()

        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Scale to physical pixels and tag the result with the screen's
        # scale, or the pixmap is stretched blurry on any non-100%
        # display - same move as the sidebar logo in main.py.
        dpr = QApplication.primaryScreen().devicePixelRatio()
        pixmap = QPixmap(str(_ICON_PATH))
        if not pixmap.isNull():
            pixmap = pixmap.scaledToHeight(
                int(LOGO_HEIGHT * dpr),
                Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
            logo.setPixmap(pixmap)
        col.addWidget(logo)

        title = QLabel("Welcome to Atomic", objectName="SectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(title)

        # A single "&": QLabel only treats an ampersand as a mnemonic
        # marker when it has a buddy, so unlike the QCheckBox case in
        # Settings this one must NOT be doubled - "&&" here draws both.
        intro = QLabel(
            "One dashboard for your anime, reading, movies & series, "
            "games, apps and websites - tracked in your own files, on "
            "this machine.")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(intro)

        later = QLabel(
            "The next steps are optional - everything here can be "
            "changed later in Settings.", objectName="Muted")
        later.setWordWrap(True)
        later.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(later)

        col.addStretch()
        return page

    # ------------------------------------------------------------------
    def _build_accounts_step(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        self.api_key_edits = {}
        first_group = True
        for heading, names in app_settings.API_KEY_GROUPS:
            if not first_group:
                form.addSpacing(18)
            first_group = False
            form.addWidget(QLabel(heading, objectName="SectionTitle"))
            for name in names:
                label, unlocks = app_settings.API_KEYS.get(name, (name, ""))
                row = QHBoxLayout()
                caption = QLabel(label)
                caption.setFixedWidth(LABEL_COLUMN_WIDTH)
                row.addWidget(caption)
                edit = QLineEdit(app_settings.get_api_key(name))
                # Password echo, like Settings: this can be on screen
                # with somebody watching, and a key on screen is a key
                # on screen. "Show keys" below reveals them for checking
                # a paste went in whole.
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                edit.setPlaceholderText(f"Paste your {label} key (optional)")
                edit.editingFinished.connect(
                    lambda n=name: self._save_api_key(n))
                row.addWidget(edit, stretch=1)
                form.addLayout(row)

                help_line = app_settings.API_KEY_HELP.get(name, "")
                if name == "tmdb":
                    # The one key the app already carries - without this
                    # line the field reads as required for artwork.
                    hint_text = ("A key is already bundled with the app - "
                                 "pasting one here only overrides it · "
                                 f"{help_line}")
                else:
                    hint_text = f"{unlocks} · {help_line}"
                # "Get a key" is a real link straight to the page that
                # issues this key (the owner's ask): rich text with
                # openExternalLinks, styled in the accent so it reads as
                # the one clickable thing in a muted hint.
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
                hint.setContentsMargins(LABEL_COLUMN_WIDTH + 8, 0, 0, 4)
                form.addWidget(hint)
                self.api_key_edits[name] = edit
                if name == "tmdb":
                    # Live, not on editingFinished: the Skip button below
                    # is gated on this field having something in it, and a
                    # button that only appears once you click elsewhere
                    # reads as broken.
                    edit.textChanged.connect(lambda _t: self._sync_skip())

        form.addSpacing(12)
        show_keys = QCheckBox("Show keys")
        show_keys.toggled.connect(self._toggle_api_key_echo)
        form.addWidget(show_keys)

        form.addStretch()
        return page

    def _toggle_api_key_echo(self, shown):
        mode = (QLineEdit.EchoMode.Normal if shown
                else QLineEdit.EchoMode.Password)
        for edit in self.api_key_edits.values():
            edit.setEchoMode(mode)

    def _save_api_key(self, name):
        edit = self.api_key_edits.get(name)
        if edit is not None:
            app_settings.set_api_key(name, edit.text().strip())

    # ------------------------------------------------------------------
    def _build_preferences_step(self):
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(4, 4, 12, 4)
        form.setSpacing(6)

        form.addWidget(QLabel("Playback", objectName="SectionTitle"))
        resolution_row = QHBoxLayout()
        resolution_row.addWidget(QLabel("Default resolution"))
        self.resolution_combo = QComboBox()
        labels = _resolution_labels()
        for value in app_settings.RESOLUTION_CHOICES:
            self.resolution_combo.addItem(labels.get(value, value), value)
        index = self.resolution_combo.findData(
            app_settings.get_preferred_resolution())
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
        # currentIndexChanged, not activated - same reasoning as
        # Settings: activated misses a keyboard change.
        self.resolution_combo.currentIndexChanged.connect(
            self._save_resolution)
        resolution_row.addWidget(self.resolution_combo)
        resolution_row.addStretch()
        form.addLayout(resolution_row)
        resolution_hint = QLabel(
            "Which quality the player starts on when a title offers "
            "several. It falls back to the nearest available one.",
            objectName="Muted")
        resolution_hint.setWordWrap(True)
        form.addWidget(resolution_hint)

        form.addSpacing(10)
        self.auto_pick_check = QCheckBox("Auto choose source to play")
        self.auto_pick_check.setChecked(app_settings.get_auto_pick_source())
        self.auto_pick_check.toggled.connect(app_settings.set_auto_pick_source)
        form.addWidget(self.auto_pick_check)
        auto_pick_hint = QLabel(
            "Pressing an episode starts the best source right away, "
            "instead of listing every source to pick from first.",
            objectName="Muted")
        auto_pick_hint.setWordWrap(True)
        form.addWidget(auto_pick_hint)

        downloads_module = _downloads_page()
        self._downloads_module = downloads_module
        if downloads_module is not None:
            form.addSpacing(18)
            form.addWidget(QLabel("Downloads", objectName="SectionTitle"))
            folder_row = QHBoxLayout()
            self.folder_edit = QLineEdit(downloads_module.saved_folder())
            # Read-only: the picker below is the one writer, and it is
            # what persists the choice (choose_folder remembers it).
            self.folder_edit.setReadOnly(True)
            folder_row.addWidget(self.folder_edit, stretch=1)
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(self._browse_download_folder)
            use_hover_cursor(browse_btn)
            folder_row.addWidget(browse_btn)
            form.addLayout(folder_row)
            folder_hint = QLabel(
                "Where downloaded episodes, seasons and chapters are "
                "saved.", objectName="Muted")
            folder_hint.setWordWrap(True)
            form.addWidget(folder_hint)

        form.addSpacing(18)
        form.addWidget(QLabel("Startup", objectName="SectionTitle"))
        self.startup_check = QCheckBox("Launch on Windows startup")
        self.startup_check.setChecked(startup.is_enabled())
        self.startup_check.toggled.connect(self._toggle_startup)
        form.addWidget(self.startup_check)
        startup_hint = QLabel(
            "Starts Atomic automatically when you sign in to Windows.",
            objectName="Muted")
        startup_hint.setWordWrap(True)
        form.addWidget(startup_hint)

        self.fullscreen_check = QCheckBox(
            "Fullscreen mode when launch on startup")
        self.fullscreen_check.setChecked(
            app_settings.get_fullscreen_on_startup())
        self.fullscreen_check.toggled.connect(
            app_settings.set_fullscreen_on_startup)
        form.addWidget(self.fullscreen_check)

        # Normally empty; carries the rolled-back registry failure, which
        # a modal box would make the loudest thing in a wizard where
        # every field is optional.
        self.startup_status = QLabel("")
        self.startup_status.setWordWrap(True)
        self.startup_status.setStyleSheet(
            f"color: {theme.DANGER}; background: transparent;")
        form.addWidget(self.startup_status)
        self._sync_fullscreen_check()

        form.addStretch()
        return page

    def _browse_download_folder(self):
        if self._downloads_module is None:
            return
        picked = self._downloads_module.choose_folder(
            self, self.folder_edit.text())
        if picked:
            self.folder_edit.setText(picked)

    def _toggle_startup(self, checked):
        try:
            startup.set_enabled(checked)
            self.startup_status.setText("")
        except OSError as exc:
            # Roll the box back so it shows what is actually registered,
            # the same recovery Settings does.
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not checked)
            self.startup_check.blockSignals(False)
            self.startup_status.setText(
                f"Couldn't update the startup setting: {exc}")
        self._sync_fullscreen_check()

    def _sync_fullscreen_check(self):
        # Only means anything while there is a startup launch - greys out
        # with the toggle above rather than sitting there ticked and
        # inert. Its saved value is left alone, same as Settings.
        enabled = self.startup_check.isChecked()
        self.fullscreen_check.setEnabled(enabled)
        self.fullscreen_check.setToolTip(
            "" if enabled else "Turn on \"Launch on Windows startup\" first.")

    def _save_resolution(self, index):
        value = self.resolution_combo.itemData(index)
        if value:
            app_settings.set_preferred_resolution(value)

    # ------------------------------------------------------------------
    def _build_done_step(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(8, 8, 8, 8)
        col.setSpacing(10)
        col.addStretch()

        title = QLabel("You're set", objectName="SectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(title)
        outro = QLabel(
            "Atomic works as-is with sensible defaults. Anything you "
            "skipped - the account, keys, preferences - is waiting in "
            "Settings whenever you want it.", objectName="Muted")
        outro.setWordWrap(True)
        outro.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(outro)

        col.addStretch()
        return page
