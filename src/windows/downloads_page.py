"""Downloads page: what has been queued, what is running, and where it
went.

The whole page is a view over `helpers.downloads.list_groups()` - it owns
no state of its own beyond the folder the user last picked and which
seasons are expanded. Jobs live on disk and the worker thread writes them
there as they change, so this page can be closed, rebuilt, or opened in
the middle of a season without anything being lost.

Rows are *groups*, not jobs. A season is queued as one job per episode
(downloads.queue_season explains why), and drawing 28 bars for one
request buries every other download on the page - so a season is one row
with one averaged bar, expandable to the episodes underneath it when the
one that failed is the thing being looked for.

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
    downloads.PAUSED: "Paused",
}
STATE_COLOUR = {
    downloads.PAUSED: theme.TEXT_MUTED,
    downloads.QUEUED: theme.TEXT_MUTED,
    downloads.RUNNING: theme.ACCENT,
    downloads.DONE: theme.SUCCESS,
    downloads.FAILED: theme.DANGER,
    downloads.CANCELLED: theme.TEXT_DIM,
}
ACTIVE_STATES = (downloads.QUEUED, downloads.RUNNING)

# How far an episode row is inset under its season, so the two read as
# one thing containing another rather than as two downloads.
GROUP_INDENT = 18


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
    # DontUseNativeDialog is not a style choice - it is the fix for the
    # app freezing here. The native Windows folder picker runs its own
    # Win32 modal loop, and while the video player is open mpv owns a
    # native child window in the same top-level; the two message loops
    # deadlock and the app stops responding. Qt's own dialog runs inside
    # Qt's event loop and does not.
    picked = QFileDialog.getExistingDirectory(
        parent, "Download To", start,
        QFileDialog.Option.DontUseNativeDialog)
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
        # row key (job id, or group id for a season) -> the widgets a
        # tick can update without a rebuild. Episode rows inside an
        # expanded season are in here under their own job ids too.
        self._rows = {}
        # Every job's (id, state) as last drawn - the one thing that
        # decides whether a tick can update in place or has to rebuild.
        self._shape = None
        # Which seasons are open. Held here rather than on the row
        # widgets because a state change rebuilds them all, and a season
        # collapsing itself the moment one of its episodes finished
        # would close the list out from under whoever opened it.
        self._expanded = set()

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

        self._render(self._groups())

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
        self._render(self._groups())
        self._sync_indicator()

    # ---- the list -----------------------------------------------------
    def _groups(self):
        try:
            return downloads.list_groups()
        except Exception:
            # A downloads.json this page cannot read must not take the
            # page down with it - an empty list is the honest answer and
            # leaves everything else on the page usable.
            logs.exception("Could not read the download queue")
            return []

    @staticmethod
    def _row_key(row):
        """A season is keyed by its group id, a lone job by its own id.

        Both come off the jobs themselves rather than from the row, so
        this cannot drift from what cancel_group is given."""
        jobs = row.get("jobs") or []
        if not jobs:
            return None
        return jobs[0].get("group") if row.get("kind") == "group" else jobs[0].get("id")

    @staticmethod
    def _shape_of(rows):
        """Still every *job's* state, not the row's: an episode finishing
        inside an expanded season changes that episode's buttons, and a
        row-level shape would leave a Cancel button on something already
        done."""
        return [(job.get("id"), job.get("state"))
                for row in rows for job in (row.get("jobs") or [])]

    @staticmethod
    def _group_state(row):
        """The row's own state, except for a season whose episodes have
        all been cancelled.

        helpers.downloads weighs done/failed/running when it decides a
        group's state and falls through to QUEUED otherwise, so a season
        that was entirely cancelled comes back "queued" - which would
        leave it drawing a progress bar and a Cancel All button that can
        no longer do anything (measured: 5 cancelled episodes, row state
        "queued"). The fix belongs in that function; until it lands the
        jobs are believed over the row."""
        jobs = row.get("jobs") or []
        if (row.get("state") in ACTIVE_STATES and jobs
                and not any(j.get("state") in ACTIVE_STATES for j in jobs)):
            return downloads.CANCELLED
        return row.get("state")

    @staticmethod
    def _any_active(rows):
        return any(job.get("state") in ACTIVE_STATES
                   for row in rows for job in (row.get("jobs") or []))

    def _tick(self):
        rows = self._groups()
        shape = self._shape_of(rows)
        if shape != self._shape:
            self._render(rows)
        else:
            for row in rows:
                self._update_row(self._row_key(row), row.get("progress"),
                                 row.get("detail"))
                for job in row.get("jobs") or []:
                    self._update_row(job.get("id"), job.get("progress"),
                                     job.get("detail"))
        # The sidebar strip is driven from here as well as from its own
        # timer: while this page is open, this tick is the freshest read
        # in the app and the two would otherwise disagree by a poll.
        self._sync_indicator()
        if not self._any_active(rows):
            self._timer.stop()

    def _sync_indicator(self):
        refresh = getattr(self.app, "refresh_download_indicator", None)
        if callable(refresh):
            refresh()

    def _sync_timer(self, rows):
        active = self._any_active(rows)
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()

    def _render(self, rows):
        layout = self.list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparented as well as deleted, and in that order: a
                # deleteLater'd row stays a visible child at its old
                # geometry until the event loop gets to it, which the
                # player's episode list already had to be fixed for.
                # hide() before the unparent - see details._clear_rows:
                # a queued show landing on a parentless widget becomes a
                # framed desktop window, flashing white.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._rows = {}

        if not rows:
            layout.addWidget(QLabel(
                "Nothing has been downloaded yet. Use the download button in "
                "the player or the reader to save an episode, a season or a "
                "chapter here.", objectName="Muted"))
        for row in rows:
            layout.addWidget(self._build_row(row))

        self._shape = self._shape_of(rows)
        self.clear_btn.setEnabled(
            any(job.get("state") not in ACTIVE_STATES
                for row in rows for job in (row.get("jobs") or [])))
        self._sync_timer(rows)

    # ---- pieces a row is made of --------------------------------------
    def _card(self) -> QFrame:
        card = QFrame()
        # Not a widgets.Card: a row is not clickable as a whole (its
        # buttons are), and Card's hover highlight and hand cursor would
        # promise a click that does nothing. Borderless like every list
        # row now (the Harbor pass), so the fill carries the shape alone
        # - and it is SURFACE_HOVER, not PANEL_FILL, because this page's
        # #Panel ground *is* PANEL_FILL and a same-colour slab with no
        # border is invisible (measured on the offscreen render).
        card.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE_HOVER};"
            f" border: none;"
            f" border-radius: {theme.RADIUS}px; }}")
        return card

    @staticmethod
    def _title_row(text, state, *, size="11pt"):
        top = QHBoxLayout()
        top.setSpacing(10)
        label = QLabel(text or "Download")
        label.setWordWrap(True)
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {size}; font-weight: 600;"
            f" background: transparent; border: none;")
        top.addWidget(label, stretch=1)
        badge = QLabel(STATE_TEXT.get(state, str(state or "")))
        badge.setStyleSheet(
            f"color: {STATE_COLOUR.get(state, theme.TEXT_MUTED)};"
            f" font-size: 10pt; font-weight: 700;"
            f" background: transparent; border: none;")
        top.addWidget(badge)
        return top

    @staticmethod
    def _detail_label(text, *, wrap=True) -> QLabel:
        label = QLabel(text or "")
        label.setWordWrap(wrap)
        label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 9.5pt;"
            f" background: transparent; border: none;")
        return label

    def _bar(self, progress, height=6) -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setValue(self._permille(progress))
        bar.setTextVisible(False)
        bar.setFixedHeight(height)
        # The track is SURFACE, one step under the row it sits in - it
        # was SURFACE_HOVER when the rows were SURFACE, and keeping that
        # would sink it into the lifted row fill above.
        bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.SURFACE};"
            f" border: none; border-radius: {height // 2}px; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GRADIENT};"
            f" border-radius: {height // 2}px; }}")
        return bar

    # ---- rows ---------------------------------------------------------
    def _build_row(self, row):
        if row.get("kind") == "group":
            return self._build_group(row)
        return self._build_job(row.get("jobs")[0] if row.get("jobs") else {})

    def _build_job(self, job):
        state = job.get("state")
        card = self._card()
        column = QVBoxLayout(card)
        column.setContentsMargins(14, 11, 14, 11)
        column.setSpacing(6)
        column.addLayout(self._title_row(job.get("label"), state))

        bar = None
        if state == downloads.RUNNING:
            bar = self._bar(job.get("progress"))
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
            detail = self._detail_label(job.get("detail"))
            bottom.addWidget(detail, stretch=1)
            column.addLayout(bottom)
            self._add_job_button(bottom, job, state)

        self._rows[job.get("id")] = (bar, detail)
        return card

    def _add_job_button(self, bottom, job, state):
        if state in ACTIVE_STATES:
            # Pause beside Cancel (the owner's ask). The difference is
            # what happens to the bytes already fetched: pausing keeps
            # them and can be resumed, cancelling gives them up.
            hold = QPushButton("Pause", objectName="Small")
            hold.setToolTip("Hold this download - what has arrived is kept")
            use_hover_cursor(hold)
            hold.clicked.connect(
                lambda _checked=False, i=job.get("id"): self._pause(i))
            bottom.addWidget(hold)
            cancel = QPushButton("Cancel", objectName="Small")
            cancel.setToolTip("Stop this download")
            use_hover_cursor(cancel)
            cancel.clicked.connect(
                lambda _checked=False, i=job.get("id"): self._cancel(i))
            bottom.addWidget(cancel)
        elif state == downloads.PAUSED:
            go = QPushButton("Resume", objectName="Small")
            go.setToolTip("Carry on from where this stopped")
            use_hover_cursor(go)
            go.clicked.connect(
                lambda _checked=False, i=job.get("id"): self._resume(i))
            bottom.addWidget(go)
            cancel = QPushButton("Cancel", objectName="Small")
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

    def _build_group(self, row):
        jobs = row.get("jobs") or []
        group_id = self._row_key(row)
        state = self._group_state(row)

        card = self._card()
        column = QVBoxLayout(card)
        column.setContentsMargins(14, 11, 14, 11)
        column.setSpacing(6)
        column.addLayout(self._title_row(row.get("label"), state))

        bar = None
        if state in ACTIVE_STATES:
            # Drawn while the season is merely queued too, unlike a lone
            # job: two of five episodes already saved is real progress
            # even in the second where nothing is moving.
            bar = self._bar(row.get("progress"))
            column.addWidget(bar)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        detail = self._detail_label(row.get("detail"))
        bottom.addWidget(detail, stretch=1)

        expanded = group_id in self._expanded
        toggle = QPushButton("Hide Episodes" if expanded else "Show Episodes",
                             objectName="Small")
        toggle.setToolTip(f"The {len(jobs)} episodes in this season")
        use_hover_cursor(toggle)
        bottom.addWidget(toggle)

        if state in ACTIVE_STATES:
            hold = QPushButton("Pause All", objectName="Small")
            hold.setToolTip("Hold this season - what has arrived is kept")
            use_hover_cursor(hold)
            hold.clicked.connect(
                lambda _checked=False, g=group_id: self._pause_group(g))
            bottom.addWidget(hold)
            cancel = QPushButton("Cancel All", objectName="Small")
            cancel.setToolTip("Stop every episode in this season")
            use_hover_cursor(cancel)
            cancel.clicked.connect(
                lambda _checked=False, g=group_id: self._cancel_group(g))
            bottom.addWidget(cancel)
        elif state == downloads.PAUSED:
            go = QPushButton("Resume All", objectName="Small")
            go.setToolTip("Carry on with every held episode")
            use_hover_cursor(go)
            go.clicked.connect(
                lambda _checked=False, g=group_id: self._resume_group(g))
            bottom.addWidget(go)
            cancel = QPushButton("Cancel All", objectName="Small")
            use_hover_cursor(cancel)
            cancel.clicked.connect(
                lambda _checked=False, g=group_id: self._cancel_group(g))
            bottom.addWidget(cancel)
        column.addLayout(bottom)

        episodes = QWidget(objectName="Bare")
        inner = QVBoxLayout(episodes)
        inner.setContentsMargins(GROUP_INDENT, 4, 0, 0)
        inner.setSpacing(8)
        for job in jobs:
            inner.addWidget(self._build_episode(job))
        episodes.setVisible(expanded)
        column.addWidget(episodes)
        toggle.clicked.connect(
            lambda _checked=False, g=group_id, w=episodes, b=toggle:
            self._toggle_group(g, w, b))

        self._rows[group_id] = (bar, detail)
        return card

    def _build_episode(self, job):
        """One episode inside an expanded season - still individually
        cancellable, since the reason to open a season is usually the one
        episode that is stuck or failed."""
        state = job.get("state")
        frame = QFrame()
        # Styled explicitly rather than left bare: a stylesheet set on
        # the season card applies to every QFrame under it, so without
        # this an episode gets the card's border and reads as a separate
        # download rather than part of one.
        frame.setStyleSheet("QFrame { background: transparent; border: none; }")
        column = QVBoxLayout(frame)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        line = QHBoxLayout()
        line.setSpacing(8)
        label = QLabel(job.get("label") or "Episode")
        label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 10pt;"
            f" background: transparent; border: none;")
        line.addWidget(label, stretch=1)
        # Not wrapped: an episode's detail is a short speed/peer line and
        # wrapping it would grow the row taller than its own label.
        detail = self._detail_label(job.get("detail"), wrap=False)
        line.addWidget(detail)
        badge = QLabel(STATE_TEXT.get(state, str(state or "")))
        badge.setStyleSheet(
            f"color: {STATE_COLOUR.get(state, theme.TEXT_MUTED)};"
            f" font-size: 9pt; font-weight: 700;"
            f" background: transparent; border: none;")
        line.addWidget(badge)
        self._add_job_button(line, job, state)
        column.addLayout(line)

        bar = None
        if state == downloads.RUNNING:
            bar = self._bar(job.get("progress"), height=4)
            column.addWidget(bar)

        self._rows[job.get("id")] = (bar, detail)
        return frame

    def _toggle_group(self, group_id, container, button):
        showing = not container.isVisible()
        container.setVisible(showing)
        if showing:
            self._expanded.add(group_id)
        else:
            self._expanded.discard(group_id)
        button.setText("Hide Episodes" if showing else "Show Episodes")

    @staticmethod
    def _permille(progress) -> int:
        try:
            fraction = float(progress or 0.0)
        except (TypeError, ValueError):
            fraction = 0.0
        return max(0, min(1000, int(fraction * 1000)))

    def _update_row(self, key, progress, detail_text):
        drawn = self._rows.get(key)
        if not drawn:
            return
        bar, detail = drawn
        if bar is not None:
            bar.setValue(self._permille(progress))
        if detail is not None:
            detail.setText(detail_text or "")

    # ---- actions ------------------------------------------------------
    def _cancel(self, job_id):
        if not job_id:
            return
        downloads.cancel(job_id)
        show_toast(self, "Download Cancelled")
        self._render(self._groups())
        self._sync_indicator()

    def _pause(self, job_id):
        if not job_id:
            return
        downloads.pause(job_id)
        show_toast(self, "Download Paused")
        self._render(self._groups())
        self._sync_indicator()

    def _resume(self, job_id):
        if not job_id:
            return
        downloads.resume(job_id)
        show_toast(self, "Download Resumed")
        self._render(self._groups())
        self._sync_indicator()

    def _pause_group(self, group_id):
        if not group_id:
            return
        downloads.pause_group(group_id)
        show_toast(self, "Season Paused")
        self._render(self._groups())
        self._sync_indicator()

    def _resume_group(self, group_id):
        if not group_id:
            return
        downloads.resume_group(group_id)
        show_toast(self, "Season Resumed")
        self._render(self._groups())
        self._sync_indicator()

    def _cancel_group(self, group_id):
        if not group_id:
            return
        downloads.cancel_group(group_id)
        show_toast(self, "Download Cancelled")
        self._render(self._groups())
        self._sync_indicator()

    def _reveal(self, job):
        if not open_containing_folder(job):
            show_toast(self, "That Folder Is No Longer There")
