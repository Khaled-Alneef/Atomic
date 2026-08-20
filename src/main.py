"""Atomic - single-window shell with a persistent sidebar.

Home / Anime / Reading / Series / Games / Apps / Websites each live in the
same content area, swapped with a vertical slide transition whose
direction mirrors the sidebar: moving to an item further down the list
slides up from below, moving to one further up slides down from above -
matching how "scrolling down" a page works. Back/Forward history works
by Alt+Left/Right or the mouse side buttons (X1/X2), same as a browser,
even though every section is also one click away in the sidebar; those
also slide by sidebar position, not by history direction.

Home is pinned at the top; the rest of the sidebar is a drag-to-reorder
list (windows.home.HomePage mirrors whatever order the user picks here).
Since the transition direction is computed fresh from the saved nav
order on every navigation, dragging the sidebar into a new order updates
the slide direction immediately too - nothing to keep in sync by hand.
"""

import sys
import threading
from pathlib import Path

from helpers import (app_settings, downloads, global_search, images, logs,
                     native_cursor, startup, storage, theme, updater,
                     whats_new)
from helpers.nav_config import HOME_ITEM, nav_position, visible_nav_items
from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal as Signal,
)
from PyQt6.QtGui import QCursor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QMessageBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from helpers.settings_dialog import SettingsDialog
from helpers.widgets import (release_stale_hover_cursors, show_toast,
                             take_live_redo, take_live_undo, use_hover_cursor)
from windows import home as home_page_module
from windows import link_grid as link_grid_module
from windows import tracker as tracker_module
from windows.apps import AppsPage
from windows.downloads_page import DownloadsPage
from windows.games import GamesPage
from windows.home import HomePage
from windows.tracker import AnimePage, MangaPage, SeriesPage
from windows.websites import WebsitesPage

APP_DIR = Path(__file__).resolve().parent

# How often the pointer is checked for movement, to re-derive its cursor
# when it has moved. Fast enough that a wrong cursor is never on screen
# long enough to notice, and each check that finds no movement is a
# single coordinate comparison. See MainWindow._cursor_watchdog_tick.
CURSOR_WATCHDOG_MS = 120

# The image sizes each page renders at, for the startup prewarm (see
# _prewarm_image_specs) - read off the pages themselves so a size
# changed there can't quietly leave the prewarm decoding the wrong one.
tracker_poster_size = tracker_module.POSTER_SIZE
home_poster_size = home_page_module.POSTER_SIZE
home_hero_cover_size = home_page_module.HERO_COVER_SIZE
home_icon_size = home_page_module.ICON_SIZE
home_row_icon_size = home_page_module.ROW_ICON_SIZE
games_icon_size = link_grid_module.THUMB_SIZE
link_thumb_size = link_grid_module.THUMB_SIZE

PAGES = {
    "home": HomePage,
    "anime": AnimePage,
    "manga": MangaPage,
    "series": SeriesPage,
    "games": GamesPage,
    "apps": AppsPage,
    "websites": WebsitesPage,
    "downloads": DownloadsPage,
}

# label, page to jump to, action to run on that page once it's showing
ADD_ITEMS = [
    ("Anime Entry", "anime", lambda page: page._open_form()),
    ("Reading Entry", "manga", lambda page: page._open_form()),
    ("Movie or Series Entry", "series", lambda page: page._open_form()),
    ("Game", "games", lambda page: page._add_game()),
    ("App", "apps", lambda page: page._open_add_form()),
    ("Website", "websites", lambda page: page._open_add_form()),
]

ANIM_DURATION_MS = 220
LOGO_HEIGHT = 120

# How long after the window is up the startup update check waits before
# asking GitHub. Launch is already the busiest moment this app has - the
# image prewarm is decoding covers and whichever page is showing may be
# firing its own backfill through lookup_pool - and nobody is waiting on
# this answer, so it goes last rather than competing for the connection.
UPDATE_CHECK_DELAY_MS = 4000

# The accent dot drawn over the Settings button while an update is
# waiting, and how far in from the button's top-right corner it sits.
# 5, not 7: the collapsed rail is only 36px of button, and at 7 the dot
# landed on the gear glyph's top-right edge rather than beside it
# (measured off a grab of the folded sidebar).
UPDATE_DOT_SIZE = 8
UPDATE_DOT_MARGIN = 5

# How long the window has to sit still before its size/position is
# written out. A resize or a drag emits a continuous stream of events -
# saving on each one would rewrite settings.json dozens of times per
# gesture - and nothing reads the value again until the next launch, so
# there is no reason to be prompt. closeEvent flushes whatever is still
# pending, so a window closed mid-gesture loses nothing.
GEOMETRY_SAVE_DELAY_MS = 400

# Room kept above a restored window's client area when clamping it onto
# a screen. geometry() is the client rectangle, so the title bar lives
# *above* its top edge: clamping straight to available.y() would push
# that bar off the screen and leave nothing to drag the window by, which
# is the exact failure the clamp exists to prevent. Measured 31px on
# this Windows 11 machine at 100% scale; 48 leaves headroom for larger
# scale factors, and it only ever applies to a window being rescued from
# off-screen coordinates.
TITLE_BAR_ALLOWANCE = 48

# The download strip under the Downloads nav button, and how often it is
# re-read. 4px so it reads as a progress strip rather than a second
# button in the rail; hidden entirely when nothing is downloading.
#
# Polled, like the Downloads page itself, and for the same reason: the
# download worker is a plain daemon thread with no Qt in it (deliberately
# - it keeps running with no window open), so there is no signal to
# connect to. It is also the only way a download queued from the player
# or the reader can reach the sidebar at all, which is why the idle poll
# does not stop the way the page's does - active_progress() reads a list
# already in memory, so 2.5s of nothing costs nothing.
DOWNLOAD_BAR_HEIGHT = 4
DOWNLOAD_POLL_MS = 1000
DOWNLOAD_IDLE_POLL_MS = 2500

SIDEBAR_WIDTH = 220
# Wide enough for the nav bullets and the +/gear buttons once the text
# labels are dropped (see _set_sidebar_collapsed).
SIDEBAR_COLLAPSED_WIDTH = 68
SIDEBAR_ANIM_MS = 180


class _UpdateCheckSignals(QObject):
    # The startup check runs off the UI thread; this carries its answer
    # back onto it. Nothing but the update dict (or None) crosses - a
    # failed check has nothing to say (see _update_check_worker).
    found = Signal(object)


class NavListWidget(QListWidget):
    """A QListWidget sized to fit its rows instead of stretching to fill
    the sidebar - the leftover space should go to the trailing stretch,
    not swallow it into empty list rows. Measures the real per-row
    height (via sizeHintForRow) rather than guessing a constant - the
    emoji glyphs in each row's text pull in taller font metrics than
    the base UI font, so a fixed guess clips rows off the bottom."""

    def sizeHint(self):
        width = super().sizeHint().width()
        if self.count() == 0:
            return QSize(width, 0)
        row_height = self.sizeHintForRow(0)
        total = row_height * self.count() + self.spacing() * (self.count() + 1)
        # Safety margin: the selected-item QSS border renders right at
        # the edge of the last row's box, and without enough slack the
        # list widget clips its own bottom border/corners off.
        return QSize(width, total + 12)

    def minimumSizeHint(self):
        return self.sizeHint()

    def wheelEvent(self, event):
        # This list is always sized to show every row - there is nothing
        # to scroll to. QAbstractItemView scrolls on the wheel regardless
        # of scrollbar visibility, which without this reads as the whole
        # sidebar "jiggling" under the cursor for no reason.
        event.ignore()

    def scrollTo(self, index, hint=QAbstractItemView.ScrollHint.EnsureVisible):
        # QAbstractItemView auto-scrolls to keep the clicked/selected row
        # visible - normally invisible, but if this list is ever a few
        # pixels taller than the space it's actually given, that "helpful"
        # scroll snaps the view down and hides the row(s) above it. This
        # list should never move at all, for any reason.
        pass


def _fit_to_available_screen(rect):
    """`rect` moved, and shrunk if it has to be, until it sits wholly
    inside the usable area of a screen that exists right now.

    A geometry saved on a monitor that has since been unplugged is what
    this is for: the coordinates still describe a perfectly valid
    rectangle, just one nobody can see or reach, and restoring it as-is
    opens Atomic somewhere off the desktop with no title bar to drag it
    back by. The screen chosen is whichever one the saved rectangle
    overlaps most, so a window that merely straddled a monitor edge stays
    where the user put it; with no overlap anywhere - the unplugged
    monitor - the primary screen takes it.

    availableGeometry(), not geometry(): the taskbar's strip is not
    somewhere a window should be restored underneath.
    """
    best = None
    best_area = 0
    for screen in QApplication.screens():
        overlap = screen.availableGeometry().intersected(rect)
        area = overlap.width() * overlap.height()
        if area > best_area:
            best, best_area = screen, area
    if best is None:
        best = QApplication.primaryScreen()
    if best is None:
        # No screens at all is not a real desktop state, but reporting a
        # rectangle is still better than raising during startup.
        return QRect(rect)
    available = best.availableGeometry()
    width = min(rect.width(), available.width())
    height = min(rect.height(), available.height() - TITLE_BAR_ALLOWANCE)
    x = min(max(rect.x(), available.x()), available.right() - width + 1)
    y = min(max(rect.y(), available.y() + TITLE_BAR_ALLOWANCE),
            available.bottom() - height + 1)
    return QRect(x, y, width, height)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Atomic")
        # Before the first resize()/setGeometry() below, because both
        # deliver events that move/resizeEvent answer by starting this
        # timer - built any later and startup raises AttributeError.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.timeout.connect(self._save_window_geometry)
        # The size a profile with nothing saved opens at - see
        # _restore_window_geometry, which overrides it for every launch
        # after the first.
        self.resize(1280, 840)
        # The window explicitly owns the plain arrow, rather than leaving
        # it as "no cursor set". Without an explicit cursor anywhere in
        # the chain, Qt has nothing to hand Windows when the pointer sits
        # over ordinary content, so it makes no native cursor call at all
        # and Windows simply keeps painting whichever cursor was last
        # set - which is how the pointing hand from a button or card
        # could stay on screen indefinitely. Owning the arrow means every
        # move onto plain content actively restores it.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        theme.apply_dark_titlebar(self)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_sidebar())

        self.container = QWidget()
        root_layout.addWidget(self.container, stretch=1)

        self._history = ["home"]
        self._history_index = 0
        self._current_page = None
        self._anim_group = None
        self._was_maximized = False
        self._last_pointer_pos = QCursor.pos()
        self._cursor_watchdog = QTimer(self)
        self._cursor_watchdog.timeout.connect(self._cursor_watchdog_tick)
        self._cursor_watchdog.start(CURSOR_WATCHDOG_MS)

        self._update_signals = _UpdateCheckSignals()
        self._update_signals.found.connect(self._on_update_found)

        self._restore_rect = None
        self._open_maximized = self._restore_window_geometry()

        self._show_page("home", animate=False)

        # Application-wide, so it sees the container's own resize events
        # too (see eventFilter), not just this window's.
        QApplication.instance().installEventFilter(self)
        QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

    # ------------------------------------------------------------------
    def _build_sidebar(self):
        sidebar = QWidget(objectName="Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        self.sidebar = sidebar
        self._sidebar_collapsed = False
        # Set before anything styles the Settings button, which reads it
        # for its tooltip. Filled in by the startup update check.
        self._pending_update_version = ""
        # Same, for the Downloads button: how many downloads are running
        # and what to say about them. Both empty until the first poll.
        self._download_count = 0
        self._download_tooltip = ""
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(4)

        # Collapse/expand toggle, pinned top-right of the sidebar so it
        # stays put (and stays reachable) at either width.
        self.fold_btn = QPushButton("«", objectName="FoldButton")
        self.fold_btn.setFixedSize(28, 28)
        self.fold_btn.setToolTip("Collapse sidebar")
        use_hover_cursor(self.fold_btn)
        self.fold_btn.clicked.connect(self._toggle_sidebar)
        fold_row = QHBoxLayout()
        fold_row.setContentsMargins(0, 0, 0, 0)
        fold_row.addStretch()
        fold_row.addWidget(self.fold_btn)
        layout.addLayout(fold_row)

        # Logo, centered - the artwork already has the "Atomic" wordmark
        # built in, so it's the whole brand header on its own (no separate
        # text label) - anchoring the top of the sidebar, with the nav
        # list/Add button below pushed down to make room for it (see the
        # extra spacing further down).
        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Scale to LOGO_HEIGHT *physical* pixels (source PNG is 1672px tall,
        # plenty of headroom) and tag the result with the screen's DPI
        # scale, or Qt stretches the merely-260px-tall pixmap to fill a
        # 260*scale screen area on any non-100%-scaled display (125% here)
        # and it comes out visibly blurry.
        dpr = QApplication.primaryScreen().devicePixelRatio()
        logo_pixmap = QPixmap(str(APP_DIR / "atomic_icon.png")).scaledToHeight(
            int(LOGO_HEIGHT * dpr), Qt.TransformationMode.SmoothTransformation)
        logo_pixmap.setDevicePixelRatio(dpr)
        logo_label.setPixmap(logo_pixmap)
        self.logo_label = logo_label
        layout.addWidget(logo_label)

        layout.addSpacing(16)

        # Home as a single-row NavList of its own - not a QPushButton -
        # so it's rendered by exactly the same widget type/QSS rules as
        # every other nav item below it (a QPushButton, even styled to
        # match on paper, doesn't compute the same box-model height/
        # padding as a QListWidgetItem, so it never quite lined up).
        # Kept as a separate list rather than folded into nav_list itself
        # so it stays put above the user's drag-to-reorder order instead
        # of becoming a reorderable/draggable row.
        home_name, home_page = HOME_ITEM
        self.home_list = NavListWidget(objectName="NavList")
        self.home_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.home_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.home_list.setFrameShape(QFrame.Shape.NoFrame)
        self.home_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.home_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.home_list.setSpacing(2)  # matches nav_list's item inset, else Home renders edge-to-edge
        self.home_list.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # QListWidget's item delegate paints with the widget's own font(),
        # not the ::item QSS font-family - the stylesheet rule alone gets
        # silently ignored for list items, so it has to be set here too.
        self.home_list.setFont(theme.nav_font())
        home_item = QListWidgetItem()
        self.home_list.addItem(home_item)
        self._style_nav_item(home_item, home_name, home_page)
        self.home_list.itemClicked.connect(lambda: self.navigate_to("home"))
        # NavListWidget.sizeHint() pads in a generous +12 "safety margin"
        # below the last row (see its docstring) - fine for nav_list's
        # multi-row case, but on a single-item list that margin is just
        # dead space, widening the visible gap before the next item well
        # past the ~spacing()px gap between every other pair of items.
        # A minimal explicit height (one row + its own top/bottom inset)
        # keeps that gap consistent instead.
        self._sync_home_list_height()
        layout.addWidget(self.home_list)

        self.nav_list = NavListWidget(objectName="NavList")
        self.nav_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.nav_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.nav_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav_list.setFrameShape(QFrame.Shape.NoFrame)
        self.nav_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.nav_list.setSpacing(2)
        self.nav_list.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.nav_list.setFont(theme.nav_font())
        self._populate_nav_list()
        self.nav_list.itemClicked.connect(self._on_nav_item_clicked)
        self.nav_list.model().rowsMoved.connect(self._on_nav_reordered)
        self.nav_list.updateGeometry()
        layout.addWidget(self.nav_list)

        # Add and Settings both sit at the very bottom, Add directly
        # above Settings, with the stretch above them pushing the pair
        # down clear of the nav list.
        layout.addStretch()

        # Downloads sits with Settings rather than in the nav list above:
        # it is a utility view over a queue, not one of the user's
        # sections, and the nav list is their own drag-to-reorder order -
        # a row appearing in the middle of it would be one they never put
        # there. A NavButton for the same reason Settings is one: it is
        # already proven to render at both sidebar widths.
        self.downloads_btn = QPushButton(objectName="NavButton")
        self.downloads_btn.setFixedHeight(34)
        self.downloads_btn.setCheckable(True)
        use_hover_cursor(self.downloads_btn)
        self.downloads_btn.clicked.connect(lambda: self.navigate_to("downloads"))
        # A notification dot in the button's top-right corner while
        # anything is downloading (the owner's ask) - readable at a
        # glance where the count in the label is not, and the only
        # signal the folded rail has room for. A child at a fixed
        # offset, not a layout row, so nothing in the column moves when
        # it appears. Mouse-transparent: it sits on its own button.
        self.downloads_dot = QLabel(self.downloads_btn)
        self.downloads_dot.setFixedSize(9, 9)
        self.downloads_dot.setStyleSheet(
            f"background: {theme.ACCENT}; border: 1px solid {theme.BG};"
            f" border-radius: 4px;")
        self.downloads_dot.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.downloads_dot.hide()
        self._style_downloads_btn()
        layout.addWidget(self.downloads_btn)

        # Progress where it can be seen without opening the page - the
        # thing actually asked for. A slim accent strip directly under
        # the Downloads button, plus the count in that button's own
        # label; the season and episode being downloaded are in the
        # tooltip, since there is no room for a title at 220px and none
        # at all in the folded rail.
        #
        # Hidden rather than zeroed when nothing is running: a bar
        # sitting at 0% reads as a download that is stuck, and an idle
        # sidebar should look exactly as it did before this existed. It
        # is the only thing in the column below the stretch that changes
        # height, so showing it lifts the Downloads button by its own
        # 4px + the layout's 4px spacing and leaves Add/Settings put.
        self.downloads_bar = QProgressBar()
        self.downloads_bar.setRange(0, 1000)
        self.downloads_bar.setTextVisible(False)
        self.downloads_bar.setFixedHeight(DOWNLOAD_BAR_HEIGHT)
        self.downloads_bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.SURFACE_HOVER};"
            f" border: none; border-radius: {DOWNLOAD_BAR_HEIGHT // 2}px; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GRADIENT};"
            f" border-radius: {DOWNLOAD_BAR_HEIGHT // 2}px; }}")
        self.downloads_bar.hide()
        layout.addWidget(self.downloads_bar)

        self._downloads_timer = QTimer(self)
        self._downloads_timer.timeout.connect(self.refresh_download_indicator)
        self._downloads_timer.start(DOWNLOAD_IDLE_POLL_MS)

        self.add_btn = QPushButton("+", objectName="AddButton")
        self.add_btn.setFixedHeight(34)
        self.add_btn.setToolTip("Add")
        use_hover_cursor(self.add_btn)
        # setMenu, not clicked -> menu.exec(...): Qt then places the popup
        # itself, on the screen the button is actually on. Positioning it
        # by hand means mapToGlobal, which returns coordinates divided by
        # the *other* screen's scale factor on a mixed-DPI pair - the same
        # trap that once put toasts 200px off (.claude/rules/ui.md). The
        # tracker's filter button is built this way for the same reason.
        self._add_menu = QMenu(self)
        self._add_menu.aboutToShow.connect(self._build_add_menu)
        self.add_btn.setMenu(self._add_menu)
        layout.addWidget(self.add_btn)

        self.settings_btn = QPushButton(objectName="NavButton")
        # Matches the Add button above it, and gives the collapsed gear
        # glyph room to render without being clipped.
        self.settings_btn.setFixedHeight(34)
        use_hover_cursor(self.settings_btn)
        self.settings_btn.clicked.connect(self._open_settings)
        self._style_settings_btn()
        layout.addWidget(self.settings_btn)

        # The "an update is waiting" marker: a plain accent dot in the
        # button's corner, hidden until the startup check finds one. A
        # child widget rather than a character appended to the button's
        # text, because that text is drawn in the Segoe icon font at both
        # sidebar widths and a dot glyph there is one more codepoint that
        # can come out as a missing-glyph box on a machine without it.
        # Transparent to the mouse so the button underneath keeps its own
        # hover highlight and hand cursor (.claude/rules/ui.md - never
        # leave a cursor set on something that isn't handling the click).
        self._update_dot = QLabel(self.settings_btn)
        self._update_dot.setFixedSize(UPDATE_DOT_SIZE, UPDATE_DOT_SIZE)
        self._update_dot.setStyleSheet(
            f"background: {theme.ACCENT}; border-radius: {UPDATE_DOT_SIZE // 2}px;")
        self._update_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._update_dot.hide()

        # A download left running from the last session (or queued by a
        # player window opened before this one) should be visible on the
        # first frame, not after the first poll.
        self.refresh_download_indicator()

        return sidebar

    def _position_update_dot(self):
        """Top-right corner of the Settings button. Re-run on every resize
        of that button (see eventFilter) rather than placed once: folding
        the sidebar animates its width from 220 to 68, and a dot placed at
        the old width would sit outside the collapsed rail."""
        button = self.settings_btn
        self._update_dot.move(
            button.width() - UPDATE_DOT_SIZE - UPDATE_DOT_MARGIN, UPDATE_DOT_MARGIN)

    # ------------------------------------------------------------------
    def _style_nav_item(self, item, name, page_name):
        """Expanded rows keep the bullet+label they always had; collapsed
        ones swap to that section's own glyph, centred in the rail, with
        the label moved to a tooltip since there's no room to show it."""
        if self._sidebar_collapsed:
            item.setText(theme.NAV_ICONS.get(page_name, theme.NAV_BULLET))
            item.setFont(theme.icon_font())
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setToolTip(name)
        else:
            item.setText(f"{theme.NAV_BULLET}  {name}")
            item.setFont(theme.nav_font())
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip("")

    def _sync_home_list_height(self):
        """The single-row Home list is pinned to its row height, which
        changes when that row swaps between the nav and icon fonts - so
        it has to be re-measured whenever the sidebar folds."""
        self.home_list.setFixedHeight(
            self.home_list.sizeHintForRow(0) + self.home_list.spacing() * 2)

    def _style_downloads_btn(self):
        """Same two-width treatment as the Settings button below it: glyph
        and label when the sidebar is open, glyph alone in the rail with
        the label moved to a tooltip.

        The glyph lives here rather than in theme.NAV_ICONS because this
        entry is not one of the reorderable nav sections that table feeds
        - written as an escape, not the character itself, since a private
        -use codepoint does not survive a tool re-encoding this file
        (CLAUDE.md records that happening twice)."""
        collapsed = self._sidebar_collapsed
        glyph = ""   # Download, Segoe Fluent Icons
        # The count goes in the label only when the label is showing; the
        # folded rail has no room for it, which is what the strip under
        # the button is for there. A live download's title outranks the
        # usual tooltip at either width, the same way a waiting update
        # does on Settings below.
        count = f" ({self._download_count})" if self._download_count else ""
        self.downloads_btn.setText(
            glyph if collapsed else f"  {glyph}   Downloads{count}")
        self.downloads_btn.setFont(theme.icon_font() if collapsed else theme.icon_font(10))
        self.downloads_btn.setToolTip(
            self._download_tooltip or ("Downloads" if collapsed else ""))
        self.downloads_btn.setProperty("collapsed", collapsed)
        self.downloads_btn.style().unpolish(self.downloads_btn)
        self.downloads_btn.style().polish(self.downloads_btn)

    def refresh_download_indicator(self):
        """Re-read what is downloading and show (or hide) the strip.

        Called by this window's own timer and, while it is open, by the
        Downloads page's 1s tick as well - that tick is the freshest read
        in the app and the two would otherwise disagree by a poll.

        Nothing at all when active_progress() is None: no strip, no
        count, and the tooltip back to plain "Downloads"."""
        try:
            active = downloads.active_progress()
            # A season whose episodes were all cancelled still reports
            # "queued" (helpers.downloads decides a group's state from
            # its done/failed/running counts and falls through to
            # QUEUED), which would leave this strip up forever after a
            # Cancel All - measured on 5 cancelled episodes. Believing
            # the jobs costs one more copy of a list already in memory;
            # when that function is fixed this guard never fires.
            if active is not None and not any(
                    job.get("state") in (downloads.QUEUED, downloads.RUNNING)
                    for job in downloads.list_jobs()):
                active = None
        except Exception:
            # A downloads.json that cannot be read must not take the
            # sidebar with it - no indicator is the honest answer, and
            # the Downloads page says the same thing for the same reason.
            logs.exception("Could not read download progress")
            active = None

        if active is None:
            self.downloads_bar.hide()
            self.downloads_dot.hide()
            count, tooltip = 0, ""
        else:
            # The dot rides the button's own top-right corner; placed
            # here because the button's width depends on the sidebar
            # state and this is the one place that runs on every change.
            self.downloads_dot.move(self.downloads_btn.width() - 13, 3)
            self.downloads_dot.show()
            self.downloads_dot.raise_()
            count = int(active.get("count") or 0)
            try:
                fraction = float(active.get("progress") or 0.0)
            except (TypeError, ValueError):
                fraction = 0.0
            self.downloads_bar.setValue(max(0, min(1000, int(fraction * 1000))))
            self.downloads_bar.show()
            label = active.get("label") or "Download"
            extra = f" (+{count - 1} more)" if count > 1 else ""
            tooltip = f"{label} - {int(round(fraction * 100))}%{extra}"

        # Only when it actually changed: restyling unpolishes and
        # re-polishes the button, and doing that every second would make
        # it flicker under the pointer for no new information.
        if count != self._download_count or tooltip != self._download_tooltip:
            self._download_count = count
            self._download_tooltip = tooltip
            self._style_downloads_btn()

        interval = DOWNLOAD_POLL_MS if active else DOWNLOAD_IDLE_POLL_MS
        if self._downloads_timer.interval() != interval:
            self._downloads_timer.setInterval(interval)

    def _style_settings_btn(self):
        collapsed = self._sidebar_collapsed
        # Same glyph either way. Expanded used to show the ⚙ emoji, which
        # is a different symbol drawn in its own fixed colors - folding the
        # sidebar swapped the icon out from under you. SETTINGS_ICON is
        # monochrome and inherits the button's color, so it works at both
        # widths; it just renders at the label's 10.5pt here rather than
        # the collapsed rail's 14pt, matching the size the emoji had.
        self.settings_btn.setText(
            theme.SETTINGS_ICON if collapsed else f"  {theme.SETTINGS_ICON}   Settings")
        self.settings_btn.setFont(theme.icon_font() if collapsed else theme.icon_font(10))
        # A waiting update outranks the usual tooltip at either width -
        # the dot says something is there, this says what. Expanded, the
        # button already reads "Settings", so there is otherwise nothing
        # to add and the tooltip stays empty.
        if self._pending_update_version:
            self.settings_btn.setToolTip(
                f"Atomic {self._pending_update_version} is available")
        else:
            self.settings_btn.setToolTip("Settings" if collapsed else "")
        # Drives the [collapsed="true"] QSS rule; Qt only re-evaluates
        # property-based selectors after an explicit unpolish/polish.
        self.settings_btn.setProperty("collapsed", collapsed)
        self.settings_btn.style().unpolish(self.settings_btn)
        self.settings_btn.style().polish(self.settings_btn)

    def _toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        collapsed = self._sidebar_collapsed

        self.fold_btn.setText("»" if collapsed else "«")
        self.fold_btn.setToolTip("Expand sidebar" if collapsed else "Collapse sidebar")
        self.logo_label.setVisible(not collapsed)
        self._style_downloads_btn()
        self._style_settings_btn()

        # Restyled in place rather than rebuilt, so the user's drag order
        # and the current selection both survive the fold.
        home_name, home_page = HOME_ITEM
        self._style_nav_item(self.home_list.item(0), home_name, home_page)
        self._sync_home_list_height()
        for row, (name, page_name) in enumerate(visible_nav_items()):
            item = self.nav_list.item(row)
            if item is not None:
                self._style_nav_item(item, name, page_name)
        self.nav_list.updateGeometry()

        # The card grids fit one more card per row against the folded
        # rail (link_grid.grid_columns), so whatever is showing re-flows
        # now rather than looking wrong until the next time it is opened.
        # Before the animation, not after it: the column count is decided
        # by the fold, not by the width it is currently passing through.
        relayout = getattr(self._current_page, "relayout_for_sidebar", None)
        if callable(relayout):
            relayout()

        target = SIDEBAR_COLLAPSED_WIDTH if collapsed else SIDEBAR_WIDTH
        # setFixedWidth pins min and max together, so the animation drives
        # maximumWidth and drags minimumWidth along with it - animating
        # only one would let the other clamp the result.
        anim = QPropertyAnimation(self.sidebar, b"maximumWidth", self)
        anim.setDuration(SIDEBAR_ANIM_MS)
        anim.setStartValue(self.sidebar.width())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda value: self.sidebar.setMinimumWidth(int(value)))
        anim.finished.connect(lambda: self.sidebar.setFixedWidth(target))
        self._sidebar_anim = anim  # keep a reference so it isn't gc'd mid-animation
        anim.start()

    def _populate_nav_list(self):
        self.nav_list.clear()
        for name, page_name in visible_nav_items():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, page_name)
            self.nav_list.addItem(item)
            self._style_nav_item(item, name, page_name)
        self.nav_list.updateGeometry()

    def _refresh_nav_list(self):
        """Called by Settings when a section is hidden/unhidden, so the
        sidebar updates immediately instead of needing a restart."""
        self.nav_list.blockSignals(True)
        self._populate_nav_list()
        self.nav_list.blockSignals(False)
        self._sync_nav_highlight(self._history[self._history_index])

    def _on_nav_item_clicked(self, item):
        self.navigate_to(item.data(Qt.ItemDataRole.UserRole))

    def _on_nav_reordered(self, *_args):
        order = [
            self.nav_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.nav_list.count())
        ]
        app_settings.set_nav_order(order)
        # Home lays its preview sections out in this same order, so a
        # drop while Home is showing has to redraw it - otherwise the
        # sidebar and the page under it disagree until the user navigates
        # away and back. Deferred by a tick: this runs from inside the
        # drop, and the page being rebuilt owns widgets the drag is still
        # unwinding through.
        if self._history[self._history_index] == "home":
            QTimer.singleShot(0, self.refresh_current_page)

    def _build_add_menu(self):
        """Rebuilt every time it opens, not once at startup: hiding a
        section in Settings has to drop it from here without a restart,
        the way it already does from the sidebar."""
        self._add_menu.clear()
        hidden = set(app_settings.get_hidden_sections())
        for label, page_name, action in ADD_ITEMS:
            if page_name in hidden:
                continue
            self._add_menu.addAction(label, lambda p=page_name, a=action: self._add_via(p, a))

    def _add_via(self, page_name, action):
        self.navigate_to(page_name, animate=False)
        action(self._current_page)

    def _open_settings(self):
        SettingsDialog(self)

    # ---- Startup update check -----------------------------------------
    def schedule_update_check(self):
        """Ask GitHub once per launch whether a newer release exists.

        Until now check_for_update() was reachable only from Settings'
        button, so anyone who never opened Settings never learned a new
        version had shipped (roadmap #13). This changes nothing about
        *how* the check is made - same function, same API contract - only
        that something asks it without being told to.

        Not while running from source: there is no executable to replace
        (updater.is_frozen), so the offer would lead to Settings saying
        exactly that - which is also why packaging/test_update.py has to
        pretend the app is frozen to see any of this at all. Called after the window is showing, and after the
        what's-new dialog has been dismissed - that one is modal, and a
        timer started before it would fire into its nested event loop and
        drop a toast on top of the dialog."""
        if not updater.is_frozen():
            return
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._start_update_check)

    def _start_update_check(self):
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self):
        """Off the UI thread, and silent on failure. The user did not ask
        for this check, so no network, a rate-limited GitHub or a
        malformed answer must produce nothing visible at all - the
        Settings button still reports properly when it *is* asked. Broad
        except on purpose: an uncaught exception in a worker dies silently
        and would leave the signal unemitted."""
        try:
            found = updater.check_for_update()
        except Exception:
            found = None
        self._update_signals.found.emit(found)

    def _on_update_found(self, found):
        if not found:
            return
        version = found.get("version") or ""
        self._pending_update_version = version
        self._update_dot.show()
        self._position_update_dot()
        self._style_settings_btn()
        # First time this version is seen: a toast, which is enough to
        # say it exists. Every launch after that, while it is still
        # waiting: an alert, because the owner asked to be *reminded*
        # rather than told once - a reminder nobody sees twice is not
        # one. The dot carries it in between either way.
        first_time = app_settings.get_notified_update_version() != version
        app_settings.set_notified_update_version(version)
        if first_time:
            show_toast(self, f"Atomic {version} Is Available - Install It in Settings", 6000)
            return
        self._remind_about_update(version)

    def _remind_about_update(self, version):
        """The reminder, once per launch while an update is waiting.

        A dialog and not a toast, unlike the first notice: this one asks
        something (take it now, or not yet), and `.claude/rules/ui.md`
        draws that line - dialogs are for what the user must decide or
        must not miss. Answering "Later" leaves the dot and asks again
        next launch; there is no "stop asking", because the way to stop
        it is to install the update, which is one click away in the same
        dialog."""
        box = QMessageBox(self)
        box.setWindowTitle("Update Available")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Atomic {version} is available.")
        box.setInformativeText("You are running "
                               f"{updater.APP_VERSION}. Updating keeps your entries.")
        install = box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        theme.apply_dark_titlebar(box)
        box.exec()
        if box.clickedButton() is install:
            self._open_settings()

    def refresh_current_page(self):
        """Re-create whichever page is currently showing, fresh from
        disk - each page only loads its saved entries once, in __init__,
        so an edit made elsewhere (Settings > Clear Data, wiping a
        category out from under a page that's already open behind the
        dialog) wouldn't otherwise show up until the user navigated away
        and back on their own."""
        self._show_page(self._history[self._history_index], animate=False)

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        # Pages are positioned by hand (they slide over each other on
        # navigation) rather than sitting in a layout, so nothing resizes
        # them automatically. Following the *container* covers every way
        # it can change width - not only the window resizing, but the
        # sidebar collapsing/expanding, which widens the container
        # without the window itself changing size at all. Without this a
        # page kept whatever width it was built at, and the uncovered
        # strip of window showed through down the right-hand side,
        # looking like a second sidebar had appeared out of nowhere.
        if obj is self.container and event.type() == QEvent.Type.Resize:
            self._fit_current_page()
        if obj is self.settings_btn and event.type() == QEvent.Type.Resize:
            self._position_update_dot()
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.BackButton:
                self.go_back()
                return True
            if event.button() == Qt.MouseButton.ForwardButton:
                self.go_forward()
                return True
        return super().eventFilter(obj, event)

    def _fit_current_page(self):
        if self._current_page is not None:
            self._current_page.setGeometry(self.container.rect())

    def _drain_override_cursor(self):
        """Drop any application-wide override cursor.

        This is what left the pointing hand stuck over every page after
        launching a game. A cursor set on a widget (which is all this app
        does - see widgets.Card) is re-evaluated the moment the pointer
        crosses into a widget that doesn't set one, so it cannot be
        responsible for a shape that survives moving the mouse
        everywhere. An *override* cursor can: it sits on top of the whole
        application and outranks every widget until it is popped.

        Nothing here pushes one deliberately, but Qt does internally -
        drag-and-drop being the one this app enables (the sidebar's
        InternalMove nav list), and a drag whose grab is broken by
        another window taking focus mid-gesture can leave its override
        behind. Popping to empty is therefore always the right move, and
        can't discard anything the app meant to keep. The loop is because
        overrides nest, and the guard is so a stuck one can't spin here
        forever."""
        for _ in range(16):
            if QApplication.overrideCursor() is None:
                return
            QApplication.restoreOverrideCursor()

    def _cursor_watchdog_tick(self):
        """Keep the pointer's cursor honest, by re-deriving it whenever
        the pointer has moved since the last tick.

        This exists because Qt can lose track of which widget the pointer
        is over, and then stops updating the cursor at all. Closing a
        modal dialog is the reproducible way in: open Settings from the
        sidebar button - which asks for the pointing-hand cursor - close
        it, and moving the pointer off that button never restores the
        arrow. The hand then follows you across every page. Confirmed by
        reading the OS cursor directly, not inferred.

        It has to be a timer rather than an event handler. Qt only
        delivers MouseMove for a widget with mouse tracking switched on,
        which nothing here does, and the Enter event that would normally
        cover it is exactly what the stale state stops being generated -
        so the app cannot see this movement at all. Polling the pointer
        position sidesteps that entirely.

        Repairing has to happen *after* the pointer has moved, too: every
        candidate repair applied at the moment of closing leaves it
        stuck, and every one applied after a move clears it - hence
        re-deriving on movement rather than on the dialog closing."""
        pos = QCursor.pos()
        moved = pos != self._last_pointer_pos
        self._last_pointer_pos = pos
        if not moved:
            return
        # Never mid-drag: Qt drives drag-and-drop with an override cursor
        # of its own (the sidebar's reorderable nav list), and tearing
        # that down under it would break the drag's feedback. A held
        # button is the cheapest reliable "a drag may be in progress"
        # test there is.
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return
        # Any widget still claiming the hand cursor that the pointer has
        # actually left lets go of it, so Qt's own answer is right.
        release_stale_hover_cursors(pos)
        if QApplication.overrideCursor() is not None:
            self._drain_override_cursor()
            return
        # Then make Windows agree with that answer. Qt stops issuing
        # native cursor calls for this window after a modal dialog
        # closes, so this is the only step that actually reaches the
        # screen in that state - see helpers/native_cursor.
        widget = QApplication.widgetAt(pos)
        if widget is not None:
            native_cursor.enforce(widget.cursor().shape())

    def _on_app_state_changed(self, state):
        """Returning to the front is the other way the pointer can end
        up somewhere Qt has lost track of - after a game or website
        launched from a card took focus mid-click."""
        if state == Qt.ApplicationState.ApplicationActive:
            self._drain_override_cursor()

    # ------------------------------------------------------------------
    def _restore_window_geometry(self):
        """Put the window back at the size and position it was last left
        at, and answer whether it should open maximized.

        True when nothing has been saved: opening maximized is what every
        launch did before this existed, so a first-ever run is unchanged
        (1280x840 restored size, shown maximized).

        The saved rectangle is never trusted as-is - see
        _fit_to_available_screen for why the monitor it was saved on may
        not be there any more."""
        saved = app_settings.get_window_geometry()
        if not saved:
            self._restore_rect = None
            return True
        self._restore_rect = _fit_to_available_screen(
            QRect(saved["x"], saved["y"], saved["width"], saved["height"]))
        # Applied here so the window is never painted at a default
        # position first; show_remembered applies it a second time, which
        # is the pass that actually lands exactly - see there.
        self.setGeometry(self._restore_rect)
        return saved["maximized"]

    def _save_window_geometry(self):
        """Write the restored size/position plus whether the window is
        maximized, for the next launch to reopen with.

        Only a window that is genuinely normal reports a rectangle worth
        storing. While maximized, the restored rectangle is Windows'
        own bookkeeping rather than anything that was asked for, and on
        a fractional-scale display it reads back a few pixels off what
        was restored into it - re-saving it each launch is how a
        deliberate 900x700 walked down to 895x682 in three launches
        (measured). So a maximized window updates the flag and keeps the
        size it was last given by hand.

        Nothing at all is written while minimized or full screen:
        neither is a size anyone chose, and what is already stored is. A
        minimized window reports an off-screen position on Windows, and
        full screen is an F11 mode rather than a shape to reopen at."""
        if self.isMinimized() or self.isFullScreen():
            return
        if self.isMaximized():
            saved = app_settings.get_window_geometry()
            if saved:
                kept = self._restored_rect_for_current_screen(
                    QRect(saved["x"], saved["y"],
                          saved["width"], saved["height"]))
                if saved["maximized"] and kept == QRect(
                        saved["x"], saved["y"], saved["width"], saved["height"]):
                    return  # nothing has changed - don't rewrite the file
                app_settings.set_window_geometry(kept.x(), kept.y(),
                                                 kept.width(), kept.height(),
                                                 True)
                return
            # Nothing stored yet - a first-ever launch, which opens
            # maximized. Its restored rectangle is the 1280x840 fallback
            # and is worth keeping, so un-maximizing later has somewhere
            # sensible to go.
            rect = self.normalGeometry()
        else:
            # geometry(), the rectangle setGeometry takes back unchanged
            # (measured: stable across four apply-and-read cycles on both
            # monitors). normalGeometry matches it exactly here anyway.
            rect = self.geometry()
        # Invalid until the window has been laid out once; a save fired
        # in that window would store an empty box that
        # get_window_geometry then has to throw away.
        if rect.width() <= 0 or rect.height() <= 0:
            return
        app_settings.set_window_geometry(rect.x(), rect.y(),
                                         rect.width(), rect.height(),
                                         self.isMaximized())

    def _restored_rect_for_current_screen(self, rect):
        """`rect` centred on the screen this window is actually on, if it
        describes somewhere else entirely.

        A maximized window has no position of its own worth storing, so
        the stored restored rectangle is also what decides which monitor
        the next launch maximizes onto (showMaximized fills the screen
        the window is already on). Left alone, maximizing on the second
        monitor and relaunching reopened on the first."""
        screen = self.screen()
        if screen is None:
            return rect
        available = screen.availableGeometry()
        if available.intersects(rect):
            return rect
        moved = QRect(rect)
        moved.moveCenter(available.center())
        return _fit_to_available_screen(moved)

    def show_remembered(self):
        """Open the window the way it was last left - maximized, or at
        the restored size and position saved from the last session.

        The geometry is applied a second time here, after show(), on
        purpose. A rectangle set on a window with no native frame yet
        comes back changed: measured on this two-monitor setup (125%
        primary, 100% secondary), asking for 300,200 900x700 before
        show() put 301,206 898x694 on screen - and since what is on
        screen is what gets saved, every launch nudged the window 6px
        further down and 6px smaller, reaching 895x682 by the third.
        The same call once the window exists is exact and stays exact
        when repeated.

        Plain showMaximized() rather than theme.without_window_animation:
        that guard is for a *visible* window changing state, where
        Windows zooms the maximize out from the restored size and briefly
        paints it. Nothing is on screen yet to zoom from - the same
        reasoning start_fullscreen records."""
        if self._open_maximized:
            self.showMaximized()
            return
        self.show()
        if self._restore_rect is not None:
            self.setGeometry(self._restore_rect)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._geometry_save_timer.start(GEOMETRY_SAVE_DELAY_MS)

    def closeEvent(self, event):
        # Flush rather than wait: the debounce is still pending after a
        # window that was dragged or resized and then closed straight
        # away, and there is no later chance to write it.
        self._geometry_save_timer.stop()
        self._save_window_geometry()
        super().closeEvent(event)

    def start_fullscreen(self):
        """Open straight into full screen, for a sign-in launch with
        Settings > Startup > "Fullscreen mode when launch on startup" on.

        _was_maximized is set by hand because nothing set it on the way
        in: this window has never been shown in another state, and
        leaving full screen reads that flag to decide what to go back to
        - left False, F11/Escape would drop the app to the 1280x840
        restored size it was never actually shown at. Maximized is what
        every other launch gives you, so that is what it returns to.

        No without_window_animation here, unlike toggle_fullscreen: that
        one exists for a *visible* window changing between maximized and
        full screen, and there is nothing on screen yet to animate from."""
        self._was_maximized = True
        self.showFullScreen()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
            return
        # Remembered so leaving full screen puts the window back the way
        # it was found, rather than dropping a maximized window down to a
        # restored one.
        self._was_maximized = self.isMaximized()
        theme.without_window_animation(self, self.showFullScreen)

    def exit_fullscreen(self):
        if not self.isFullScreen():
            return
        # Both ways through without_window_animation: Windows' maximize
        # animation zooms from the window's restored size, which on this
        # trip is not where it is coming from or going to - see its
        # docstring for what that looked like.
        if self._was_maximized:
            theme.without_window_animation(self, self.showMaximized)
        else:
            theme.without_window_animation(self, self.showNormal)

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.AltModifier:
            if event.key() == Qt.Key.Key_Left:
                self.go_back()
                return
            if event.key() == Qt.Key.Key_Right:
                self.go_forward()
                return
        if event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_F:
                # The page's own search box, not the global panel - F is
                # "find in this list" everywhere else too.
                box = getattr(self._current_page, "search_box", None)
                if box is not None:
                    box.setFocus()
                    box.selectAll()
                return
            if event.key() == Qt.Key.Key_Y:
                # Redo: do again whatever Ctrl+Z just undid. Only ever
                # what was last undone - see widgets.take_live_redo.
                redo = take_live_redo()
                if redo is not None:
                    redo()
                else:
                    show_toast(self, "Nothing To Redo")
                return
            if event.key() == Qt.Key.Key_Z:
                # Undo means the offer that is on screen right now, not a
                # history of its own - see widgets.take_live_undo.
                toast = take_live_undo()
                if toast is not None:
                    toast.trigger_undo()
                else:
                    show_toast(self, "Nothing To Undo")
                return
            if event.key() == Qt.Key.Key_N:
                # The same menu the + button opens, at the same place -
                # setMenu means Qt positions it, so this is one call and
                # no geometry maths (see _show_add_menu's comment).
                self.add_btn.showMenu()
                return
            if event.key() == Qt.Key.Key_Comma:
                self._open_settings()
                return
            if Qt.Key.Key_1 <= event.key() <= Qt.Key.Key_9:
                # Sidebar order, not a fixed map: the sidebar is
                # drag-to-reorder and hideable, so Ctrl+3 has to mean the
                # third row the user can actually see. Home is row 1.
                pages = ["home", *(page for _label, page in visible_nav_items())]
                index = event.key() - Qt.Key.Key_1
                if index < len(pages):
                    self.navigate_to(pages[index])
                return
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_K):
            # Ctrl+K rather than Ctrl+F: F belongs to the page's own
            # search box (see item #17), and K is what every app with a
            # "search everything" panel binds it to.
            self.open_global_search()
            return
        if event.key() == Qt.Key.Key_Escape:
            # A search box is the thing most worth escaping from, so it
            # wins over leaving full screen. Focused *or* holding text,
            # not text alone: keyed on text, an empty box swallowed
            # nothing and Escape left the caret sitting in it - reported,
            # and the half of this the first fix missed. The text half
            # stays because a query still narrowing the grid is worth
            # escaping from after the focus has moved to a card. A box
            # that is neither is not what Escape is about, so it keeps
            # its usual meaning the rest of the time.
            box = getattr(self._current_page, "search_box", None)
            if box is not None and (box.hasFocus() or box.text()):
                box.clear()
                box.clearFocus()
                self._current_page.setFocus()
                return
            # Otherwise Escape only means "leave full screen" while in
            # it; left alone otherwise, so it keeps closing dialogs and
            # menus as usual.
            if self.isFullScreen():
                self.exit_fullscreen()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    def _direction_between(self, from_page, to_page):
        """'down' if `to_page` sits further down the sidebar than
        `from_page` (slides up into view, like scrolling down a page),
        'up' otherwise. Reads the live sidebar order every time, so a
        just-dragged reorder is reflected on the very next navigation."""
        return "down" if nav_position(to_page) > nav_position(from_page) else "up"

    def navigate_to(self, page_name, animate=True):
        current = self._history[self._history_index]
        if current == page_name:
            return
        self._history = self._history[: self._history_index + 1]
        self._history.append(page_name)
        self._history_index += 1
        self._show_page(page_name, direction=self._direction_between(current, page_name), animate=animate)

    def open_global_search(self, initial=""):
        """Ctrl+K, from any page: go to Home and put the cursor in its
        search field.

        Home rather than a panel over whatever page is showing, because
        Home is where the field lives now - one search box, in one place,
        reached from everywhere."""
        self.navigate_to("home")
        page = self._current_page
        field = getattr(page, "search_bar", None)
        if field is None:
            return
        field.setFocus()
        if initial:
            field.setText(initial)
            page._search_bar_typed(initial)

    def go_back(self):
        if self._history_index > 0:
            current = self._history[self._history_index]
            self._history_index -= 1
            target = self._history[self._history_index]
            self._show_page(target, direction=self._direction_between(current, target))

    def go_forward(self):
        if self._history_index < len(self._history) - 1:
            current = self._history[self._history_index]
            self._history_index += 1
            target = self._history[self._history_index]
            self._show_page(target, direction=self._direction_between(current, target))

    def _sync_nav_highlight(self, page_name):
        self.downloads_btn.setChecked(page_name == "downloads")
        if page_name == "home":
            self.home_list.setCurrentRow(0)
        else:
            self.home_list.clearSelection()
        match = None
        for i in range(self.nav_list.count()):
            item = self.nav_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == page_name:
                match = item
                break
        if match is not None:
            self.nav_list.setCurrentItem(match)
        else:
            self.nav_list.clearSelection()

    def _show_page(self, page_name, direction="down", animate=True):
        self._sync_nav_highlight(page_name)

        old_page = self._current_page
        new_page = PAGES[page_name](self)
        new_page.setParent(self.container)
        self._current_page = new_page

        rect = self.container.rect()
        if not animate or old_page is None:
            new_page.setGeometry(rect)
            new_page.show()
            if old_page is not None:
                old_page.deleteLater()
            return

        if self._anim_group is not None:
            self._anim_group.stop()

        # "down" = the target sits below the source in the sidebar, so
        # the new page enters from below and slides up into place (like
        # scrolling down a page); "up" is the mirror image.
        height = rect.height()
        dy = height if direction == "down" else -height
        new_page.setGeometry(rect.translated(0, dy))
        new_page.show()
        new_page.raise_()

        group = QParallelAnimationGroup(self)
        anim_new = QPropertyAnimation(new_page, b"pos", self)
        anim_new.setDuration(ANIM_DURATION_MS)
        anim_new.setStartValue(QPoint(0, dy))
        anim_new.setEndValue(QPoint(0, 0))
        anim_new.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_new)

        anim_old = QPropertyAnimation(old_page, b"pos", self)
        anim_old.setDuration(ANIM_DURATION_MS)
        anim_old.setStartValue(QPoint(0, 0))
        anim_old.setEndValue(QPoint(0, -dy))
        anim_old.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(anim_old)

        group.finished.connect(old_page.deleteLater)
        self._anim_group = group
        group.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_current_page()
        self._geometry_save_timer.start(GEOMETRY_SAVE_DELAY_MS)


def _prewarm_image_specs():
    """(path, size) pairs for every cover/icon a page might draw, at the
    sizes those pages actually ask for.

    Assembled here rather than inside images.py, which has no business
    knowing which file holds what - and read straight off disk rather
    than off the pages, since the whole point is to have this done
    before those pages are ever built. Each page renders at its own
    size, and a cover shown at two sizes is two separate decodes, so
    both are listed."""
    specs = []
    for data_file, key in (("tracker.json", "cover_path"), ("series.json", "cover_path")):
        for entry in storage.load(data_file, []):
            path = entry.get(key)
            if path:
                specs.append((path, tracker_poster_size))
                specs.append((path, home_poster_size))
                specs.append((path, home_hero_cover_size))
    for game in storage.load("games.json", []):
        if game.get("icon"):
            specs.append((game["icon"], games_icon_size))
            specs.append((game["icon"], home_icon_size))
    for data_file in ("apps.json", "websites.json"):
        for entry in storage.load(data_file, []):
            if entry.get("image"):
                specs.append((entry["image"], link_thumb_size))
                specs.append((entry["image"], home_row_icon_size))
    return specs


def main():
    # Before QApplication, because an exception raised anywhere after this
    # point - in a slot, in a paint event, during startup - otherwise
    # takes the whole process down through qFatal with no traceback
    # anywhere. See helpers/logs.py.
    logs.install_excepthook()
    app = QApplication(sys.argv)
    icon_path = APP_DIR / "app_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    theme.apply_theme(app)

    window = MainWindow()
    # Full screen only for a launch Windows itself started at sign-in
    # (the registered command carries startup.STARTUP_FLAG, nothing else
    # does) - opening the app by hand is unaffected by that setting.
    if startup.launched_on_startup() and app_settings.get_fullscreen_on_startup():
        window.start_fullscreen()
    else:
        window.show_remembered()
    # Not just shown but actually brought forward. Launched normally this
    # is what already happens; launched by the updater's relaunch it is
    # not, and the window would otherwise sit behind everything blinking
    # in the taskbar (see theme.bring_window_to_front).
    theme.bring_window_to_front(window)
    # Only after the window is up and forward: this is modal, and a
    # dialog raised before its parent is showing would sit behind it -
    # the same foreground problem the relaunch already has to solve
    # above, and this launch is precisely the one that came from a
    # relaunch. Does nothing unless this launch followed an update.
    whats_new.show_if_updated(window)
    # Started after the window is up, so it fills the time the user
    # spends looking at Home rather than delaying it appearing.
    images.prewarm(_prewarm_image_specs())
    # Last, and on its own delay: the one thing here nobody is waiting
    # for. After whats_new deliberately - that dialog is modal, so a
    # timer armed before it would tick inside its nested event loop.
    window.schedule_update_check()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
