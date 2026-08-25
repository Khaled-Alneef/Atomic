"""Shared dark theme for the whole app: palette constants + one big Qt
stylesheet (QSS). Call `apply_theme(app)` once on the QApplication, and
`apply_dark_titlebar(widget)` on the main window so Windows draws its
native title bar in dark mode. Dialogs no longer have a native bar at
all - they go through `widgets.frameless_dialog` instead.
"""

import ctypes
import sys

from PIL import Image, ImageDraw
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont

from . import storage

# ---- Palette -------------------------------------------------------------
# The Harbor navy + teal theme (the owner's reference screenshot,
# 25 August 2026 - replaces the warm gold/near-black spec). Six tones
# are fixed by the app's color spec and everything else is derived from
# them, so the whole UI stays one family rather than drifting per page:
#
#   TEXT #E8EEF6 · TEXT_MUTED #93A1B5 · SURFACE #141B28
#   SURFACE_HOVER #1C2534 · BORDER #2A3548 · ACCENT #2FB9A6
#
# Every derived tone keeps the spec's hue (a cool navy around 215-222
# deg at low value, with the accent's teal at ~172) and only moves
# lightness - a neutral gray mixed in anywhere reads as a dirty patch
# against these, the same way it did against the gold palette this
# replaces.
#
# The accent is a mid-value *teal*, and that is why ON_ACCENT survives
# the hue change: white on #2FB9A6 computes to a 2.4:1 contrast ratio -
# the same trap the gold and the cyan before it both had - where
# ON_ACCENT on the same fill computes to 7.5:1 (and 4.6:1 on the
# gradient's deeper sea-green end, against white's 4.0:1 there;
# computed 25 August 2026, not eyeballed). Anything filled with the
# accent (or with ACCENT_GRADIENT) takes ON_ACCENT for its text/glyph,
# never white.
BG = "#0a0e16"           # app background - near-black, cool navy
BG_ALT = "#10151f"       # secondary background (page panels)
# The two lobes of the nebula the page backdrop paints in from its right
# edge (widgets.GlassPage): a teal core with a blue bloom off it.
GLOW = "#14515a"         # deep teal core of the backdrop glow
GLOW_ALT = "#16375f"     # ...and the blue bloom beside it
SIDEBAR = "#070a11"      # sidebar column
SIDEBAR_SHEEN = "#121824"  # subtle highlight at the sidebar's top edge
SIDEBAR_DEEP = "#05070c"   # ...fading darker toward its bottom
SURFACE = "#141b28"      # cards / inputs ("card background")
SURFACE_HOVER = "#1c2534"  # ...lifted on hover ("card elevated")
SURFACE_ACTIVE = "#263143"
# The lit top lip a "glass" panel catches - one step above SURFACE_HOVER
# and used only as a gradient's first stop, never as a fill on its own.
SURFACE_SHEEN = "#2f3b4f"
BORDER = "#2a3548"

TEXT = "#e8eef6"
TEXT_MUTED = "#93a1b5"
TEXT_DIM = "#64718a"
# Pure white, for text sitting directly over video/artwork (the player's
# top bar) where the palette's blue-tinted TEXT reads as dingy against a
# bright frame. Not for text on the app's own surfaces - TEXT is
# calibrated against those.
TEXT_OVER_MEDIA = "#ffffff"

ACCENT = "#2fb9a6"        # primary action - teal
ACCENT_HOVER = "#48d2be"
ACCENT_ACTIVE = "#1f9a89"
# The deeper sea-green end every accent gradient runs into. The name
# keeps the "blue" it had when the gradient ran cyan->blue, on purpose:
# every consumer refers to the token, and renaming it would touch files
# this values-only re-theme must not.
ACCENT_BLUE = "#1c8f80"
ACCENT_BLUE_HOVER = "#26a795"
ACCENT_SOFT = "#102a2c"   # tinted background for the active nav item
# Text/glyph color on any accent-filled surface. See the note above -
# white on teal is the one combination this palette cannot use.
ON_ACCENT = "#021815"

# Green, but deliberately not the old #2ee0a4: that one sits at hue 160,
# 12 deg off the new teal accent (172), and would read as a second
# accent everywhere a DONE badge and a play button share a card. #4ade80
# at hue 142 is 30 deg clear (computed 25 August 2026), still plainly
# "success green" - and ON_ACCENT holds 10.5:1 on it, so badges filled
# with it keep the same ink as accent fills.
SUCCESS = "#4ade80"
DANGER = "#ff5470"
DANGER_HOVER = "#ff7285"
# Back to a true amber: the gold palette had to shift warning toward
# orange-red because amber was a near-twin of the gold accent (hue 43 vs
# 41). Against the teal accent that collision is gone - #f5b342 sits at
# hue 38, 134 deg from ACCENT and 48 deg from DANGER's pink (computed
# 25 August 2026), so it reads as its own color beside both.
WARNING = "#f5b342"

def mix(base: str, other: str, amount: float) -> str:
    """`base` blended `amount` (0..1) of the way toward `other`, as a
    palette-shaped hex token.

    Here rather than at a call site for the same reason `rgba` is: an
    animated colour is still a colour, and the moment it is written as a
    literal it stops tracking the palette. main._RailDelegate walks a
    row's icon and label from TEXT_MUTED to TEXT along this - see
    main.RAIL_TINTS for why the ramp is precomputed rather than
    evaluated per frame."""
    amount = 0.0 if amount < 0.0 else (1.0 if amount > 1.0 else float(amount))
    start = base.lstrip("#")
    end = other.lstrip("#")
    channels = []
    for index in (0, 2, 4):
        first = int(start[index:index + 2], 16)
        second = int(end[index:index + 2], 16)
        channels.append(int(round(first + (second - first) * amount)))
    return "#{:02x}{:02x}{:02x}".format(*channels)


def lit_fill(top: str, body: str) -> str:
    """`body` with `top` as a lit lip along its upper edge - the shade
    every filled control in the reference carries.

    Vertical, always. A fill lit from a corner reads as a flat wash on
    anything wider than it is tall, because the eye has no horizon to
    read the light against; the reference lights each control from its
    top edge and lets it settle into the body colour, which is what
    makes a 6px pill and a 46px button look like the same material."""
    return (f"qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {top}, stop:1 {body})")


# **The accent ramp is top-lit and vertical** - the owner's ask, 25
# August 2026, pointing at the reference's CONTINUE WATCHING chip and
# Continue button: *"they do have some shade on them, make sure to
# follow this style in the app"*.
#
# It ran corner-to-corner (0,0 -> 1,1) before. One call site had already
# reached past that for (0,0,0,1) by hand - tracker.py's featured chip -
# which is the same instinct arrived at independently.
#
# Three stops, not two, and this is the part that is easy to get wrong:
# the lift is a *lip*, only the top 7% of the fill. A two-stop ramp
# starting at the lifted colour makes the whole control paler instead of
# lit, which is a different thing and reads as a washed-out button. The
# reference's own highlight is a single brighter line at the top edge.
ACCENT_LIP_MIX = 0.28      # how far the lip lifts toward TEXT
ACCENT_LIP_STOP = 0.07     # ...and how little of the fill it covers


def accent_stops(hover=False):
    """The accent ramp as (position, colour) pairs.

    The single source both the stylesheet and the hand-painted chips
    read. poster_grid draws its chips with QPainter and repeated the two
    stops as literals - with a comment still calling them "gold to
    amber" a whole re-theme later. A ramp written twice is a ramp that
    drifts, and that one had."""
    teal = ACCENT_HOVER if hover else ACCENT
    deep = ACCENT_BLUE_HOVER if hover else ACCENT_BLUE
    return ((0.0, mix(teal, TEXT, ACCENT_LIP_MIX)),
            (ACCENT_LIP_STOP, teal),
            (1.0, deep))


def accent_gradient(x1=0, y1=0, x2=0, y2=1, hover=False):
    """The accent fill as QSS. Vertical by default now; the coordinates
    stay parameterised for the one surface that wants another angle."""
    stops = ", ".join(f"stop:{at} {colour}"
                      for at, colour in accent_stops(hover))
    return f"qlineargradient(x1:{x1}, y1:{y1}, x2:{x2}, y2:{y2}, {stops})"


ACCENT_GRADIENT = accent_gradient()
ACCENT_GRADIENT_HOVER = accent_gradient(hover=True)

# The quiet half of the accent language: the reference's CONTINUE
# WATCHING chip is *not* a bright accent pill with dark ink on it - it
# is a dark teal chip with mint lettering, which is what lets it sit on
# a bright backdrop without competing with the Continue button beside
# it. ACCENT_SOFT is that chip's body; the lip is mixed toward the
# accent rather than toward TEXT, so the chip lights in its own hue.
ACCENT_SOFT_GRADIENT = lit_fill(mix(ACCENT_SOFT, ACCENT, 0.22), ACCENT_SOFT)
ACCENT_SOFT_TEXT = ACCENT_HOVER

# **The accent splits in two** - the owner's ask, 25 August 2026, after
# seeing the reference beside the app: *"yes split them, deeper teal
# with white text"*.
#
# The reference reserves the bright teal for things that only have to be
# *seen* - the mark, the active pager pill, a progress chunk, a chip on
# artwork - and fills the things you *click* with a deeper teal carrying
# white lettering. Atomic had one accent doing both jobs, which is why
# its Continue button came out bright with near-black ink where the
# reference's is deep with white.
#
# Contrast against white, computed 25 August 2026:
#
#   body #1a7568  5.54:1        foot #13594f  8.17:1
#   body #1e8375  4.61:1 hover  foot #166459  7.00:1
#   lip  #22897c  4.25:1        lip  #279486  3.71:1 hover
#
# The lip is the one pair under 4.5 and that is not a failure to fix: it
# is the top 7% of the fill, a highlight line, and a button's label is
# vertically centred - it sits on the body and the foot, which are the
# two the ratio is actually about. Judging a control by its brightest
# strip would forbid having a highlight at all, which is the feature.
#
# ON_ACCENT is not superseded and must not be swept up: it is still the
# only readable ink on the *bright* accent (white on that is 2.44:1), and
# every indicator still filled with it - ArtChip, the reader's language
# badge, the details page's watched markers, both progress chunks -
# keeps that pairing on purpose.
ACCENT_DEEP = "#1a7568"
ACCENT_DEEP_LIP = "#22897c"
ACCENT_DEEP_FOOT = "#13594f"
ACCENT_DEEP_HOVER = "#1e8375"
ACCENT_DEEP_LIP_HOVER = "#279486"
ACCENT_DEEP_FOOT_HOVER = "#166459"
ACCENT_DEEP_ACTIVE = "#12524a"
ON_ACCENT_DEEP = "#ffffff"


def accent_button_gradient(x1=0, y1=0, x2=0, y2=1, hover=False):
    """The fill for a *pressable* accent surface. Same three-stop shape
    as accent_gradient - lip, body, foot - a couple of steps deeper so
    white reads on it."""
    lip = ACCENT_DEEP_LIP_HOVER if hover else ACCENT_DEEP_LIP
    body = ACCENT_DEEP_HOVER if hover else ACCENT_DEEP
    foot = ACCENT_DEEP_FOOT_HOVER if hover else ACCENT_DEEP_FOOT
    return (f"qlineargradient(x1:{x1}, y1:{y1}, x2:{x2}, y2:{y2},"
            f" stop:0 {lip}, stop:{ACCENT_LIP_STOP} {body}, stop:1 {foot})")


ACCENT_BUTTON_GRADIENT = accent_button_gradient()
ACCENT_BUTTON_GRADIENT_HOVER = accent_button_gradient(hover=True)

# The CARD_* glossy-gradient family is gone with the boxes it painted:
# tiles are frameless in the Harbor language (see the #Card rules below)
# and the one remaining card fill is the flat SURFACE the matte variant
# always used.

# One "glass panel" fill, used by everything that reads as a translucent
# slab rather than a card: the page panels, Home's section frames, the
# player's overlay bars. Top-lit, so the panel's lip catches light and
# the body settles into the background - the same trick the cards use,
# spread over a much larger area, so the sheen is far subtler (a card's
# sheen across a 700px-wide panel reads as an uneven wash).
def glass_fill(top=SURFACE_SHEEN, body=SURFACE, foot=None):
    """One flat panel colour now - the lit top lip and the darker foot
    are gone at the owner's ask ("make it all the same color"): under
    the gold palette the warm sheen at a panel's top edge read as a gold
    stain rather than glass. Signature kept so no caller changes; only
    `body` decides the colour."""
    return body


PANEL_FILL = glass_fill()

FONT_FAMILY = "Segoe UI"
FONT_FAMILY_EMOJI = "Segoe UI Emoji"
# Sidebar nav list. Bahnschrift is the DIN-derived technical face that
# ships with Windows 10/11 - geometric and a little industrial, which
# suits the app's "atomic" styling better than the very condensed,
# dated Agency FB it replaces, while staying far more legible at the
# larger nav size below. Listed as a fallback chain rather than one
# name so a machine without it degrades to Segoe UI rather than to
# whatever Qt picks by itself.
FONT_FAMILY_NAV = "Bahnschrift"
FONT_FAMILY_NAV_FALLBACKS = (FONT_FAMILY_NAV, "Segoe UI Semibold", "Segoe UI")
# Same chain in the form a QSS font-family property wants.
FONT_STACK_NAV = ", ".join(f'"{name}"' for name in FONT_FAMILY_NAV_FALLBACKS)
NAV_FONT_SIZE = 13

# Fallback marker for a nav row whose section has no icon in NAV_ICONS
# below, and for one whose PNG is missing from the bundle. Expanded rows
# lead with the section's own icon; the bullet only ever appears for an
# unmapped key, so a new section still gets a readable row instead of a
# blank one - which is the failure mode that matters, since
# images.tinted_asset answers a missing file with a *null* pixmap and Qt
# draws that as nothing at all, silently.
NAV_BULLET = "◈"

# **Every rail row's icon is a bundled SVG now, not a cut PNG and not a
# font codepoint** (the owner's icon pack, 25 August 2026; the PNG sheet
# of 18 it replaces landed 22 August). All 24x24, `fill="none"
# stroke="#FFFFFF" stroke-width="2"` with round caps and joins, so
# images.tinted_asset can recolour one with SourceIn exactly as it did
# the alpha-only PNGs - the stroke's antialiased edge is the alpha.
#
# **Vector, so the rail stops paying for a raster scale.** A PNG cut at
# one size was resampled to whatever the fold and the DPI asked for (26
# expanded, 29 folded, x1.0/1.25/1.5/2.0); an SVG is rendered straight
# at the device size instead - see images._rendered_svg. The squareness
# the old sheet was hand-trimmed for is now the viewBox's, so the folded
# rail still centres every row on one axis with no per-shape offset
# table (main.py carried three tuned constants for exactly two
# hand-drawn icons once; none are needed).
#
# Recoloured rather than shipped in a fixed colour: the row's colour is
# a *state* - muted at rest, brightening toward TEXT as it is hovered or
# selected, and main._RailDelegate lerps it across that range - and a
# coloured file would ignore it the same way an emoji did.
RAIL_ICON_DIR = "assets/icons"
# What tells an icon row from a glyph row. Named here rather than typed
# at each test, because the two ends have to move together: main.py's
# _style_rail_item keys off exactly this suffix, and when it said ".png"
# in one place and the files said ".svg" in the other, *every* row would
# have silently fallen back to NAV_BULLET with nothing to say why.
RAIL_ICON_SUFFIX = ".svg"


def rail_icon(name: str) -> str:
    """`name` as the path images.tinted_asset wants, relative to the
    asset root. Written as a call rather than typed out per row so the
    tables below read as a mapping and the directory lives in one place.
    """
    return f"{RAIL_ICON_DIR}/{name}{RAIL_ICON_SUFFIX}"


def is_rail_icon(token) -> bool:
    """True when `token` is one of the icon paths above rather than a
    font codepoint - main._style_rail_item's test, living beside the
    suffix it depends on. A Segoe private-use codepoint can never end in
    it, so the test cannot go wrong on a future entry."""
    return str(token).endswith(RAIL_ICON_SUFFIX)


# The Segoe icon fonts still dress the Downloads, Settings, Back and
# fold-arrow *buttons*, and theme.NAV_BULLET's fallback row - the
# owner's sheet has no icon for any of them, so they keep their Fluent
# glyphs and the two font stacks below stay live. Monochrome and
# inheriting the row's colour is why they were chosen over emoji, and
# the PNGs above are tinted for exactly the same reason.
FONT_FAMILY_ICONS = "Segoe Fluent Icons"
FONT_FAMILY_ICON_FALLBACKS = (FONT_FAMILY_ICONS, "Segoe MDL2 Assets", FONT_FAMILY)
# Same chain in the form a QSS font-family property wants. Needed
# because a widget that any QSS rule gives font properties to resolves
# its font from that rule, not from setFont() - so a styled button
# showing one of these glyphs has to be handed the family here too, or
# it renders the codepoint as a missing-glyph box.
FONT_STACK_ICONS = ", ".join(f'"{name}"' for name in FONT_FAMILY_ICON_FALLBACKS)
# Page key -> icon file. The two names that do not match their key are
# the tracker pages: the key is "manga"/"series" because that is what
# saved nav orders and the JSON files already say (see nav_config), and
# the *rows* read "Read"/"Watch" - so the artwork is library.svg (a
# shelf of books) and live-tv.svg. Anime merged into the Watch page long
# ago, so anime.svg belongs to the cat_anime section
# (main.SECTION_ICONS), not to a nav row.
NAV_ICONS = {
    "home": rail_icon("home"),
    "manga": rail_icon("library"),
    "series": rail_icon("live-tv"),
    "games": rail_icon("games"),
    "apps": rail_icon("apps"),
    "websites": rail_icon("websites"),
}
SETTINGS_ICON = ""   # Setting (gear) - a button, not a rail row

RADIUS_SM = 8
RADIUS = 12
RADIUS_LG = 18

# The window's own top bar (helpers/window_chrome), which replaced the
# native Windows caption. Here rather than in that module because the
# stylesheet below needs both numbers and theme cannot import it back.
TITLE_BAR_HEIGHT = 48
TOP_SEARCH_HEIGHT = 34

# The hero eyebrow: CONTINUE WATCHING / CONTINUE READING on Home, and
# FEATURED / TOP RESULT on the tracker's featured banner. One string
# because it is one design on two surfaces - widgets.hero_split already
# says the two heroes are deliberately the same shape - and it had been
# written out identically in both files, which is how the *last* pair of
# duplicated colours got a re-theme behind before anyone noticed.
#
# Soft accent, not the bright fill it used to be: on the reference this
# chip is dark teal with mint lettering, which is what lets it sit two
# inches from the Continue button without the pair competing. A second
# bright accent pill up there read as two primary actions.
EYEBROW_CHIP_QSS = (
    f"color: {ACCENT_SOFT_TEXT}; background: {ACCENT_SOFT_GRADIENT};"
    f" border-radius: {RADIUS_SM}px; padding: 3px 10px;"
    f" font-size: 8.5pt; font-weight: 700; letter-spacing: 1px;")


def rgba(color: str, alpha: int) -> str:
    """A palette hex token as a QSS rgba() carrying `alpha`, so a
    translucent fill still has the palette as its single source - a
    hand-typed rgba would be a literal colour with extra steps."""
    value = color.lstrip("#")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


# Also referenced outside the stylesheet (see windows.home) to compensate
# fixed-width centered content for the vertical scrollbar's width - it
# only ever eats space from the right of a scroll area's viewport, never
# both sides, which would otherwise throw off that centering.
SCROLLBAR_WIDTH = 11


def font(size=10, weight=QFont.Weight.Normal, family=FONT_FAMILY, fallbacks=()):
    f = QFont(family, size)
    f.setWeight(weight)
    if fallbacks:
        # setFamilies is the only way to hand Qt a real fallback chain -
        # QFont(family) alone silently resolves to some default face if
        # that one name is missing, rather than trying the next choice.
        f.setFamilies(list(fallbacks))
    return f


def icon_font(size=14):
    """The Segoe icon font used for the collapsed sidebar's glyphs."""
    return font(size, QFont.Weight.Normal, FONT_FAMILY_ICONS,
                fallbacks=FONT_FAMILY_ICON_FALLBACKS)


def nav_font():
    """The sidebar nav list's font. QListWidget's item delegate paints
    with the widget's own font(), not the ::item QSS font-family, so the
    stylesheet rule alone is silently ignored for list items - both have
    to be kept in step, hence one helper rather than a literal at each
    call site."""
    return font(NAV_FONT_SIZE, QFont.Weight.Bold, FONT_FAMILY_NAV,
                fallbacks=FONT_FAMILY_NAV_FALLBACKS)


def nav_row_font():
    """An expanded nav row's font: glyph + label in one QListWidgetItem,
    which carries exactly one font. The icon face leads the fallback
    chain so the Fluent glyph resolves from it, and - because Segoe
    Fluent Icons carries no Latin letters (the Settings button already
    relies on this, see the #NavButton QSS note) - the label falls
    through to the nav face beside it."""
    return font(NAV_FONT_SIZE, QFont.Weight.Bold, FONT_FAMILY_ICONS,
                fallbacks=(FONT_FAMILY_ICONS, *FONT_FAMILY_NAV_FALLBACKS))


def _ensure_checkmark_asset() -> str:
    """A small checkmark PNG for QCheckBox::indicator:checked.

    Once any QSS is applied to ::indicator, Qt stops drawing the native
    checkmark glyph on top of it - without this, "checked" only shows as
    a subtle color swap (easy to miss). QSS url() needs forward slashes
    even on Windows, hence as_posix().

    Drawn in ON_ACCENT rather than white, because the checked indicator
    is filled with the teal accent and a white tick on it is the same
    unreadable pairing ON_ACCENT exists to avoid. Written under a new
    filename ("...teal") on purpose, the third rename for the same
    reason as the first two ("...dark", "...gold"): this only creates
    what is missing, so a recolored asset under the old name would never
    be drawn - every existing install would keep its gold-era tick
    forever.
    """
    path = storage.DATA_DIR / "ui_assets" / "checkmark_teal.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.line([(3, 8), (6, 12), (13, 4)], fill=ON_ACCENT, width=2, joint="curve")
        img.save(path)
    return path.as_posix()


CHECKMARK_PATH = _ensure_checkmark_asset()


def _ensure_chevron_asset() -> str:
    """A small down-chevron PNG for QComboBox::down-arrow.

    Every dropdown in the app was rendering with *no* arrow at all: the
    QSS below styles QComboBox, which turns off Qt's native primitive
    drawing, and ::down-arrow takes an `image:` url and nothing else -
    there was no file to point it at. The owner rightly asked how anyone
    is meant to know the box unfolds. Drawn at 2x and referenced at half
    size so it stays crisp on a 125%/150% display, same reason the
    checkmark above exists as a file at all.

    "...cool" filename: drawn in TEXT_MUTED, which the navy re-theme
    changed again, and this function only creates what is missing -
    under the old name every existing install would keep its warm
    gold-era chevron (the checkmark above carries the same trap, paid
    for twice now).
    """
    path = storage.DATA_DIR / "ui_assets" / "chevron_down_cool.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.line([(5, 9), (12, 16), (19, 9)], fill=TEXT_MUTED, width=3,
                  joint="curve")
        img.save(path)
    return path.as_posix()


CHEVRON_DOWN_PATH = _ensure_chevron_asset()


STYLESHEET = f"""
* {{
    outline: none;
}}
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "{FONT_FAMILY}";
    font-size: 10.5pt;
}}
QToolTip {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    border-radius: 7px;
}}
QLabel {{
    background: transparent;
}}

/* ---- Sidebar --------------------------------------------------------- */
/* A restrained top-to-bottom sheen rather than a flat fill - just enough
   gradient to catch the light without competing with the item cards,
   which carry the app's more pronounced gloss.
   The two right-hand corners are rounded so the column reads as a panel
   laid on the background rather than as a wall built into the window
   edge. Corners only - the sidebar's width, its border and everything
   inside it are untouched, so nothing moves. */
QWidget#Sidebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SIDEBAR_SHEEN},
        stop:0.35 {SIDEBAR},
        stop:1 {SIDEBAR_DEEP});
    border-right: 1px solid {BORDER};
    border-top-right-radius: {RADIUS_LG}px;
    border-bottom-right-radius: {RADIUS_LG}px;
}}
/* The collapse/expand chevron pinned at the sidebar's top edge. The
   icon stack is here because the glyph is a Fluent chevron
   (main.FOLD_*), and a QSS-styled button resolves its font from the
   rule, not from setFont - without the family the codepoint renders as
   a box. 14pt because the owner asked for the single chevron drawn
   large; the button stays 28px, which a 14pt Fluent glyph still fits. */
QPushButton#FoldButton {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 2px;
    font-family: {FONT_STACK_ICONS};
    /* 12pt against the button's 24px box (main._build_fold_button).
       The glyph shrank with the button - at 14pt in a 24px box the
       chevron touches the padding and the hover square clips it. */
    font-size: 12pt;
    font-weight: 400;
}}
QPushButton#FoldButton:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
/* The icon font stack is on the base rule, not only on the collapsed one
   below, because both states now draw the same gear (see main._style_
   settings_btn) - expanded used an emoji, which rendered in its own fixed
   colors and at its own size, so folding the sidebar visibly swapped the
   symbol. Segoe Fluent Icons carries no Latin letters, so the "Settings"
   label after the glyph falls through the chain to Segoe UI on its own
   and looks exactly as it did. */
QPushButton#NavButton {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS}px;
    text-align: left;
    padding: 7px 12px;
    font-family: {FONT_STACK_ICONS};
    font-size: 10.5pt;
    font-weight: 600;
}}
QPushButton#NavButton:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
/* Collapsed sidebar: the label is gone, so the lone glyph gets centred
   and the left text inset that positioned that label is dropped - with
   it still applied the glyph was pushed off-centre and clipped against
   the narrow rail's edge. */
QPushButton#NavButton[collapsed="true"] {{
    text-align: center;
    padding: 7px 0px;
    font-family: {FONT_STACK_ICONS};
    font-size: 14pt;
    font-weight: 400;
}}
/* The active row is Harbor's: a full-width rounded pill in a soft
   neutral from the warm surface family, label lifted to TEXT - no
   accent border, no gradient. The fill alone says "you are here"; the
   accent stays reserved for actions and hovers, which is what keeps a
   sidebar of one active row and six muted ones reading calm the way
   the reference does. */
QPushButton#NavButton:checked {{
    background: {SURFACE_HOVER};
    color: {TEXT};
    border: none;
    border-radius: {RADIUS}px;
}}
/* Just a centered "+" now, so it reads the same at either sidebar width
   instead of having a label that only fits when expanded. Gold into
   amber, the app's one primary-action fill. */
QPushButton#AddButton {{
    background: {ACCENT_BUTTON_GRADIENT};
    color: {ON_ACCENT_DEEP};
    border: none;
    border-radius: {RADIUS}px;
    text-align: center;
    padding: 4px;
    font-size: 16pt;
    font-weight: 700;
}}
QPushButton#AddButton:hover {{ background: {ACCENT_BUTTON_GRADIENT_HOVER}; }}
QPushButton#AddButton:pressed {{ background: {ACCENT_ACTIVE}; }}

QListWidget#NavList {{
    background: transparent;
    border: none;
    padding: 0px;
    outline: none;
}}
/* Taller rows than the app's other lists, on purpose - Harbor's nav
   breathes. The transparent resting border reserves the pill's space
   so nothing shifts when a row becomes the active one. 8px horizontal,
   down from 12: the leading glyph is wider than the bullet it
   replaced, and at 12 the longest label ("Movies & Series") elided -
   measured on a real-window grab.

   **No `background` and no `color` for a *state* any more: the hover
   wash, the selected pill, its accent indicator and the row's text
   colour are all painted by main._RailDelegate**, so they can animate
   (25 August 2026 - the owner's Harbor nav feel: fade in over
   120-160ms, and the selection moves to a new row rather than
   snapping). A QSS rule cannot be interpolated, and leaving these here
   would have painted a second, un-animated pill underneath the drawn
   one; a `color` here would likewise win over the palette the delegate
   sets, because QStyleSheetStyle configures the palette from the rule
   after the delegate has filled it in.

   What stays is everything geometric - padding, the transparent
   resting border, the radius, the font - because those are what the
   folded rail's icon centring and the row height were measured
   against (see main._RailDelegate.initStyleOption). */
QListWidget#NavList::item {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 11px 8px;
    font-family: {FONT_STACK_NAV};
    font-size: {NAV_FONT_SIZE}pt;
    font-weight: 700;
}}
/* Kept, and deliberately identical to the resting rule. The delegate
   strips State_MouseOver/State_Selected off the option before handing
   the row to drawControl, so neither of these can match while it is
   installed - but a row drawn without it (the drag pixmap, a future
   view) then still comes out flat instead of gaining a pill nothing
   animates. */
QListWidget#NavList::item:hover,
QListWidget#NavList::item:selected {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
}}

/* ---- The window's own title bar ------------------------------------- */
/* SIDEBAR, not BG: the bar and the sidebar column meet at a corner, and
   two different near-blacks meeting there reads as a seam rather than as
   one piece of chrome. The hairline underneath is what separates the
   chrome from the page, and it is the only border on it. */
QWidget#TitleBar {{
    background: {SIDEBAR};
    border-bottom: 1px solid {BORDER};
}}
QPushButton#BackButton {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 6px 14px;
    font-family: {FONT_STACK_ICONS};
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton#BackButton:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
QPushButton#BackButton:disabled {{ color: {TEXT_DIM}; background: transparent; }}
/* Square, full-height, no radius: these are Windows' caption buttons in
   the place Windows' caption buttons go, and rounding them would put a
   gap of bar colour in the window's own top-right corner. */
QPushButton#WindowButton, QPushButton#WindowClose {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: 0px;
    padding: 0px;
    font-family: {FONT_STACK_ICONS};
}}
QPushButton#WindowButton:hover {{ background: {SURFACE_HOVER}; color: {TEXT}; }}
QPushButton#WindowButton:pressed {{ background: {SURFACE_ACTIVE}; }}
/* Red on hover, which is the one caption-button convention Windows users
   read without looking - and the only place in the app DANGER is a
   hover state rather than a warning. */
QPushButton#WindowClose:hover {{ background: {DANGER}; color: {TEXT_OVER_MEDIA}; }}
QPushButton#WindowClose:pressed {{ background: {DANGER_HOVER}; }}
/* One search field for the whole app. Fully rounded, because it is the
   only control on the bar that is not square and the pill is what tells
   the eye it is a field rather than a label. */
QLineEdit#TopSearch {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {TOP_SEARCH_HEIGHT // 2}px;
    padding: 4px 14px;
    font-size: 10pt;
}}
QLineEdit#TopSearch:hover {{ border: 1px solid {mix(BORDER, ACCENT, 0.4)}; }}
QLineEdit#TopSearch:focus {{ border: 1px solid {ACCENT}; background: {SURFACE_HOVER}; }}

/* ---- Generic page chrome --------------------------------------------- */
QWidget#Panel {{
    background: {PANEL_FILL};
    border-radius: {RADIUS_LG}px;
}}
QLabel#PanelTitle {{
    color: {TEXT};
    font-size: 19pt;
    font-weight: 700;
    background: transparent;
}}
/* Home's clock, at the far end of the greeting's line. Larger than the
   greeting's own 19pt rather than equal to it: set at the same size the
   clock reads as the smaller of the two, because the greeting is a wide
   phrase with an emoji beside it and the clock is five narrow glyphs. */
QLabel#HomeClock {{
    color: {TEXT};
    font-size: 22pt;
    font-weight: 700;
    background: transparent;
}}
QLabel#HeroTitle {{
    color: {TEXT};
    font-size: 16pt;
    font-weight: 700;
    background: transparent;
}}
QLabel#PanelSubtitle {{
    color: {TEXT_MUTED};
    font-size: 10.5pt;
    background: transparent;
}}
QLabel#SectionTitle {{
    color: {TEXT};
    font-size: 12.5pt;
    font-weight: 700;
    background: transparent;
}}
QLabel#Muted {{
    color: {TEXT_MUTED};
    background: transparent;
}}
/* One frame per key, so "Ctrl+K" reads as two keys and not as a string
   with a plus in it. The darker bottom border is the whole illusion -
   a flat rectangle reads as a badge, an edge under it reads as a cap
   with a side. */
QLabel#KeyCap {{
    color: {TEXT};
    background: {SURFACE_HOVER};
    border: 1px solid {BORDER};
    border-bottom: 2px solid {BG};
    border-radius: {RADIUS_SM}px;
    padding: 2px 8px;
    font-weight: 600;
}}
QLabel#KeyPlus {{
    color: {TEXT_DIM};
    background: transparent;
}}

/* ---- Cards ------------------------------------------------------------ */
/* Tiles are frameless, Harbor's language: the rounded artwork itself
   floats on the ground (images.py clips every thumbnail to the same
   radius) with the title under it, and there is no box at all until
   the pointer arrives. Hover is a tile's one lit moment - the gold
   ring plus ACCENT_SOFT's tinted lift, the same pair #HomeItem below
   has always used, so Home and the tracker grids read as one system.
   The transparent resting border reserves the ring's space so nothing
   reflows on mouse-over. Used by the Anime/Reading/Series poster
   grids, Discover's rows, and the Games/Apps/Websites tiles. */
QFrame#Card {{
    background: transparent;
    border-radius: {RADIUS}px;
    border: 1px solid transparent;
}}
QFrame#Card[hoverable="true"]:hover {{
    background: {ACCENT_SOFT};
    border: 1px solid {ACCENT};
}}
/* Matte variant (Card(matte=True), or the property set directly on a
   plain #Card frame) - the *row* treatment now: details' episode and
   chapter rows, the reader's chapter list, the player's overlay rows,
   Discover's featured panel and the Schedule rows. Harbor rows are
   borderless, so the flat SURFACE fill carries the shape alone and the
   border only exists on hover, as the same accent ring the tiles get.
   Both selectors outrank the frameless rules above on specificity
   (same id, one more attribute). */
QFrame#Card[matte="true"] {{
    background: {SURFACE};
    border: 1px solid transparent;
}}
QFrame#Card[matte="true"][hoverable="true"]:hover {{
    background: {SURFACE_HOVER};
    border: 1px solid {ACCENT};
}}
/* Home's frameless item cards - poster/game tiles and quick-list rows.
   No resting fill, so the section behind them shows through, plus a
   hover highlight.

   These need an objectName of their own rather than reusing #Bare:
   the transparent-background #Bare rule above is an ID selector, which
   outranks any attribute- or pseudo-class-based rule on CSS
   specificity, so a #Bare item can never paint a hover background at
   all no matter how the hover rule is written. The transparent resting
   border reserves the hover border's space so nothing shifts on
   mouse-over. */
QFrame#HomeItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
}}
QFrame#HomeItem:hover {{
    background: {ACCENT_SOFT};
    border: 1px solid {ACCENT};
}}
QLabel#CardTitle {{
    color: {TEXT};
    font-weight: 600;
    background: transparent;
}}
QLabel#CardMeta {{
    color: {TEXT_MUTED};
    font-size: 9pt;
    background: transparent;
}}
/* The little chips drawn ON a card's artwork (a rating, a Saved mark).
   Object names rather than a per-widget setStyleSheet: every
   setStyleSheet call is a style recomputation for that widget, and these
   are built one per card while the grid fills under a scroll - measured
   23 August 2026 as part of the 40-frames-a-second fill. The values are
   the ones tracker._chip used inline, unchanged. */
QLabel#ArtChip {{
    background: {ACCENT_GRADIENT};
    color: {ON_ACCENT};
    border-radius: {RADIUS_SM}px;
    padding: 2px 8px;
    font-size: 8pt;
    font-weight: 700;
}}
QLabel#ArtChipPlain {{
    background: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 2px 7px;
    font-size: 8pt;
    font-weight: 700;
}}
QFrame#Hero {{
    background: transparent;
    border: none;
}}
/* One shared frame per Home section (Anime & Reading, Series, Games,
   Apps, Websites) instead of a frame behind every individual item
   inside it - the items themselves are styled #Bare now.

   A flat fill and no border: Harbor's panels are soft rounded slabs
   with nothing drawn around them, and the 1px ring this carried made
   it the last outlined box on a page that no longer has any. */
QWidget#SectionBox {{
    background: {PANEL_FILL};
    border-radius: {RADIUS_LG}px;
    border: none;
}}
QWidget#Bare {{
    background: transparent;
}}

/* ---- Buttons ----------------------------------------------------------- */
/* Glass rather than a flat slab: a one-step lift at the top edge that
   settles back to the card colour, so the button catches the same light
   the panels do. Padding and font size are exactly as they were - only
   the fill and the corner radius moved. */
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SURFACE_HOVER}, stop:1 {SURFACE});
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 8px 16px;
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SURFACE_ACTIVE}, stop:1 {SURFACE_HOVER});
    border: 1px solid {ACCENT};
}}
QPushButton:pressed {{ background: {SURFACE_ACTIVE}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; }}

/* The primary action anywhere in the app: the top-lit teal ramp with
   near-black ON_ACCENT on it - white on this teal computes to 2.4:1
   (see the palette note). Rounded to RADIUS rather than RADIUS_SM so
   the ends read as caps; padding is untouched, so nothing changes
   size. */
QPushButton#Accent {{
    background: {ACCENT_BUTTON_GRADIENT};
    color: {ON_ACCENT_DEEP};
    border: none;
    border-radius: {RADIUS}px;
    padding: 10px 18px;
    font-weight: 700;
}}
QPushButton#Accent:hover {{ background: {ACCENT_BUTTON_GRADIENT_HOVER}; }}
QPushButton#Accent:pressed {{ background: {ACCENT_DEEP_ACTIVE}; }}

QPushButton#AccentIcon {{
    background: {ACCENT_BUTTON_GRADIENT};
    color: {ON_ACCENT_DEEP};
    border: none;
    border-radius: {RADIUS}px;
    padding: 0px;
    font-size: 18pt;
    font-weight: 700;
}}
QPushButton#AccentIcon:hover {{ background: {ACCENT_BUTTON_GRADIENT_HOVER}; }}
QPushButton#AccentIcon:pressed {{ background: {ACCENT_DEEP_ACTIVE}; }}

QPushButton#Danger {{
    background: {lit_fill(DANGER_HOVER, DANGER)};
    color: {ON_ACCENT};
    border: none;
}}
QPushButton#Danger:hover {{ background: {lit_fill(mix(DANGER_HOVER, TEXT, 0.18), DANGER_HOVER)}; }}

QPushButton#Icon {{
    background: {lit_fill(SURFACE_HOVER, SURFACE)};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px;
    font-size: 18pt;
    font-weight: 700;
}}
QPushButton#Icon:hover {{ background: {lit_fill(SURFACE_ACTIVE, SURFACE_HOVER)}; color: {TEXT}; border: 1px solid {ACCENT}; }}
/* A button carrying a menu (the tracker's filter) gets Qt's own little
   down-arrow drawn into it, beside the icon it already has. Nothing here
   wants two glyphs, so it is removed rather than styled. */
QPushButton::menu-indicator {{ image: none; width: 0px; height: 0px; }}

/* The round arrows over a sideways-scrolling row (widgets.SideScroller).
   Circular via a radius of half the fixed 30px size, and deliberately
   the card colour rather than the accent: it sits on top of the row's
   own cards, and an accent disc there reads as one of them being
   selected. 15pt for the chevron, which is a small glyph at label
   size. */

QPushButton#Flat {{
    background: transparent;
    border: none;
    padding: 4px;
}}
QPushButton#Flat:hover {{ background: {SURFACE_HOVER}; }}

QPushButton#Small {{
    padding: 6px 4px;
}}

/* The section pills in a tracker page's header (Saved / Schedule /
   History). Quiet at rest so the page title leads, and the checked one
   wears the same soft pill the sidebar's active row does - one
   active-thing language across the window.

   **There was no rule for these at all until 25 August 2026**, so they
   fell through to the generic QPushButton above: a slab with a border,
   identical whether or not it was the section on screen, since nothing
   styled :checked either. The icon each one carries is set in
   tracker._build_header_tabs, tinted to match these two colours. */
QPushButton#Ghost {{
    background: transparent;
    color: {TEXT_MUTED};
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 7px 14px;
    font-size: 10pt;
    font-weight: 700;
}}
QPushButton#Ghost:hover {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid transparent;
}}
QPushButton#Ghost:checked {{
    background: {SURFACE_HOVER};
    color: {TEXT};
    border: 1px solid transparent;
}}

/* ---- Inputs ------------------------------------------------------------ */
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 8px 10px;
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 26px; }}
/* Every foldable box carries a visible down-chevron at its right edge,
   app-wide - drawn to a file because ::down-arrow accepts an image url
   and nothing else (see _ensure_chevron_asset). Widgets that paint
   their own arrow (the reader's chapter combo) switch this off with
   image: none in their own stylesheet. */
QComboBox::down-arrow {{
    image: url({CHEVRON_DOWN_PATH});
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
    outline: none;
    padding: 4px;
}}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {SURFACE};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    image: url({CHECKMARK_PATH});
}}
/* Disabled state, for a checkbox that only applies while another one is
   on (Settings > Startup's fullscreen option follows the Windows-startup
   toggle above it). Spelled out because the rules above replace Qt's
   native indicator drawing entirely - including the greying it would
   otherwise do for free, which left a dead checkbox looking every bit as
   live as a working one. */
QCheckBox:disabled {{ color: {TEXT_MUTED}; }}
QCheckBox::indicator:disabled {{
    border: 1px solid {BORDER};
    background: {BG};
}}
QCheckBox::indicator:checked:disabled {{
    background: {ACCENT_SOFT};
    border: 1px solid {ACCENT_SOFT};
}}

/* ---- Lists / Trees ------------------------------------------------------ */
QTreeWidget, QListWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    color: {TEXT};
    padding: 4px;
}}
QTreeWidget::item, QListWidget::item {{
    padding: 6px 4px;
    border-radius: 6px;
}}
QTreeWidget::item:hover, QListWidget::item:hover {{
    background: {SURFACE_HOVER};
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background: {ACCENT_GRADIENT};
    color: {ON_ACCENT};
}}
QHeaderView::section {{
    background: {BG_ALT};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-weight: 700;
    font-size: 9pt;
}}
QTreeWidget::branch {{ background: transparent; }}

/* ---- Scroll areas -------------------------------------------------------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: {SCROLLBAR_WIDTH}px;
    margin: 2px 0px 2px 0px;
}}
QScrollBar::handle:vertical {{
    background: {SURFACE_HOVER};
    min-height: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0px 2px 0px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {SURFACE_HOVER};
    min-width: 28px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
/* Without this Qt paints the groove either side of the handle with the
   native style's checkerboard dither, which on this near-black theme
   reads as a strip of white dots (the owner's screenshot). The vertical
   rules above always had it; the horizontal pair was simply missed. */
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

/* ---- Menus --------------------------------------------------------------- */
QMenu {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 8px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{ background: {ACCENT_BUTTON_GRADIENT}; color: {ON_ACCENT_DEEP}; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 6px 4px;
}}
/* Checkable menu items draw a real box - the owner's ask, 24 August
   2026 ("a box that will be checked if choose otherwise empty"). Once
   QMenu itself is styled, Qt stops drawing the native check glyph, so
   without these rules a ticked filter looked identical to an unticked
   one. Same recipe as QCheckBox below: empty bordered box at rest,
   accent fill with the drawn tick when checked. */
QMenu::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {TEXT_DIM};
    border-radius: 4px;
    background: {BG};
    margin-left: 4px;
}}
QMenu::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url({CHECKMARK_PATH});
}}

/* ---- Dialogs --------------------------------------------------------------- */
/* The square QDialog fill never paints on the app's own dialogs any
   more - widgets.frameless_dialog consumes the Paint event and draws
   the rounded panel itself. The rule stays for any stray dialog that
   hasn't been through it, so that still opens dark rather than white. */
QDialog {{ background: {BG}; }}
QMessageBox {{ background: {BG}; }}
/* The small heading widgets.frameless_dialog puts inside a panel whose
   native title bar was its only name. */
QLabel#DialogTitle {{
    color: {TEXT};
    font-size: 13pt;
    font-weight: 700;
    background: transparent;
}}

/* ---- Splitters / Separators ------------------------------------------------ */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {BORDER}; }}
"""


def apply_theme(app):
    """Configure the app-wide QSS + default font. Safe to call once."""
    app.setStyleSheet(STYLESHEET)
    app.setFont(font(10))
    return app


def apply_dark_titlebar(widget):
    """Ask Windows to draw this window's native title bar in dark mode.

    No-op on non-Windows platforms and silently ignored on Windows builds
    that don't support the DWM attribute (pre-1809).
    """
    if sys.platform != "win32":
        return
    try:
        # **c_void_p, not a bare Python int.** ctypes types an untyped
        # integer argument as C `int`, which truncates a 64-bit HWND to
        # 32 bits and hands DWM a handle that is not one. Not a bug that
        # has been observed here - HWNDs on this machine have stayed
        # small enough - but `_set_window_transitions` a few lines below
        # has always wrapped its handle and this one did not, and a
        # latent truncation that only fires on unlucky handle values is
        # exactly the kind of thing that reads as a random crash later.
        hwnd = ctypes.c_void_p(int(widget.winId()))
        value = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+/Win11).
        # 19 = same attribute on earlier Win10 1809/1903 builds.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        pass


def without_window_animation(widget, action, resume_after_ms=400):
    """Run `action` (a window state change) with Windows' own open/close/
    minimize/maximize animation switched off for this window, then switch
    it back on.

    This is what makes leaving full screen look right. Windows animates a
    window being maximized by zooming it out from wherever its *restored*
    size sits - and a window that went full screen from maximized is,
    underneath, a restored-size window with a full-screen frame on top.
    So the return trip played that zoom: the window appeared at its small
    restored size for a moment and then flew back out to full size.
    Measured, not guessed - reading the screen every 10 ms across the
    transition, the window failed to cover its own maximized area for
    about 120 ms of it, and with the animation off, for none of it.

    Restoring the setting afterwards rather than leaving it off keeps the
    ordinary animations (minimize to the taskbar, restore from it) that
    the user does still expect to see.
    """
    if sys.platform != "win32":
        action()
        return
    handle = None
    try:
        handle = ctypes.c_void_p(int(widget.winId()))
        _set_window_transitions(handle, disabled=True)
    except Exception:
        handle = None
    try:
        action()
    finally:
        if handle is not None:
            # After the state change has settled, not immediately: the
            # animation this is suppressing would otherwise still be
            # queued up and play as soon as it is re-enabled.
            QTimer.singleShot(resume_after_ms,
                              lambda: _set_window_transitions(handle, disabled=False))


def _set_window_transitions(handle, disabled):
    try:
        value = ctypes.c_int(1 if disabled else 0)
        # 3 = DWMWA_TRANSITIONS_FORCEDISABLED
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            handle, 3, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def bring_window_to_front(widget):
    """Show `widget`'s window, restore it if minimized, and put it in
    front of everything else.

    Needed because a window opened by a process Windows doesn't consider
    to be in the foreground is not allowed to raise itself - it appears
    behind whatever was there and only blinks in the taskbar. That is how
    the app came back after an update: relaunched by a detached script,
    with no foreground rights of its own. The updater hands those rights
    over before it quits (see updater._allow_foreground_for_relaunch);
    this is the other half, the new window actually claiming them.

    SW_RESTORE rather than showNormal(): it returns a minimized window to
    whatever it was before - maximized stays maximized - where showNormal
    would drop it to a restored size.
    """
    widget.show()
    widget.raise_()
    widget.activateWindow()
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = int(widget.winId())
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
