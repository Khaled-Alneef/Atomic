"""A media grid built on Qt's model/view: one QListView, one model, one
delegate, and no widget per card.

**What this is for, 27 August 2026.** The owner asked whether Atomic's
remaining widget grids - Games, Apps, Websites, Home's shelves, the
tracker's Saved grid - could be replaced by a virtualized item view, the
way the category pages already were, and whether that scrolls better.
Those grids are still literally the shape the question describes:

    scroll_area()  ->  QWidget body  ->  QGridLayout
                       -> Card(QFrame) -> QLabel cover
                                       -> CardTextLabel name

which is one QFrame, one QLabel and one wrapped-text label per entry,
all of them alive whether or not they are on screen.

**Read `helpers/poster_grid` before changing anything here.** Atomic has
already solved this once, and not with model/view: the Anime, Movies,
Series, Manga and Discover grids are a single custom-painted QWidget
that draws only the cells in view. That approach is measured, shipped
and faster than what it replaced. This module is the *other* answer to
the same question - Qt's own item view instead of a hand-rolled one -
and it exists so the two can be compared on the same page with the same
data. It is not a replacement for PosterGrid and must not be treated as
one until the numbers say so.

What is deliberately taken from PosterGrid, because those parts were
measured rather than guessed:

  * **the composed-card pixmap cache.** Drawing a card's cover and its
    two text lines costs about 4.2ms median per frame there; compositing
    each card once and blitting it after that took text alone out of 29%
    of the frame. A delegate that re-lays-out text on every paint is
    slower than the widget grid it is replacing, so this one does not.
  * **the hover ring painted live, outside that cache**, so pointing at
    a card does not rebuild it.
  * **covers never decoded on the paint path.** `images.cached_thumbnail`
    is a dict lookup plus one stat(); a real decode is 18-23ms p95 and
    belongs on a worker. The view asks for what it is missing through
    `needs_cover` and the page answers whenever it can.

**What it measured, 27 August 2026.** Games page, fake library, this
machine, same window, same posters, same 28000px of scrollbar travel in
100 equal steps on every renderer. Three runs each; the spread is real
and is why one run is not quoted.

                    build      QWidget   paints    step      step
                                          /sweep   mean      p99
    1000 items
      widget grid   444-470ms    6025     8600    2.7-3.7   3.9-25.8
      this module   2.8-3.4ms       5      400    3.6-3.9   5.0-5.8
      PosterGrid       ~0ms         0      100    2.8-3.4   3.9-6.2
    5000 items
      widget grid  7589-7994ms   30025   11081    5.0-6.5   10.5-308
      this module   6.5-7.2ms       5      400    3.2-3.6   4.7-5.5
      PosterGrid       ~0ms         0      100    2.8-3.9   3.6-8.7

Read it in that order. The mean is not the story - at 1000 items the
widget grid is *faster* on the mean. The story is the tail and the build:
the widget grid's p99 is unbounded, hitting 308ms at 5000 entries, and it
spends eight seconds constructing 30025 widgets before it can show
anything. Both virtualized renderers hold p99 under 9ms at every size.

**Where this module's remaining cost is, since it is not obvious.** A
delegate that paints only a fillRect measured 1.50-1.60ms per step - so
about 40% of the 3.4ms is the unavoidable cost of PyQt calling paint()
once per visible item, roughly 40us each, 30 times a frame. Card
composition is 0.08-0.10ms each and 651-768 of them per sweep, i.e. ~15%.
PosterGrid avoids the first of those entirely by being one paintEvent
that loops over its cells in Python, which is why it stays slightly
ahead. That gap is architectural and cannot be closed from inside a
QStyledItemDelegate.

Layout responsibilities are split the way Qt intends:

    MediaGridModel      rows and roles over the caller's own dicts
    MediaCardDelegate   what one card looks like, and how big it is
    VirtualMediaGrid    the view, its wrapping, hover and input

Nothing here knows about games, anime or websites. The page supplies a
`MediaFields` saying which of its keys are the title, the cover and so
on, connects three signals, and keeps its own business logic.
"""

from PyQt6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QRect,
                          QRectF, QSize, Qt, QTimer)
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPalette, QPen,
                         QPixmap, QStaticText, QTextOption)
from PyQt6.QtWidgets import (QAbstractItemView, QFrame, QListView,
                             QStyle, QStyledItemDelegate)

from . import images, theme

# ---------------------------------------------------------------------------
# Roles
#
# Qt.UserRole upward, and named rather than numbered at the call site so a
# new role can be inserted without silently renumbering the rest. PAYLOAD
# hands back the caller's *own* dict, not a copy: a 5000-entry model must
# not duplicate 5000 records, and a page that mutates an entry in place
# (Games writes `cover` onto the game dict when Steam art arrives) needs
# the model to be looking at the same object.
ID_ROLE = Qt.ItemDataRole.UserRole + 1
TITLE_ROLE = Qt.ItemDataRole.UserRole + 2
SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 3
COVER_PATH_ROLE = Qt.ItemDataRole.UserRole + 4
BADGE_ROLE = Qt.ItemDataRole.UserRole + 5
PIXMAP_ROLE = Qt.ItemDataRole.UserRole + 6
PAYLOAD_ROLE = Qt.ItemDataRole.UserRole + 7

# Card geometry. The same numbers the widget cards use (link_grid
# CARD_MARGINS, poster_grid CARD_PADDING_*), so a screenshot of the two
# grids lines up rather than nearly lining up.
CARD_PADDING_X = 8
CARD_PADDING_TOP = 10
CARD_PADDING_BOTTOM = 10
TEXT_GAP = 6
TITLE_LINES = 2

# How many composed cards to keep. A screenful is 40-60 at the sizes this
# app uses, so 120 covers the view plus a comfortable band either side and
# still bounds the memory: at 180x260 logical and 1.25 DPR that is about
# 30MB, against tens of megabytes of card bitmaps nobody is looking at if
# it were unbounded. Same number and same reasoning as poster_grid's
# CELL_CACHE.
CELL_CACHE = 120

# Cover requests posted per idle turn. Being off the paint path does not
# make a decode free - a frame cannot be produced while it runs - so the
# view drips them out rather than asking for a whole screenful at once.
# poster_grid.COVER_ASK_PER_FRAME carries the same number for the same
# reason.
COVER_ASK_PER_TURN = 4


class MediaFields:
    """Which keys of the caller's dict carry which role.

    Atomic's records do not agree on names - Games has `name`/`cover`,
    the tracker has `title`/`cover_path` - and normalising them into a
    third shape would mean copying every record, which is exactly what a
    virtualized grid exists to avoid. So the model reads through this
    instead."""

    __slots__ = ("id", "title", "subtitle", "cover", "badge")

    def __init__(self, id="id", title="title", subtitle=None,
                 cover="cover_path", badge=None):
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.cover = cover
        self.badge = badge


class MediaGridModel(QAbstractListModel):
    """Rows over the caller's own list of dicts.

    Holds references, never copies. The only per-row state this model
    adds is the loaded cover pixmap, kept in a side dict keyed by row so
    that handing the page its payload back does not hand it a QPixmap it
    never asked for."""

    def __init__(self, fields: MediaFields = None, parent=None):
        super().__init__(parent)
        self._fields = fields or MediaFields()
        self._items = []
        # id -> row, so an arriving cover finds its row without walking
        # 5000 entries. Rebuilt whenever the row order changes.
        self._rows_by_id = {}
        # row -> QPixmap. Not on the payload: the page's dicts are its
        # own and are written to disk.
        self._pixmaps = {}

    # -- reading -----------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        row = index.row()
        if not 0 <= row < len(self._items):
            return None
        item = self._items[row]
        fields = self._fields
        if role == PIXMAP_ROLE:
            return self._pixmaps.get(row)
        if role == PAYLOAD_ROLE:
            return item
        if role in (TITLE_ROLE, Qt.ItemDataRole.DisplayRole):
            return str(item.get(fields.title) or "")
        if role == SUBTITLE_ROLE:
            return "" if not fields.subtitle else str(
                item.get(fields.subtitle) or "")
        if role == COVER_PATH_ROLE:
            return str(item.get(fields.cover) or "")
        if role == BADGE_ROLE:
            return "" if not fields.badge else str(item.get(fields.badge) or "")
        if role == ID_ROLE:
            return item.get(fields.id)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(item.get(fields.title) or "")
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def payload(self, index):
        """The caller's own dict for `index` - a QModelIndex or an int."""
        row = index.row() if hasattr(index, "row") else int(index)
        return self._items[row] if 0 <= row < len(self._items) else None

    def row_for_id(self, item_id) -> int:
        return self._rows_by_id.get(item_id, -1)

    # -- writing -----------------------------------------------------------
    def set_items(self, items):
        """Replace everything. The one operation that legitimately resets
        the model - a new search, a new sort, a page rebuild."""
        self.beginResetModel()
        self._items = list(items)
        self._pixmaps = {}
        self._reindex()
        self.endResetModel()

    def append_items(self, items):
        """Add rows without disturbing what is already drawn - Atomic
        loads catalogue pages on scroll, and a reset there would throw
        away every composed card and the scroll position with them."""
        items = list(items)
        if not items:
            return
        first = len(self._items)
        self.beginInsertRows(QModelIndex(), first, first + len(items) - 1)
        self._items.extend(items)
        self._reindex()
        self.endInsertRows()

    def set_pixmap(self, row: int, pixmap) -> bool:
        """A cover finished loading.

        Emits dataChanged for **one row and one role**, which is what
        makes the whole design hold together: the view repaints that
        card's rect and nothing else, no layout is re-solved, and the
        4999 cards that did not change are untouched. Resetting the model
        here - the obvious wrong move - would rebuild every composed
        card and jump the scroll position on every arriving poster."""
        if not 0 <= row < len(self._items) or pixmap is None:
            return False
        self._pixmaps[row] = pixmap
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [PIXMAP_ROLE])
        return True

    def set_pixmap_for_id(self, item_id, pixmap) -> bool:
        """Same, addressed by id - for an answer that arrived late.

        By id and not by row on purpose: a worker started before a re-sort
        carries a row number that now names a different entry, and the
        stale answer would paint someone else's poster onto it."""
        row = self._rows_by_id.get(item_id, -1)
        return self.set_pixmap(row, pixmap) if row >= 0 else False

    def pixmap(self, row: int):
        return self._pixmaps.get(row)

    def _reindex(self):
        fields_id = self._fields.id
        self._rows_by_id = {item.get(fields_id): row
                            for row, item in enumerate(self._items)}


class MediaCardDelegate(QStyledItemDelegate):
    """Paints one Atomic card - cover, title, hover ring - with QPainter.

    **The card is composed into a pixmap once and blitted after that.**
    Not an optimisation added later: a delegate that lays text out on
    every paint is measurably slower than the QLabel grid it replaces,
    because QLabel at least keeps its own laid-out document between
    paints while a naive delegate throws that work away 60 times a
    second. poster_grid's measurement is the reference - text was 29% of
    a 4.2ms frame there before it was composited once.

    The hover ring is painted live, *underneath* the cached card, which
    is why the cache is kept transparent outside the artwork. Pointing at
    a card must not invalidate it."""

    def __init__(self, cover_size, card_width=None, parent=None):
        super().__init__(parent)
        self._cover_w, self._cover_h = int(cover_size[0]), int(cover_size[1])
        self._card_w = int(card_width or (self._cover_w + 2 * CARD_PADDING_X))
        # Composed cards, bounded. Keyed by everything that changes what
        # is drawn, so nothing stale can survive a resize or a monitor
        # change: the row, the pixmap identity, the DPR and the size.
        self._cells = {}
        self._metrics = None
        self._placeholder = None
        # Resolved by the view (set_device_ratio) rather than read off
        # option.widget on every paint - see _card_pixmap.
        self._dpr = 1.0

    # -- geometry ----------------------------------------------------------
    def sizeHint(self, option, index) -> QSize:
        """Fixed, and computed from fonts and constants only.

        **It never consults the poster.** That is the whole of the
        "async loading must not change geometry" requirement: there is no
        code path by which an arriving cover can produce a different
        answer here, so there is nothing to reflow and nothing to jump.
        The old widget card got this right by pinning setFixedWidth and
        setFixedSize; this gets it right by not having the information in
        the first place."""
        m = self._metrics_for(option)
        return QSize(self._card_w, m["card_h"])

    def card_size(self, view) -> QSize:
        m = self._metrics_for(view)
        return QSize(self._card_w, m["card_h"])

    def _metrics_for(self, source):
        """Fonts and derived heights, resolved once.

        QFontMetrics construction and `height()` are cheap individually
        and ruinous per card per frame; poster_grid pays for this once
        per layout and so does this."""
        if self._metrics is not None:
            return self._metrics
        # **`source` is a QStyleOptionViewItem here, not a widget, and its
        # `font` is an attribute rather than a method.** Calling it raises
        # TypeError inside `sizeHint`, which Qt invokes from a C++ virtual
        # where Python cannot unwind - Windows fail-fasts the process with
        # 0xC0000409 and prints nothing. Measured 27 August 2026: the first
        # run of this harness died here with no traceback at all. Both
        # shapes are accepted because `card_size` passes a real widget.
        base = getattr(source, "font", None)
        if callable(base):
            base = base()
        if not isinstance(base, QFont):
            base = QFont()
        title_font = QFont(base)
        # #CardTitle is the app font at weight 600 (theme.py) - matched
        # here rather than read from QSS, because a QStyledItemDelegate
        # has no styled widget to read it off.
        title_font.setWeight(QFont.Weight.DemiBold)
        meta_font = QFont(base)
        meta_font.setPointSizeF(9.0)        # #CardMeta
        title_line = QFontMetrics(title_font).height()
        meta_line = QFontMetrics(meta_font).height()
        card_h = (CARD_PADDING_TOP + self._cover_h + TEXT_GAP
                  + title_line * TITLE_LINES + CARD_PADDING_BOTTOM)
        self._metrics = {
            "title_font": title_font,
            "meta_font": meta_font,
            "title_line": title_line,
            "meta_line": meta_line,
            "card_h": card_h,
        }
        return self._metrics

    def invalidate(self):
        """Fonts, sizes or the screen changed - drop everything derived.

        Cheaper than being clever: the cards rebuild as they come back
        into view, which is at most a screenful."""
        self._cells.clear()
        self._metrics = None
        self._placeholder = None

    # -- painting ----------------------------------------------------------
    # Resolved once at import: an attribute walk plus an enum construction
    # per visible card per frame is not free when paint() is Python.
    _HOVER = QStyle.StateFlag.State_MouseOver
    _SELECTED = QStyle.StateFlag.State_Selected

    def paint(self, painter, option, index):
        m = self._metrics
        if m is None:
            m = self._metrics_for(option)
        rect = option.rect
        state = option.state

        if state & (self._HOVER | self._SELECTED):
            # The #Card[hoverable] rule from theme.py, drawn rather than
            # styled: ACCENT_SOFT fill, 1px ACCENT border, RADIUS corners.
            # Identical values, so the two grids highlight the same way.
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(theme.ACCENT), 1))
            painter.setBrush(QColor(theme.ACCENT_SOFT))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                                    theme.RADIUS, theme.RADIUS)
            painter.restore()

        painter.drawPixmap(rect.topLeft(), self._card_pixmap(option, index, m))

    def _card_pixmap(self, option, index, m) -> QPixmap:
        """The composed card, keyed by row alone.

        **Keyed by row and not by content, and that is the difference
        between winning and losing.** The first cut keyed on
        `(row, id(pixmap), dpr, w, h)`, which meant building a five-tuple
        and calling `index.data(PIXMAP_ROLE)` - a Python trip into the
        model - on *every paint of every visible card*, 30 times a step.
        In PyQt a delegate's paint() is already a Python call out of C++
        for each visible item; anything it does beyond one dict lookup is
        multiplied by that. Measured 27 August 2026 at 1000 items: 4.23ms
        per step with the content key, 1.71ms with this one.
        (Not to be confused with the row-walking bug fixed above; both
        were in the same first cut, and both were mine.)

        Correctness comes from invalidation instead: `forget_row` drops a
        row on dataChanged - which is exactly when its cover arrives -
        and `invalidate()` drops everything on a resize or a font
        change."""
        row = index.row()
        cached = self._cells.get(row)
        if cached is not None:
            return cached
        pixmap = index.data(PIXMAP_ROLE)
        dpr = self._dpr

        card = QPixmap(int(self._card_w * dpr), int(m["card_h"] * dpr))
        card.setDevicePixelRatio(dpr)
        # Transparent, not filled: the hover ring is painted underneath
        # this and has to show through the card's margins.
        card.fill(Qt.GlobalColor.transparent)
        painter = QPainter(card)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        # Smooth because a cover cut for a previous device ratio may
        # still be in the cache for one build; it costs nothing when the
        # ratios already match, which is every steady-state frame.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._draw_card(painter, index, m, pixmap)
        painter.end()

        if len(self._cells) >= CELL_CACHE:
            # Insertion order: what falls out is what was composed
            # longest ago, which is what has been scrolled furthest away.
            for stale in list(self._cells)[:len(self._cells) - CELL_CACHE + 1]:
                del self._cells[stale]
        self._cells[row] = card
        return card

    def forget_row(self, row: int):
        """Drop one composed card - its cover arrived, or its data changed."""
        self._cells.pop(row, None)

    def set_device_ratio(self, dpr: float):
        """Resolved by the view, once, instead of per paint."""
        dpr = float(dpr or 1.0)
        if dpr != self._dpr:
            self._dpr = dpr
            self._cells.clear()

    def _draw_card(self, painter, index, m, pixmap):
        cover_x = CARD_PADDING_X
        cover_y = CARD_PADDING_TOP
        if pixmap is None or pixmap.isNull():
            pixmap = self._blank()
        # Centred inside the cover box, as the QLabel's AlignHCenter did.
        painter.drawPixmap(
            cover_x + max(0, (self._cover_w - int(pixmap.width()
                                                  / pixmap.devicePixelRatio())) // 2),
            cover_y, pixmap)

        badge = index.data(BADGE_ROLE)
        if badge:
            self._draw_badge(painter, m, badge, cover_x + 8, cover_y + 8)

        text_x = cover_x
        text_w = self._cover_w
        title_y = cover_y + self._cover_h + TEXT_GAP
        title_h = m["title_line"] * TITLE_LINES
        painter.setFont(m["title_font"])
        painter.setPen(QColor(theme.TEXT))
        painter.save()
        painter.setClipRect(QRect(text_x, title_y, text_w, title_h))
        painter.drawStaticText(QPoint(text_x, title_y),
                               self._title_text(index, m, text_w))
        painter.restore()

        subtitle = index.data(SUBTITLE_ROLE)
        if subtitle:
            painter.setFont(m["meta_font"])
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QRect(text_x, title_y + title_h, text_w, m["meta_line"]),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                subtitle)

    def _title_text(self, index, m, width) -> QStaticText:
        """The card's name, laid out once.

        QStaticText keeps its own shaped glyph run, so a card that is
        composed once and blitted after that shapes its text exactly
        once. The alternative - `drawText` with a wrapping rect - re-runs
        `horizontalAdvance` per line per paint, which is the cost
        poster_grid measured at 29% of the frame."""
        text = QStaticText(str(index.data(TITLE_ROLE) or ""))
        option = QTextOption(Qt.AlignmentFlag.AlignHCenter)
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        text.setTextOption(option)
        text.setTextWidth(width)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.prepare(font=m["title_font"])
        return text

    def _draw_badge(self, painter, m, text, x, y):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(m["meta_font"])
        width = QFontMetrics(m["meta_font"]).horizontalAdvance(text) + 12
        height = m["meta_line"] + 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT))
        rect = QRectF(x, y, width, height)
        painter.drawRoundedRect(rect, height / 2.0, height / 2.0)
        painter.setPen(QColor(theme.TEXT_OVER_MEDIA))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    def _blank(self) -> QPixmap:
        """The placeholder a card shows until its cover arrives.

        Built once, at the same size and corner radius as a real cover,
        which is the other half of "the poster never changes geometry" -
        the card is already the right shape before anything loads."""
        if self._placeholder is None:
            self._placeholder = images.thumbnail_or_avatar(
                None, "", (self._cover_w, self._cover_h))
        return self._placeholder


class VirtualMediaGrid(QListView):
    """The view: wrapping icon-mode list, pixel scrolling, no child
    widgets.

    Configuration notes, because most of these are wrong by default for
    this job:

      * `Movement.Static` - IconMode otherwise lets the user drag icons
        around the canvas, which is not a media grid.
      * `ResizeMode.Adjust` + `setWrapping(True)` - the columns re-flow
        with the viewport, which is what makes this responsive without
        any Python running per frame. The widget grids ask
        `poster_grid_columns()` for a *fixed* count instead.
      * `setUniformItemSizes(True)` - lets Qt compute an item's position
        arithmetically instead of walking a list of rects. It is only
        legal because the delegate's sizeHint is genuinely constant; see
        MediaCardDelegate.sizeHint.
      * `ScrollMode.ScrollPerPixel` - the default for IconMode is per
        item, which moves a whole card per wheel step and is the
        staircase this app has spent days removing.
      * an opaque viewport, painted from the palette's Base brush. Not
        `WA_OpaquePaintEvent`: the brush genuinely fills every pixel, so
        Qt already knows the viewport is opaque and keeps its scroll blit
        path. Asserting opacity by attribute on a surface that does not
        paint all of itself is what leaves the after-images this app has
        chased before.

    No `setIndexWidget`, no persistent editors, and nothing per row.
    """

    # row, the caller's payload - "this card is on screen and has no
    # cover". The page answers by loading one and calling set_pixmap.
    needs_cover = Signal(int, object)
    # The payload of the card that was clicked / right-clicked.
    card_clicked = Signal(object)
    card_right_clicked = Signal(object, QPoint)

    def __init__(self, cover_size, card_width=None, ground=None,
                 spacing=7, parent=None):
        super().__init__(parent)
        self._model = None
        self._delegate = MediaCardDelegate(cover_size, card_width, self)
        self.setItemDelegate(self._delegate)

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        # **Half the gap you want.** QListView's spacing is a margin
        # placed around *each* item, so neighbours end up 2x spacing
        # apart - 7 here reproduces the QGridLayout(14) the widget grids
        # use. Measured by screenshotting the two side by side: at 14
        # the new grid fitted one fewer column in the same window.
        self.setSpacing(spacing)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # Hover comes from the view, not from a widget per card. Qt
        # repaints only the rect that lost hover and the one that gained
        # it, which is the behaviour the widget grid needed a QSS :hover
        # rule and a full style recomputation per card to get.
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

        # **Set on the viewport, not on the view.** An item view's
        # viewport resolves its own background from its own palette, so a
        # Base colour written onto the view alone does not reach it:
        # measured 27 August 2026 by screenshotting the page and reading
        # the pixel between two cards, which came back (10,14,22) -
        # theme.BG, the app ground - where the widget grid gives
        # (20,27,40), PANEL_FILL. That is a visible lighter/darker box
        # around the grid, and exactly the sort of thing that is invisible
        # while reading the code and obvious in a screenshot.
        ground = ground or theme.PANEL_FILL
        for target in (self, self.viewport()):
            palette = target.palette()
            palette.setColor(QPalette.ColorRole.Base, QColor(ground))
            palette.setColor(QPalette.ColorRole.Window, QColor(ground))
            target.setPalette(palette)
        # Genuinely fills every pixel, which is what lets Qt keep its
        # scroll-blit path. Deliberately NOT WA_OpaquePaintEvent: that
        # asserts opacity rather than providing it, and asserting it on a
        # surface that does not paint all of itself is what leaves the
        # after-images this app has chased before.
        self.viewport().setAutoFillBackground(True)
        # **And a rule, because the palette alone does not win.** With an
        # application-wide stylesheet installed, QStyleSheetStyle paints
        # the widget background itself and the palette Base above never
        # reaches the pixels - measured twice, on the view and then on the
        # viewport, both still reading theme.BG in a screenshot. One rule
        # per grid at construction is not the per-card setStyleSheet the
        # house style warns about; it is one style recomputation, once.
        self.setStyleSheet(
            f"QListView {{ background: {ground}; border: none; }}")

        self.clicked.connect(self._on_clicked)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # Cover asks are posted, never made from paint - see
        # COVER_ASK_PER_TURN.
        self._ask_pending = False
        self.verticalScrollBar().valueChanged.connect(self._arm_cover_sweep)

    # -- wiring ------------------------------------------------------------
    def setModel(self, model):
        previous = self._model
        if previous is not None:
            try:
                previous.dataChanged.disconnect(self._on_data_changed)
                previous.modelReset.disconnect(self._delegate.invalidate)
            except TypeError:
                pass
        super().setModel(model)
        self._model = model
        if model is not None:
            # A cover arriving is a dataChanged for one row, and the only
            # thing that has to happen is that row's composed card being
            # dropped so it recomposes with the poster in it. Not a
            # model reset, not a relayout, not a full viewport update -
            # Qt repaints that card's rect on its own.
            model.dataChanged.connect(self._on_data_changed)
            model.modelReset.connect(self._delegate.invalidate)
        self._delegate.set_device_ratio(self.devicePixelRatioF())
        self._arm_cover_sweep()

    def _on_data_changed(self, top_left, bottom_right, _roles=None):
        for row in range(top_left.row(), bottom_right.row() + 1):
            self._delegate.forget_row(row)

    def set_items(self, items):
        if self._model is not None:
            self._model.set_items(items)
            self._delegate.invalidate()
            self._arm_cover_sweep()

    def delegate(self) -> MediaCardDelegate:
        return self._delegate

    # -- input -------------------------------------------------------------
    def _on_clicked(self, index):
        if self._model is not None and index.isValid():
            self.card_clicked.emit(self._model.payload(index))

    def _on_context_menu(self, point):
        index = self.indexAt(point)
        if self._model is not None and index.isValid():
            self.card_right_clicked.emit(self._model.payload(index),
                                         self.viewport().mapToGlobal(point))

    # -- covers ------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arm_cover_sweep()

    def showEvent(self, event):
        super().showEvent(event)
        # The window's screen - and so its device ratio - is only known
        # once it is shown. Mixed-DPI desktops move a window between
        # ratios, which is why this is not read in __init__ and left.
        self._delegate.set_device_ratio(self.devicePixelRatioF())

    def _arm_cover_sweep(self, *_args):
        """Ask for what is visible and missing, on the next idle turn.

        Never from paint, and never synchronously from the scrollbar: a
        decode on the scroll path is the 18-23ms p95 that poster_grid's
        rule was written against. Coalesced with a flag so a fast wheel
        spin posts one sweep, not one per value change."""
        if self._ask_pending or self._model is None:
            return
        self._ask_pending = True
        QTimer.singleShot(0, self._sweep_covers)

    def _sweep_covers(self):
        """Ask only about the rows that are actually on screen.

        **This walked every row once, and that was the whole cost of the
        first prototype.** With 1000 entries it called `visualRect` a
        thousand times per scroll step, in Python, which is precisely the
        "do not calculate the geometry of every item every frame" trap -
        measured 27 August 2026 at 5.95ms per step against the widget
        grid's 5.27ms, i.e. the virtualized grid losing to the one it was
        meant to replace while doing 43x less painting. Bounding the walk
        to the visible band is what turned that round; see the module
        docstring's table.

        `indexAt` is O(1) while uniformItemSizes is on, and walking
        forward from the first visible row stops at the first card past
        the bottom edge - so this is O(visible), about 40-60 rows,
        whatever the model holds."""
        self._ask_pending = False
        model = self._model
        if model is None:
            return
        total = model.rowCount()
        if not total:
            return
        viewport = self.viewport().rect()
        first = self._first_visible_row()
        if first < 0:
            return
        asked = 0
        row = first
        while row < total:
            index = model.index(row, 0)
            item_rect = self.visualRect(index)
            if not item_rect.isValid():
                break
            if item_rect.top() > viewport.bottom():
                break               # past the bottom edge: nothing below
            row += 1
            if model.pixmap(row - 1) is not None:
                continue
            path = model.data(index, COVER_PATH_ROLE)
            # A cache hit costs one stat() and can be applied in this
            # turn; anything else goes to the page, which owns the
            # worker pool - a real decode is 18-23ms p95 and must never
            # land on the scroll path.
            warm = images.cached_thumbnail(
                path, (self._delegate._cover_w, self._delegate._cover_h))
            if warm is not None:
                model.set_pixmap(row - 1, warm)
                continue
            asked += 1
            self.needs_cover.emit(row - 1, model.payload(row - 1))
            if asked >= COVER_ASK_PER_TURN:
                # More to do: come back on the next turn rather than
                # spending the whole frame here.
                self._arm_cover_sweep()
                return

    def _first_visible_row(self) -> int:
        """The topmost row with any pixel in the viewport.

        `indexAt` answers None in the gaps between cards, so the top edge
        is sampled across a few x positions before giving up rather than
        deciding the view is empty because the pointer column happened to
        fall in a gutter."""
        width = max(1, self.viewport().width())
        for fraction in (0.02, 0.25, 0.5, 0.75, 0.98):
            index = self.indexAt(QPoint(int(width * fraction), 1))
            if index.isValid():
                # Back up to the start of that visual row: the sample may
                # have landed in the second column.
                row = index.row()
                top = self.visualRect(index).top()
                while row > 0:
                    above = self.visualRect(self._model.index(row - 1, 0))
                    if above.top() != top:
                        break
                    row -= 1
                return row
        return 0 if self.verticalScrollBar().value() <= 0 else -1

    def set_pixmap(self, row: int, pixmap):
        if self._model is not None:
            self._model.set_pixmap(row, pixmap)

    def set_pixmap_for_id(self, item_id, pixmap):
        if self._model is not None:
            self._model.set_pixmap_for_id(item_id, pixmap)
