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

from . import theme, updater, widgets

# version -> what changed, in the user's terms. Newest first is not
# required; they get sorted by version when shown.
NOTES = {
    "1.8": [
        "Games imported from a launcher now start through that launcher. "
        "Steam sees you playing, Overwatch no longer asks you to sign in "
        "from inside the game, and VALORANT starts at all.",
        "Home has a clock, top right, on the greeting's line.",
        "Home keeps up while you are looking at it: a game you just "
        "played moves to the front of its row, an app you just opened "
        "moves to the top of its list, and dragging the sidebar into a "
        "new order rearranges Home's sections to match.",
        "Games, Apps and Websites fit 13 cards to a row, and 14 while "
        "the sidebar is folded - re-flowing the moment you fold it.",
        "The sideways rows on Anime, Reading and Movies & Series have an "
        "arrow at each end, with the cards fading out beneath them.",
        "Checking a website in Settings searches it for titles you "
        "actually track, instead of one fixed title it may not carry - "
        "which is why sites that open pages perfectly well were being "
        "reported as search-only. Check All now fills in every row.",
        "Two things saving at the same moment can no longer lose one "
        "another's changes.",
        "Searching again straight after clicking a result works. It used "
        "to show nothing until you left the Home page and came back.",
    ],
    "1.7": [
        "The Title field in Add and Edit simply says \"type to search\" - "
        "it no longer names Stremio or your reading websites.",
    ],
    "1.6": [
        "The Anime and Movies & Series icons have swapped places in the "
        "sidebar - Anime is the monitor now, Movies & Series the camera.",
        "Escape leaves a search box instead of only emptying it, so the "
        "cursor is no longer left blinking in a box you are done with.",
    ],
    "1.5": [
        "One search across everything you track. Ctrl+K, or the box "
        "beside the greeting on Home - picking a result opens it, the "
        "same way its own card would.",
        "Settings is now eight categories instead of one long form, with "
        "Uninstall on a page of its own rather than a scroll below the "
        "buttons that clear a single page.",
        "Every page can select several entries at once - set a status or "
        "delete them in one go, with one confirmation and one undo for "
        "the whole batch.",
        "Ctrl+Z undoes the last thing you did and Ctrl+Y puts it back. "
        "The full list of shortcuts is in Settings, under Keybinds.",
        "Your data can be copied to a file before something goes wrong, "
        "and put back from it afterwards.",
        "An update you have not taken yet is offered again at each "
        "launch, instead of being mentioned once and then only as a dot "
        "on the Settings button.",
        "Dragging a card onto another to reorder it works again on the "
        "Anime, Reading and Movies & Series pages.",
        "Each status section scrolls sideways in a row of its own, so a "
        "long section no longer pushes everything else down the page.",
        "Atomic says when Stremio has stopped accepting the saved "
        "sign-in, reopens where you left it, and no longer clips long "
        "card names or closes itself on an empty tracker page.",
    ],
    "1.4": [
        "Films are tracked alongside your shows - the page is now "
        "Movies & Series, and searching in Add or Edit finds films too.",
        "Netflix is now offered on installs that already had video "
        "websites saved, and any watched type can be pinned to it - not "
        "just anime.",
        "Watch progress now comes from Stremio and nothing else. The "
        "Crunchyroll and AniList progress settings are gone: each was "
        "silently wrong often enough to be worse than no number at all.",
        "A filter button beside each tracker's search box narrows the "
        "grid by status, and by type where there is a choice of them.",
        "Hovering a card now says Last Released, and the boxes you type "
        "in say Last Watched - the two used to share one label.",
        "A tick in Add and Edit chooses whether an entry shows a "
        "last-watched number, which brings back the + and - buttons on "
        "the entries you keep by hand.",
        "Opening a page refreshes it. The refresh button and its long "
        "\"Updating...\" toast are gone; results land on the cards as "
        "they arrive.",
        "Move Up and Move Down are gone from every page - drag a card "
        "onto the slot you want instead.",
        "A sequel no longer opens its predecessor's page, and the "
        "garbled characters on the Last Season box, the website dropdown "
        "and the Settings site list are gone.",
    ],
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

    WIDTH = 440
    MARGIN_H = 22

    def __init__(self, parent, current: str, sections: list):
        super().__init__(parent)
        self.setWindowTitle("Atomic Updated")
        self.setMinimumWidth(self.WIDTH)
        theme.apply_dark_titlebar(self)

        body = QVBoxLayout(self)
        body.setContentsMargins(self.MARGIN_H, 18, self.MARGIN_H, 16)
        body.setSpacing(8)

        body.addWidget(QLabel(f"Atomic is now version {current}", objectName="SectionTitle"))
        intro = QLabel("Here's what changed:", objectName="Muted")
        intro.setWordWrap(True)
        body.addWidget(intro)
        body.addSpacing(6)

        # The notes scroll; the heading and the button never do. Without
        # this the dialog is as tall as its notes make it - 1.4's nine
        # measured 720px, and an update crossing 1.2-1.4 measured 1243px,
        # already past the 1104px this desktop can show, with the button
        # off the bottom edge and no way to reach it.
        notes = QWidget()
        notes_body = QVBoxLayout(notes)
        notes_body.setContentsMargins(0, 0, 0, 0)
        notes_body.setSpacing(8)
        for index, (version, lines) in enumerate(sections):
            # Only worth labelling which version a line came from when
            # the update crossed more than one - otherwise the heading
            # just repeats the title above.
            if len(sections) > 1:
                if index:
                    notes_body.addSpacing(6)
                notes_body.addWidget(QLabel(f"Version {version}", objectName="SectionTitle"))
            for line in lines:
                notes_body.addWidget(self._bullet(line))
        # Keeps the notes packed at the top when the area is taller than
        # they are; QLabel grows to fill spare height otherwise.
        notes_body.addStretch()

        area = widgets.scroll_area(notes)
        body.addWidget(area, 1)

        body.addSpacing(14)
        button_row = QHBoxLayout()
        button_row.addStretch()
        done = QPushButton("Got It", objectName="Accent")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        button_row.addWidget(done)
        body.addLayout(button_row)

        self._size_to_notes(area, notes)

    def _size_to_notes(self, area, notes):
        """Give the scroll area exactly the height its notes need, capped
        at what the screen can actually show.

        The height has to be set rather than left to Qt: QScrollArea's
        own sizeHint is *bounded* at 24 line-heights regardless of its
        contents, so a dialog laid out normally would open around 400px
        and scroll even when nothing overflows - and the requirement is
        that today's nine notes still show whole.

        Measured with heightForWidth at the width the text will really
        wrap to, because a word-wrapped QLabel's sizeHint is a guess at a
        line length, not the height it takes here."""
        # Polish first: the section headings get their larger font from
        # the stylesheet, and measuring before that applies reports the
        # default font's line heights - which came out 326px short on a
        # three-version dialog in one run and right in another.
        notes.ensurePolished()
        inner = self.WIDTH - 2 * self.MARGIN_H
        natural = (notes.heightForWidth(inner) if notes.hasHeightForWidth()
                   else notes.sizeHint().height())

        # What the heading, spacings, margins and button take - measured
        # by collapsing the area rather than adding up constants that
        # would drift the moment the layout changes.
        area.setMaximumHeight(0)
        self.layout().activate()
        chrome = self.sizeHint().height()

        # Leave room for the title bar and a margin of desktop, so the
        # dialog lands inside the work area rather than exactly filling
        # it. availableGeometry already excludes the taskbar, and
        # screen() is the parent window's monitor - not the primary one,
        # which on two displays is often not the one Atomic is on.
        budget = self.screen().availableGeometry().height() - 96
        fit = max(120, min(natural, budget - chrome))

        # Min and max together: the max is what caps the dialog (a
        # layout's maximum constrains its window), the min is what stops
        # QScrollArea's bounded sizeHint shrinking it back.
        area.setMinimumHeight(fit)
        area.setMaximumHeight(fit)
        self.layout().activate()
        self.resize(self.sizeHint())

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
