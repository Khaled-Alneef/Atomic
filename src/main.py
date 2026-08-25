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

from helpers import (app_settings, downloads, global_search, images, layout, logs,
                     native_cursor, setup_wizard, startup,
                     storage, theme, updater, whats_new)
from helpers.nav_config import (HOME_ITEM, nav_position, route_page,
                                route_section, visible_nav_groups,
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
from PyQt6.QtGui import QCursor, QFont, QIcon, QPainter, QPixmap
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
from helpers.widgets import (PageSlide, SmoothTween, confirm, hold_hover_cursor,
                             install_edge_wheel,
                             install_horizontal_wheel_guard,
                             release_hover_cursor,
                             install_stray_window_guard,
                             release_stale_hover_cursors, scroll_area,
                             show_toast, take_live_redo, take_live_undo,
                             use_hover_cursor, warm_display_clock)
from windows import home as home_page_module
from windows import link_grid as link_grid_module
from windows import tracker as tracker_module
from windows.apps import AppsPage
from windows.downloads_page import DownloadsPage
from windows.games import GamesPage
from windows.home import HomePage
from windows.tracker import DiscoverPage, MangaPage, SeriesPage
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
#
# **Read inside the function, not here.** These are module-level
# statements, so they used to run at import - which is before main()
# constructs the QApplication and therefore before layout.adopt() can
# size a card against the screen it will be drawn on. Bound here, the
# prewarm would go on decoding 160x216 tiles for a grid drawing 216x292
# ones, and every one of them would be decoded a second time on the UI
# thread, which is precisely the cost prewarm exists to avoid.
home_icon_size = home_page_module.ICON_SIZE
home_row_icon_size = home_page_module.ROW_ICON_SIZE
link_thumb_size = link_grid_module.THUMB_SIZE

# The one page every way out of a section lands on - the section Back
# button here, and the reader's and player's doors (reader.HOME_PAGE).
HOME_PAGE_NAME = "home"

PAGES = {
    "home": HomePage,
    # No "anime" page: it merged into "series" (one watch page under the
    # camera glyph, the owner's ask). Anything still asking for it by
    # name - an old saved nav order, a stale history entry - resolves
    # through _page_name below rather than KeyErroring mid-navigation.
    # One Discover for both media - the sidebar's own row (see
    # nav_config.NAV_GROUPS).
    "discover": DiscoverPage,
    "manga": MangaPage,
    "series": SeriesPage,
    "games": GamesPage,
    "apps": AppsPage,
    "websites": WebsitesPage,
    "downloads": DownloadsPage,
}


def _page_name(name: str) -> str:
    """The page a route names, as a key that exists today.

    Routes are `page` or `page:section` since the section rail was folded
    into the main one (see nav_config.NAV_GROUPS), so everything that
    looks a page up has to take the half in front of the colon. "anime"
    merged into "series" long before that; saved nav orders, session
    histories and shortcuts may still say it."""
    page = route_page(name)
    return "series" if page == "anime" else page


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
# **84, up from 68, alongside RAIL_ICON_SIZE 26 -> 40** (the owner, 24
# August 2026: "make the sidebar icons in the main page/ reading /
# watching 50% larger"). The two move together by the ceiling recorded
# at RAIL_ICON_SIZE: a folded row lays out at viewport - 2*spacing and
# the delegate only centres an icon at or under row - 2*margin, so a
# 40px icon needs a 46px row, a 50px viewport, and the collapsed rail
# holds viewport + 32px of holder chrome (measured 36 in 68). See
# RAIL_ICON_SIZE for the centring measurement 84 was chosen against.
#
# **72, up from 68 - the owner's ask, 25 August 2026: "make the folded
# sidebar icons 10% larger".** The same arithmetic as the 84/40 pairing
# above, run for the 29px folded icon (RAIL_ICON_SIZE_FOLDED): the
# delegate centres an icon only at or under `row - 2*margin`, margin is
# 3, and a row lays out at `viewport - 2*spacing` with spacing 2 - so 29
# needs a 35px row, a 39px viewport, and 39 + 32 of holder chrome is 71.
# 72 rather than 71 keeps the width even, which is what puts the row's
# true centre on a whole pixel (the +0.5px floor recorded at
# RAIL_ICON_SIZE is what an odd cell costs). Measured after the change:
# viewport 40, row 36, ceiling 30 >= 29.
SIDEBAR_COLLAPSED_WIDTH = 72
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

# **A block gap is half a row now** (the owner, 25 August 2026: "in the
# sidebar reduce the spaces of empty buttons (separators) by 50%"). It
# was a whole blank row, which on a laptop was two rows' worth of column
# spent on air while real rows had nowhere to go.
RAIL_GAP_FRACTION = 0.5

# How short a row may be squeezed to keep every one of them on screen,
# and what a row spends on air before anything is drawn in it (theme.py
# gives #NavList::item 11px top and bottom).
#
# **44 because that is where a row stops reading as a button**: at the
# natural 63 the icon box is 41px, at 44 it is 22, and below that the
# icon is smaller than the 20px grid the sheet is drawn on. A window too
# short even for 44 keeps the scrolling column (_rail_scroller) as the
# last resort, so a row is never lost - it just has to be scrolled to.
MIN_ROW_PITCH = 44
ROW_PADDING = 22

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
# **Every rail row's icon is a bundled PNG now** (the owner's sheet of
# 18, 22 August 2026), not a Segoe Fluent codepoint and not a path this
# app draws. theme.rail_icon names the files and theme.NAV_ICONS is the
# page half of the mapping; SECTION_ICONS below is the section half.
#
# It replaces two mechanisms, not one. Most rows typed a glyph as the
# item's *text*; two - Saved and Anime - could not, because the font
# carries neither shape (E8A4, the one named "Bookmarks", draws a
# bulleted list, and there is no cat in it at all), so they were stroked
# by helpers/rail_icons.py and handed to Qt as a decoration. Every row
# now takes the decoration path and nothing types a glyph, which is why
# three tuned alignment constants left with the drawn pair: a stroked
# path's ink is not centred in its own grid and the two shapes were
# different widths, so each needed its own offset to sit on the column's
# axis. Every PNG in the sheet is trimmed to its ink and re-padded to a
# centred square, so "centre the icon box" is exact for all seventeen
# and there is nothing left to tune per shape.
#
# **helpers/rail_icons.py has no callers left as of this change.** It is
# kept rather than deleted because what it holds is a measurement, not
# code - the ink-box/optical-centre reasoning that four rounds of
# alignment paid for - and because it is the only thing here that can
# draw a rail icon without an asset, which is the fallback anyone adding
# a row the sheet has no picture for would reach for first.
#
# The reason these are recoloured rather than shipped in the right
# colour is the reason they are not emoji, which is the reason the
# glyphs came from an icon font in the first place: a row's colour is a
# *state* - muted at rest, TEXT when selected or hovered - and artwork
# with its own palette ignores it.
#
# Logical pixels. 21, not the 20 the icon grid is designed on: measured
# against the Fluent rows it used to sit beside, a 20px box put ~15px of
# ink on screen where those rows put ~17. One size at both widths - a
# folded row and an expanded one show the same picture, and the sheet's
# icons are square, so there is no second number to keep in step.
# **24, not 21, and it is metrically free.** A/B'd at 21/24/26 on the
# real window: row height is 33 folded and 35 expanded at all three,
# folded skew stays within +/-1 at all three, and the longest label
# ("Schedule") ends at x=111 of a 184px row, so nothing elides. What it
# buys is legibility across the whole set - the clapperboard's slate
# teeth, the monitor's stand, the calendar's rings, the clock's hands -
# and it is the difference between the Anime face's hair fusing into a
# crown and its spikes separating into points.
#
# It does not rescue Manhua. That icon's meaning is a Chinese character
# inside a scroll, and the character is a blob at 21 and still a blob at
# 26; what actually tells it apart from Manga and Manhwa at rail size is
# the scroll silhouette, which reads fine. Measured, not assumed.
#
# **26, not 24 - the owner's ask, 22 August 2026: "make the icons in the
# main and watch and read sidebars larger (folded/unfolded)".** Row
# height is not at risk from this either way: _RailDelegate.sizeHint
# takes a decorationless row's own height regardless of icon size (see
# its docstring), so nothing above was re-measured *because* of this
# change - it was re-measured because the 33/35 the A/B above recorded
# is simply stale now, for unrelated reasons. Fresh on today's window:
# 57 folded, 59 expanded.
#
# **28 was tried first and measured broken**: every folded row shifted
# +1.5px right, uniformly, across all three rails - see the `max(...)`
# in _RailDelegate.initStyleOption for the mechanism. That delegate
# only keeps a folded icon centred while RAIL_ICON_SIZE stays at or
# under `32 - margin*2` (the row is pinned to 32px wide,
# _sync_rail_icon_widths; margin is 3, PM_FocusFrameHMargin + 1) - 26 is
# that ceiling exactly, and it is also the largest value the original
# A/B above already vetted on row height and skew, so nothing about
# raising it to exactly this number is untested ground. Going further
# needs the row itself widened first (SIDEBAR_COLLAPSED_WIDTH and
# everything sized off it), not just this constant.
#
# Re-verified on the real window at 26: every row on all three rails
# still centres within 0-0.5px folded, same as at 24 (see
# RAIL_FOLDED_QSS's note above) - unlike 28, this size does not reopen
# the centring this pass was also asked to fix.
#
# **40, 24 August 2026 - the owner: "make the sidebar icons in the main
# page/ reading / watching 50% larger", then "the icons on the sidebar
# while folded seem to be shifted to the right a bit".** Both measured
# by lit-pixel centroid on the offscreen-rendered rail: at 39 (26*1.5,
# the first cut) every folded row's ink sat **+1.0px** right of the
# viewport centre - odd icon in an even cell, the half-pixels all
# rounding the same way. At 40 with the rail at 84 the same measure
# reads **+0.5px**, which is the floor integer pixels allow (an
# odd-width ink span cannot measure closer). The ceiling moved with it:
# folded row 48px, 48 - 2*3 = 42 >= 40.
# **20, half of 40 - the owner, 24 August 2026: "make the sidebars
# (main/read/watch) icons 50% smaller", two builds after asking for them
# 50% larger. The rail came down with it (SIDEBAR_COLLAPSED_WIDTH back
# to 68): a 20px icon centred in an 84px column reads as a wide empty
# strip with a speck in it. 68/20 is inside the delegate's ceiling with
# room to spare - the folded row is 32px and 32 - 2*3 = 26 >= 20 - so
# this is the well-trodden geometry, not new ground.
#
# **26, 24 August 2026 - the owner: "increase the icons in the sidebars
# (main/read/watch) by 28%".** 20 x 1.28 = 25.6, and the integer either
# side of it is not a free choice: 26 is *exactly* the delegate ceiling
# derived above (folded row 32px, margin 3, so 32 - 2*3 = 26), the
# largest size that keeps a folded icon centred rather than shifted
# right. 25.6 rounds up onto that ceiling and 27 would step over it, so
# the ask lands on the one number this file has already vetted twice -
# see the 22 August note above, where 26 was measured centred within
# 0-0.5px on all three rails and 28 was measured broken at +1.5px.
# SIDEBAR_COLLAPSED_WIDTH stays 68: that pairing is what shipped before
# the 40/84 experiment, not new geometry.
RAIL_ICON_SIZE = 26

# **The folded rail draws its icons bigger than the expanded one now**
# (the owner, 25 August 2026: "make the folded sidebar icons 10%
# larger"). 26 * 1.1 = 28.6, and 29 is the integer above it; the rail
# grew with it (SIDEBAR_COLLAPSED_WIDTH 68 -> 72) because the delegate's
# centring ceiling is a property of the *row*, not of the icon - see
# _RailDelegate.initStyleOption for the mechanism and
# SIDEBAR_COLLAPSED_WIDTH for the arithmetic.
#
# Two sizes rather than one, which is new here: every previous change
# moved both widths together on the grounds that a row shows the same
# picture folded and unfolded. It still does - only the size differs,
# and only in the state where the icon *is* the row. An expanded row's
# icon sits beside a 13pt label and is sized against it; a folded one
# has nothing beside it to be out of scale with.
#
# Nothing reads this constant at paint time: _RailDelegate takes the
# size off the rail's own iconSize (kept in step by
# _sync_rail_icon_widths), because a delegate deriving artwork from
# state it cannot see is what shipped a snap-back the last time - see
# its paint().
RAIL_ICON_SIZE_FOLDED = 29

# Applied to a rail while it is folded - see _sync_rail_icon_widths for
# the measurement. Only the padding is named, so every other property of
# theme.py's #NavList::item rule (colour, radius, font, the hover and
# selected pills) still comes from there.
# **Applied to a rail while it is folded, and it names the horizontal
# padding only.** That restraint is the whole trick, and it was measured
# three ways on the real window:
#
#   as shipped (no widget sheet)   row 33px, icons +5px right of centre
#   padding: 11px 0px              row 55px, centred
#   padding-left/right: 0px        row 33px, centred
#
# Restating the vertical padding makes the app rule's 11px and this
# rule's 11px both count, and every folded row grows by 22px. Naming
# just the two sides that need changing leaves the height alone.
#
# A `[folded="true"]::item` rule in theme.py was tried first and is not
# an option: Qt does not re-resolve ::item sub-control rules against a
# dynamic property on the view. Measured - the rects came back byte
# identical after unpolish/polish/doItemsLayout, and the icons stayed
# +5px right.
#
# **Re-measured 22 August 2026 after the owner reported it still right
# of centre**, this time sampling lit-pixel ink bounding boxes on the
# real window rather than trusting this note - across all three rails,
# not just the one this table was taken on: main (Home, Watch, Read,
# Games, Apps, Websites) and both section rails (Watch's Discover,
# Movies, Series, Anime, Saved, Schedule; Read's Manga, Manhwa, Manhua,
# Other, Discover, Saved, History). Every one centred within 0-0.5px of
# the 32px-wide folded pill's true middle - this fix still holds, and
# now covers every rail rather than the one it was written against. See
# RAIL_ICON_SIZE below for the row-height numbers this table's own 33px
# no longer matches, and for the same re-check repeated after growing
# the icon. The built Atomic.exe was not part of this - only the source
# tree, run directly - so a stale build is still an open possibility if
# this is seen again.
RAIL_FOLDED_QSS = ("QListWidget#NavList::item {"
                   " padding-left: 0px; padding-right: 0px; }")
# **The rail's iconSize is square now, and the three blank columns that
# used to hang off a drawn icon's right are gone.** They existed to push
# a drawn row's label out to where a *glyph* row's label started - a glyph
# row spent its lead on two literal spaces in the text - and with no
# glyph rows left there is nothing to match: every row is placed by the
# same decoration-to-text gap, which is what makes them line up. Measured
# to be a no-op either way at today's sizes: a view reserves
# QIcon.actualSize(iconSize), which for a 21x21 pixmap is 21x21 whether
# the box asked for is 21 or 24 wide.

# **Four rounds of ink-centring constants used to live here and are
# gone with the artwork they aimed.** Kept as a note because the fact
# they recorded is what makes the replacement safe rather than lucky:
# what the eye centres on down a narrow column is a shape's *ink mass*,
# not its canvas, and the two hand-drawn shapes had ink 12.6 and 19.6
# grid units wide inside the same 24-unit box - so neither centring the
# canvas nor sharing a left edge put them on one axis, and each needed
# its own measured offset (folded and expanded differed too). The sheet's
# PNGs are each trimmed to their ink and re-padded to a centred square
# before they ship, so ink centre and canvas centre are the same point
# for every one of them and the offsets have nothing left to correct.
# The role a row's icon file is parked on. Read by _RailDelegate (which
# rebuilds the pixmap in the row's own colour on hover) and by
# _sync_rail_icon_widths. UserRole itself already carries the page or
# section key, so this is UserRole + 1.
RAIL_ICON_ROLE = Qt.ItemDataRole.UserRole + 1

# Section key -> icon file, the other half of theme.NAV_ICONS. The keys
# are tracker.WATCH_CATEGORIES / READ_CATEGORIES plus the four standing
# sections; an unmapped one still falls through to theme.NAV_BULLET, and
# so does a *mapped* one whose PNG is missing from the bundle - see
# _rail_icon, which refuses to hand back a null pixmap.
#
# The three reading flavours keep three distinct pictures for the reason
# they always did: "Manhwa" and "Manhua" differ by one letter and could
# never be told apart by their text at rail width, where the label is
# gone entirely and the icon is the whole row.
SECTION_ICONS = {
    "discover": theme.rail_icon("discover"),
    "saved": theme.rail_icon("saved"),
    "schedule": theme.rail_icon("schedule"),
    "history": theme.rail_icon("history"),
    "cat_movies": theme.rail_icon("movies"),
    "cat_series": theme.rail_icon("series"),
    "cat_anime": theme.rail_icon("anime"),
    "cat_manga": theme.rail_icon("manga"),
    "cat_manhwa": theme.rail_icon("manhwa"),
    "cat_manhua": theme.rail_icon("manhua"),
    "cat_other": theme.rail_icon("other"),
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


class _RailCursor(QObject):
    """The pointing-hand cursor over a rail's rows - the owner's ask, 22
    August 2026: "make the mouse cursor hover in the sidebar (main/read/
    watch) change to finger-pointing shape".

    A rail is one QListWidget, so its rows are drawn items rather than
    widgets and `use_hover_cursor` (which is Enter/Leave on a *widget*)
    has nothing per-row to attach to. This asks where the pointer
    actually is instead - `indexAt`, hit-testing the view - so the hand
    appears over a real row and not over the padding under the last one
    or the blank spacer row between two blocks.

    Deliberately *not* a permanent `setCursor` on the viewport, which is
    what .claude/rules/ui.md warns against: it goes through the same
    hold/release registry every other hover cursor here uses, so the
    cursor watchdog can put it back after a modal dialog steals Qt's
    idea of where the pointer is."""

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.Type.MouseMove, QEvent.Type.Enter):
            view = obj.parent()
            try:
                over_row = view.indexAt(event.position().toPoint()).isValid()
            except (AttributeError, RuntimeError):
                over_row = False
            if over_row:
                hold_hover_cursor(view)
            else:
                release_hover_cursor(view)
        elif kind == QEvent.Type.Leave:
            try:
                release_hover_cursor(obj.parent())
            except RuntimeError:
                pass        # the rail went away under the pointer
        return False


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


def _rail_dpr(widget):
    """The ratio a rail's pixmaps have to be cut at. Taken off the widget
    rather than the primary screen, since the window can be dragged to a
    monitor with a different scale factor."""
    try:
        return float(widget.devicePixelRatioF()) if widget is not None else 1.0
    except (AttributeError, RuntimeError):
        return 1.0


def _rail_view_icon_size(widget) -> int:
    """The icon size the rail `widget` is currently drawing at.

    Read off the view rather than off the window's collapsed flag: a
    delegate paints rows for three different rails and has no business
    knowing which of them is folded, and deriving artwork from state the
    paint cannot see is what shipped a sideways snap-back once already
    (see _RailDelegate.paint). _sync_rail_icon_widths is what keeps the
    view's iconSize true."""
    try:
        size = int(widget.iconSize().height()) if widget is not None else 0
    except (AttributeError, RuntimeError):
        size = 0
    return size if size > 0 else RAIL_ICON_SIZE


def _rail_icon(path, dpr, size=RAIL_ICON_SIZE):
    """A rail row's decoration: TEXT_MUTED at rest, TEXT when the row is
    selected. Hover is _RailDelegate's to draw - QStyle only ever asks an
    icon for Normal, Disabled or Selected.

    **Returns None rather than an empty icon when the file is missing.**
    images.tinted_asset answers a missing asset with a null QPixmap, by
    design (an empty icon, not a crash) - but an empty icon in a folded
    rail is a *blank row*, with no label to say which one it is and
    nothing on screen to say anything is wrong. The caller falls back to
    theme.NAV_BULLET instead, so a bundle that shipped without one of the
    PNGs loses the picture and keeps the row.
    """
    normal = images.tinted_asset(path, theme.TEXT_MUTED, size, dpr)
    if normal.isNull():
        return None
    built = QIcon(normal)
    built.addPixmap(
        images.tinted_asset(path, theme.TEXT, size, dpr),
        QIcon.Mode.Selected)
    return built


class _RailDelegate(QStyledItemDelegate):
    """The three things a rail row needs that the stock delegate does not
    do, and nothing else.

    1. **A row's icon follows the row's colour.** QStyle only ever asks a
       QIcon for Normal, Disabled or Selected (QCommonStyle decides the
       mode from `State_Selected` alone), so a *hovered* row's icon would
       stay muted while its label turned bright. Hover is therefore
       mapped onto the icon's Selected pixmap here.
    2. **A collapsed row centres its icon.** With the label gone the row
       is the icon, and QStyleOptionViewItem's decorationAlignment is
       AlignLeft in list mode - so the icon sat hard against the left
       edge of a 68px rail while every glyph row centred its text.
    3. **A row is as tall as a typed one, decoration or not** - see
       sizeHint. This used to apply to two rows out of nine and now
       applies to all of them, which is what keeps the row height where
       it was when the glyphs were text.
    """

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if not option.text:
            option.decorationAlignment = Qt.AlignmentFlag.AlignCenter
            # Or QCommonStyle lays the (empty) text out to the icon's
            # right and centres the *pair*, which puts the icon left of
            # the rail's middle by half a label's width.
            option.displayAlignment = Qt.AlignmentFlag.AlignCenter
            # **And the empty label still costs 12px**, which neither
            # alignment above can reach: measured 22 August 2026 on a
            # folded row, HasDisplay set makes the style size the content
            # at 39px (21 icon + 12 empty text + margins) where the row
            # is pinned to 32. Clearing the flag drops that to 33.
            #
            # A row that types a glyph (the theme.NAV_BULLET fallback)
            # has real text and never reaches this branch, so it keeps
            # centring exactly as every folded row did before.
            option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDisplay
            # **AlignCenter centres the pixmap inside the decoration
            # *cell*, not the cell inside the row** - which is why
            # clearing HasDisplay above moved the row's width and not one
            # pixel of its icon. QCommonStylePrivate::viewItemLayout puts
            # that cell at `rect.x + margin` and gives it exactly
            # `decorationSize.width()`, so a 21px icon in a 32px row sat
            # 2.5px left of centre however it was aligned (measured: ink
            # margins 7 left, 12 right, against 8/9 for the glyph rows it
            # replaced). The old drawn pair paid this with a hand-tuned
            # per-shape offset baked into the artwork; widening the cell
            # to the row instead means AlignCenter lands the icon on the
            # true centre and there is no constant to keep true.
            #
            # **`margin` is measured off the style, not computed from a
            # pixel metric - and the metric was wrong.** This used to
            # read PM_FocusFrameHMargin + 1 (the expression
            # QCommonStylePrivate::viewItemLayout itself uses), which
            # answers 3 here; the cell Qt actually lays out starts 4px
            # in. Measured 25 August 2026 by asking the style where the
            # decoration goes - `SE_ItemViewItemDecoration` came back at
            # x=6 for an item at x=2, at every decoration width from 10
            # to 50 - so the offset is a constant of the *style*, and
            # QStyleSheetStyle (which is what is in front of the base
            # style whenever an app stylesheet is set) does not agree
            # with the base style's own metric. That one pixel is the
            # +1.04px right-skew this file has twice recorded as fixed
            # and twice had reported again.
            #
            # Asking costs one subElementRect per painted row and cannot
            # drift: whatever the style does, the cell is made symmetric
            # against where it actually lands.
            #
            # This leans on the folded rail having no horizontal padding
            # (RAIL_FOLDED_QSS): `rect` here is the whole item, and
            # QStyleSheetStyle hands the base style the content box, so
            # the two are the same row only while that padding is 0.
            widget = option.widget
            style = widget.style() if widget is not None else QApplication.style()
            size = _rail_view_icon_size(widget)
            option.decorationSize = QSize(size, size)
            cell = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration, option, widget)
            margin = max(0, cell.x() - option.rect.x())
            # **The `max(...)` here has a ceiling, and RAIL_ICON_SIZE must
            # stay under it.** Found 22 August 2026 while trying to raise
            # RAIL_ICON_SIZE to 28: every folded row shifted **+1.5px
            # right, uniformly** (measured on the real window, all three
            # rails, no exceptions) - the first time this file's own
            # centring fix has reproducibly failed rather than just
            # sitting on a stale note.
            #
            # The cause is this line, not the value 28 itself. Qt's own
            # viewItemLayout - not this code - always starts the
            # decoration cell at `rect.x() + margin` (see above) and
            # gives it exactly `decorationSize.width()`; the cell is only
            # centred when that width is *exactly*
            # `option.rect.width() - margin*2` (26 here, row 32, margin
            # 3), because that is the one width whose right edge lands
            # `margin` from the row's right side too. `max(RAIL_ICON_SIZE,
            # ...)` was written to stop Qt shrinking a too-big icon to
            # fit its cell - and it still does that job - but the moment
            # RAIL_ICON_SIZE > 26 it *replaces* the symmetric width with
            # a plain `RAIL_ICON_SIZE`-wide cell still anchored at the
            # same left-only `x + margin`, so the cell (and the icon
            # filling it, since the two are now the same width) inherits
            # a left margin of 3 and a right margin of `32-3-28=1` - a
            # 2px lopsided pair, half of which is the +1px-plus-rounding
            # this measured.
            #
            # No fix landed here: raising the row's own 32px would move
            # this ceiling but touches SIDEBAR_COLLAPSED_WIDTH and
            # everything sized off it, well past an icon-size change.
            # RAIL_ICON_SIZE instead stays at or under `32 - margin*2`
            # (26) - see its own comment for why 26 rather than a smaller
            # safe value.
            #
            # **The size is the rail's own iconSize, not a constant.**
            # The folded rail draws at RAIL_ICON_SIZE_FOLDED and the
            # expanded one at RAIL_ICON_SIZE; _sync_rail_icon_widths
            # sets that on the view at each fold, so this reads the
            # state off the widget it is painting rather than off the
            # window's fold flag, which the delegate cannot see (the
            # same rule paint() below records paying for).
            option.decorationSize = QSize(
                max(size, option.rect.width() - margin * 2), size)

    def sizeHint(self, option, index):
        """An icon row is exactly as tall as a typed one.

        Measured: carrying a decoration at all cost a row **2px**
        (59 -> 61 expanded, 57 -> 61 collapsed) whatever size the icon
        was - 18px through 22px all produced 61 - so it is the style's
        decoration margins, not the artwork. That was worth undoing when
        two rows out of nine had a decoration and read as uneven spacing
        down the column; now that *every* row has one it is worth
        undoing for a second reason - it is the only thing holding the
        rail's row height where the typed glyphs left it (33px folded,
        35 expanded, measured before and after this change).

        **Standing in a placeholder character for the folded row's empty
        label was tried here and measured wrong**, which is worth a line
        because the reasoning for it sounded right: the height wanted is
        the one a typed row takes, and a folded row types nothing. But
        the style already answers 33 for a decorationless row with an
        empty label, and answers 40 for the same row carrying one
        character of the folded rail's 14pt icon face - so the
        placeholder did not reproduce the old height, it invented a
        taller one. Measured on the real window: rows went 33 -> 40.

        **The placeholder is back, and it is right this time, because
        the face under it changed.** A folded icon row now carries the
        nav face rather than the icon one (_style_rail_item, 25 August
        2026 - the owner's ask that folded and unfolded rows sit at the
        same y), and the two things that made the old attempt wrong were
        both that font: it measured a *different* face, and it measured
        it against a row whose height came from the icon face too. What
        is left is the one difference this cannot reach otherwise - an
        empty string does not measure as tall as a typed one in the same
        face. Measured on the real window: folded rows 55px against the
        expanded 59 with no placeholder, 59 with it."""
        hint = super().sizeHint(option, index)
        if not index.data(RAIL_ICON_ROLE):
            return hint
        # **An explicitly pinned height wins.** _fit_rails shrinks the
        # rows to keep every one of them on a short screen, and it does
        # that by setting the item's own size hint - which the height
        # computed below would otherwise throw away, silently, leaving
        # the rows at their natural 59px and the column scrolling
        # anyway. Measured before this line: item hint 40px,
        # sizeHintForRow 59px, body still 189px past its viewport.
        explicit = index.data(Qt.ItemDataRole.SizeHintRole)
        if isinstance(explicit, QSize) and explicit.height() > 0:
            hint.setHeight(explicit.height())
            return hint
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.icon = QIcon()
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        if not opt.text:
            opt.text = "A"
            opt.features |= QStyleOptionViewItem.ViewItemFeature.HasDisplay
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
            path = index.data(RAIL_ICON_ROLE)
            if path:
                # Rebuilt in TEXT rather than swapped to the icon's
                # Selected pixmap through a mode, because the style is
                # what picks the mode and it will only ever pick
                # Selected for a selected row. Cheap on repeat -
                # images.tinted_asset caches per (file, colour, size,
                # ratio), and a hover asks for one combination.
                #
                # The size and the ratio are read off the row, not off
                # the sidebar's state: the delegate has no business
                # knowing whether the rail is folded, and a previous
                # pass shipped a snap-back here by deriving the artwork
                # from something the paint could not see - the row
                # shifted sideways under the pointer.
                hot = images.tinted_asset(str(path), theme.TEXT,
                                          _rail_view_icon_size(opt.widget),
                                          _rail_dpr(opt.widget))
                if not hot.isNull():
                    opt.icon = QIcon(hot)
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


class _FoldFreeze(QWidget):
    """The page, as one flat picture, for the length of a sidebar fold.

    **This is the owner's "the sidebar stutters in read or watch", and
    it is the same cause as the page transition** (widgets.PageSlide):
    folding animates the sidebar holder's width, which moves *and*
    resizes the container the page sits in, and Qt's blit path
    (`QWidgetPrivate::moveRect`) is only taken for a pure move - a
    move-and-resize invalidates the whole region instead. So every
    QLabel, Card, CardTextLabel and cover on the page re-rendered on
    every step of the fold. Measured 22 August 2026, real window,
    1920x1040, on the owner's data, at his panel's 6.94ms budget:

        Home                       63-67 paints/position   ~94-99 fps
        Watch / Discover           85-102               ~74-85 fps
        Watch / Movies (category) 100-127               ~44-70 fps
        Read / Discover            71-86                ~69-85 fps

    which is exactly the "fine on Home, bad on the tracker pages" split
    the owner reported: the same per-widget cost, over four times as
    many widgets. Hiding the page for the length of a fold - the
    isolation test - took Movies from 58/93 fps to 110/127, naming the
    page's repaint as the dominant term; the sidebar's own repaint,
    tested the same way, was worth about 1ms of median.

    So the page is rendered once, here, and a fold step is one
    `drawPixmap` with the real widgets hidden behind it. The picture is
    taken at the *widest* geometry the page holds for the whole fold
    (see _toggle_sidebar), so the container simply clips it as it
    resizes, exactly as it clipped the live page."""

    def __init__(self, parent, pixmap):
        super().__init__(parent)
        # Same pair as PageSlide: this covers every pixel it is given,
        # so Qt can skip the erase and stop painting the container under
        # it.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._pixmap = pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        # Whole-pixmap draw at the origin: it carries its own
        # devicePixelRatio from grab(), so Qt places it 1:1 on any
        # display without resampling (see .claude/rules/ui.md).
        painter.drawPixmap(0, 0, self._pixmap)
        painter.end()


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
        # One more fit once the event loop has laid the window out: the
        # pass during construction reads a viewport that has not settled
        # and lands on a smaller pitch than there is room for (measured
        # at 900px: 44 during the build, 59 once shown).
        QTimer.singleShot(0, self._fit_rails)
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
        # Every scrolling rail column in the window (_rail_scroller), so
        # the fold can set their scrollbar policy without naming them.
        self._rail_scrollers = []
        # The same columns with their rails and gaps, for _fit_rails.
        self._rail_columns = []
        self._rail_bucket = None
        # The row pitch and icon size the last fit settled on.
        self._rail_pitch = 0
        self._rail_icon_px = RAIL_ICON_SIZE
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
        # The pointing-hand cursor over a row (see _RailCursor). Installed
        # on the viewport, which is what receives the mouse events, and
        # parented to it so the filter dies with the rail. Here rather
        # than at each of the three call sites, for the same reason every
        # other property moved into this function: three copies drifted.
        rail.viewport().setMouseTracking(True)
        rail.viewport().installEventFilter(_RailCursor(rail.viewport()))
        # **Ignored, not Preferred, across.** A rail is 184px wide
        # expanded and 32px folded, and it must take whatever the column
        # hands it either way. Inside the scrolling column
        # (_rail_scroller) Preferred meant the body kept the rail's
        # *expanded* width hint after a fold - measured: rail viewports
        # still 256px in a 90px rail, rows laid out 262px wide, so every
        # folded icon was drawn off the right-hand edge and the rail
        # looked empty. setMinimumWidth(0) for the other half of it:
        # QAbstractScrollArea reports a minimum of its own that a layout
        # will not go under.
        rail.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Fixed)
        rail.setMinimumWidth(0)
        # QListWidget's item delegate paints with the widget's own font(),
        # not the ::item QSS font-family - the stylesheet rule alone is
        # silently ignored for list items, so it has to be set here too.
        rail.setFont(theme.nav_font())
        rail.setIconSize(QSize(RAIL_ICON_SIZE, RAIL_ICON_SIZE))
        # setItemDelegate, not setItemDelegateForRow: the drawn rows move
        # around as blocks are refilled, and a per-row delegate would be
        # attached to whichever row happened to hold one at build time.
        # Owned by the list, so it lives exactly as long as it does.
        rail.setItemDelegate(_RailDelegate(rail))
        if self._rail_bucket is not None:
            self._rail_bucket["rails"].append(rail)
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
        if self._rail_bucket is not None:
            self._rail_bucket["gaps"].append(gap)
        return gap

    def _make_nav_gap(self):
        """A block gap in the *main* bar, also indexed in `_nav_gaps` so
        _populate_nav_list can hide it with the block below it."""
        gap = self._make_rail_gap()
        self._nav_gaps.append(gap)
        return gap

    def _rail_gap_height(self, pitch=None) -> int:
        """A block gap: half a row (RAIL_GAP_FRACTION).

        Measured off a real row rather than assumed - the QSS gives
        ::item 11px of vertical padding on top of whatever the font
        needs, so the number is a product of the stylesheet and the
        font, not something worth hard-coding twice."""
        if pitch is None:
            pitch = getattr(self, "_rail_pitch", 0) or None
        if pitch is None:
            row = self.home_list.sizeHintForRow(0)
            pitch = (row if row > 0 else RAIL_GAP_FALLBACK)
            pitch += self.home_list.spacing() * 2
        return max(1, int(pitch * RAIL_GAP_FRACTION) - RAIL_GAP_SLACK)

    def _sync_rail_gaps(self):
        """Every block gap set to half a row's height."""
        height = self._rail_gap_height()
        for gap in getattr(self, "_rail_gaps", ()):
            try:
                gap.setFixedHeight(height)
            except RuntimeError:
                pass

    def _fit_rails(self):
        """Shrink the sidebar until every row is on screen without a
        scroll - the owner's ask, 25 August 2026: *"make all buttons
        showing without scrolling"*.

        Two levers, spent in this order, because they cost different
        things:

        1. **The logo band.** It is decoration; the rows are the
           navigation. Measured on the window that started this: at
           720px the main bar needs 1115px of column for 160px of fixed
           furniture, 120px of logo, eleven rows and two gaps.
        2. **The row pitch**, down to MIN_ROW_PITCH. At 720px that
           settles on ~46px against the natural 63, which still holds a
           readable label and a ~24px icon.

        Below that the scrolling column is still there and still works -
        losing rows off the bottom is the one outcome this must not
        have, and a 400px window is not worth deforming every row for.

        The arithmetic is done against the *body's* size hint rather
        than by adding up the furniture: the hint already counts the
        margins, the spacings and whatever else a bar carries, and the
        two bars carry different things (the section bar has Back and a
        separator where the main one has a logo).
        """
        columns = list(getattr(self, "_rail_columns", ()))
        if not columns:
            return
        base_icon = (RAIL_ICON_SIZE_FOLDED if self._sidebar_collapsed
                     else RAIL_ICON_SIZE)
        # Natural first: clear every pinned hint, so what a row asks for
        # unaided is what the search below starts from.
        for bucket in columns:
            for rail in bucket["rails"]:
                for index in range(rail.count()):
                    rail.item(index).setSizeHint(QSize())
                rail.updateGeometry()

        def content(bucket, pitch):
            """What this column needs at `pitch`, computed rather than
            measured.

            **Measured is what it used to be, and that oscillated.** The
            body's own sizeHint is a cache of the last layout - the one
            this pass is about to change - so reading it back gave a
            different answer every fold: 53px, then 44, then 52, each
            correct for the layout before it. This is
            NavListWidget.sizeHint's arithmetic written out (rows at the
            pitch, plus its bottom margin), so it depends on nothing
            this function then sets."""
            rows = [r.count() for r in bucket["rails"] if r.count()]
            gaps = len(bucket["gaps"])
            total = sum(n * pitch + NAV_LIST_BOTTOM_MARGIN for n in rows)
            total += gaps * self._rail_gap_height(pitch)
            total += SIDEBAR_LAYOUT_SPACING * max(0, len(rows) + gaps - 1)
            return total

        pitch, drop_logo = 0, False
        for bucket in columns:
            area = bucket.get("area")
            rails = [r for r in bucket["rails"] if r.count()]
            if area is None or not rails:
                continue
            try:
                natural = max(r.sizeHintForRow(0) + r.spacing() * 2
                              for r in rails)
                room = area.viewport().height()
            except RuntimeError:
                continue
            if room <= 0 or natural <= 0:
                continue
            # **Normalised to "no logo at all", never to the band's
            # current height.** Reading the live height made the answer
            # depend on the last pass's answer: drop the band, and the
            # next pass sees 0 to reclaim, decides the rows fit without
            # dropping anything, puts the band back, and the rows
            # overflow again. Measured at 900px: pitch 60 with the band
            # restored and the column scrolling, on the second pass.
            bare = room + (self.logo_label.height()
                           if bucket is columns[0] else 0)
            has_logo = bucket is columns[0]

            def largest(limit):
                for candidate in range(natural, MIN_ROW_PITCH - 1, -1):
                    if content(bucket, candidate) <= limit:
                        return candidate
                return 0

            with_logo = largest(bare - (LOGO_HEIGHT if has_logo else 0))
            if with_logo == natural:
                fitted, drops = natural, False
            else:
                # The logo band is decoration and the rows are the
                # navigation, so the band gives first - see the class
                # note. Only then are the rows squeezed.
                without = largest(bare)
                if without > with_logo:
                    fitted, drops = without, True
                else:
                    fitted, drops = (with_logo or MIN_ROW_PITCH), with_logo == 0
            drop_logo = drop_logo or drops
            pitch = fitted if not pitch else min(pitch, fitted)

        if not pitch:
            return
        # One pitch for the whole window, not one per bar: the two bars
        # swap places in the same column, and rows that changed height on
        # the swap would read as the list jumping.
        self._rail_pitch = pitch
        icon_px = max(16, min(base_icon, pitch - ROW_PADDING))
        rebuild_icons = icon_px != self._rail_icon_px
        self._rail_icon_px = icon_px
        for bucket in columns:
            for rail in bucket["rails"]:
                try:
                    if rebuild_icons:
                        rail.setIconSize(QSize(icon_px, icon_px))
                        for index in range(rail.count()):
                            item = rail.item(index)
                            path = item.data(RAIL_ICON_ROLE)
                            if path:
                                item.setIcon(_rail_icon(str(path),
                                                        _rail_dpr(self),
                                                        icon_px) or QIcon())
                except RuntimeError:
                    pass
        self._sync_rail_gaps()
        self._sync_rail_icon_widths()
        self._sync_home_list_height()
        self._set_logo_height(0 if drop_logo else LOGO_HEIGHT)

    def _set_logo_height(self, height):
        """The logo band's height - LOGO_HEIGHT when the rows fit
        around it, 0 when they do not (see _fit_rails)."""
        height = max(0, min(LOGO_HEIGHT, int(height)))
        if height == self.logo_label.height():
            return
        self.logo_label.setFixedHeight(height)
        self.logo_label.setVisible(height > 0)
        self._style_logo()

    def _sync_rail_icon_widths(self):
        """Pin every folded row to the width the rail actually has, so
        they all centre on the same axis.

        **Measured 22 August 2026 on the folded section rail**, sampling
        the lit pixels of each row's icon:

            Saved     (drawn)  centroid x = 19.0
            Schedule  (glyph)  centroid x = 17.9
            History   (glyph)  centroid x = 17.9

        which is the owner's "the saved icon is moved more to the right
        when folded". The cause is not the artwork: a row carrying a
        *decoration* reserves the icon **and** the view's
        decoration-to-text gap even when the collapsed label is empty,
        so the Saved item's rect came out **39px wide inside a 36px
        viewport** and AlignCenter centred it on 19.5 rather than on the
        rail. Widening or padding the pixmap cannot fix that - the extra
        width is the item's, not the icon's.

        **Pinned to the viewport now rather than to the glyph rows in the
        same rail**, because there are no glyph rows left to measure
        against - every row carries a decoration since the icon sheet
        landed, so the old `min(width of the rows without one)` had
        nothing to take a minimum of and quietly did nothing at all,
        leaving every row 3px over the viewport and centred off the rail.
        The number it used to find is the one computed here: a row with
        no decoration lays out at the full viewport width less the list's
        spacing on each side - measured 32 in a 36px viewport at
        spacing 2, both before and after this change.

        Width only; the height stays the delegate's, which is what
        _sync_rail_gaps reads. Expanded rails are left alone - there the
        label needs the natural width.
        """
        # getattr throughout: this runs from _populate_nav_list, which
        # the main bar builds before the section bar or the Home row
        # exist at all.
        # **No scrollbar while folded.** The rail is 36px of viewport
        # there and Qt would take ~10 of them for the bar, which both
        # looks wrong and narrows the box the folded icons are centred
        # in (_RailDelegate reads the row width). The wheel still
        # scrolls the column either way.
        for area in getattr(self, "_rail_scrollers", ()):
            try:
                area.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                    if self._sidebar_collapsed
                    else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            except RuntimeError:
                pass
        rails = list(getattr(self, "nav_lists", ()))
        rails += list(getattr(self, "section_lists", ()))
        rails.append(getattr(self, "home_list", None))
        for rail in rails:
            if rail is None:
                continue
            try:
                # **The folded rail takes its left padding off, and that
                # is what finally centres it.**
                #
                # Measured 22 August 2026 on the real window, ink margins
                # inside the 36px viewport: Movies +5, Series +5, Anime
                # +5, Saved +4, Schedule +5, History +6 - *every* row
                # sitting right of centre, not just the two drawn ones.
                # Four rounds of this were spent aligning the bookmark
                # and the cat to the glyph column, and the glyph column
                # was itself the thing that was off.
                #
                # The cause is one number: Qt lays the content out at
                # `item.x + padding-left` and lets it run to the item's
                # right edge, so the box the glyph centres in is inset
                # on one side only. Probed with deliberately absurd
                # values - padding-left 30 put the text at x=32, exactly
                # item.x + 30 - which makes the arithmetic exact:
                # content centre = x + padL + (w - padL)/2, and with the
                # item at x=2 w=32 in a 36px viewport that lands on 18
                # (the true centre) only when padL is 0.
                #
                # Set on the widget rather than through a
                # [collapsed="true"] rule in theme.py: that was tried
                # first and never matched - Qt does not re-resolve
                # `::item` sub-control rules against a dynamic property
                # on the view, and the measured rects came back byte
                # identical after an unpolish/polish. A widget
                # stylesheet merges with the app's, so the hover and
                # selected rules still apply.
                # Set as a *property* read by theme.py's
                # `[folded="true"]::item` rule, not as a widget
                # stylesheet. A widget stylesheet naming padding was
                # measured taking the row height from 33px to 55px -
                # exactly twice the 11px vertical padding, i.e. applied
                # once by the app rule and again by the widget one.
                # doItemsLayout() is required as well: the rule changes
                # the item's metrics, and the view caches them.
                sheet = RAIL_FOLDED_QSS if self._sidebar_collapsed else ""
                if rail.styleSheet() != sheet:
                    rail.setStyleSheet(sheet)
                # The folded rail draws a larger icon than the expanded
                # one (RAIL_ICON_SIZE_FOLDED). Set here rather than in
                # _make_rail_list because it changes with the fold, and
                # this is the one place every rail is already walked on
                # every fold - _RailDelegate reads it back off the view.
                size = (RAIL_ICON_SIZE_FOLDED if self._sidebar_collapsed
                        else RAIL_ICON_SIZE)
                if rail.iconSize().height() != size:
                    rail.setIconSize(QSize(size, size))
                items = [rail.item(i) for i in range(rail.count())]
                if not items:
                    continue
                # **Only pin a rail that is actually at its settled
                # width.** This runs from _populate_nav_list and from the
                # fold *before* the width animation, where the rows are
                # still 184px wide and pinning to them puts the icon
                # 5.7px off centre - worse than the 1.1px being fixed.
                # The post-animation call in _toggle_sidebar's landed()
                # is the one that does the work.
                settled = (rail.viewport().width() <= SIDEBAR_COLLAPSED_WIDTH
                           if self._sidebar_collapsed
                           else rail.viewport().width() > SIDEBAR_COLLAPSED_WIDTH)
                rail.doItemsLayout()
                target = rail.viewport().width() - rail.spacing() * 2
                if target <= 0:
                    continue
                # **Height as well as width, and in both states.** The
                # fitted pitch (_fit_rails) is what keeps every row on a
                # short screen, and it can only be applied here - a
                # cleared hint would put the delegate's natural 63px
                # back and lose the bottom rows again.
                pitch = getattr(self, "_rail_pitch", 0)
                height = max(1, pitch - rail.spacing() * 2) if pitch else 0
                for item in items:
                    if not height:
                        item.setSizeHint(QSize())
                        continue
                    # **The height goes on whether or not the width has
                    # settled.** It does not depend on the width, and
                    # gating both meant a fold applied neither until
                    # some later pass - measured: the folded column
                    # still 179px past its viewport while the expanded
                    # one fitted exactly.
                    width = target if settled else (item.sizeHint().width()
                                                    or target)
                    item.setSizeHint(QSize(max(1, width), height))
                # **updateGeometry, or none of that reaches the layout.**
                # NavListWidget.sizeHint is computed from
                # sizeHintForRow, and Qt caches a widget's size hint
                # until it is told otherwise - measured: rows correctly
                # pinned to 40px while the rail went on reporting the
                # 453px it wanted at the old row height, so the column
                # scrolled with every row already short enough to fit.
                rail.updateGeometry()
            except RuntimeError:
                pass                # the rail is being torn down

    def _rail_scroller(self, layout):
        """The rails, in a column that scrolls when the window is too
        short to hold them, added to `layout`. Returns the layout to put
        them in.

        **Measured 25 August 2026, on the owner's work laptop and then
        here at the same window height**: at 720px the reading block is
        handed **131px for 453px of rows**, so five of its seven rows
        were not drawn and nothing on screen said so - his screenshot
        shows Manhwa cut through the middle and Manhua simply absent.
        The sidebar's column is fixed furniture (logo, Home, the blocks,
        Downloads, Settings) and on a laptop it does not fit; a
        QVBoxLayout answers that by squeezing whatever will squeeze,
        which for a rail means clipping rows off the bottom.

        This pre-dates the logo keeping its height while folded (25
        August) - at 720px the *expanded* bar clipped exactly as much -
        but that change removed the 124px of relief folding used to
        give, so the folded bar lost rows too. Both are fixed by the
        rows having somewhere to go.

        No ground colour: the sidebar is a vertical gradient
        (theme.py's #Sidebar), and an opaque flat body would paint a
        flat strip down the middle of it. A rail is a dozen rows, so the
        repaint this costs is nothing - see scroll_area's note for where
        that matters and where it does not."""
        body = QWidget(objectName="Bare")
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SIDEBAR_LAYOUT_SPACING)
        # Everything built from here until the next call belongs to this
        # column - see _fit_rails, which has to know which rows share a
        # viewport before it can decide how tall they may be.
        self._rail_bucket = {"body": body, "rails": [], "gaps": []}
        self._rail_columns.append(self._rail_bucket)
        area = scroll_area(body)
        area.setObjectName("Bare")
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Vertical policy is set per fold in _sync_rail_icon_widths: a
        # scrollbar inside the 36px folded rail would both look wrong and
        # narrow the viewport the folded icons are centred against.
        layout.addWidget(area, stretch=1)
        self._rail_scrollers.append(area)
        self._rail_bucket["area"] = area
        return column

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
        # The flat picture standing in for the page while a fold runs,
        # the page it was taken of, and whatever on that page held the
        # keyboard - see _FoldFreeze/_settle_fold.
        self._fold_freeze = None
        self._fold_frozen_page = None
        self._fold_focus = None
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
        # Centred on both axes, not just across: the label keeps the
        # expanded logo's full height at both widths (see below), so a
        # top-aligned rail mark would sit ~45px above where the same
        # mark sits unfolded.
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Scale to LOGO_HEIGHT *physical* pixels (source PNG is 1672px tall,
        # plenty of headroom) and tag the result with the screen's DPI
        # scale, or Qt stretches the merely-260px-tall pixmap to fill a
        # 260*scale screen area on any non-100%-scaled display (125% here)
        # and it comes out visibly blurry.
        dpr = QApplication.primaryScreen().devicePixelRatio()
        source = QPixmap(str(APP_DIR / "assets" / "atomic_icon.png"))

        def _scaled(height):
            shrunk = source.scaledToHeight(
                int(height * dpr), Qt.TransformationMode.SmoothTransformation)
            shrunk.setDevicePixelRatio(dpr)
            return shrunk

        # **Two sizes, and the header keeps its full height at both**
        # (the owner's ask, 25 August 2026: "make the Icons vertical
        # position in the sidebar while folded = to the unfolded
        # position"). Folding used to hide this label outright, and the
        # whole column below it jumped up by the 124px that freed -
        # measured on the real window, Home at y=190 expanded and y=66
        # folded. Every row's y is now the same at both widths.
        #
        # The mark shrinks to the rail's inner width rather than being
        # blanked, because 120px of empty strip at the top of a 72px
        # rail reads as a layout bug; the artwork is a mark with no
        # wordmark in it, so it survives being small.
        self._logo_wide = _scaled(LOGO_HEIGHT)
        self._logo_rail = _scaled(
            (SIDEBAR_COLLAPSED_WIDTH - 32) * source.height() / source.width())
        logo_label.setFixedHeight(LOGO_HEIGHT)
        self.logo_label = logo_label
        self._style_logo()
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
        # Everything from Home down to the last block scrolls together -
        # see _rail_scroller for the window height that made that
        # necessary.
        column = self._rail_scroller(layout)
        column.addWidget(self.home_list)
        # Home is built before the column exists (it has to be, the
        # scroller is created around it), so it is registered by hand -
        # without this the fit budgets for ten rows and lays out
        # eleven, which is exactly one row of overflow.
        self._rail_bucket["rails"].insert(0, self.home_list)

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
        column.addWidget(self._make_nav_gap())

        # One list per block, not one list with separators in it: a
        # QListWidget in InternalMove mode will happily drop a row past
        # anything sitting in the list, so a spacer row would be
        # draggable, land anywhere, and take the grouping with it. Split
        # this way, a drag reorders its own block and cannot leave it.
        self.nav_lists = []
        for index, group in enumerate(visible_nav_groups()):
            if index:
                column.addWidget(self._make_nav_gap())
            nav_list = self._make_rail_list(draggable=True)
            nav_list.itemClicked.connect(self._on_nav_item_clicked)
            nav_list.model().rowsMoved.connect(self._on_nav_reordered)
            self.nav_lists.append(nav_list)
            column.addWidget(nav_list)
        column.addStretch()
        self._populate_nav_list()

        # Add and Settings both sit at the very bottom. The scrolling
        # rail column above them takes the slack now (it is the layout's
        # only stretch), so there is no addStretch here - one would fight
        # it for the same space and shrink the rows back.

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
        # Scrolling, like the main bar's - and this one needs it more:
        # Read publishes eight sections against the main bar's seven
        # rows, with Back and a separator above them.
        column = self._rail_scroller(layout)
        for index in range(SECTION_BLOCKS):
            if index:
                column.addWidget(self._make_rail_gap())
            rail = self._make_rail_list(draggable=True)
            rail.itemClicked.connect(self._on_section_item_clicked)
            rail.model().rowsMoved.connect(self._on_sections_reordered)
            self.section_lists.append(rail)
            column.addWidget(rail)
        column.addStretch()

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
        self._fit_rails()

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
        # **Always the main rail now.** The section bar is gone as a
        # concept: its rows are on the main rail as routes (see
        # nav_config.NAV_GROUPS) and the three that are not - Saved,
        # Schedule, History - are header tabs on the page itself. The
        # machinery below is left in place rather than deleted because it
        # is what puts the main bar back, and because a page type wanting
        # a rail of its own is a one-line change again.
        sectioned = False
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
        """A nav row's picture, looked up by route.

        Three tries, widest last: the whole route (nothing uses one yet,
        but a row wanting its own picture should be able to say so), the
        *section* it names - which is where the six type rows get theirs,
        from the same SECTION_ICONS the section rail used to draw - and
        finally the page. A row that matches none keeps the bullet, as
        it always did."""
        section = route_section(page_name)
        page = _page_name(page_name)
        icon = (theme.NAV_ICONS.get(page_name)
                or (SECTION_ICONS.get(section) if section else None)
                or theme.NAV_ICONS.get(page)
                # SECTION_ICONS by page name too: "discover" is a whole
                # page now and its picture has always lived there, so
                # without this the one row on the rail with no section
                # was the one row drawn as a bullet.
                or SECTION_ICONS.get(page)
                or theme.NAV_BULLET)
        self._style_rail_item(item, name, icon)

    def _style_rail_item(self, item, name, icon_token):
        """Harbor's row language, shared by the nav list and the section
        list (they are the same widget on purpose): the row's icon leads
        its label when the sidebar is expanded, and the collapsed rail
        shows that same icon alone and centred with the label moved to a
        tooltip - one picture per row at both widths, so folding never
        swaps the symbol out from under the user.

        `icon_token` is a bundled PNG's path for every row this app
        ships (theme.NAV_ICONS and SECTION_ICONS, both built by
        theme.rail_icon). It used to be a Segoe codepoint typed into the
        item's *text*, with two hand-drawn exceptions handed over as a
        decoration; now every row takes the decoration path and the text
        branch below is only a fallback - which is the point of it.

        **A row must never come out blank, and a missing PNG is how that
        happens quietly**: images.tinted_asset answers a missing asset
        with a null pixmap by design, Qt draws a null pixmap as nothing,
        and a folded row has no label to give it away. So _rail_icon
        reports the miss instead of returning an empty icon, and the row
        falls through to theme.NAV_BULLET - the same branch an unmapped
        section key has always used.

        The expanded font stays the two-family chain
        (theme.nav_row_font) even though nothing types a glyph in the
        normal case: it resolves the icon face first and falls through
        to the nav face, and an item carries exactly one font, so a
        fallback row typing a bullet still gets a face that has one."""
        path = str(icon_token) if str(icon_token).endswith(".png") else ""
        size = (RAIL_ICON_SIZE_FOLDED if self._sidebar_collapsed
                else RAIL_ICON_SIZE)
        icon = _rail_icon(path, _rail_dpr(self), size) if path else None
        if icon is not None:
            item.setData(RAIL_ICON_ROLE, path)
            item.setIcon(icon)
            glyph = ""
        else:
            # Cleared rather than left alone: this runs again on every
            # fold and every section refill, so a row that lost its icon
            # would otherwise keep the one it was built with and draw the
            # fallback text on top of it.
            item.setData(RAIL_ICON_ROLE, None)
            item.setIcon(QIcon())
            glyph = theme.NAV_BULLET if path else str(icon_token)
        if self._sidebar_collapsed:
            item.setText(glyph)
            # **The nav face, not the icon face, on a row that types
            # nothing** (the owner's ask, 25 August 2026: "make the
            # Icons vertical position in the sidebar while folded = to
            # the unfolded position"). A row's height comes off its font
            # (see _RailDelegate.sizeHint), and the two faces do not
            # measure the same: folded rows came out 57px against the
            # expanded 59, so every row below the first sat 2px further
            # up than its unfolded self and the drift accumulated down
            # the column - 124px at Home, 140px by Manhua (measured on
            # the real window before this change).
            #
            # A *fallback* row still types a bullet from the icon face
            # and keeps it: that row has a glyph to render, and its
            # height was never the one out of step.
            item.setFont(theme.icon_font() if glyph else theme.nav_row_font())
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setToolTip(name)
        else:
            # Two spaces, not three, on a fallback row: with a wide glyph
            # leading, three pushed "Movies & Series" past the 220px
            # column and elided it (measured on a real-window grab). An
            # icon row spends no spaces at all - Qt's own
            # decoration-to-text gap places the label.
            item.setText(name if not glyph else f"{glyph}  {name}")
            item.setFont(theme.nav_row_font())
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip("")

    def _sync_home_list_height(self):
        """The single-row Home list is pinned to its row height, which
        changes when that row swaps between the nav and icon fonts - so
        it has to be re-measured whenever the sidebar folds."""
        self.home_list.setFixedHeight(
            self.home_list.sizeHintForRow(0) + self.home_list.spacing() * 2)

    def _style_logo(self):
        """The brand mark at whichever size the current width wants.

        The label's *height* never changes (see _build_sidebar): it is
        what holds every row below it at the same y folded and
        unfolded."""
        self.logo_label.setPixmap(self._logo_rail if self._sidebar_collapsed
                                  else self._logo_wide)

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

    def _reflow_for_fold(self, page):
        """Re-wrap a page's grid for the width the sidebar has settled at.

        Split out only so it can be deferred a turn - see the caller."""
        if page is not self._current_page:
            return              # navigated away while the timer was queued
        relayout = getattr(page, "relayout_for_sidebar", None)
        if not callable(relayout):
            return
        try:
            relayout()
        except RuntimeError:
            pass                # the page was replaced mid-fold

    def _settle_fold(self):
        """Put the live page back, whatever ended the fold.

        One place undoes the freeze, so an interrupted fold (a second
        click mid-flight, a navigation) and a completed one land
        identically - the same shape as _settle_swap. Without it a
        SmoothTween.stop() would strand the page hidden behind a picture
        of itself, because stop() deliberately does not run on_done."""
        freeze, self._fold_freeze = self._fold_freeze, None
        page, self._fold_frozen_page = self._fold_frozen_page, None
        focused, self._fold_focus = self._fold_focus, None
        if page is not None:
            try:
                page.show()
            except RuntimeError:
                pass        # the page was replaced mid-fold
        if focused is not None:
            # Hiding a widget takes the keyboard off it and Qt does not
            # give it back on show(): without this, folding the sidebar
            # while typing in a search box left the caret nowhere and the
            # next keystroke went to the window.
            try:
                if focused.isVisible():
                    focused.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass
        # **And re-flow the grid now the width is real.** The
        # `relayout_for_sidebar()` before the animation is right for the
        # link_grid pages, which decide their column count from
        # `_sidebar_collapsed` - a boolean, already flipped. It is wrong
        # for the tracker's category grid, which counts columns from its
        # body's *measured* width, and at that moment the body is still
        # the width it is about to stop being. Measured 22 August 2026:
        # category_body reported 1604 before the fold and 1756 after, so
        # the pre-fold re-flow recomputed 8 columns every time and the
        # owner's "9 per row when folded" never happened. Running it here
        # as well costs one rebuild from the already-warm catalogue
        # cache, after the animation, so it cannot touch the fold's
        # frame rate.
        # On a zero timer, not inline: `landed()` has only just pinned
        # the holder's width, and the page's own layout pass has not run
        # yet - measured, unfolding re-flowed against a body still
        # reporting its folded 1750px and kept 9 columns where 8 was
        # right. One turn of the event loop is all it needs.
        if page is not None:
            QTimer.singleShot(0, lambda p=page: self._reflow_for_fold(p))
        if freeze is not None:
            freeze.hide()
            freeze.deleteLater()

    def _toggle_sidebar(self):
        # A second click while the last fold is still running: end its
        # freeze here rather than leaving two stacked.
        self._settle_fold()
        self._sidebar_collapsed = not self._sidebar_collapsed
        collapsed = self._sidebar_collapsed

        self._sync_fold_buttons()
        self._style_logo()
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
        self._fit_rails()

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
            # The live page comes back only now, at its real width, with
            # the picture dropped in the same turn of the event loop -
            # showing it first and removing the picture after would cost
            # the page two repaints instead of one.
            self._settle_fold()
            # **After the fold, not before it.** _sync_rail_icon_widths
            # pins a drawn row to what its glyph neighbours measure, and
            # measuring is only meaningful once the rail is at its final
            # width: called before the animation it read the *expanded*
            # 184px row and pinned the Saved icon to it, which put the
            # bookmark 5.7px off centre in a 36px rail instead of 1.1px
            # - worse than the bug it fixes (measured).
            self._fit_rails()
            # Once more when the layout has caught up with the width the
            # fold just set: the pass above runs in the same turn as
            # setFixedWidth, so a rail can still report the old viewport.
            QTimer.singleShot(0, self._fit_rails)

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
            # **Painted once and blitted for the rest of the fold** - the
            # page's own repaint was 80-91% of the paint events in a
            # fold, and all of the difference between Home and the
            # tracker pages. See _FoldFreeze for the numbers.
            #
            # Taken *before* relayout_for_sidebar below, and that
            # ordering is not cosmetic: a category grid rebuilds its
            # cards in chunks off a timer, so a picture taken after the
            # re-flow starts is a picture of an **empty page** - which is
            # what the first version of this shipped into the pixel diff
            # (38% of the frame, caught only by comparing the fold
            # against the same fold with the picture switched off).
            shot = self._current_page.grab()
            freeze = _FoldFreeze(self.container, shot)
            freeze.setGeometry(widest)
            freeze.show()
            freeze.raise_()
            self._fold_freeze = freeze
            self._fold_frozen_page = self._current_page
            focused = QApplication.focusWidget()
            self._fold_focus = (focused if focused is not None
                                and self._current_page.isAncestorOf(focused)
                                else None)
            self._current_page.hide()

        # The card grids fit one more card per row against the folded
        # rail (link_grid.grid_columns), so whatever is showing re-flows
        # now rather than looking wrong until the next time it is opened.
        # Before the animation, not after it: the column count is decided
        # by the fold, not by the width it is currently passing through -
        # the page is already pinned at that width above, so this reads
        # the fold's own answer either way.
        relayout = getattr(self._current_page, "relayout_for_sidebar", None)
        if callable(relayout):
            relayout()
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
        self._fit_rails()

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
        # The image caches are brought back under their limit on the way
        # out as well as at launch - the owner's "> 1.2 GB" image_cache,
        # 24 August 2026 (see images.trim_cache). Bounded by its own
        # budget, so a slow disk cannot hold the window open.
        try:
            images.trim_cache(budget_s=2.0)
        except Exception:
            pass
        # The reading-music window closes with the app - the back button
        # already did this, closing the app around an open reader did
        # not, and the music played on in the browser (the owner's
        # report). Synchronous: see reader.close_music_now for why the
        # normal grace-timer route cannot run during shutdown.
        try:
            from windows import reader
            reader.close_music_now()
        except Exception:
            logs.exception("could not close the reading music at exit")
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
                #
                # On Watch and Read that box is now the **Discover**
                # search, and reaching it switches to Discover first
                # (the owner's ask, 22 August 2026: "make the Ctrl+F go
                # to discover search while in the watch or read pages").
                # Every other page keeps exactly what it had: the saved
                # rows' filter, which is what _focus_page_search
                # declining falls through to.
                if not self._focus_page_search():
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
            # wins over leaving full screen.
            box = self._live_search_box()
            if box is not None:
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
        """Go to a route - `page` or `page:section`.

        A route naming a section of the page already showing does not
        rebuild it: switching from Movies to Anime is the same page
        changing what it lists, and tearing it down would throw away its
        covers and its scroll position to draw the same widget again."""
        route = page_name if ":" in str(page_name) else _page_name(page_name)
        current = self._history[self._history_index]
        if current == route:
            return
        if (route_section(route)
                and _page_name(current) == _page_name(route)
                and self._current_page is not None):
            self._history = self._history[: self._history_index + 1]
            self._history.append(route)
            self._history_index += 1
            self._sync_nav_highlight(route)
            self._apply_route_section(self._current_page, route)
            return
        page_name = route
        self._history = self._history[: self._history_index + 1]
        self._history.append(page_name)
        self._history_index += 1
        self._show_page(page_name, direction=self._direction_between(current, page_name), animate=animate)

    def _page_search_boxes(self):
        """Every search field the page on screen owns, most specific
        first.

        Duck-typed, like every other page hook here (SECTIONS,
        relayout_for_sidebar): `search_box` is the filter a page draws
        over its own saved rows, and `page_search_field()` is whatever
        that page calls its main search - on the tracker pages, the
        Discover box, which is built lazily and so has to be *asked*
        for rather than read off the class."""
        page = self._current_page
        boxes = [getattr(page, "search_box", None)]
        field = getattr(page, "page_search_field", None)
        if callable(field):
            try:
                boxes.append(field())
            except RuntimeError:
                pass        # the page went away under a queued key
        return [box for box in boxes if box is not None]

    def _live_search_box(self):
        """The box Escape is actually about.

        Focused *or* holding text, not text alone: keyed on text, an
        empty box swallowed nothing and Escape left the caret sitting in
        it - reported, and the half of this the first fix missed. The
        text half stays because a query still narrowing the grid is
        worth escaping from after the focus has moved to a card. A box
        that is neither is not what Escape is about, so it keeps its
        usual meaning the rest of the time.

        Focus wins over text, and a hidden box counts for neither: a
        tracker page carries two of these now and only one section is
        ever on screen, so a stale query in the Saved filter must not
        eat the Escape pressed in Discover."""
        for box in self._page_search_boxes():
            if box.hasFocus():
                return box
        for box in self._page_search_boxes():
            if box.isVisible() and box.text():
                return box
        return None

    def _focus_page_search(self) -> bool:
        """Ctrl+F: put the cursor in this page's own search field, and
        show the section that field lives in first.

        False when the page has no such field, which is what leaves
        Ctrl+F alone everywhere else. The section switch goes through
        _on_section_clicked rather than the page's setter directly, so
        the rail's highlight follows - a section reached by keyboard has
        to look the same as one reached by clicking it."""
        page = self._current_page
        if not callable(getattr(page, "page_search_field", None)):
            return False
        section = getattr(page, "PAGE_SEARCH_SECTION", None)
        current = getattr(page, "current_section", None)
        if section is not None and (not callable(current)
                                    or current() != section):
            self._on_section_clicked(section)
        # After the switch, never before: the section that owns the
        # field is what builds it (tracker._show_discover).
        box = page.page_search_field()
        if box is None:
            return False
        box.setFocus(Qt.FocusReason.ShortcutFocusReason)
        # Selected rather than cleared, so a second Ctrl+F over an
        # existing query types straight over it without throwing away a
        # query the user may have wanted to edit.
        box.selectAll()
        return True

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

    def _apply_route_section(self, page, route):
        """Put `page` on the section its route names, if it names one.

        Duck-typed like every other page hook here: a page with no
        sections simply has no `set_active_section` and the route cannot
        carry one either."""
        section = route_section(route)
        if not section:
            return
        setter = getattr(page, "set_active_section", None)
        if setter is None:
            return
        try:
            setter(section)
        except Exception:
            logs.exception("could not open the section a nav row names")

    def _show_page(self, page_name, direction="down", animate=True):
        if ":" not in str(page_name):
            page_name = _page_name(page_name)
        # A navigation arriving mid-fold (Ctrl+3 while the sidebar is
        # still folding) would otherwise leave the outgoing page's
        # picture sitting over the incoming one - the fold's own tween
        # carries on, it is only the freeze that has to end here.
        self._settle_fold()
        self._sync_nav_highlight(page_name)

        old_page = self._current_page
        new_page = PAGES[_page_name(page_name)](self)
        self._apply_route_section(new_page, page_name)
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
        # The sidebar re-fits with the window: how many rows fit without
        # scrolling is a function of its height (see _fit_rails).
        try:
            self._fit_rails()
        except Exception:
            logs.exception("sidebar fit failed")
        self._fit_current_page()
        self._geometry_save_timer.start(GEOMETRY_SAVE_DELAY_MS)


def _strip_scraped_ids_from_titles():
    """Take LavaScans' card id back off any title already saved with it.

    A title is a *name*, and "2072267132 The Eternal Supreme" is not
    one - it is the site's slug id, which four of manga_sites' six title
    producers were passing straight through until 24 August 2026 (see
    manga_sites._tidy_rows). The source is fixed, but a title copied
    onto an entry when it was added stays wrong forever: the owner's own
    Home hero reads "2072267132 The Eternal Supreme", which is where
    this was reported from.

    Deliberately narrow - six or more leading digits, which no real
    title in a library carries. "86" and "20th Century Boys" are
    untouched, which is the whole reason the threshold is six and not
    one. Idempotent, and never writes when there is nothing to change."""
    from helpers import manga_sites
    for filename in ("tracker.json", "series.json"):
        try:
            for entry in storage.load(filename, []):
                title = entry.get("title") or ""
                cleaned = manga_sites._LEADING_ID_RE.sub("", title).strip()
                if cleaned and cleaned != title:
                    storage.update_entry(filename, entry.get("id"),
                                         {"title": cleaned})
        except Exception:
            logs.exception(f"could not tidy titles in {filename}")


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
                specs.append((path, tracker_module.POSTER_SIZE))
                specs.append((path, home_page_module.POSTER_SIZE))
    for game in storage.load("games.json", []):
        if game.get("icon"):
            specs.append((game["icon"], home_icon_size))
        if game.get("cover"):
            specs.append((game["cover"], tracker_module.POSTER_SIZE))
    for entry in storage.load("apps.json", []):
        # The Apps grid draws art (or the icon standing in for it)
        # square at poster width; Home's quick list still draws the
        # small icon.
        art = entry.get("art") or entry.get("image")
        if art:
            specs.append((art, (link_grid_module.POSTER_ART_SIZE[0],
                                link_grid_module.POSTER_ART_SIZE[0])))
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


def _prewarm_anime_identity():
    """Resolve the arc-name season map for every tracked anime that has
    an IMDb id, in the background. See the call site for why this is done
    at launch rather than at play time; helpers/anime_identity dedupes an
    in-flight id and returns immediately for one already cached, so this
    is cheap to call on every start."""
    from helpers import anime_identity
    seen = set()
    for data_file in ("series.json", "tracker.json"):
        for entry in storage.load(data_file, []):
            if entry.get("type") != "Anime":
                continue
            imdb_id = entry.get("imdb_id")
            if not imdb_id or imdb_id in seen:
                continue
            seen.add(imdb_id)
            anime_identity.prewarm(dict(entry))


def _preload_overlays():
    """Import the player, reader and details modules ahead of the first
    click that needs one. Never raises: a failed preload just means the
    old lazy import happens at click time, exactly as before."""
    for name in ("windows.details", "windows.reader", "windows.player"):
        try:
            __import__(name)
        except Exception:
            logs.exception(f"could not preload {name}")
    # And the tracker's own once-per-session costs, for the same reason
    # and on the same timer - see tracker.prewarm for what they were
    # measured costing on the first Home -> Watch.
    try:
        from windows import tracker
        tracker.prewarm()
    except Exception:
        logs.exception("could not warm the tracker pages")


def main():
    # Before QApplication, because an exception raised anywhere after this
    # point - in a slot, in a paint event, during startup - otherwise
    # takes the whole process down through qFatal with no traceback
    # anywhere. See helpers/logs.py.
    logs.install_excepthook()
    # Before the QApplication, so every timer Qt ever creates lives in a
    # process that Windows has agreed not to quantise - see
    # startup.allow_precise_timers for the 13.9ms-per-4ms-timer
    # measurement. The reader no longer depends on this (it rides the
    # vblank), but every tween and page slide still ticks.
    startup.allow_precise_timers()
    app = QApplication(sys.argv)
    icon_path = APP_DIR / "assets" / "app_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    theme.apply_theme(app)
    # And how big a card is on this screen, before the first page is
    # built out of that number (helpers/layout - the owner's "the cards
    # sorting per row must be diff from 2K to 4K to 1080P monitors,
    # make it auto detect and adapt").
    layout.adopt()
    # Decide which display clock this machine actually has, now, off the
    # first scroll's critical path. The ticker times its candidates
    # before it trusts one (see widgets._VBlankTicker._plausible - on
    # this owner's machine DXGI's WaitForVBlank returns S_OK in 0.6
    # microseconds and is not a clock at all), and that probe waits eight
    # refreshes: 33ms at 240Hz, 133ms at 60. Paid here, once, rather than
    # by whoever scrolls first.
    warm_display_clock()
    # The wheel over a page's dead margins - the strip right of the
    # scrollbar above all (the owner's ask). One filter for every page,
    # kept alive by the app it is parented to.
    install_edge_wheel(app)
    install_horizontal_wheel_guard(app)
    # A parentless widget that gets shown becomes a window with a title
    # bar - the owner's "a small window appears then closes". Suppressed
    # and named in the log; see widgets._StrayWindowGuard.
    install_stray_window_guard(app)

    # Before the window exists: the pages it builds each load their own
    # file, and the move has to be finished before either looks.
    _merge_anime_into_series()
    _strip_scraped_ids_from_titles()
    # One-time repair, 24 August 2026: the cache trim used to evict
    # logo_cache, and each eviction's failed refetch left a permanent
    # 0-byte `.none` marker holding the loading screen logoless (Bleach
    # TYBW, the owner's report). The markers now expire (artwork.
    # NEGATIVE_TTL_S) and the trim no longer touches this folder, but
    # the ones already written today would stand for hours yet - drop
    # every empty marker once so tonight's first episode asks fresh.
    try:
        for stale in (storage.DATA_DIR / "logo_cache").glob("*.none"):
            stale.unlink(missing_ok=True)
    except Exception:
        pass

    window = MainWindow()
    # Every cover tile is cut for the screen this window is on
    # (images.set_device_ratio - the note there records the 1080p
    # pixelation that taking the sharpest screen instead caused).
    # Adopted now and again on every screen change; cover_fetch reads it
    # from workers, where walking the screen list does not belong.
    images.set_device_ratio(window.devicePixelRatioF())
    window.winId()          # ensure the QWindow exists to connect to
    handle = window.windowHandle()
    if handle is not None:
        handle.screenChanged.connect(
            lambda screen: images.set_device_ratio(
                screen.devicePixelRatio() if screen else 1.0))
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
    # And the caches trimmed to their limit, off the UI thread, once the
    # prewarm has been queued - see images.trim_cache.
    def _tidy_images():
        # Shrink the backlog of oversized files first, then enforce the
        # cap on whatever is left - both budgeted, both off the UI thread.
        try:
            images.shrink_existing()
            images.trim_cache()
        except Exception:
            logs.exception("image cache tidy failed")
    threading.Thread(target=_tidy_images, daemon=True,
                     name="atomic-image-trim").start()
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
    # The arc-name season maps for tracked anime, warmed here for one
    # reason: **AniList is what supplies the romaji, and AniList goes
    # away.** Measured 23 August 2026, mid-session, it began answering
    # 403 to this whole network (the documented rate-limit trap), and a
    # map resolved during that window carries only TMDB's English arc
    # names - enough to catch "Hashira Geiko" but not "Yuukaku Hen", so
    # five wrong-season rows survived a Demon Slayer S01E01 lookup that
    # a complete map cleared entirely.
    #
    # A complete map is cached for thirty days, so it only has to be
    # built once during any healthy window. Warming at launch means that
    # window is "some start-up in the last month" rather than "the moment
    # the owner pressed play", which is the difference between the filter
    # being reliable and being a coin toss. Costs two requests per anime
    # title per month, deduplicated, on a background thread, and does
    # nothing at all for a title already warm.
    try:
        _prewarm_anime_identity()
    except Exception:
        logs.exception("Could not prewarm the anime season maps")
    # Last, and on its own delay: the one thing here nobody is waiting
    # for. After whats_new deliberately - that dialog is modal, so a
    # timer armed before it would tick inside its nested event loop.
    window.schedule_update_check()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
