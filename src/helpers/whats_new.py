"""The "what's new" summary shown once after an in-app update.

Updating from Settings replaces the executable and relaunches, which
otherwise gives no sign of what actually changed - the app just reopens
looking identical. This holds a short, plain-language line per change
per version, and the dialog that shows them.

Written for the person using the app, not for the person who wrote it:
no module names, no version-control wording, no "fixed a race in
EntryForm._save". If a line can't be explained in terms of something
the user would notice, it doesn't belong here - internal work that
changes nothing visible is deliberately absent rather than padded in.

Notes are keyed by the version they shipped in, and a jump that skips
versions (1.1 straight to 1.4) shows every version in between, so
nothing is silently missed.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import theme, updater

# version -> what changed, in the user's terms. Newest first is not
# required; they get sorted by version when shown.
NOTES = {
    "1.3": [
        "Netflix titles now open on the show's own page instead of "
        "Netflix's search results.",
        "A Netflix title that isn't available in your country falls back "
        "to search rather than opening a dead page.",
        "Your progress no longer reverts to an older number after "
        "switching between the Anime and Reading pages.",
        "An episode number you have typed in yourself now shows on the "
        "card once you save it.",
    ],
    "1.2": [
        "Anime and series now open on their own page, instead of dropping "
        "you on the website's search results.",
        "Crunchyroll titles open straight to the show as well.",
        "Adding a video or reading website now only needs the site's "
        "address - no more typing a search link by hand.",
        "Fixed Atomic closing itself about 20 seconds after opening.",
        "Fixed the app freezing when a website replied with something "
        "unexpected.",
        "Background lookups no longer slow down the rest of your internet.",
    ],
    "1.1": [
        "Drag a card onto another to reorder it, on any page.",
        "Optional full screen when Atomic opens with Windows.",
        "The Settings icon no longer changes shape when the sidebar folds.",
    ],
}


def notes_between(previous: str, current: str) -> list:
    """[(version, [lines])] for every version newer than `previous` and
    no newer than `current`, newest first. Empty when there is nothing
    to show - an unchanged version, a downgrade, or versions with no
    notes written for them.

    Compared with updater.parse_version so the ordering is the numeric
    one the updater itself uses, not string order (1.10 above 1.9)."""
    if not previous or not current:
        return []
    low, high = updater.parse_version(previous), updater.parse_version(current)
    if low >= high:
        return []
    found = [(version, lines) for version, lines in NOTES.items()
             if low < updater.parse_version(version) <= high and lines]
    return sorted(found, key=lambda item: updater.parse_version(item[0]), reverse=True)


class UpdateSummaryDialog(QDialog):
    """What changed, shown once, right after an update's relaunch.

    A dialog rather than a toast on purpose: this is the one moment the
    user has just asked for an update and is waiting to learn what they
    got. Toasts here disappear on their own, and this is the whole point
    of the relaunch (see the house rule in .claude/rules/ui.md - dialogs
    are for what the user must not miss)."""

    def __init__(self, parent, current: str, sections: list):
        super().__init__(parent)
        self.setWindowTitle("Atomic Updated")
        self.setMinimumWidth(440)
        theme.apply_dark_titlebar(self)

        body = QVBoxLayout(self)
        body.setContentsMargins(22, 18, 22, 16)
        body.setSpacing(8)

        body.addWidget(QLabel(f"Atomic is now version {current}", objectName="SectionTitle"))
        intro = QLabel("Here's what changed:", objectName="Muted")
        intro.setWordWrap(True)
        body.addWidget(intro)
        body.addSpacing(6)

        for index, (version, lines) in enumerate(sections):
            # Only worth labelling which version a line came from when
            # the update crossed more than one - otherwise the heading
            # just repeats the title above.
            if len(sections) > 1:
                if index:
                    body.addSpacing(6)
                body.addWidget(QLabel(f"Version {version}", objectName="SectionTitle"))
            for line in lines:
                body.addWidget(self._bullet(line))

        body.addSpacing(14)
        button_row = QHBoxLayout()
        button_row.addStretch()
        done = QPushButton("Got It", objectName="Accent")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        button_row.addWidget(done)
        body.addLayout(button_row)

    @staticmethod
    def _bullet(text: str) -> QWidget:
        """A hanging-indent bullet: the dot stays put while the wrapped
        text lines up under itself, which a single wrapped QLabel with a
        literal bullet character does not do."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 1, 0, 1)
        layout.setSpacing(8)
        dot = QLabel("•")
        dot.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(dot)
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label, 1)
        return row


def current_release_notes() -> list:
    """[(version, lines)] for the newest version at or below this build -
    what to show when the app has clearly been updated but the version
    it came from is unknowable. Empty if this build has no notes."""
    here = updater.parse_version(updater.APP_VERSION)
    known = [v for v in NOTES if updater.parse_version(v) <= here and NOTES[v]]
    if not known:
        return []
    newest = max(known, key=updater.parse_version)
    return [(newest, NOTES[newest])]


def _sections_to_show(app_settings) -> list:
    """What this launch should show, from three narrowing cases.

    The explicit marker is the accurate one, but it only exists if the
    build being replaced was new enough to write it. The releases before
    that wrote neither marker, so an update from one of them arrives
    looking exactly like a first-ever launch apart from one thing: the
    profile already holds settings from those earlier runs. That's the
    third case, and without it the very first update into this feature
    would silently show nothing."""
    previous = app_settings.take_updated_from()
    if previous:
        return notes_between(previous, updater.APP_VERSION)

    # No marker: an exe swapped by hand, or a build too old to leave one.
    last_seen = app_settings.get_last_seen_version()
    if last_seen:
        return notes_between(last_seen, updater.APP_VERSION)

    # Never recorded a version, but has run here before - an upgrade from
    # a build predating any of this. Which version it was can't be
    # recovered, so show this release's own notes rather than nothing.
    if app_settings.has_run_before():
        return current_release_notes()

    return []  # genuine first install: nothing has changed *for them*


def show_if_updated(parent):
    """Show the summary once, if this launch followed an update. Always
    records the running version on the way out, so the next launch is
    silent whether or not anything was shown here.

    Fails soft: anything wrong (unreadable settings, a version string
    that won't parse) must not stop the app opening, hence the wrap."""
    try:
        from . import app_settings
        sections = _sections_to_show(app_settings)
        app_settings.set_last_seen_version(updater.APP_VERSION)
        if sections:
            UpdateSummaryDialog(parent, updater.APP_VERSION, sections).exec()
    except Exception:
        pass
