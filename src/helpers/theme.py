"""Shared dark theme for the whole app: palette constants + one big Qt
stylesheet (QSS). Call `apply_theme(app)` once on the QApplication, and
`apply_dark_titlebar(widget)` on every top-level window/dialog so Windows
draws its native title bar in dark mode too.
"""

import ctypes
import sys

from PIL import Image, ImageDraw
from PyQt6.QtGui import QFont

from . import storage

# ---- Palette -------------------------------------------------------------
BG = "#0a0b0f"           # app background
BG_ALT = "#111319"       # secondary background (page panels)
GLOW = "#241f3d"         # soft purple tint used in the page glow backdrop
SIDEBAR = "#0d0e13"      # sidebar column
SIDEBAR_SHEEN = "#171922"  # subtle highlight at the sidebar's top edge
SIDEBAR_DEEP = "#08090d"   # ...fading darker toward its bottom
SURFACE = "#1b1d25"      # cards / inputs
SURFACE_HOVER = "#242731"
SURFACE_ACTIVE = "#2b2e3a"
BORDER = "#282b35"

TEXT = "#e9e9ee"
TEXT_MUTED = "#90929e"
TEXT_DIM = "#5c5e69"

ACCENT = "#7c6cf0"
ACCENT_HOVER = "#8d7ff5"
ACCENT_ACTIVE = "#6656d6"
ACCENT_SOFT = "#221f38"   # tinted background for the active nav item

SUCCESS = "#2ecc71"
DANGER = "#e74c3c"
DANGER_HOVER = "#ef6152"
WARNING = "#f1c40f"

# Glossy dark gradient used by every #Card item across the app (poster
# grids, game icons, quick-list rows, the Home hero carousel) - darker
# than a flat fill, with a highlight band near the top that reads as a
# sheen rather than matte. Tinted purple (the same family as ACCENT/
# ACCENT_SOFT) rather than neutral gray, to match the app's theme.
CARD_SHEEN = "#3a3260"
CARD_TOP = "#211d38"
CARD_MID = "#161328"
CARD_BOTTOM = "#0a0915"
CARD_BORDER = "#453d6e"
CARD_HOVER_SHEEN = "#4f4483"
CARD_HOVER_TOP = "#2e2850"
CARD_HOVER_MID = "#201c3a"
CARD_HOVER_BOTTOM = "#141228"

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

# Bullet marker prefixed onto every sidebar nav label (Home + each
# section) in place of the old rasterized arrow-glyph icon column - as
# plain text it always matches FONT_FAMILY_NAV/color/size exactly,
# instead of a separately-rendered PNG that could fall out of sync.
NAV_BULLET = "◈"

# Per-section glyphs, shown in place of the bullet+label when the
# sidebar is collapsed (the expanded sidebar keeps the bullets).
# Deliberately from the Segoe icon fonts that ship with Windows rather
# than emoji: these are monochrome and inherit whatever color the QSS
# gives the row, so they pick up the nav's normal/hover/selected colors
# automatically, while emoji would render in their own fixed colors and
# clash with the theme.
FONT_FAMILY_ICONS = "Segoe Fluent Icons"
FONT_FAMILY_ICON_FALLBACKS = (FONT_FAMILY_ICONS, "Segoe MDL2 Assets", FONT_FAMILY)
# Same chain in the form a QSS font-family property wants. Needed
# because a widget that any QSS rule gives font properties to resolves
# its font from that rule, not from setFont() - so a styled button
# showing one of these glyphs has to be handed the family here too, or
# it renders the codepoint as a missing-glyph box.
FONT_STACK_ICONS = ", ".join(f'"{name}"' for name in FONT_FAMILY_ICON_FALLBACKS)
NAV_ICONS = {
    "home": "",      # Home
    "anime": "",     # Video
    "manga": "",     # ReadingMode
    "series": "",    # TVMonitor
    "games": "",     # Game (controller)
    "apps": "",      # AllApps
    "websites": "",  # Globe
}
SETTINGS_ICON = ""   # Setting (gear)

RADIUS_SM = 8
RADIUS = 12
RADIUS_LG = 18

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


def _ensure_checkmark_asset() -> str:
    """A small white checkmark PNG for QCheckBox::indicator:checked.

    Once any QSS is applied to ::indicator, Qt stops drawing the native
    checkmark glyph on top of it - without this, "checked" only shows as
    a subtle color swap (easy to miss). QSS url() needs forward slashes
    even on Windows, hence as_posix().
    """
    path = storage.DATA_DIR / "ui_assets" / "checkmark.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.line([(3, 8), (6, 12), (13, 4)], fill="white", width=2, joint="curve")
        img.save(path)
    return path.as_posix()


CHECKMARK_PATH = _ensure_checkmark_asset()


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
    border-radius: 6px;
}}
QLabel {{
    background: transparent;
}}

/* ---- Sidebar --------------------------------------------------------- */
/* A restrained top-to-bottom sheen rather than a flat fill - just enough
   gradient to catch the light without competing with the item cards,
   which carry the app's more pronounced gloss. */
QWidget#Sidebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {SIDEBAR_SHEEN},
        stop:0.35 {SIDEBAR},
        stop:1 {SIDEBAR_DEEP});
    border-right: 1px solid {BORDER};
}}
/* The collapse/expand chevron pinned at the sidebar's top edge. */
QPushButton#FoldButton {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 2px;
    font-size: 13pt;
    font-weight: 700;
}}
QPushButton#FoldButton:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
QPushButton#NavButton {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS_SM}px;
    text-align: left;
    padding: 7px 12px;
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
QPushButton#NavButton:checked {{
    background: {ACCENT_SOFT};
    color: {TEXT};
    border: 1px solid {ACCENT};
}}
/* Just a centered "+" now, so it reads the same at either sidebar width
   instead of having a label that only fits when expanded. */
QPushButton#AddButton {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: {RADIUS_SM}px;
    text-align: center;
    padding: 4px;
    font-size: 16pt;
    font-weight: 700;
}}
QPushButton#AddButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#AddButton:pressed {{ background: {ACCENT_ACTIVE}; }}

QListWidget#NavList {{
    background: transparent;
    border: none;
    padding: 0px;
    outline: none;
}}
QListWidget#NavList::item {{
    background: transparent;
    color: {TEXT_MUTED};
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
    padding: 9px 12px;
    font-family: {FONT_STACK_NAV};
    font-size: {NAV_FONT_SIZE}pt;
    font-weight: 700;
}}
QListWidget#NavList::item:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
QListWidget#NavList::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
    border: 1px solid {ACCENT};
}}

/* ---- Generic page chrome --------------------------------------------- */
QWidget#Panel {{
    background: {BG_ALT};
    border-radius: {RADIUS_LG}px;
}}
QLabel#PanelTitle {{
    color: {TEXT};
    font-size: 19pt;
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

/* ---- Cards ------------------------------------------------------------ */
QFrame#Card {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {CARD_SHEEN},
        stop:0.18 {CARD_TOP},
        stop:0.6 {CARD_MID},
        stop:1 {CARD_BOTTOM});
    border-radius: {RADIUS}px;
    border: 1px solid {CARD_BORDER};
}}
QFrame#Card[hoverable="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {CARD_HOVER_SHEEN},
        stop:0.18 {CARD_HOVER_TOP},
        stop:0.6 {CARD_HOVER_MID},
        stop:1 {CARD_HOVER_BOTTOM});
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
/* Home hero carousel peeks (_HeroCardLabel) - pixmap-filled, so a
   background highlight would be painted over invisibly; a border
   isn't, since QLabel is a QFrame under the hood. */
QLabel#HeroCardLabel:hover {{
    border: 2px solid {ACCENT};
    border-radius: {RADIUS}px;
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
QFrame#Hero {{
    background: transparent;
    border: none;
}}
/* One shared glossy frame per Home section (Anime & Reading, Series,
   Games, Apps, Websites) instead of a frame behind every individual
   item inside it - the items themselves are styled #Bare now. */
QWidget#SectionBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {CARD_SHEEN},
        stop:0.18 {CARD_TOP},
        stop:0.6 {CARD_MID},
        stop:1 {CARD_BOTTOM});
    border-radius: {RADIUS_LG}px;
    border: 1px solid {CARD_BORDER};
}}
QWidget#Bare {{
    background: transparent;
}}

/* ---- Buttons ----------------------------------------------------------- */
QPushButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 8px 16px;
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton:hover {{ background: {SURFACE_HOVER}; }}
QPushButton:pressed {{ background: {SURFACE_ACTIVE}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; }}

QPushButton#Accent {{
    background: {ACCENT};
    color: white;
    border: none;
    padding: 10px 18px;
    font-weight: 700;
}}
QPushButton#Accent:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Accent:pressed {{ background: {ACCENT_ACTIVE}; }}

QPushButton#AccentIcon {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 0px;
    font-size: 18pt;
    font-weight: 700;
}}
QPushButton#AccentIcon:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#AccentIcon:pressed {{ background: {ACCENT_ACTIVE}; }}

QPushButton#Danger {{
    background: {DANGER};
    color: white;
    border: none;
}}
QPushButton#Danger:hover {{ background: {DANGER_HOVER}; }}

QPushButton#Icon {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 6px;
    font-size: 18pt;
    font-weight: 700;
}}
QPushButton#Icon:hover {{ background: {SURFACE_HOVER}; color: {TEXT}; border: 1px solid {ACCENT}; }}

QPushButton#Flat {{
    background: transparent;
    border: none;
    padding: 4px;
}}
QPushButton#Flat:hover {{ background: {SURFACE_HOVER}; }}

QPushButton#Small {{
    padding: 6px 4px;
}}

/* ---- Inputs ------------------------------------------------------------ */
QLineEdit, QComboBox, QPlainTextEdit {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 8px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
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
    background: {ACCENT};
    color: white;
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
}}
QScrollBar::handle:horizontal {{
    background: {SURFACE_HOVER};
    min-width: 28px;
    border-radius: 5px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

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
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 6px 4px;
}}

/* ---- Dialogs --------------------------------------------------------------- */
QDialog {{ background: {BG}; }}
QMessageBox {{ background: {BG}; }}

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
        hwnd = int(widget.winId())
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
