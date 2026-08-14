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
from pathlib import Path

from helpers import app_settings, theme
from helpers.nav_config import HOME_ITEM, nav_position, visible_nav_items
from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
)
from PyQt6.QtGui import QFont, QIcon, QPixmap
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
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from helpers.settings_dialog import SettingsDialog
from windows.apps import AppsPage
from windows.games import GamesPage
from windows.home import HomePage
from windows.tracker import AnimePage, MangaPage, SeriesPage
from windows.websites import WebsitesPage

APP_DIR = Path(__file__).resolve().parent

PAGES = {
    "home": HomePage,
    "anime": AnimePage,
    "manga": MangaPage,
    "series": SeriesPage,
    "games": GamesPage,
    "apps": AppsPage,
    "websites": WebsitesPage,
}

# label, page to jump to, action to run on that page once it's showing
ADD_ITEMS = [
    ("Anime Entry", "anime", lambda page: page._open_form()),
    ("Reading Entry", "manga", lambda page: page._open_form()),
    ("Series Entry", "series", lambda page: page._open_form()),
    ("Game", "games", lambda page: page._add_game()),
    ("App", "apps", lambda page: page._open_add_form()),
    ("Website", "websites", lambda page: page._open_add_form()),
]

ANIM_DURATION_MS = 220
LOGO_HEIGHT = 120

SIDEBAR_WIDTH = 220
# Wide enough for the nav bullets and the +/gear buttons once the text
# labels are dropped (see _set_sidebar_collapsed).
SIDEBAR_COLLAPSED_WIDTH = 68
SIDEBAR_ANIM_MS = 180


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Atomic")
        self.resize(1280, 840)
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

        self._show_page("home", animate=False)

        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------
    def _build_sidebar(self):
        sidebar = QWidget(objectName="Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        self.sidebar = sidebar
        self._sidebar_collapsed = False
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(4)

        # Collapse/expand toggle, pinned top-right of the sidebar so it
        # stays put (and stays reachable) at either width.
        self.fold_btn = QPushButton("«", objectName="FoldButton")
        self.fold_btn.setFixedSize(28, 28)
        self.fold_btn.setToolTip("Collapse sidebar")
        self.fold_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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

        self.add_btn = QPushButton("+", objectName="AddButton")
        self.add_btn.setFixedHeight(34)
        self.add_btn.setToolTip("Add")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: self._show_add_menu(self.add_btn))
        layout.addWidget(self.add_btn)

        self.settings_btn = QPushButton(objectName="NavButton")
        # Matches the Add button above it, and gives the collapsed gear
        # glyph room to render without being clipped.
        self.settings_btn.setFixedHeight(34)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        self._style_settings_btn()
        layout.addWidget(self.settings_btn)

        return sidebar

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

    def _style_settings_btn(self):
        collapsed = self._sidebar_collapsed
        self.settings_btn.setText(theme.SETTINGS_ICON if collapsed else "  ⚙   Settings")
        self.settings_btn.setFont(theme.icon_font() if collapsed else theme.font())
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

    def _show_add_menu(self, anchor_btn):
        hidden = set(app_settings.get_hidden_sections())
        menu = QMenu(self)
        for label, page_name, action in ADD_ITEMS:
            if page_name in hidden:
                continue
            menu.addAction(label, lambda p=page_name, a=action: self._add_via(p, a))
        menu.exec(anchor_btn.mapToGlobal(anchor_btn.rect().bottomLeft()))

    def _add_via(self, page_name, action):
        self.navigate_to(page_name, animate=False)
        action(self._current_page)

    def _open_settings(self):
        SettingsDialog(self)

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
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.BackButton:
                self.go_back()
                return True
            if event.button() == Qt.MouseButton.ForwardButton:
                self.go_forward()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.AltModifier:
            if event.key() == Qt.Key.Key_Left:
                self.go_back()
                return
            if event.key() == Qt.Key.Key_Right:
                self.go_forward()
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
        if self._current_page is not None:
            self._current_page.setGeometry(self.container.rect())


def main():
    app = QApplication(sys.argv)
    icon_path = APP_DIR / "app_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    theme.apply_theme(app)

    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
