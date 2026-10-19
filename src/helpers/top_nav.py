"""The page menu in the window's own bar - what replaced the sidebar.

The owner's ask, 19 October 2026: *"re-design the side bar as a better
way to show the menu of pages ... you can even use something other than
the sidebar"*, and his pick of the three directions offered: **a top
bar**, Apple TV's centred segments with Crunchyroll's grouped menus.
What was looked at before choosing, so it is not re-dug:

    tv.apple.com        no sidebar; a row of segments at the top, search
                        right, the page full-bleed underneath it
    crunchyroll.com     logo, a few flat links, and "Categories" as a
                        dropdown - how a big catalogue fits one bar
    music.apple.com     a slim grouped sidebar (direction C, not taken)
    1367studio.com      the header pill the title bar already borrows

Atomic has twelve page rows and that is too many for one flat row, but
they already fall into the three blocks nav_config.NAV_GROUPS keeps -
Watch, Read and the launchers - so the bar is Home, Discover and those
three as menus. A group's segment reads as the section it is showing
("Anime" rather than "Watch") while one of its pages is on screen, which
is the "where am I" the sidebar's lit row used to answer.

Order and visibility are nav_config's, unchanged: the user's saved drag
order sorts each menu, and a page hidden in Settings is not offered. A
group with nothing left in it is not drawn at all.

**The menu is a top-level popup, not a child.** Every page is a WebView2
native child window, and a native child paints over any non-native
sibling whatever the z-order says (.claude/rules/ui.md, "Over the video,
native or nothing") - a child dropdown would open *under* the page. A
Qt.Popup is a window of its own, the way the search suggestions are.
"""

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal as Signal
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractButton, QHBoxLayout, QSizePolicy,
                             QVBoxLayout, QWidget)

from . import images, logs, nav_config, theme
from .widgets import SmoothTween, use_hover_cursor

# What each block is called on the bar, by NAV_GROUPS' index. The third
# is the launchers - Games, Apps, Websites: things that open something
# else rather than keep progress, which is what "Library" means on a
# console shelf.
GROUP_TITLES = ("Watch", "Read", "Library")

# A route's picture in the dropdown - the same files the sidebar rows
# drew, so a page looks like itself wherever it is offered.
ROUTE_ICONS = {
    "home": theme.rail_icon("home"),
    "discover": theme.rail_icon("discover"),
    "series:cat_movies": theme.rail_icon("movies"),
    "series:cat_series": theme.rail_icon("shows"),
    "series:cat_anime": theme.rail_icon("anime"),
    "manga:cat_manga": theme.rail_icon("manga"),
    "manga:cat_manhwa": theme.rail_icon("manhwa"),
    "manga:cat_manhua": theme.rail_icon("manhua"),
    "manga:cat_novels": theme.rail_icon("novels"),
    "games": theme.rail_icon("games"),
    "apps": theme.rail_icon("apps"),
    "websites": theme.rail_icon("websites"),
}

SEGMENT_HEIGHT = 32
SEGMENT_PAD_X = 15
SEGMENT_PAD_X_DENSE = 9
CHEVRON_W = 14           # room the ▾ takes after a group's label
CAPSULE_PAD = 3          # the capsule's own inset round its segments
MARK_HEIGHT = 26

# The lit pill's slide between segments. A24's drawer curve family the
# rest of the redesign uses for things that move (ui.md, "The redesign"),
# well inside rule 7's second.
SLIDE_MS = 320
MENU_FADE_MS = 140
MENU_ROW_HEIGHT = 40
MENU_WIDTH = 220
MENU_ICON = 20
MENU_PAD = 6
# A press outside an open popup closes it and Qt then hands the same
# press to whatever was underneath - so a second click on the segment
# that opened the menu would close it and open it again in one motion.
# A press inside this long after a close on the same segment is that
# replay, not a fresh ask.
REOPEN_GUARD_MS = 260


def _font(size=11, weight=QFont.Weight.DemiBold):
    return theme.font(size, weight, theme.FONT_FAMILY_NAV,
                      fallbacks=theme.FONT_FAMILY_NAV_FALLBACKS)


def _window_origin(widget) -> QPoint:
    """`widget`'s top-left in global coordinates, from the window's own
    geometry - never mapToGlobal, which divides by the *other* screen's
    scale factor on a mixed-DPI pair (.claude/rules/ui.md)."""
    window = widget.window()
    return window.geometry().topLeft() + widget.mapTo(window, QPoint(0, 0))


class _Segment(QAbstractButton):
    """One word on the bar. Paints its label and its own hover wash; the
    lit pill behind the active one is the capsule's, so it can slide."""

    def __init__(self, label, route=None, group=None, parent=None):
        super().__init__(parent)
        self.route = route          # a page row: where a click goes
        self.group = group          # a menu: its index in NAV_GROUPS
        self.lit = False
        self.pad = SEGMENT_PAD_X
        self._hover = 0.0
        self._tween = SmoothTween(self, self._set_hover, 140)
        self.setFont(_font())
        self.setText(label)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Fixed: in full screen the bar's centre row hands its leftover
        # width out, and a growing segment drew "Home" with a gap after it
        # (photographed on the source run, 19 October 2026).
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        use_hover_cursor(self)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def setText(self, text):
        super().setText(text)
        self.updateGeometry()

    def sizeHint(self):
        width = QFontMetrics(self.font()).horizontalAdvance(self.text())
        width += 2 * self.pad
        if self.group is not None:
            width += CHEVRON_W
        return QSize(int(width), SEGMENT_HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()

    def enterEvent(self, event):
        self._tween.start(self._hover, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._tween.start(self._hover, 0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        if not self.lit and self._hover > 0.001:
            wash = QColor(theme.TEXT)
            wash.setAlpha(int(18 * self._hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(wash)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if self.lit:
            colour = QColor(theme.ON_ACCENT)
        else:
            colour = QColor(theme.mix(theme.TEXT_MUTED, theme.TEXT, self._hover))
        painter.setPen(colour)
        painter.setFont(self.font())
        text_rect = rect.adjusted(self.pad, 0, -self.pad, 0)
        if self.group is not None:
            text_rect.setRight(text_rect.right() - CHEVRON_W)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter
                         | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.group is not None:
            # The chevron is drawn, not typed: a ▾ in Bahnschrift sits on
            # the baseline and reads as a bullet beside the cap height.
            cx = text_rect.right() + CHEVRON_W / 2 + 2
            cy = rect.center().y() + 1
            path = QPainterPath(QPointF(cx - 3.5, cy - 2))
            path.lineTo(cx, cy + 1.75)
            path.lineTo(cx + 3.5, cy - 2)
            pen = QPen(colour, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()


class _Mark(QAbstractButton):
    """The brand mark at the bar's left end. A press goes Home, the way a
    site's logo does."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Home")
        use_hover_cursor(self)
        self._pixmap = None

    def sizeHint(self):
        return QSize(int(MARK_HEIGHT * 1.5) + 8, SEGMENT_HEIGHT)

    def paintEvent(self, _event):
        dpr = float(self.devicePixelRatioF() or 1.0)
        if self._pixmap is None or abs(self._pixmap.devicePixelRatio() - dpr) > 0.01:
            # Tinted to the accent like the sidebar's mark was ("matte",
            # his 25 August ask), and cut at the screen's ratio so it is
            # not soft on his 125% panel.
            self._pixmap = images.tinted_asset("assets/atomic_icon.png",
                                               theme.ACCENT, MARK_HEIGHT, dpr)
        if self._pixmap is None or self._pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        size = self._pixmap.size() / dpr
        x = (self.width() - size.width()) / 2
        y = (self.height() - size.height()) / 2
        painter.drawPixmap(QPointF(x, y), self._pixmap)
        painter.end()


class _MenuRow(QAbstractButton):
    """One page in a dropdown: its icon and its name."""

    def __init__(self, label, route, current, parent=None):
        super().__init__(parent)
        self.route = route
        self.current = current
        self.setText(label)
        self.setFont(_font(11, QFont.Weight.Medium))
        self.setFixedHeight(MENU_ROW_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        use_hover_cursor(self)
        self._hover = 0.0
        self._tween = SmoothTween(self, self._set_hover, 120)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def enterEvent(self, event):
        self._tween.start(self._hover, 1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._tween.start(self._hover, 0.0)
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        lift = 1.0 if self.current else self._hover
        if lift > 0.001:
            wash = QColor(theme.TEXT)
            wash.setAlpha(int((26 if self.current else 16) * lift))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(wash)
            painter.drawRoundedRect(rect, theme.RADIUS_SM, theme.RADIUS_SM)
        lit = 1.0 if self.current else self._hover
        colour = theme.mix(theme.TEXT_MUTED, theme.TEXT, lit)
        icon_path = ROUTE_ICONS.get(self.route)
        x = 12.0
        if icon_path:
            dpr = float(self.devicePixelRatioF() or 1.0)
            icon = images.tinted_asset(icon_path, colour, MENU_ICON, dpr)
            if not icon.isNull():
                size = icon.size() / dpr
                painter.drawPixmap(QPointF(x, (rect.height() - size.height()) / 2),
                                   icon)
            x += MENU_ICON + 12
        painter.setPen(QColor(colour))
        painter.setFont(self.font())
        painter.drawText(rect.adjusted(x, 0, -12, 0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self.text())
        if self.current:
            # A dot at the far end: the row he is on, said without colour.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.TEXT))
            painter.drawEllipse(QPointF(rect.right() - 16, rect.center().y()), 3, 3)
        painter.end()


class _NavMenu(QWidget):
    """A group's pages, dropped under its segment."""

    def __init__(self, owner, segment, rows, current):
        super().__init__(None, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("Bare")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # Moves outside the popup arrive here while it holds the mouse,
        # which is how sliding along the bar switches menus (_track).
        self.setMouseTracking(True)
        self._owner = owner
        self.segment = segment
        column = QVBoxLayout(self)
        column.setContentsMargins(MENU_PAD, MENU_PAD, MENU_PAD, MENU_PAD)
        column.setSpacing(2)
        for label, route in rows:
            row = _MenuRow(label, route, route == current, self)
            row.clicked.connect(lambda _c=False, r=route: self._pick(r))
            column.addWidget(row)
        self.setFixedWidth(MENU_WIDTH)
        self.adjustSize()
        self.setWindowOpacity(0.0)
        self._fade = SmoothTween(self, self.setWindowOpacity, MENU_FADE_MS)

    def popup_at(self, point: QPoint):
        self.move(point)
        self.show()
        self._fade.start(0.0, 1.0)

    def _pick(self, route):
        self.close()
        self._owner.navigate.emit(route)

    def mouseMoveEvent(self, event):
        self._owner._track(QCursor.pos(), self)
        super().mouseMoveEvent(event)

    def closeEvent(self, event):
        self._owner._menu_closed(self)
        super().closeEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(theme.BORDER), 1.0))
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(rect, theme.RADIUS, theme.RADIUS)
        painter.end()


class _Capsule(QWidget):
    """The rounded ground the segments sit on, and the lit pill - both
    painted by the owning TopNav, which holds the slide's state."""

    def __init__(self, owner):
        super().__init__(owner, objectName="Bare")
        self._owner = owner

    def paintEvent(self, event):
        self._owner._paint_capsule(event)


class TopNav(QWidget):
    """The mark, then Home, Discover, Watch, Read and Library in one
    capsule. Emits `navigate(route)`; the window does the navigating."""

    navigate = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, objectName="Bare")
        self.setFixedHeight(theme.TITLE_BAR_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(10)
        self.mark = _Mark(self)
        self.mark.clicked.connect(lambda: self.navigate.emit("home"))
        row.addWidget(self.mark)
        self._capsule = _Capsule(self)
        self._capsule.setFixedHeight(SEGMENT_HEIGHT + 2 * CAPSULE_PAD)
        self._seg_row = QHBoxLayout(self._capsule)
        self._seg_row.setContentsMargins(CAPSULE_PAD, CAPSULE_PAD,
                                         CAPSULE_PAD, CAPSULE_PAD)
        self._seg_row.setSpacing(2)
        row.addWidget(self._capsule, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._segments = []
        self._groups = []           # NAV_GROUPS index -> [(label, route)]
        self._current = "home"
        self._lit = None
        # The pill's slide: from where it was drawn to the lit segment's
        # live geometry, so a label changing width mid-slide is followed.
        self._slide_from = None
        self._slide_t = 1.0
        self._slide = SmoothTween(self, self._set_slide, SLIDE_MS)
        self._menu = None
        self._closed_segment = None
        self._closed_at = 0.0
        self.rebuild()

    # ---- building -------------------------------------------------------
    def rebuild(self):
        """Read nav_config again - Settings calls this (through
        main._refresh_nav_list) when a page is hidden or shown."""
        if self._menu is not None:
            self._menu.close()
        for segment in self._segments:
            self._seg_row.removeWidget(segment)
            segment.deleteLater()
        self._segments = []
        for label, route in nav_config.visible_pinned_items():
            self._add_segment(_Segment(label, route=route, parent=self._capsule))
        self._groups = nav_config.visible_nav_groups()
        for index, rows in enumerate(self._groups):
            if not rows:
                continue
            title = GROUP_TITLES[index] if index < len(GROUP_TITLES) else "More"
            segment = _Segment(title, group=index, parent=self._capsule)
            segment.title = title
            self._add_segment(segment)
        self._lit = None
        self.set_current(self._current, animate=False)

    def set_dense(self, dense):
        """Tighter segments and no mark, for a narrow window (the bar
        decides when - window_chrome.NAV_DENSE_BELOW). The mark only
        repeats Home, which is still the first segment."""
        dense = bool(dense)
        if dense == getattr(self, "_dense", False):
            return
        self._dense = dense
        for segment in self._segments:
            segment.pad = SEGMENT_PAD_X_DENSE if dense else SEGMENT_PAD_X
            segment.updateGeometry()
        if not getattr(self.window(), "isFullScreen", lambda: False)():
            self.mark.setVisible(not dense)
        self._capsule.update()

    def _add_segment(self, segment):
        if getattr(self, "_dense", False):
            segment.pad = SEGMENT_PAD_X_DENSE
        segment.clicked.connect(lambda _c=False, s=segment: self._pressed(s))
        self._seg_row.addWidget(segment)
        self._segments.append(segment)

    # ---- state ----------------------------------------------------------
    def _owner_of(self, route):
        """(segment, label) for the segment `route` belongs to, or
        (None, None) - Downloads, say, which is no row of the bar."""
        page = nav_config.route_page(route)
        for segment in self._segments:
            if segment.route is not None and segment.route == route:
                return segment, None
        for segment in self._segments:
            if segment.group is None:
                continue
            rows = self._groups[segment.group]
            for label, row_route in rows:
                if row_route == route:
                    return segment, label
        # A route of a grouped page that is no row of its own - Saved,
        # Schedule and History are `series:saved` and the like. The page
        # still belongs to the group; the segment keeps the group's name.
        for segment in self._segments:
            if segment.group is None:
                continue
            if any(nav_config.route_page(r) == page
                   for _l, r in self._groups[segment.group]):
                return segment, segment.title
        return None, None

    def set_current(self, route, animate=True):
        self._current = str(route or "")
        segment, label = self._owner_of(self._current)
        for other in self._segments:
            if other.group is not None:
                other.setText(label if other is segment and label else other.title)
        if segment is self._lit:
            self._capsule.update()
            return
        previous = self._lit
        if previous is not None:
            previous.lit = False
            previous.update()
        self._lit = segment
        if segment is not None:
            segment.lit = True
            segment.update()
        if animate and previous is not None and segment is not None and self.isVisible():
            self._slide_from = self._pill_rect()
            self._slide_t = 0.0
            self._slide.start(0.0, 1.0)
        else:
            self._slide.stop()
            self._slide_from = None
            self._slide_t = 1.0
            self._capsule.update()

    def _set_slide(self, value):
        self._slide_t = float(value)
        self._capsule.update()

    def _pill_rect(self):
        """Where the lit pill is drawn right now, in capsule coordinates."""
        if self._lit is None:
            return None
        target = QRectF(self._lit.geometry())
        start = self._slide_from
        if start is None or self._slide_t >= 1.0:
            return target
        # SmoothTween has already eased the run (ease_out_cubic), so the
        # value is used as it stands - easing again would bend it twice.
        t = self._slide_t
        return QRectF(start.x() + (target.x() - start.x()) * t,
                      start.y() + (target.y() - start.y()) * t,
                      start.width() + (target.width() - start.width()) * t,
                      start.height() + (target.height() - start.height()) * t)

    def _paint_capsule(self, _event):
        painter = QPainter(self._capsule)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        box = QRectF(self._capsule.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = box.height() / 2
        fill = QColor(theme.TEXT)
        fill.setAlpha(14)
        edge = QColor(theme.TEXT)
        edge.setAlpha(30)
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, radius, radius)
        pill = self._pill_rect()
        if pill is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.end()

    # ---- input ----------------------------------------------------------
    def _pressed(self, segment):
        if segment.route is not None:
            if self._menu is not None:
                self._menu.close()
            self.navigate.emit(segment.route)
            return
        import time
        if (self._closed_segment is segment
                and (time.monotonic() - self._closed_at) * 1000 < REOPEN_GUARD_MS):
            return
        self._open_menu(segment)

    def _open_menu(self, segment):
        if self._menu is not None:
            old, self._menu = self._menu, None
            old.close()
        rows = self._groups[segment.group] if segment.group is not None else []
        if not rows:
            return
        menu = _NavMenu(self, segment, rows, self._current)
        self._menu = menu
        origin = _window_origin(segment)
        menu.popup_at(QPoint(origin.x() - MENU_PAD,
                             origin.y() + segment.height() + CAPSULE_PAD + 8))
        logs.info(f"top nav: opened {segment.title} with {len(rows)} rows")

    def _menu_closed(self, menu):
        import time
        self._closed_segment = menu.segment
        self._closed_at = time.monotonic()
        if self._menu is menu:
            self._menu = None

    def _track(self, global_pos, menu):
        """Slide along the bar with a menu open and the menu follows,
        the way a desktop menu bar behaves."""
        for segment in self._segments:
            if segment.group is None or segment is menu.segment:
                continue
            origin = _window_origin(segment)
            local = global_pos - origin
            if segment.rect().contains(local):
                # After this event has unwound: closing the popup from
                # inside its own mouse handler is what Qt asks not to do.
                QTimer.singleShot(0, lambda s=segment: self._open_menu(s))
                return

    def close_menu(self):
        if self._menu is not None:
            self._menu.close()
