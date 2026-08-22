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
                     native_cursor, rail_icons, setup_wizard, startup,
                     storage, theme, updater, whats_new)
from helpers.nav_config import (HOME_ITEM, nav_position, visible_nav_groups,
                                visible_nav_items)
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
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)
from helpers.settings_dialog import SettingsDialog
from helpers.widgets import (PageSlide, SmoothTween, confirm, install_edge_wheel,
                             release_stale_hover_cursors, show_toast,
                             take_live_redo, take_live_undo, use_hover_cursor)
from windows import home as home_page_module
from windows import link_grid as link_grid_module
from windows import tracker as tracker_module
from windows.apps import AppsPage
from windows.downloads_page import DownloadsPage
from windows.games import GamesPage
from windows.home import HomePage
from windows.tracker import MangaPage, SeriesPage
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
home_icon_size = home_page_module.ICON_SIZE
home_row_icon_size = home_page_module.ROW_ICON_SIZE
link_thumb_size = link_grid_module.THUMB_SIZE
# The poster grids' art sizes: Steam covers at the tracker's poster
# size, app-store icons square at the poster card's width.
game_cover_size = tracker_module.POSTER_SIZE
app_art_size = (link_grid_module.POSTER_ART_SIZE[0],
                link_grid_module.POSTER_ART_SIZE[0])

# The one page every way out of a section lands on - the section Back
# button here, and the reader's and player's doors (reader.HOME_PAGE).
HOME_PAGE_NAME = "home"

PAGES = {
    "home": HomePage,
    # No "anime" page: it merged into "series" (one watch page under the
    # camera glyph, the owner's ask). Anything still asking for it by
    # name - an old saved nav order, a stale history entry - resolves
    # through _page_name below rather than KeyErroring mid-navigation.
    "manga": MangaPage,
    "series": SeriesPage,
    "games": GamesPage,
    "apps": AppsPage,
    "websites": WebsitesPage,
    "downloads": DownloadsPage,
}


def _page_name(name: str) -> str:
    """A page key that exists today. "anime" merged into "series"; saved
    nav orders, session histories and shortcuts may still say it."""
    return "series" if name == "anime" else name


# label, page to jump to, action to run on that page once it's showing
ADD_ITEMS = [
    ("Anime Entry", "series", lambda page: page._open_form(default_type="Anime")),
    ("Reading Entry", "manga", lambda page: page._open_form()),
    ("Movie or Series Entry", "series", lambda page: page._open_form(default_type="Series")),
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
# The spacing every sidebar's QVBoxLayout is built with, and the slack
# NavListWidget.sizeHint keeps under its last row - both read by
# RAIL_GAP_SLACK below, so a change to either stays in step with the
# blank row it has to be subtracted from.
SIDEBAR_LAYOUT_SPACING = 4
NAV_LIST_BOTTOM_MARGIN = 12

# The blank row between two blocks of rail rows, used only until a real
# row has been laid out and can be measured (see _sync_rail_gaps). 44 is
# what a row measures at the expanded width on this machine: 11px of QSS
# padding top and bottom, a 13pt Bahnschrift line, and the 1px resting
# border that reserves the selected pill's space.
RAIL_GAP_FALLBACK = 44

# What is already between two rail blocks before the gap widget is added,
# and therefore what has to come off it so the air between them measures
# exactly one row. Measured on a real-window grab: block-to-block was
# 146px against a 63px row pitch, i.e. 20px too much for the "one button"
# the owner asked for.
#
#   12  NavListWidget.sizeHint's safety margin under its last row
#    8  the sidebar layout's 4px spacing, once above the gap and once below
#
# Named rather than written as 20, because both halves are values that
# live elsewhere and could move.
RAIL_GAP_SLACK = NAV_LIST_BOTTOM_MARGIN + SIDEBAR_LAYOUT_SPACING * 2

# How many blocks the section rail draws. Fixed rather than read off the
# showing page, because the bar is built once and refilled per page -
# and every page that has sections has the same three (see
# tracker._section_groups).
SECTION_BLOCKS = 3

# Glyphs for the contextual section sidebar that replaces the main one
# over any page exposing SECTIONS (the tracker pages). Segoe Fluent/MDL2
# codepoints written as escapes, not the characters themselves, since a
# private-use literal does not survive a tool re-encoding this file
# (CLAUDE.md records that happening twice). The fallback for a section
# key not named here is theme.NAV_BULLET, so an unmapped section still
# gets a readable row in the collapsed rail.
SECTION_BACK_ICON = "\uE72B"    # Back (left-pointing arrow)
# **Two rail rows are drawn rather than typed** - see VECTOR_ICONS
# below and helpers/rail_icons.py. Both had been standing in as *emoji*
# because this icon font carries neither shape: E8A4, the one named
# "Bookmarks", draws a bulleted list (checked by rendering the whole
# E8A0-E8BF range), and there is no cat in it at all - E933 drew an
# I-beam. The owner's ask, 22 August 2026: "replace the save emoji and
# the cat face emoji in anime to a proper icons not emojis". An emoji is
# the wrong thing here for a reason that is on screen rather than a
# matter of taste: it is a *colour* glyph, so it renders in its own
# fixed palette and ignores the row's normal/hover/selected colour - a
# pink ribbon and a ginger cat in a column of monochrome gold-white
# glyphs. theme.py already says exactly this about why every other rail
# glyph comes from the Segoe icon fonts.
VECTOR_ICONS = {"saved": "bookmark", "cat_anime": "cat"}
# Logical pixels. 21, not the 20 the icon grid is designed on: measured
# against the Fluent rows beside it, a 20px box puts ~15px of ink on
# screen where those rows put ~17, and the bookmark read small.
VECTOR_ICON_SIZE = 21

# **Bigger in the folded rail, and that is an optical fix, not a
# geometric one** - a 21px bookmark alone in a 36px rail reads small
# beside a 21px-wide camera glyph. It does not fix alignment; see
# VECTOR_ICON_INK_LEFT below for that, and note that a bookmark is
# intrinsically narrow and cannot be made as wide as a camera glyph
# without becoming a different object.
VECTOR_ICON_SIZE_COLLAPSED = 24
# Reserved to the *right* of a drawn icon, so the label beside it starts
# where a glyph row's label does. Measured on a real-window grab: a
# glyph row's label ink begins at x=61 and a drawn row's at x=58, and the
# view reserves `iconSize` for the decoration - so three columns of
# nothing on the right is the whole fix, with no second pixmap to keep
# in step. Confirmed still true 22 August 2026 after the ink-left change
# below: every label in the expanded section rail starts at x=39-40,
# drawn rows and glyph rows alike.
VECTOR_ICON_LEAD = 3

# **Align left edges, not centres.** Third report from the owner, 22
# August 2026: both Saved and Anime "need to be moved to the left a
# bit", folded *and* expanded. Measured on a real window, the lit-pixel
# box of every row in the section rail, before this change:
#
#     folded (32px row)          expanded (184px row)
#     Movies   x 8..28  w21      Movies   x 8..27  w20
#     Series   x 8..28  w21      Series   x 8..27  w20
#     Anime    x10..26  w17      Anime    x11..27  w17   <- drawn
#     Saved    x13..23  w11      Saved    x14..24  w11   <- drawn
#     Schedule x10..26  w17      Schedule x 9..26  w18
#     History  x 9..28  w20      History  x 9..27  w19
#
# Centres already agreed to within a pixel (18.0 folded), which is why
# two rounds of centre-matching did not answer the complaint. What the
# eye reads down a narrow column is the *left* edge, and the bookmark's
# began 4-6px right of every glyph's. So the drawn rows are now
# positioned by their ink's left edge instead: 0.0 means "ink starts at
# the decoration box's left edge", and the view puts that box exactly
# where a text-only row's text begins - i.e. on the same axis the
# glyphs' own ink comes off. rail_icons.pixmap does the arithmetic, per
# shape, from rail_icons.ink_box.
#
# Not negative, though the glyph mean sits a shade left of it: ink at
# x<0 is ink clipped off the pixmap. This is as far left as the artwork
# can go and still be whole.
VECTOR_ICON_INK_LEFT = 0.0
VECTOR_ICON_INK_LEFT_COLLAPSED = 0.0
# The role a drawn row's icon name is parked on, so _RailDelegate can
# tell one from an ordinary glyph row. UserRole itself already carries
# the page/section key.
VECTOR_ROLE = Qt.ItemDataRole.UserRole + 1

SECTION_ICONS = {
    "saved": VECTOR_ICONS["saved"],
    "discover": "\uE721",  # Search
    "schedule": "\uE787",  # Calendar
    "history": "\uE81C",   # History (the clock-with-arrow)
    # The category sections (tracker.WATCH_CATEGORIES / READ_CATEGORIES).
    # Chosen to read at a glance in the *collapsed* rail, where the label
    # is gone and the glyph is all there is - a TV set for series, a
    # camera for anime, a filmstrip for movies. The three reading
    # flavours get three distinct book shapes for the same reason:
    # "Manhwa" and "Manhua" differ by one letter and could never be told
    # apart by their text at rail width.
    "cat_series": "\uE7F4",      # TVMonitor - the screen
    "cat_anime": VECTOR_ICONS["cat_anime"],
    "cat_movies": "\uE714",      # Video - the camera
    "cat_manga": "",     # Library
    "cat_manhwa": "",    # ReadingList
    "cat_manhua": "",    # Page
    "cat_other": "",     # Dictionary
}

# The fold toggle's two faces - single Fluent chevrons, drawn large
# (the owner's ask: "keep it like this shape '\u203A' but large", replacing
# the small double guillemets). Escapes, not the characters, for the
# same re-encoding reason as SECTION_ICONS; the #FoldButton QSS rule
# carries the icon font stack and the size so they resolve.
FOLD_CLOSE_ICON = "\uE76B"      # ChevronLeft - points at the folding edge
FOLD_OPEN_ICON = "\uE76C"       # ChevronRight - points back out


class _MouseNavFilter(QObject):
    """Mouse buttons 4 and 5 as back/forward, everywhere.

    Installed on the application rather than on the pages (the owner's
    ask: "in the whole app even in the player or reader mode"). The
    window's own eventFilter only ever saw presses on the container and
    the sidebar, so the two overlays that cover the container - the
    player and the reader - answered neither button.

    One caveat worth stating: mpv renders into a native child window,
    and clicks landing on the video surface itself do not travel through
    Qt's event system at all (see player.VideoSurface). The buttons work
    over every Qt surface, the player's own bars included; over the bare
    picture the player's pointer poll is what sees the mouse."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        try:
            button = event.button()
        except Exception:
            return False
        if button == Qt.MouseButton.BackButton:
            self._window.navigate_back()
            return True
        if button == Qt.MouseButton.ForwardButton:
            self._window.navigate_forward()
            return True
        return False


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
        # count * (row + 2*spacing), because QListView's spacing surrounds
        # *each* item - so the pitch is row + 2*spacing and the content is
        # that times the number of rows. The old form
        # (row*count + spacing*(count+1)) undershot by spacing*(count-1),
        # which is why two blocks of different lengths measured a
        # different amount of air below their last row (126px against
        # 122px between blocks, for one blank 63px row in both cases).
        total = (row_height + self.spacing() * 2) * self.count()
        # Safety margin: the selected-item QSS border renders right at
        # the edge of the last row's box, and without enough slack the
        # list widget clips its own bottom border/corners off. Named,
        # because the blank row between two blocks has to subtract it -
        # see RAIL_GAP_SLACK.
        return QSize(width, total + NAV_LIST_BOTTOM_MARGIN)

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


class _RailDelegate(QStyledItemDelegate):
    """The two things a rail row needs that the stock delegate does not
    do, and nothing else - every ordinary glyph row is drawn exactly as
    it was.

    1. **A drawn icon follows the row's colour.** QStyle only ever asks a
       QIcon for Normal, Disabled or Selected (QCommonStyle decides the
       mode from `State_Selected` alone), so a *hovered* row's icon would
       stay muted while its label turned bright - the one visual where
       the drawn rows would have read differently from the glyph rows
       beside them. Hover is therefore mapped onto the icon's Selected
       pixmap here.
    2. **A collapsed row centres its icon.** With the label gone the row
       is the icon, and QStyleOptionViewItem's decorationAlignment is
       AlignLeft in list mode - so the icon sat hard against the left
       edge of a 68px rail while every glyph row centred its text.
    """

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if not option.text:
            option.decorationAlignment = Qt.AlignmentFlag.AlignCenter
            # Or QCommonStyle lays the (empty) text out to the icon's
            # right and centres the *pair*, which puts the icon left of
            # the rail's middle by half a label's width.
            option.displayAlignment = Qt.AlignmentFlag.AlignCenter

    def sizeHint(self, option, index):
        """A drawn row is exactly as tall as a typed one.

        Measured: carrying a decoration at all cost a row **2px**
        (59 -> 61 expanded, 57 -> 61 collapsed) whatever size the icon
        was - 18px through 22px all produced 61 - so it is the style's
        decoration margins, not the artwork. Two rows out of nine being
        2px taller reads as uneven spacing down the column, so the
        height is taken from what the same row would want with no
        decoration in it at all."""
        hint = super().sizeHint(option, index)
        if not index.data(VECTOR_ROLE):
            return hint
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.icon = QIcon()
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        plain = style.sizeFromContents(
            QStyle.ContentsType.CT_ItemViewItem, opt, QSize(), widget)
        if plain.height() > 0:
            hint.setHeight(plain.height())
        return hint

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        if hovered and not (opt.state & QStyle.StateFlag.State_Selected):
            name = index.data(VECTOR_ROLE)
            if name:
                # The lead read back off the box the row is already
                # using, rather than the sidebar's state - the delegate
                # has no business knowing whether the rail is folded.
                lead = max(0, opt.decorationSize.width()
                           - opt.decorationSize.height())
                # Same reading for the ink offset: an empty label *is*
                # the folded state as far as this delegate is concerned
                # (initStyleOption already keys off it). Without it,
                # hovering a folded drawn row snapped its artwork back
                # to wherever the default put it - the row shifted
                # sideways under the pointer.
                ink_left = (VECTOR_ICON_INK_LEFT if opt.text
                            else VECTOR_ICON_INK_LEFT_COLLAPSED)
                size = (VECTOR_ICON_SIZE if opt.text
                        else VECTOR_ICON_SIZE_COLLAPSED)
                opt.icon = QIcon(rail_icons.pixmap(
                    str(name), size, theme.TEXT, lead, ink_left))
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt,
                          painter, widget)


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

        root_layout.addWidget(self._build_sidebars())

        self.container = QWidget()
        root_layout.addWidget(self.container, stretch=1)

        self._history = ["home"]
        self._history_index = 0
        self._current_page = None
        # The page-slide compositor while one is running (see
        # widgets.PageSlide), so a second navigation can end it.
        self._page_slide = None
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

        # **On the two widgets it actually watches, not on the whole
        # application.** It was app-wide, which means Qt handed it every
        # event delivered to every object in the process - measured
        # while profiling the sidebar fold, **13,172 calls across six
        # folds**, roughly 2,200 per fold, each one constructing a
        # QEvent.Type enum in Python up to four times over. That was the
        # largest single Python cost left in a fold, and it was buying
        # two resize hooks.
        #
        # The settings buttons install this on themselves already (see
        # _build_utility_footer), and mouse buttons 4/5 are handled by
        # _MouseNavFilter - which is app-wide because it has to be, and
        # which routes through navigate_back rather than go_back, so it
        # leaves an overlay properly instead of walking page history
        # underneath it. Nothing is lost by narrowing this one.
        self.container.installEventFilter(self)
        self.sidebar_holder.installEventFilter(self)
        QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

    # ------------------------------------------------------------------
    def _build_sidebars(self):
        """The sidebar column: a fixed-width holder carrying both bars -
        the main sidebar and the contextual section bar - as manually
        positioned children that slide over each other on a swap (see
        _sync_section_sidebar). The holder, not a bar, is what sits in
        the window's layout and what the fold animation drives, so a
        swap never changes the column's width and nothing to its right
        moves. It shares the bars' objectName on purpose: every palette
        colour is opaque, so painting the same gradient panel underneath
        costs nothing while settled, and mid-swap - when both bars are
        short of the right edge and a strip between them and the page
        would otherwise show bare window background - that strip shows
        sidebar instead."""
        holder = QWidget(objectName="Sidebar")
        holder.setFixedWidth(SIDEBAR_WIDTH)
        self.sidebar_holder = holder
        # Which bar owns the column, and the swap animation in flight if
        # one is. _layout_sidebars is the single place geometry is
        # derived from these.
        self._section_bar_showing = False
        # The sidebar-swap compositor while one runs (widgets.PageSlide).
        self._bar_slide = None
        # Every Downloads/Settings pair on screen - one per sidebar (see
        # _build_utility_footer). The style and indicator refreshes walk
        # this rather than naming widgets, so a bar gaining or losing a
        # footer changes nothing else.
        self._utility_bars = []
        self._build_sidebar(holder)
        self._build_section_sidebar(holder)
        self._sync_fold_buttons()
        return holder

    def _build_utility_footer(self, layout, *, primary):
        """Downloads and Settings at the foot of a sidebar.

        **Both bars carry a pair** (the owner's ask): the contextual
        section bar replaces the main one over a tracker page, and
        having neither control there meant pressing Back before either
        could be reached. Twin widgets rather than one pair reparented
        between the bars - a widget moved between layouts on every page
        swap is a flicker and a lifetime problem, where two are just two.

        Every pair is registered in `_utility_bars`, and the style and
        indicator refreshes walk that list, so nothing has to know how
        many bars exist. `primary` names the main bar's copies as the
        attributes the rest of the window already talks to.

        Downloads sits with Settings rather than in the nav list above:
        it is a utility view over a queue, not one of the user's
        sections, and the nav list is their own drag-to-reorder order -
        a row appearing in the middle of it would be one they never put
        there. A NavButton for the same reason Settings is one: it is
        already proven to render at both sidebar widths."""
        downloads = QPushButton(objectName="NavButton")
        # 40, matching Settings below and the section bar's rows: the
        # nav rows above grew to Harbor's more generous height, and a
        # 34px row under a column of 44px ones read as a different
        # control rather than the same list continuing.
        downloads.setFixedHeight(40)
        downloads.setCheckable(True)
        use_hover_cursor(downloads)
        downloads.clicked.connect(lambda: self.navigate_to("downloads"))

        # A notification badge in the button's top-right corner while
        # anything is downloading (the owner's ask) - it carries the
        # *count*, not just a dot, so how many are running is readable
        # without opening the page, at either sidebar width. A child at
        # a fixed offset, not a layout row, so nothing in the column
        # moves when it appears. Mouse-transparent: it sits on its own
        # button. Sized per count in refresh_download_indicator (two
        # digits need a wider pill than one).
        dot = QLabel(downloads)
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setFixedSize(16, 16)
        dot.setStyleSheet(
            f"background: {theme.ACCENT}; border: 1px solid {theme.BG};"
            f" border-radius: 8px; color: {theme.ON_ACCENT};"
            f" font-size: 8pt; font-weight: 700;")
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        dot.hide()
        layout.addWidget(downloads)

        # Progress where it can be seen without opening the page. A slim
        # accent strip directly under the Downloads button, plus the
        # count in that button's own label; what is downloading is in
        # the tooltip, since there is no room for a title at 220px and
        # none at all in the folded rail.
        #
        # Hidden rather than zeroed when nothing is running: a bar
        # sitting at 0% reads as a download that is stuck, and an idle
        # sidebar should look exactly as it did before this existed.
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setFixedHeight(DOWNLOAD_BAR_HEIGHT)
        bar.setStyleSheet(
            f"QProgressBar {{ background: {theme.SURFACE_HOVER};"
            f" border: none; border-radius: {DOWNLOAD_BAR_HEIGHT // 2}px; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT_GRADIENT};"
            f" border-radius: {DOWNLOAD_BAR_HEIGHT // 2}px; }}")
        bar.hide()
        layout.addWidget(bar)

        settings = QPushButton(objectName="NavButton")
        settings.setFixedHeight(40)
        use_hover_cursor(settings)
        settings.clicked.connect(self._open_settings)
        layout.addWidget(settings)

        # The "an update is waiting" marker: a plain accent dot in the
        # button's corner, hidden until the startup check finds one. A
        # child widget rather than a character appended to the button's
        # text, because that text is drawn in the Segoe icon font at both
        # sidebar widths and a dot glyph there is one more codepoint that
        # can come out as a missing-glyph box on a machine without it.
        # Transparent to the mouse so the button underneath keeps its own
        # hover highlight and hand cursor (.claude/rules/ui.md - never
        # leave a cursor set on something that isn't handling the click).
        update_dot = QLabel(settings)
        update_dot.setFixedSize(UPDATE_DOT_SIZE, UPDATE_DOT_SIZE)
        update_dot.setStyleSheet(
            f"background: {theme.ACCENT}; border-radius: {UPDATE_DOT_SIZE // 2}px;")
        update_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        update_dot.hide()
        settings.installEventFilter(self)

        pair = {"downloads": downloads, "dot": dot, "bar": bar,
                "settings": settings, "update_dot": update_dot}
        self._utility_bars.append(pair)
        if primary:
            self.downloads_btn = downloads
            self.downloads_dot = dot
            self.downloads_bar = bar
            self.settings_btn = settings
            self._update_dot = update_dot
        self._style_downloads_btn()
        self._style_settings_btn()
        return pair

    def _make_rail_list(self, *, draggable):
        """One block of rail rows - the widget both sidebars are built
        out of. Every property here used to be spelled out three times
        over (Home, the nav list, the section list) and drifted; the two
        drawn-icon rows made a fourth thing to keep in step, which is
        what finally collapsed them into one place."""
        rail = NavListWidget(objectName="NavList")
        if draggable:
            rail.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        rail.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        rail.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rail.setFrameShape(QFrame.Shape.NoFrame)
        rail.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 2, matching the item inset - without it a row renders edge to
        # edge and its selected pill is clipped by the widget border.
        rail.setSpacing(2)
        rail.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        # QListWidget's item delegate paints with the widget's own font(),
        # not the ::item QSS font-family - the stylesheet rule alone is
        # silently ignored for list items, so it has to be set here too.
        rail.setFont(theme.nav_font())
        rail.setIconSize(QSize(VECTOR_ICON_SIZE + VECTOR_ICON_LEAD,
                               VECTOR_ICON_SIZE))
        # setItemDelegate, not setItemDelegateForRow: the drawn rows move
        # around as blocks are refilled, and a per-row delegate would be
        # attached to whichever row happened to hold one at build time.
        # Owned by the list, so it lives exactly as long as it does.
        rail.setItemDelegate(_RailDelegate(rail))
        return rail

    def _make_rail_gap(self):
        """A blank row's worth of column, between two blocks.

        Registered in `_rail_gaps` so _sync_rail_gaps can re-measure it:
        the rows it is supposed to match shrink when the sidebar folds
        (the icon font's metrics are shorter than the nav face's), and a
        gap frozen at the expanded height would leave the folded rail
        with two holes in it."""
        gap = QWidget(objectName="Bare")
        gap.setFixedHeight(RAIL_GAP_FALLBACK)
        gap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._rail_gaps.append(gap)
        return gap

    def _make_nav_gap(self):
        """A block gap in the *main* bar, also indexed in `_nav_gaps` so
        _populate_nav_list can hide it with the block below it."""
        gap = self._make_rail_gap()
        self._nav_gaps.append(gap)
        return gap

    def _sync_rail_gaps(self):
        """Every block gap set to exactly one row's height.

        Measured off a real row rather than assumed - the QSS gives
        ::item 11px of vertical padding on top of whatever the font
        needs, so the number is a product of the stylesheet and the
        font, not something worth hard-coding twice."""
        row = self.home_list.sizeHintForRow(0)
        pitch = (row if row > 0 else RAIL_GAP_FALLBACK) + self.home_list.spacing() * 2
        height = max(1, pitch - RAIL_GAP_SLACK)
        for gap in getattr(self, "_rail_gaps", ()):
            try:
                gap.setFixedHeight(height)
            except RuntimeError:
                pass

    def _sync_rail_icon_widths(self):
        """Make a drawn row occupy exactly the width a glyph row does,
        so the folded rail centres them all on the same axis.

        **Measured 22 August 2026 on the folded section rail**, sampling
        the lit pixels of each row's icon:

            Saved     (drawn)  centroid x = 19.0
            Schedule  (glyph)  centroid x = 17.9
            History   (glyph)  centroid x = 17.9

        which is the owner's "the saved icon is moved more to the right
        when folded", and the same 1px sits under Anime in the rail
        above. The cause is not the artwork: a row carrying a
        *decoration* reserves the icon **and** the view's
        decoration-to-text gap even when the collapsed label is empty,
        so the Saved item's rect came out **39px wide inside a 36px
        viewport** and AlignCenter centred it on 19.5 rather than on the
        rail. Widening or padding the pixmap cannot fix that - the extra
        width is the item's, not the icon's.

        So the hint is pinned to what the glyph rows in the same rail
        actually measure. Width only; the height stays the delegate's,
        which is what _sync_rail_gaps reads. Expanded rails are left
        alone - there the label needs the natural width, and the two
        were already measured 1px apart (see _style_rail_item).

        **The centroids quoted above are history, not the current
        alignment.** Making them agree is what this function does and it
        is still needed - without the pin the item is wider than the
        viewport and centres off the rail entirely - but agreeing
        centres did not answer the owner's complaint, and the artwork is
        now placed by its *left* edge instead. See VECTOR_ICON_INK_LEFT.
        """
        # getattr throughout: this runs from _populate_nav_list, which
        # the main bar builds before the section bar or the Home row
        # exist at all.
        rails = list(getattr(self, "nav_lists", ()))
        rails += list(getattr(self, "section_lists", ()))
        rails.append(getattr(self, "home_list", None))
        for rail in rails:
            if rail is None:
                continue
            try:
                items = [rail.item(i) for i in range(rail.count())]
                # Back to the delegate's own hint first, or the widths
                # read below would be last fold's pinned ones.
                for item in items:
                    item.setSizeHint(QSize())
                if not items or not self._sidebar_collapsed:
                    continue
                # **Only measure a rail that is actually at rail width.**
                # This runs from _populate_nav_list and from the fold
                # *before* the width animation, where the rows are still
                # 184px wide and pinning to them puts the icon 5.7px off
                # centre - worse than the 1.1px being fixed. The
                # post-animation call in _toggle_sidebar's landed() is
                # the one that does the work.
                if rail.viewport().width() > SIDEBAR_COLLAPSED_WIDTH:
                    continue
                rail.doItemsLayout()
                glyph = [rail.visualItemRect(item).width() for item in items
                         if not item.data(VECTOR_ROLE)]
                if not glyph:
                    continue        # nothing to line up against
                target = min(glyph)
                for item in items:
                    if item.data(VECTOR_ROLE):
                        height = rail.visualItemRect(item).height()
                        item.setSizeHint(QSize(target, height))
            except RuntimeError:
                pass                # the rail is being torn down

    def _build_fold_button(self):
        """One fold toggle - the main bar and the section bar each carry
        their own copy (the section bar had none at all, so folding from
        a tracker page meant going Back first - the owner's ask), and
        _sync_fold_buttons keeps the pair reading the same state."""
        button = QPushButton(objectName="FoldButton")
        # 24, down from 28, at the owner's ask ("just a bit smaller").
        # Both bars keep the one size: they sit one above the other on
        # Read and Watch, where two chevrons of different sizes read as
        # two different controls.
        button.setFixedSize(24, 24)
        use_hover_cursor(button)
        button.clicked.connect(self._toggle_sidebar)
        return button

    def _sync_fold_buttons(self):
        """Both bars' fold arrows follow the one collapsed state - a
        single Fluent arrow pointing the way the press will move the
        edge, not the guillemet chevrons (the owner's ask)."""
        collapsed = self._sidebar_collapsed
        glyph = FOLD_OPEN_ICON if collapsed else FOLD_CLOSE_ICON
        tip = "Expand sidebar" if collapsed else "Collapse sidebar"
        for button in (getattr(self, "fold_btn", None),
                       getattr(self, "section_fold_btn", None)):
            if button is not None:
                button.setText(glyph)
                button.setToolTip(tip)

    def _build_sidebar(self, parent):
        # Parented at construction rather than reparented after: a
        # setParent later marks the widget hidden and it would need an
        # explicit show(), which is one more thing to forget.
        sidebar = QWidget(objectName="Sidebar", parent=parent)
        self.sidebar = sidebar
        self._sidebar_collapsed = False
        self._sidebar_anim = None
        self._fold_in_flight = False
        # Set before anything styles the Settings button, which reads it
        # for its tooltip. Filled in by the startup update check.
        self._pending_update_version = ""
        # Same, for the Downloads button: how many downloads are running
        # and what to say about them. Both empty until the first poll.
        self._download_count = 0
        self._download_tooltip = ""
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(SIDEBAR_LAYOUT_SPACING)

        # Collapse/expand toggle, pinned top-right of the sidebar so it
        # stays put (and stays reachable) at either width. Its glyph and
        # tooltip come from _sync_fold_buttons, shared with the section
        # bar's twin.
        self.fold_btn = self._build_fold_button()
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
        self.home_list = self._make_rail_list(draggable=False)
        home_item = QListWidgetItem()
        self.home_list.addItem(home_item)
        self._style_nav_item(home_item, home_name, home_page)
        self.home_list.itemClicked.connect(lambda: self.navigate_to("home"))
        # NavListWidget.sizeHint() pads in a generous +12 "safety margin"
        # below the last row (see its docstring) - fine for a nav list's
        # multi-row case, but on a single-item list that margin is just
        # dead space, widening the visible gap before the next item well
        # past the ~spacing()px gap between every other pair of items.
        # A minimal explicit height (one row + its own top/bottom inset)
        # keeps that gap consistent instead.
        self._sync_home_list_height()
        layout.addWidget(self.home_list)

        # **One row of air, then the blocks** (the owner's ask, 22 August
        # 2026 - see nav_config.NAV_GROUPS for what the blocks are and
        # why). A gap widget rather than layout.addSpacing(): the gap is
        # "one button", and a button is a different height folded than
        # unfolded, so it has to be re-measured (see _sync_rail_gaps)
        # rather than frozen at build time.
        self._rail_gaps = []
        # This bar's own gaps, indexed to match nav_lists: [0] is the one
        # under Home, and [i] is the one above block i. Kept apart from
        # _rail_gaps (which is every gap in the window, for the height
        # sync) so hiding a block's gap cannot reach into the section
        # bar's - which is exactly what happened when the two were one
        # list and _refresh_nav_list ran.
        self._nav_gaps = []
        layout.addWidget(self._make_nav_gap())

        # One list per block, not one list with separators in it: a
        # QListWidget in InternalMove mode will happily drop a row past
        # anything sitting in the list, so a spacer row would be
        # draggable, land anywhere, and take the grouping with it. Split
        # this way, a drag reorders its own block and cannot leave it.
        self.nav_lists = []
        for index, group in enumerate(visible_nav_groups()):
            if index:
                layout.addWidget(self._make_nav_gap())
            nav_list = self._make_rail_list(draggable=True)
            nav_list.itemClicked.connect(self._on_nav_item_clicked)
            nav_list.model().rowsMoved.connect(self._on_nav_reordered)
            self.nav_lists.append(nav_list)
            layout.addWidget(nav_list)
        self._populate_nav_list()

        # Add and Settings both sit at the very bottom, Add directly
        # above Settings, with the stretch above them pushing the pair
        # down clear of the nav list.
        layout.addStretch()

        # No Add button here any more (the owner's ask). The menu itself
        # survives - Ctrl+N still opens it (see the shortcut, which calls
        # _open_add_menu) and each page carries its own Add - so the
        # sidebar's foot is Downloads and Settings alone.
        self._add_menu = QMenu(self)
        self._add_menu.aboutToShow.connect(self._build_add_menu)

        self._build_utility_footer(layout, primary=True)

        self._downloads_timer = QTimer(self)
        self._downloads_timer.timeout.connect(self.refresh_download_indicator)
        self._downloads_timer.start(DOWNLOAD_IDLE_POLL_MS)

        # A download left running from the last session (or queued by a
        # player window opened before this one) should be visible on the
        # first frame, not after the first poll.
        self.refresh_download_indicator()

    # ---- The contextual section sidebar ------------------------------
    def _build_section_sidebar(self, parent):
        """The bar that takes the main sidebar's place over a sectioned
        page: the fold toggle and Back on top, a separator, then the
        page's SECTIONS as a nav list. A NavListWidget, deliberately the
        very widget the main sidebar's content is (the owner's ask -
        "make the sidebars content draggable"): same rows, same 13pt nav
        face, same drag-to-reorder, with the picked order persisted
        through app_settings.get/set_section_order. Built once, hidden;
        the rows are (re)filled per page by _sync_section_list, since
        SECTIONS is read generically off whatever page is showing."""
        bar = QWidget(objectName="Sidebar", parent=parent)
        self.section_sidebar = bar
        layout = QVBoxLayout(bar)
        # The main sidebar's own margins/spacing, so rows here sit at
        # exactly the x its rows do and the swap reads as one column
        # changing contents, not two different panels.
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(SIDEBAR_LAYOUT_SPACING)

        # The same fold control the main bar carries, in the same corner
        # - this bar had none, so folding the rail from a tracker page
        # meant pressing Back first (the owner's ask).
        self.section_fold_btn = self._build_fold_button()
        fold_row = QHBoxLayout()
        fold_row.setContentsMargins(0, 0, 0, 0)
        fold_row.addStretch()
        fold_row.addWidget(self.section_fold_btn)
        layout.addLayout(fold_row)

        self.section_back_btn = QPushButton(objectName="NavButton")
        self.section_back_btn.setFixedHeight(40)
        self.section_back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        use_hover_cursor(self.section_back_btn)
        self.section_back_btn.clicked.connect(self._section_back)
        layout.addWidget(self.section_back_btn)

        layout.addSpacing(6)
        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background: {theme.BORDER}; border: none;")
        layout.addWidget(separator)
        layout.addSpacing(6)

        # **Three blocks with a row of air between them** (the owner's
        # ask, 22 August 2026, spelled out row by row): Discover, then
        # the categories, then Saved / Schedule / History. A page names
        # its blocks in SECTION_GROUPS; SECTIONS stays the flat tuple
        # everything that only wants the keys reads.
        #
        # One list per block for the same reason the main bar has one
        # per block: a QListWidget in InternalMove mode will drop a row
        # past anything in the list, so a spacer row would be draggable
        # and the grouping would last until the first drag.
        self.section_lists = []
        for index in range(SECTION_BLOCKS):
            if index:
                layout.addWidget(self._make_rail_gap())
            rail = self._make_rail_list(draggable=True)
            rail.itemClicked.connect(self._on_section_item_clicked)
            rail.model().rowsMoved.connect(self._on_sections_reordered)
            self.section_lists.append(rail)
            layout.addWidget(rail)
        layout.addStretch()

        # This bar's own Downloads and Settings (the owner's ask): the
        # section bar takes the main one's place over a tracker page, and
        # without a pair here reaching either meant pressing Back first.
        self._build_utility_footer(layout, primary=False)

        self._section_labels = {}    # key -> label, for restyles on fold
        self._section_keys = None    # what the list was last filled for
        self._style_section_bar()
        bar.hide()

    def _style_section_bar(self):
        """Restyle this bar for the current sidebar width: the Back
        button gets the Downloads/Settings two-width treatment, and the
        section rows restyle exactly as the main nav rows do. Called on
        build, on refill, and from _toggle_sidebar - not on every
        navigation, since unpolish/polish makes a button flicker when
        repeated for no new information."""
        collapsed = self._sidebar_collapsed

        button = self.section_back_btn
        button.setText(SECTION_BACK_ICON if collapsed
                       else f"  {SECTION_BACK_ICON}   Back")
        button.setFont(theme.icon_font() if collapsed else theme.icon_font(10))
        button.setToolTip("Back" if collapsed else "")
        button.setProperty("collapsed", collapsed)
        button.style().unpolish(button)
        button.style().polish(button)

        for rail in self.section_lists:
            for row in range(rail.count()):
                item = rail.item(row)
                key = item.data(Qt.ItemDataRole.UserRole)
                self._style_rail_item(item, self._section_labels.get(key, key),
                                      SECTION_ICONS.get(key, theme.NAV_BULLET))
            rail.updateGeometry()
        self._sync_rail_gaps()
        self._sync_rail_icon_widths()

    def _sync_section_list(self, page):
        """Make the section rows match `page`: refill when its SECTIONS
        differ from what is built (cheap - it never happens between the
        tracker pages, which share one tuple), ordered by the user's
        saved drag order with the page's own order for anything new, and
        re-highlight from the page's current_section() every time - each
        page remembers its own section in session state, so crossing
        between tracker pages can change the highlight without a click."""
        sections = tuple(getattr(page, "SECTIONS", ()) or ())
        keys = tuple(key for key, _label in sections)
        if keys != self._section_keys:
            by_key = dict(sections)
            groups = getattr(page, "SECTION_GROUPS", None) or (sections,)
            saved = app_settings.get_section_order()
            for index, rail in enumerate(self.section_lists):
                block = tuple(groups[index]) if index < len(groups) else ()
                block_keys = [key for key, _label in block]
                # The saved order is one flat list and a block only ever
                # reorders within itself, so a block's order is that list
                # filtered to it, with anything the user has never
                # dragged (a category added in a later version) keeping
                # the page's own order behind it.
                order = [k for k in saved if k in block_keys]
                order += [k for k in block_keys if k not in order]
                rail.blockSignals(True)
                rail.clear()
                for key in order:
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, key)
                    rail.addItem(item)
                rail.blockSignals(False)
                rail.setVisible(bool(order))
            self._section_labels = by_key
            self._section_keys = keys
            self._style_section_bar()
        getter = getattr(page, "current_section", None)
        current = getter() if callable(getter) else None
        for rail in self.section_lists:
            match = None
            for row in range(rail.count()):
                item = rail.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == current:
                    match = item
                    break
            # Cleared on every block that does not hold it, or two rows
            # read as active at once - one list's selection knows
            # nothing about another's.
            if match is not None:
                rail.setCurrentItem(match)
            else:
                rail.clearSelection()

    def _on_section_item_clicked(self, item):
        self._on_section_clicked(item.data(Qt.ItemDataRole.UserRole))

    def _on_sections_reordered(self, *_args):
        # Every block, in block order - the saved order is one flat list
        # and a drag never crosses a block, so concatenating is the
        # whole of the merge (see _sync_section_list).
        order = [rail.item(i).data(Qt.ItemDataRole.UserRole)
                 for rail in self.section_lists
                 for i in range(rail.count())]
        app_settings.set_section_order(order)

    def _on_section_clicked(self, key):
        """Switch the showing page's section - never navigate. The
        highlight is re-read off the page afterwards rather than trusted
        to the click, because the page may coerce an unknown key back to
        its default."""
        page = self._current_page
        setter = getattr(page, "set_active_section", None)
        if callable(setter):
            setter(key)
        self._sync_section_list(page)

    def _section_back(self):
        """The Back button on a section bar goes **Home**, always.

        It used to be history back, which is why it could land anywhere
        - Read after Watch, or the page a global search came from - and
        the owner asked for one destination: "when going back from read
        or watch make it always go to the home page not the last
        visited". The reader's and the player's doors already land there
        (reader.HOME_PAGE), so this makes every way out of a section
        agree.

        History back itself is untouched and still on Alt+Left and the
        mouse's back button, which are the two places it reads as
        "undo my last move" rather than as "leave this section"."""
        self.navigate_to(HOME_PAGE_NAME)

    def _settle_swap(self):
        """Stop any swap in flight and snap both bars to the settled
        geometry for the current _section_bar_showing. Also the finish
        handler of every swap - one place computes end-state geometry,
        so an interrupted swap and a completed one land identically."""
        slide, self._bar_slide = self._bar_slide, None
        if slide is not None:
            # The tween directly, not slide.stop(): stop() runs the
            # done-callback, which is this method.
            slide._tween.stop()
            slide.hide()
            slide.deleteLater()
        width = self.sidebar_holder.width()
        shown = self.section_sidebar if self._section_bar_showing else self.sidebar
        hidden = self.sidebar if self._section_bar_showing else self.section_sidebar
        hidden.hide()
        hidden.move(-width, 0)
        shown.move(0, 0)
        shown.show()

    def _sync_section_sidebar(self, page, animate=True):
        """Give the column to whichever bar `page` calls for: the section
        bar over a page exposing SECTIONS, the main one over everything
        else. Both slides use the left edge - the outgoing bar slides
        out beneath while the incoming one slides in on top - and the
        holder's width never changes, so the page content to the right
        does not move (measured: see the swap harness)."""
        sectioned = getattr(page, "SECTIONS", None) is not None
        if sectioned:
            self._sync_section_list(page)
        if sectioned == self._section_bar_showing:
            return
        self._settle_swap()
        self._section_bar_showing = sectioned
        incoming = self.section_sidebar if sectioned else self.sidebar
        outgoing = self.sidebar if sectioned else self.section_sidebar
        holder = self.sidebar_holder
        width, height = holder.width(), holder.height()
        incoming.resize(width, height)
        outgoing.resize(width, height)
        # Instant when asked (the startup page, refresh_current_page) and
        # before the window has real geometry, where an animation would
        # ease between meaningless rectangles.
        if not animate or width <= 0 or not self.isVisible():
            self._settle_swap()
            return
        # **Painted once each and blitted, for the same reason the page
        # stack is** - see widgets.PageSlide. Animating two full
        # sidebars' `pos` repainted every nav row, glyph and button on
        # both bars on every step, and it runs *concurrently* with the
        # page slide: with the pages already composited, the bars were
        # still contributing 70 QPushButton and 43 QLabel paints to a
        # 220ms transition (measured 22 August 2026).
        incoming.move(-width, 0)
        incoming.show()
        incoming.raise_()
        outgoing_shot = outgoing.grab()
        incoming_shot = incoming.grab()
        incoming.hide()
        outgoing.hide()
        # _settle_swap above already ended any swap in flight, and it is
        # also this one's finish handler - one place computes end-state
        # geometry, so an interrupted swap and a completed one land
        # identically.
        slide = PageSlide(holder, outgoing_shot, incoming_shot, -1,
                          SIDEBAR_ANIM_MS, axis="x",
                          on_done=self._settle_swap)
        self._bar_slide = slide
        slide.start()

    def _layout_sidebars(self):
        """Track the holder: both bars are positioned by hand (they
        slide over each other on a swap) rather than sitting in a
        layout, so nothing resizes them automatically - the same reason
        the pages follow self.container in eventFilter. Mid-swap only
        sizes are touched; the animation owns x, and _settle_swap
        re-derives positions when it ends."""
        holder = self.sidebar_holder
        width, height = holder.width(), holder.height()
        for bar in (self.sidebar, self.section_sidebar):
            bar.resize(width, height)
        if self._bar_slide is not None:
            return
        shown = self.section_sidebar if self._section_bar_showing else self.sidebar
        hidden = self.sidebar if self._section_bar_showing else self.section_sidebar
        shown.move(0, 0)
        hidden.move(-width, 0)

    def _position_update_dot(self):
        """Top-right corner of every Settings button. Re-run on each
        resize of one (see eventFilter) rather than placed once: folding
        the sidebar animates its width from 220 to 68, and a dot placed
        at the old width would sit outside the collapsed rail."""
        for pair in getattr(self, "_utility_bars", ()):
            pair["update_dot"].move(
                pair["settings"].width() - UPDATE_DOT_SIZE - UPDATE_DOT_MARGIN,
                UPDATE_DOT_MARGIN)

    # ------------------------------------------------------------------
    def _style_nav_item(self, item, name, page_name):
        self._style_rail_item(item, name,
                              theme.NAV_ICONS.get(page_name, theme.NAV_BULLET))

    def _style_rail_item(self, item, name, glyph):
        """Harbor's row language, shared by the nav list and the section
        list (they are the same widget on purpose): the row's Fluent
        glyph leads the label when expanded (replacing the
        one-shape-for-every-row ◈ bullet), and the collapsed rail shows
        the same glyph alone, centred, with the label moved to a tooltip
        - one symbol per row at both widths. The expanded font is a
        two-family chain (theme.nav_row_font): the glyph resolves from
        the icon face and the label falls through to the nav face,
        because an item carries exactly one font.

        Two rows have no glyph to type - Saved and Anime, whose symbols
        this icon font does not carry (see VECTOR_ICONS). Those are
        handed to Qt as the item's *decoration* instead, drawn by
        helpers/rail_icons and coloured by _RailDelegate, and the row is
        otherwise laid out identically: icon then label expanded, icon
        alone and centred collapsed."""
        vector = VECTOR_ICONS.get(glyph) or (
            glyph if glyph in VECTOR_ICONS.values() else "")
        if vector:
            # No lead in the folded rail: there the row is the icon and
            # it is centred, so blank columns on one side would push it
            # off centre by half of them.
            collapsed_row = self._sidebar_collapsed
            lead = 0 if collapsed_row else VECTOR_ICON_LEAD
            ink_left = (VECTOR_ICON_INK_LEFT_COLLAPSED if collapsed_row
                        else VECTOR_ICON_INK_LEFT)
            icon_size = (VECTOR_ICON_SIZE_COLLAPSED if collapsed_row
                         else VECTOR_ICON_SIZE)
            item.setData(VECTOR_ROLE, vector)
            item.setIcon(rail_icons.icon(vector, icon_size,
                                         theme.TEXT_MUTED, theme.TEXT, lead,
                                         ink_left))
        if self._sidebar_collapsed:
            item.setText("" if vector else glyph)
            item.setFont(theme.icon_font())
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setToolTip(name)
        else:
            # Two spaces, not three: with the wider glyph leading, three
            # pushed "Movies & Series" past the 220px column and elided
            # it (measured on a real-window grab). A drawn row spends no
            # spaces at all - Qt's own decoration-to-text gap already
            # sits it where the glyph rows' labels start (measured: 1px
            # apart, see the rail alignment probe).
            item.setText(name if vector else f"{glyph}  {name}")
            item.setFont(theme.nav_row_font())
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
        for pair in getattr(self, "_utility_bars", ()):
            button = pair["downloads"]
            button.setText(glyph if collapsed
                           else f"  {glyph}   Downloads{count}")
            button.setFont(theme.icon_font() if collapsed
                           else theme.icon_font(10))
            button.setToolTip(self._download_tooltip
                              or ("Downloads" if collapsed else ""))
            button.setProperty("collapsed", collapsed)
            button.style().unpolish(button)
            button.style().polish(button)

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

        bars = getattr(self, "_utility_bars", ())
        if active is None:
            for pair in bars:
                pair["bar"].hide()
                pair["dot"].hide()
            count, tooltip = 0, ""
        else:
            count = int(active.get("count") or 0)
            try:
                fraction = float(active.get("progress") or 0.0)
            except (TypeError, ValueError):
                fraction = 0.0
            for pair in bars:
                # The badge rides its button's own top-right corner;
                # placed here because the button's width depends on the
                # sidebar state and this is the one place that runs on
                # every change. Width follows the digits - "12" in a
                # 16px disc clips.
                dot, button = pair["dot"], pair["downloads"]
                dot.setText(str(count) if count else "")
                width = 16 if count < 10 else 22
                dot.setFixedSize(width, 16)
                dot.move(button.width() - width - 2, 0)
                dot.show()
                dot.raise_()
                pair["bar"].setValue(max(0, min(1000, int(fraction * 1000))))
                pair["bar"].show()
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
        for pair in getattr(self, "_utility_bars", ()):
            button = pair["settings"]
            button.setText(theme.SETTINGS_ICON if collapsed
                           else f"  {theme.SETTINGS_ICON}   Settings")
            button.setFont(theme.icon_font() if collapsed
                           else theme.icon_font(10))
            # A waiting update outranks the usual tooltip at either
            # width - the dot says something is there, this says what.
            # Expanded, the button already reads "Settings", so there is
            # otherwise nothing to add and the tooltip stays empty.
            if self._pending_update_version:
                button.setToolTip(
                    f"Atomic {self._pending_update_version} is available")
            else:
                button.setToolTip("Settings" if collapsed else "")
            # Drives the [collapsed="true"] QSS rule; Qt only
            # re-evaluates property-based selectors after an explicit
            # unpolish/polish.
            button.setProperty("collapsed", collapsed)
            button.style().unpolish(button)
            button.style().polish(button)

    def _toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        collapsed = self._sidebar_collapsed

        self._sync_fold_buttons()
        self.logo_label.setVisible(not collapsed)
        self._style_downloads_btn()
        self._style_settings_btn()
        # The section bar can only be off screen while this button is
        # reachable, but the collapsed state has to be waiting on it when
        # a tracker page next slides it in.
        self._style_section_bar()

        # Restyled in place rather than rebuilt, so the user's drag order
        # and the current selection both survive the fold.
        home_name, home_page = HOME_ITEM
        self._style_nav_item(self.home_list.item(0), home_name, home_page)
        self._sync_home_list_height()
        for rail, rows in zip(self.nav_lists, visible_nav_groups()):
            for row, (name, page_name) in enumerate(rows):
                item = rail.item(row)
                if item is not None:
                    self._style_nav_item(item, name, page_name)
            rail.updateGeometry()
        # A folded row is shorter than an expanded one, so the blocks'
        # gaps are re-measured rather than left at the other width's.
        self._sync_rail_gaps()
        self._sync_rail_icon_widths()

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
        # only one would let the other clamp the result. The holder, not
        # the bar: the bars are its manually placed children now
        # (_build_sidebars) and follow every width change through
        # _layout_sidebars, so this stays one animation however many
        # bars are in the column.
        # Driven at the screen's refresh rate rather than Qt's animation
        # clock (widgets.SmoothTween): the fold stepped for exactly the
        # reason the wheel and the sideways rows did - 60 positions a
        # second on a 144Hz panel, so every one was shown two or three
        # times. Same duration, same curve, 2.4x the steps.
        holder = self.sidebar_holder

        def apply(value):
            width = int(round(value))
            holder.setMaximumWidth(width)
            # Both, or the other one clamps the result: setFixedWidth
            # pins min and max together, so driving one alone does
            # nothing until the other is moved too.
            holder.setMinimumWidth(width)

        def landed():
            holder.setFixedWidth(SIDEBAR_COLLAPSED_WIDTH
                                 if self._sidebar_collapsed else SIDEBAR_WIDTH)
            self._fold_in_flight = False
            self._fit_current_page()
            # **After the fold, not before it.** _sync_rail_icon_widths
            # pins a drawn row to what its glyph neighbours measure, and
            # measuring is only meaningful once the rail is at its final
            # width: called before the animation it read the *expanded*
            # 184px row and pinned the Saved icon to it, which put the
            # bookmark 5.7px off centre in a 36px rail instead of 1.1px
            # - worse than the bug it fixes (measured).
            self._sync_rail_icon_widths()

        if self._sidebar_anim is not None:
            self._sidebar_anim.stop()
        else:
            self._sidebar_anim = SmoothTween(holder, apply, SIDEBAR_ANIM_MS,
                                             on_done=landed)
        # Pin the page at the widest the container reaches during this
        # fold, before the first step - see _fit_current_page.
        self._fold_in_flight = True
        if self._current_page is not None:
            widest = self.container.rect()
            widest.setWidth(max(widest.width(),
                                self.width() - SIDEBAR_COLLAPSED_WIDTH))
            self._current_page.setGeometry(widest)
        self._sidebar_anim.start(holder.width(), target)

    def _populate_nav_list(self):
        """Fill every block from nav_config, and hide a block that has
        nothing left in it - along with the gap above it, or hiding all
        of Apps/Websites in Settings would leave the rail ending in two
        rows of air."""
        groups = visible_nav_groups()
        for index, rail in enumerate(self.nav_lists):
            rows = groups[index] if index < len(groups) else []
            rail.clear()
            for name, page_name in rows:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, page_name)
                rail.addItem(item)
                self._style_nav_item(item, name, page_name)
            rail.setVisible(bool(rows))
            rail.updateGeometry()
        # Gap 0 sits under Home and always shows; gap i belongs to
        # block i and goes with it.
        for index, gap in enumerate(getattr(self, "_nav_gaps", ())):
            if index == 0:
                continue
            rail = (self.nav_lists[index]
                    if index < len(self.nav_lists) else None)
            # count(), not isVisible(): this bar is hidden whenever the
            # section bar has the column, and isVisible() answers for
            # the *parent* there - so every gap was being explicitly
            # hidden while a tracker page was showing and stayed hidden
            # after it (measured with the hide/restore probe).
            gap.setVisible(rail is not None and rail.count() > 0)
        self._sync_rail_gaps()
        self._sync_rail_icon_widths()

    def _refresh_nav_list(self):
        """Called by Settings when a section is hidden/unhidden, so the
        sidebar updates immediately instead of needing a restart."""
        for rail in self.nav_lists:
            rail.blockSignals(True)
        self._populate_nav_list()
        for rail in self.nav_lists:
            rail.blockSignals(False)
        self._sync_nav_highlight(self._history[self._history_index])

    def _on_nav_item_clicked(self, item):
        self.navigate_to(item.data(Qt.ItemDataRole.UserRole))

    def _on_nav_reordered(self, *_args):
        # Every block, in block order: the saved order is one flat list
        # (Home reads it too, see nav_config.ordered_nav_items) and a
        # block only ever reorders within itself, so concatenating them
        # is the whole of the merge.
        order = [
            rail.item(i).data(Qt.ItemDataRole.UserRole)
            for rail in self.nav_lists
            for i in range(rail.count())
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

    def _top_overlay(self):
        """The full-window surface currently covering the pages, if any.

        The player, the reader, the details page and the genre browse
        are hand-placed children of the central widget rather than
        entries in the page stack, so "what is on top" cannot be read
        from the history - it is whichever of them is visible."""
        host = self.centralWidget()
        player = getattr(self, "_player_page", None)
        if player is not None:
            try:
                if not player.isHidden():
                    return player
            except RuntimeError:
                pass
        if host is None:
            return None
        newest = None
        for child in host.children():
            # isHidden, not isVisible: isVisible is False for every child
            # while the top-level window itself is not shown, which is
            # true of a minimised window and of any offscreen run. What
            # is actually being asked is whether this overlay hid itself
            # - which is exactly what leave() does.
            if not isinstance(child, QWidget) or child.isHidden():
                continue
            # Anything that knows how to leave itself is an overlay;
            # duck-typed rather than imported, so main.py does not gain
            # a top-level import of every page that can cover it.
            if callable(getattr(child, "leave", None)):
                newest = child
        return newest

    def navigate_back(self):
        """Mouse button 4, from anywhere.

        Over an overlay it leaves that surface - the player, the reader,
        the details page - because the thing on screen is what "back"
        plainly means there. Over an ordinary page it is history back,
        exactly as Alt+Left has always been."""
        overlay = self._top_overlay()
        if overlay is None:
            self.go_back()
            return
        for name in ("close_player", "leave"):
            action = getattr(overlay, name, None)
            if callable(action):
                try:
                    action()
                except Exception:
                    logs.exception("Could not leave the overlay")
                return
        self.go_back()

    def navigate_forward(self):
        """Mouse button 5. An overlay has nothing to go forward *to*, so
        this is history forward and only that."""
        if self._top_overlay() is None:
            self.go_forward()

    def _open_add_menu(self):
        """Pop the Add menu without a button to hang it on.

        The + button that used to own it is gone from the sidebar (the
        owner's ask), so Ctrl+N places the popup itself. The anchor
        comes from `window.geometry()`, which is already in global
        coordinates - never mapToGlobal, which divides by the *other*
        screen's scale factor on a mixed-DPI pair (.claude/rules/ui.md).
        """
        self._build_add_menu()
        frame = self.geometry()
        sidebar = getattr(self, "sidebar", None)
        width = sidebar.width() if sidebar is not None else SIDEBAR_WIDTH
        self._add_menu.popup(QPoint(frame.x() + width,
                                    frame.y() + frame.height() - 120))

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
        for pair in getattr(self, "_utility_bars", ()):
            pair["update_dot"].show()
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
        if confirm(self, "Update Available",
                   f"Atomic {version} is available.\n\nYou are running "
                   f"{updater.APP_VERSION}. Updating keeps your entries.",
                   yes_text="Open Settings", no_text="Later"):
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
        # **getattr, not attribute access.** A filter installed while the
        # window is still being built starts receiving events before the
        # rest of it exists: the sidebar footers install one on their
        # Settings button (see _build_utility_footer), which fires during
        # _build_sidebar, and `self.container` is not assigned until
        # after that. The AttributeError landed inside a Qt callback,
        # where it took the whole process down with no traceback at all -
        # exactly the failure helpers/logs.install_excepthook exists for.
        # One event.type() for the whole method: each call builds a
        # QEvent.Type enum member through Python's enum machinery, and
        # this used to ask four times per event.
        kind = event.type()
        if kind == QEvent.Type.Resize:
            if obj is getattr(self, "container", None):
                self._fit_current_page()
        # The sidebars are hand-positioned children of their holder for
        # the same reason the pages are of the container - they slide
        # over each other - so they too follow their parent's resizes
        # here (window resize, and the fold animation's width sweep).
            if obj is getattr(self, "sidebar_holder", None):
                self._layout_sidebars()
            elif any(obj is pair["settings"]
                     for pair in getattr(self, "_utility_bars", ())):
                self._position_update_dot()
        return super().eventFilter(obj, event)

    def _fit_current_page(self):
        if self._current_page is None:
            return
        if self._fold_in_flight:
            # **Not while the sidebar is folding.** Every step of that
            # animation resizes the container, and re-fitting the page
            # here re-lays out everything on it - on a tracker page that
            # is a grid of a hundred-odd cards, and it was the last big
            # cost left in a fold (measured: the tracker pages held
            # 63-75 positions a second against Home's 111).
            #
            # The page is pinned to the *widest* the container will be
            # during the fold instead (see _toggle_sidebar), so it is
            # never narrower than the container and no strip of bare
            # window can show through - the failure the container hook
            # above exists to prevent. Being wider is invisible: the
            # container clips it. The real fit happens once, when the
            # fold lands.
            return
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
                # The + button is gone from the sidebar (the owner's
                # ask) but the menu it opened is not - this shortcut is
                # what still reaches it. Popped at the sidebar's foot,
                # from the window's own geometry rather than
                # mapToGlobal (.claude/rules/ui.md).
                self._open_add_menu()
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
        page_name = _page_name(page_name)
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
        for pair in getattr(self, "_utility_bars", ()):
            pair["downloads"].setChecked(page_name == "downloads")
        if page_name == "home":
            self.home_list.setCurrentRow(0)
        else:
            self.home_list.clearSelection()
        for rail in self.nav_lists:
            match = None
            for i in range(rail.count()):
                item = rail.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == page_name:
                    match = item
                    break
            # Cleared on every block that does not hold the page, or the
            # rail would show two highlighted rows at once - one list's
            # selection knows nothing about the other's.
            if match is not None:
                rail.setCurrentItem(match)
            else:
                rail.clearSelection()

    def _show_page(self, page_name, direction="down", animate=True):
        page_name = _page_name(page_name)
        self._sync_nav_highlight(page_name)

        old_page = self._current_page
        new_page = PAGES[page_name](self)
        new_page.setParent(self.container)
        self._current_page = new_page

        # The sidebar column follows the page. Here rather than in
        # navigate_to, so every route to a page - history back/forward,
        # refresh_current_page, the very first page at startup - swaps
        # the bars too; matching `animate` keeps the two slides together
        # and makes the startup case instant.
        self._sync_section_sidebar(new_page, animate=animate)

        rect = self.container.rect()
        if not animate or old_page is None:
            new_page.setGeometry(rect)
            new_page.show()
            if old_page is not None:
                old_page.deleteLater()
            return

        # A navigation arriving while one is still running: end the old
        # slide immediately (its callback puts its page in place) rather
        # than leaving two compositors stacked.
        if self._page_slide is not None:
            self._page_slide.stop()
            self._page_slide = None

        # **Both pages are painted once, into pixmaps, and the slide is
        # two blits per frame.** Animating the widgets' `pos` re-rendered
        # every child on both pages every step - 64 to 109 paint events
        # per tick, measured, which is the whole of the owner's
        # "stuttering". See widgets.PageSlide for the numbers.
        #
        # "down" = the target sits below the source in the sidebar, so
        # the new page enters from below and slides up into place (like
        # scrolling down a page); "up" is the mirror image.
        new_page.setGeometry(rect)
        old_shot = old_page.grab()
        new_shot = new_page.grab()
        # Hidden, not moved: nothing behind the compositor should be
        # painting at all while it runs.
        new_page.hide()
        old_page.hide()

        def landed(page=new_page, previous=old_page):
            self._page_slide = None
            if slide is not None:
                slide.hide()
                slide.deleteLater()
            page.setGeometry(self.container.rect())
            page.show()
            previous.deleteLater()

        slide = PageSlide(self.container, old_shot, new_shot,
                          1 if direction == "down" else -1,
                          ANIM_DURATION_MS, on_done=landed)
        self._page_slide = slide
        slide.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_current_page()
        self._geometry_save_timer.start(GEOMETRY_SAVE_DELAY_MS)


def _merge_anime_into_series():
    """One-time data move behind the page merge: Anime rows lived in
    tracker.json beside the reading types, and the merged Movies &
    Series page reads series.json - so each page keeps exactly one file
    (which everything from storage.update_entry to the progress writers
    assumes). Runs before any page is built, is idempotent (once no
    Anime remains in tracker.json it does nothing), and never duplicates
    an id already present in series.json."""
    tracker_entries = storage.load("tracker.json", [])
    anime = [e for e in tracker_entries if e.get("type") == "Anime"]
    if not anime:
        return
    series_entries = storage.load("series.json", [])
    known = {e.get("id") for e in series_entries if e.get("id")}
    series_entries.extend(e for e in anime
                          if not e.get("id") or e.get("id") not in known)
    # Series first, so a crash between the two writes duplicates rather
    # than loses: the next launch re-runs this, sees the Anime rows
    # still in tracker.json, and the id guard above skips them.
    storage.save("series.json", series_entries)
    storage.save("tracker.json",
                 [e for e in tracker_entries if e.get("type") != "Anime"])


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
    for game in storage.load("games.json", []):
        if game.get("icon"):
            specs.append((game["icon"], home_icon_size))
        if game.get("cover"):
            specs.append((game["cover"], game_cover_size))
    for entry in storage.load("apps.json", []):
        # The Apps grid draws art (or the icon standing in for it)
        # square at poster width; Home's quick list still draws the
        # small icon.
        art = entry.get("art") or entry.get("image")
        if art:
            specs.append((art, app_art_size))
        if entry.get("image"):
            specs.append((entry["image"], home_row_icon_size))
    for entry in storage.load("websites.json", []):
        if entry.get("image"):
            specs.append((entry["image"], link_thumb_size))
            specs.append((entry["image"], home_row_icon_size))
    return specs


# How long after the window is up before the overlay modules are
# imported. Long enough that the first paint and the page's own lookups
# have had the thread, short enough to be finished before anyone has
# aimed at a card.
PRELOAD_OVERLAYS_MS = 400


def _preload_overlays():
    """Import the player, reader and details modules ahead of the first
    click that needs one. Never raises: a failed preload just means the
    old lazy import happens at click time, exactly as before."""
    for name in ("windows.details", "windows.reader", "windows.player"):
        try:
            __import__(name)
        except Exception:
            logs.exception(f"could not preload {name}")


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
    # The wheel over a page's dead margins - the strip right of the
    # scrollbar above all (the owner's ask). One filter for every page,
    # kept alive by the app it is parented to.
    install_edge_wheel(app)

    # Before the window exists: the pages it builds each load their own
    # file, and the move has to be finished before either looks.
    _merge_anime_into_series()

    window = MainWindow()
    # Mouse 4/5 as back/forward across every surface, overlays included
    # (see _MouseNavFilter). Parented to the window, so it lives exactly
    # as long as the thing it navigates.
    app.installEventFilter(_MouseNavFilter(window))
    # Anything still queued when the app last closed starts again now
    # (the owner's ask). _load has already turned a stale RUNNING back
    # into QUEUED; nothing was waking the worker.
    try:
        downloads.resume_pending()
    except Exception:
        logs.exception("Could not resume the download queue")
    # The torrent session's DHT, bootstrapped now rather than on the
    # first press of an episode. Measured 2.44s to a usable routing
    # table from cold, paid once per run - which is exactly the pause
    # the owner sees on the first source and never again after it.
    # Off the UI thread and soft: see torrent_engine.warm.
    try:
        from helpers import torrent_engine
        torrent_engine.prewarm()
    except Exception:
        logs.exception("Could not warm the torrent session")
    # The three overlay modules, imported now rather than inside the
    # click that opens one. **They are imported lazily on purpose** (see
    # tracker.open_in_app) and that is still right - it keeps them off
    # the startup path - but the first press then pays the import on the
    # UI thread with the pointer already down: measured 119ms for
    # windows.details alone in the source tree, and a frozen build reads
    # its modules out of a zip. Opening a details page is otherwise
    # 48-108ms, so the import was most of the "~1 second to enter watch
    # or read".
    #
    # On the main thread, not a worker: these build QPixmap/QIcon objects
    # at import time and Qt's image classes are not safe to touch from
    # another thread. Off a timer so it lands after the first frame is
    # on screen rather than delaying it.
    QTimer.singleShot(PRELOAD_OVERLAYS_MS, _preload_overlays)
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
    # First-ever launch only: offer the setup wizard over the visible
    # window. Armed after whats_new on purpose - that dialog is modal,
    # and a timer armed before it would fire inside its nested event
    # loop. Existing installs are stamped silently and never see it
    # (the decision lives in setup_wizard._offer, not here).
    setup_wizard.show_on_first_run(window)
    # Started after the window is up, so it fills the time the user
    # spends looking at Home rather than delaying it appearing.
    images.prewarm(_prewarm_image_specs())
    # Same idea, same moment, for the Discover rows: the owner's "it
    # takes a few sec to show the lists" was never a slow fetch, only a
    # fetch that had not been started until the page was opened. Warmed
    # here, Read and Watch draw their rows on the first paint. Onto the
    # bounded pool, so it cannot crowd out anything a visible page asks
    # for (see tracker.prewarm_discover).
    try:
        tracker_module.prewarm_discover()
    except Exception:
        logs.exception("Could not prewarm the Discover rows")
    # Last, and on its own delay: the one thing here nobody is waiting
    # for. After whats_new deliberately - that dialog is modal, so a
    # timer armed before it would tick inside its nested event loop.
    window.schedule_update_check()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
