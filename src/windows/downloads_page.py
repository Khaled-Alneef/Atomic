"""Downloads page: what has been queued, what is running, and where it
went.

The whole page is a view over `helpers.downloads.list_jobs()` - it owns
no state of its own beyond the folder the user last picked. Jobs live on
disk and the worker thread writes them there as they change, so this page
can be closed, rebuilt, or opened in the middle of a season without
anything being lost.

Polled rather than signalled, on a timer that stops itself: the download
worker is a plain daemon thread with no Qt in it (deliberately - it has
to keep running with no window open), so there is no signal to connect
to. One `list_jobs()` per second while something is moving costs a read
of a list already in memory; when nothing is queued or running the timer
is stopped entirely, so an idle page is genuinely idle.

Rows are updated in place rather than rebuilt on each tick. A rebuild
every second would delete the Cancel button under the pointer twice
before it could be pressed, and would drop the hand cursor with it
(.claude/rules/ui.md - a widget holds that cursor only while the pointer
is really inside it). The layout is only rebuilt when the *set* of jobs
or one of their states changes, which is what actually changes the row's
shape.
"""

import os

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from helpers import downloads, logs, storage, theme
from helpers.widgets import GlassPage, scroll_area, show_toast, use_hover_cursor

# How often the job list is re-read while anything is moving. The worker
# updates a running video roughly every 1.5s and a chapter once per page,
# so a faster poll would only re-render the same numbers.
POLL_MS = 1000

# Where downloads land, remembered between sessions.
#
# Kept here rather than in helpers/app_settings.py on instruction (that
# module was out of scope for this change), so it reads and writes the
# same settings.json by hand - the identical read-modify-write every
# accessor in app_settings does. If this ever moves, it is two functions
# and one key, and the stored value is compatible either way.
SETTINGS_FILE = "settings.json"
FOLDER_KEY = "download_folder"

# What each state is called in front of the user, and the colour that
# says it at a glance. Title case, like every other message in the app.
STATE_TEXT = {
    downloads.QUEUED: "Waiting",
    downloads.RUNNING: "Downloading",
    downloads.DONE: "Done",
    downloads.FAILED: "Failed",
    downloads.CANCELLED: "Cancelled",
}
STATE_COLOUR = {
    downloads.QUEUED: theme.TEXT_MUTED,
    downloads.RUNNING: theme.ACCENT,
    downloads.DONE: theme.SUCCESS,
    downloads.FAILED: theme.DANGER,
    downloads.CANCELLED: theme.TEXT_DIM,
}
ACTIVE_STATES = (downloads.QUEUED, downloads.RUNNING)


def saved_folder() -> str:
    """The folder the user last downloaded into, or the default one."""
    stored = storage.load(SETTINGS_FILE, {})
    folder = stored.get(FOLDER_KEY) if isinstance(stored, dict) else None
    return str(folder) if folder else downloads.default_folder()


def remember_folder(path: str):
    if not path:
        return
    data = storage.load(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[FOLDER_KEY] = str(path)
    storage.save(SETTINGS_FILE, data)


def choose_folder(parent, current: str = None) -> str:
    """Ask for a destination and remember it. "" when cancelled.

    Shared with the player's and the reader's download dialogs so the
    three of them cannot drift apart on which folder they seed from -
    the last one used, which is nearly always the one wanted again."""
    start = current or saved_folder()
    picked = QFileDialog.getExistingDirectory(parent, "Download To", start)
    if not picked:
        return ""
    remember_folder(picked)
    return picked


def open_containing_folder(job: dict) -> bool:
    """Show the finished file's folder in Explorer.

    The folder, never the file itself: `os.startfile` on a .cbz or an
    .mkv hands it to whatever is registered for that extension, which is
    a comic reader or a video player opening - not what a button next to
    a finished download means. A chapter also lands one level deeper than
    the job's folder (downloads._run_chapter puts each title in its own
    subfolder), so this follows the recorded path rather than the folder
    that was asked for."""
    path = job.get("path") or ""
    folder = os.path.dirname(path) if path else (job.get("folder") or "")
    if not folder or not os.path.isdir(folder):
        return False
    try:
        os.startfile(folder)
    except OSError:
        logs.exception("Could not open the download folder")
        return False
    return True


class DownloadsPage(GlassPage):
    """The queue, newest first."""

    def __init__(self, app):
        super().__init__(parent=None)
        self.app = app
        # job id -> the widgets a tick can update without a rebuild.
        self._rows = {}
        # (id, state) for every job as last drawn - the one thing that
        # decides whether a tick can update in place or has to rebuild.
        self._shape = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)

        panel = QFrame(objectName="Panel")
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(QLabel("Downloads", objectName="PanelTitle"))
        header.addStretch()
        self.clear_btn = QPushButton("Clear Finished")
        self.clear_btn.setFixedHeight(40)
        self.clear_btn.setToolTip(
            "Remove everything that has finished, failed or been cancelled")
        use_hover_cursor(self.clear_btn)
        self.clear_btn.clicked.connect(self._clear_finished)
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        layout.addLayout(self._build_folder_row())

        self.list_body = QWidget(objectName="Bare")
        self.list_layout = QVBoxLayout(self.list_body)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(10)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(scroll_area(self.list_body), stretch=1)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)

        self._render(self._jobs())

    # ---- chrome -------------------------------------------------------
    def _build_folder_row(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Saving to:", objectName="Muted"))
        self.folder_label = QLabel(saved_folder())
        self.folder_label.setToolTip(saved_folder())
        row.addWidget(self.folder_label)
        row.addStretch()
        change = QPushButton("Change Folder")
        change.setFixedHeight(40)
        change.setToolTip("Where new downloads are saved")
        use_hover_cursor(change)
        change.clicked.connect(self._change_folder)
        row.addWidget(change)
        return row

    def _change_folder(self):
        picked = choose_folder(self)
        if not picked:
            return
        self.folder_label.setText(picked)
        self.folder_label.setToolTip(picked)
        show_toast(self, "Download Folder Changed")

    def _clear_finished(self):
        downloads.clear_finished()
        self._render(self._jobs())

    # ---- the list -----------------------------------------------------
    def _jobs(self):
        try:
            return downloads.list_jobs()
        except Exception:
            # A downloads.json this page cannot read must not take the
            # page down with it - an empty list is the honest answer and
            # leaves everything else on the page usable.
            logs.exception("Could not read the download queue")
            return []

    def _tick(self):
        jobs = self._jobs()
        shape = [(job.get("id"), job.get("state")) for job in jobs]
        if shape != self._shape:
            self._render(jobs)
            return
        for job in jobs:
            self._update_row(job)
        if not any(job.get("state") in ACTIVE_STATES for job in jobs):
            self._timer.stop()

    def _sync_timer(self, jobs):
        active = any(job.get("state") in ACTIVE_STATES for job in jobs)
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()

    def _render(self, jobs):
        layout = self.list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparented as well as deleted, and in that order: a
                # deleteLater'd row stays a visible child at its old
                # geometry until the event loop gets to it, which the
                # player's episode list already had to be fixed for.
                widget.setParent(None)
                widget.deleteLater()
        self._rows = {}

        if not jobs:
            layout.addWidget(QLabel(
                "Nothing has been downloaded yet. Use the download button in "
                "the player or the reader to save an episode, a season or a "
                "chapter here.", objectName="Muted"))
        for job in jobs:
            layout.addWidget(self._build_row(job))

        self._shape = [(job.get("id"), job.get("state")) for job in jobs]
        self.clear_btn.setEnabled(
            any(job.get("state") not in ACTIVE_STATES for job in jobs))
        self._sync_timer(jobs)

    def _build_row(self, job):
        state = job.get("state")
        card = QFrame()
        # Not a widgets.Card: a row is not clickable as a whole (its two
        # buttons are), and Card's hover highlight and hand cursor would
        # promise a click that does nothing.
        card.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_SM}px; }}")
        column = QVBoxLayout(card)
        column.setContentsMargins(14, 11, 14, 11)
        column.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        label = QLabel(job.get("label") or "Download")
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 11pt; font-weight: 600;"
            f" background: transparent; border: none;")
        top.addWidget(label, stretch=1)

        badge = QLabel(STATE_TEXT.get(state, str(state or "")))
        badge.setStyleSheet(
            f"color: {STATE_COLOUR.get(state, theme.TEXT_MUTED)};"
            f" font-size: 10pt; font-weight: 700;"
            f" background: transparent; border: none;")
        top.addWidget(badge)
        column.addLayout(top)

        bar = None
        if state == downloads.RUNNING:
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(self._permille(job))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {theme.SURFACE_HOVER};"
                f" border: none; border-radius: 3px; }}"
                f"QProgressBar::chunk {{ background: {theme.ACCENT};"
                f" border-radius: 3px; }}")
            column.addWidget(bar)

        # Skipped entirely when there is nothing to put in it - a
        # cancelled job has no detail line and no button, and an empty
        # label still reserves its line height, which left those rows
        # looking like they had lost something (measured on a grab: 20px
        # of blank under "Cancelled").
        has_button = state in ACTIVE_STATES or state == downloads.DONE
        detail = None
        if has_button or job.get("detail"):
            bottom = QHBoxLayout()
            bottom.setSpacing(8)
            detail = QLabel(job.get("detail") or "")
            detail.setWordWrap(True)
            detail.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
                f" background: transparent; border: none;")
            bottom.addWidget(detail, stretch=1)
            column.addLayout(bottom)

        if state in ACTIVE_STATES:
            cancel = QPushButton("Cancel", objectName="Small")
            cancel.setToolTip("Stop this download")
            use_hover_cursor(cancel)
            cancel.clicked.connect(
                lambda _checked=False, i=job.get("id"): self._cancel(i))
            bottom.addWidget(cancel)
        elif state == downloads.DONE:
            reveal = QPushButton("Open Folder", objectName="Small")
            reveal.setToolTip("Show this file in Explorer")
            use_hover_cursor(reveal)
            reveal.clicked.connect(
                lambda _checked=False, j=dict(job): self._reveal(j))
            bottom.addWidget(reveal)

        self._rows[job.get("id")] = (bar, detail)
        return card

    @staticmethod
    def _permille(job) -> int:
        try:
            fraction = float(job.get("progress") or 0.0)
        except (TypeError, ValueError):
            fraction = 0.0
        return max(0, min(1000, int(fraction * 1000)))

    def _update_row(self, job):
        drawn = self._rows.get(job.get("id"))
        if not drawn:
            return
        bar, detail = drawn
        if bar is not None:
            bar.setValue(self._permille(job))
        if detail is not None:
            detail.setText(job.get("detail") or "")

    # ---- actions ------------------------------------------------------
    def _cancel(self, job_id):
        if not job_id:
            return
        downloads.cancel(job_id)
        show_toast(self, "Download Cancelled")
        self._render(self._jobs())

    def _reveal(self, job):
        if not open_containing_folder(job):
            show_toast(self, "That Folder Is No Longer There")
