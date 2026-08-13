"""Shared dark theme for the whole app: palette constants + one big Qt
stylesheet (QSS). Call `apply_theme(app)` once on the QApplication, and
`apply_dark_titlebar(widget)` on every top-level window/dialog so Windows
draws its native title bar in dark mode too.
"""

import ctypes
import math
import sys

from PIL import Image, ImageDraw
from PyQt6.QtGui import QFont

from . import storage

# ---- Palette -------------------------------------------------------------
BG = "#0a0b0f"           # app background
BG_ALT = "#111319"       # secondary background (page panels)
GLOW = "#241f3d"         # soft purple tint used in the page glow backdrop
SIDEBAR = "#0d0e13"      # sidebar column
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

FONT_FAMILY = "Segoe UI"
FONT_FAMILY_EMOJI = "Segoe UI Emoji"

RADIUS_SM = 8
RADIUS = 12
RADIUS_LG = 18


def font(size=10, weight=QFont.Weight.Normal, family=FONT_FAMILY):
    f = QFont(family, size)
    f.setWeight(weight)
    return f


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


def _ensure_nav_icon_asset() -> str:
    """A small atom-symbol PNG (three tilted orbit rings around a nucleus,
    each with an electron riding it) used as the one shared sidebar marker
    for Home + every section, in place of a plain text glyph.

    Drawn at 4x and downsampled with LANCZOS for clean anti-aliased edges
    at the small size it's actually shown at (PIL's own draw calls aren't
    anti-aliased). Each electron is drawn directly onto its own orbit's
    layer *before* that layer is rotated, so it rides along exactly on
    the ring with no separate trig needed to place it post-rotation.
    """
    path = storage.DATA_DIR / "ui_assets" / "nav_atom.png"
    if path.exists():
        return path.as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)

    scale = 4
    size = 64 * scale
    cx = cy = size / 2
    color = TEXT

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    rx, ry = 27 * scale, 11 * scale
    stroke = 3 * scale
    electron_r = 3.4 * scale
    # (rotation angle, parametric phase the electron sits at on the
    # unrotated ring) - varied per orbit so the three electrons land at
    # different points around the atom instead of all lining up.
    orbits = ((0, 15), (60, 195), (120, 315))
    for angle, phase_deg in orbits:
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=color, width=stroke)
        phase = math.radians(phase_deg)
        ex, ey = cx + rx * math.cos(phase), cy + ry * math.sin(phase)
        layer_draw.ellipse((ex - electron_r, ey - electron_r, ex + electron_r, ey + electron_r), fill=color)
        layer = layer.rotate(angle, resample=Image.BICUBIC, center=(cx, cy))
        canvas.alpha_composite(layer)

    nucleus_r = 6.5 * scale
    canvas_draw = ImageDraw.Draw(canvas)
    canvas_draw.ellipse((cx - nucleus_r, cy - nucleus_r, cx + nucleus_r, cy + nucleus_r), fill=color)

    canvas = canvas.resize((64, 64), Image.LANCZOS)
    canvas.save(path)
    return path.as_posix()


NAV_ICON_PATH = _ensure_nav_icon_asset()


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
QWidget#Sidebar {{
    background: {SIDEBAR};
    border-right: 1px solid {BORDER};
}}
QLabel#Brand {{
    color: {TEXT};
    font-size: 15pt;
    font-weight: 700;
    padding: 4px 4px;
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
QPushButton#NavButton:checked {{
    background: {ACCENT_SOFT};
    color: {TEXT};
    border: 1px solid {ACCENT};
}}
QPushButton#AddButton {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: {RADIUS_SM}px;
    text-align: left;
    padding: 7px 12px;
    font-size: 10.5pt;
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
    padding: 7px 12px;
    font-size: 10.5pt;
    font-weight: 600;
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
    background: {SURFACE};
    border-radius: {RADIUS}px;
    border: 1px solid {BORDER};
}}
QFrame#Card[hoverable="true"]:hover {{
    background: {SURFACE_HOVER};
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
QFrame#Hero {{
    background: {SURFACE};
    border-radius: {RADIUS_LG}px;
    border: 1px solid {BORDER};
}}
QWidget#SectionBox {{
    background: {BG};
    border-radius: {RADIUS_LG}px;
    border: 1px solid {BORDER};
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
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 6px;
}}
QPushButton#Icon:hover {{ background: {SURFACE_HOVER}; color: {TEXT}; }}

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
    width: 11px;
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
