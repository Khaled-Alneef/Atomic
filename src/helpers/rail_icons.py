"""Vector icons for the sidebar rails, drawn rather than typed.

Two rows in the section rail carried **emoji** - a bookmark for Saved and
a cat face for Anime - because Segoe Fluent Icons has neither shape (the
old note in main.SECTION_ICONS records that E8A4, the one named
"Bookmarks", draws a bulleted list, and that E933 drew an I-beam where a
cat was wanted). The owner's ask, 22 August 2026: *"replace the save
emoji and the cat face emoji in anime to a proper icons not emojis"*.

An emoji is the wrong thing here for a reason that is visible on screen
rather than a matter of taste: it is a **colour** glyph, so it renders in
its own fixed palette and ignores the row's normal / hover / selected
colour entirely - a pink ribbon and a ginger cat sitting in a column of
monochrome gold-white glyphs. theme.py already says exactly this about
why every other rail glyph comes from the Segoe icon fonts.

So these are painted: stroked paths on a transparent pixmap, in whatever
colour the caller asks for, cut at the screen's devicePixelRatio and
tagged with it (the same rule the sidebar logo follows - see
.claude/rules/ui.md). The stroke weight is matched to Segoe Fluent's own
at the sizes the rail uses, measured against the glyph rows beside them.

Pixmaps are cached per (name, size, colour, lead, ink_left, ratio): the
rail restyles every row on every fold, and re-drawing four paths per row
per fold is work for nothing.
"""

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap)
from PyQt6.QtWidgets import QApplication

# Everything below is drawn inside a 24x24 box and scaled to the size
# asked for, so the geometry reads as one grid the way an icon font's
# does. Fluent's own glyphs are designed on a 20-unit body inside a
# 24-unit em, which is why the shapes here keep a unit of air on each
# side rather than filling the square.
_GRID = 24.0

# Stroke width in grid units. 1.6 measured against Segoe Fluent Icons at
# the rail's 14pt: its hairlines come out just over 1.5 device pixels at
# 20px, and a 2.0 stroke here read visibly heavier than the row above.
_STROKE = 1.6

_cache = {}
# Ink boxes are pure geometry - no screen in them - so they survive a
# clear_cache(), which exists only to drop pixmaps cut at a stale ratio.
_bounds = {}


def _ratio() -> float:
    app = QApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    return float(screen.devicePixelRatio()) if screen else 1.0


def _bookmark_path() -> QPainterPath:
    """A ribbon bookmark - the shape the owner asked for in the first
    place, and the one thing the icon font does not carry."""
    path = QPainterPath()
    path.moveTo(6.5, 3.2)
    path.lineTo(17.5, 3.2)
    path.lineTo(17.5, 20.8)
    path.lineTo(12.0, 15.9)
    path.lineTo(6.5, 20.8)
    path.closeSubpath()
    return path


def _cat_paths():
    """A cat's head: the face outline with its two ears, then the eyes,
    nose and whiskers as separate strokes.

    Returned as a list because the whiskers and the ears cannot be one
    path - a single closed outline would join the ear tips to the cheeks
    and the whole thing reads as a crown."""
    head = QPainterPath()
    # Left ear, over the top of the skull, right ear, then the jaw. The
    # ears are part of the outline rather than triangles stuck on it,
    # which is what keeps the silhouette readable at rail size.
    head.moveTo(4.6, 4.0)
    head.lineTo(8.6, 7.0)
    head.cubicTo(9.7, 6.5, 10.8, 6.3, 12.0, 6.3)
    head.cubicTo(13.2, 6.3, 14.3, 6.5, 15.4, 7.0)
    head.lineTo(19.4, 4.0)
    head.lineTo(19.4, 11.6)
    head.cubicTo(19.4, 16.6, 16.1, 20.0, 12.0, 20.0)
    head.cubicTo(7.9, 20.0, 4.6, 16.6, 4.6, 11.6)
    head.closeSubpath()

    left_eye = QPainterPath()
    left_eye.moveTo(9.1, 12.0)
    left_eye.lineTo(9.1, 13.1)
    right_eye = QPainterPath()
    right_eye.moveTo(14.9, 12.0)
    right_eye.lineTo(14.9, 13.1)

    muzzle = QPainterPath()
    # The nose as a small wedge, and the mouth as the cat's two curves
    # under it - without the mouth the eyes-and-nose alone read as a
    # bear.
    muzzle.moveTo(11.1, 15.2)
    muzzle.lineTo(12.9, 15.2)
    muzzle.lineTo(12.0, 16.2)
    muzzle.closeSubpath()

    whiskers = QPainterPath()
    whiskers.moveTo(3.0, 14.4)
    whiskers.lineTo(7.4, 15.0)
    whiskers.moveTo(3.0, 17.0)
    whiskers.lineTo(7.4, 16.2)
    whiskers.moveTo(21.0, 14.4)
    whiskers.lineTo(16.6, 15.0)
    whiskers.moveTo(21.0, 17.0)
    whiskers.lineTo(16.6, 16.2)

    return [(head, False), (left_eye, False), (right_eye, False),
            (muzzle, True), (whiskers, False)]


def _paths_for(name):
    if name == "bookmark":
        return [(_bookmark_path(), False)]
    if name == "cat":
        return _cat_paths()
    return []


def ink_box(name: str) -> QRectF:
    """Where `name` actually puts pixels, in grid units.

    The path bounds grown by half the stroke on every side, because a
    stroked path draws _STROKE/2 *outside* its own geometry - the
    bookmark's outline reaches grid x=6.5 but its ink starts at 5.7.

    This exists because the shapes are not centred in the 24-unit grid
    and were never meant to be: the bookmark's ink spans 5.7-18.3 (12.6
    wide) and the cat's 2.2-21.8 (19.6 wide). Left-aligning them by
    canvas alone would therefore leave the bookmark 3.5 units right of
    the cat, which is most of the misalignment the owner has reported
    three times."""
    found = _bounds.get(name)
    if found is not None:
        return found
    box = None
    for path, _filled in _paths_for(name):
        rect = path.boundingRect()
        box = rect if box is None else box.united(rect)
    if box is None:
        box = QRectF(0.0, 0.0, _GRID, _GRID)
    half = _STROKE / 2.0
    box = box.adjusted(-half, -half, half, half)
    _bounds[name] = box
    return box


def pixmap(name: str, size: int, color: str, lead: int = 0,
           ink_left: float = 0.0) -> QPixmap:
    """`name` drawn at `size` logical pixels in `color`. Cached.

    `lead` widens the pixmap by that many blank columns on the right.
    It exists because a view reserves `QIcon.actualSize(iconSize)` for a
    decoration and **never scales a pixmap up** - so asking the list for
    a wider icon box did nothing and the label beside a drawn row sat
    3px left of every glyph row's (measured on a real-window grab: ink
    at x=58 against x=61). The pad has to be in the pixmap itself.

    `ink_left` is where the artwork's **ink** starts, in logical pixels
    from the canvas's left edge - not where the 24-unit grid starts.
    That distinction is the whole point:

    - a view puts a decoration's left edge exactly where a text-only
      row's text begins, so ink at `ink_left=0` lands within a pixel of
      where a Segoe glyph's own ink lands (measured 22 August 2026:
      glyph ink starts at x=8-9 in an expanded row, x=8-10 folded);
    - the two shapes carry very different slack inside the grid (see
      `ink_box`), so any scheme that positions the *canvas* leaves them
      3.5 grid units apart from each other whatever it does to the pair.

    This replaced a `nudge` that shifted the artwork left inside an
    unchanged canvas. Nudge was a centre-matching fix and centres were
    never the complaint: measured, every folded row already centred on
    x=18.0 while the bookmark's ink began at 13 against the calendar's
    10 and the camera's 8. In a vertical stack the eye lines up edges,
    so an 11px-wide bookmark centred with a 21px camera reads as
    indented - which is the owner's "move it to the left a bit",
    reported three times.

    Padding still cannot do this job in a folded rail, which is why the
    offset is baked into the paint: the view reserves exactly
    `option.decorationSize`, and a pixmap wider than that is scaled
    *down* to fit rather than centred - lead=2 moved the icon not one
    pixel and quietly shrank it ~9%."""
    ratio = _ratio()
    key = (name, int(size), str(color), int(lead), round(float(ink_left), 2),
           ratio)
    found = _cache.get(key)
    if found is not None:
        return found
    device = max(1, int(round(size * ratio)))
    width = device + max(0, int(round(lead * ratio)))
    canvas = QPixmap(width, device)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = device / _GRID
    painter.scale(scale, scale)
    # In grid units, so it reads in the same coordinates the paths do.
    # `ink_left` is logical px, and one logical px is _GRID/size grid
    # units at this size - the device ratio cancels, which is why this
    # holds on a 125% display without a second term.
    painter.translate(float(ink_left) * (_GRID / float(max(1, size)))
                      - ink_box(name).left(), 0.0)
    pen = QPen(QColor(color))
    pen.setWidthF(_STROKE)
    # Round joins and caps, because Fluent's are: a mitred corner on the
    # bookmark's point reads as a spike beside them.
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    for path, filled in _paths_for(name):
        painter.setBrush(QColor(color) if filled else Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    painter.end()
    canvas.setDevicePixelRatio(ratio)
    _cache[key] = canvas
    return canvas


def icon(name: str, size: int, color: str, highlight: str,
         lead: int = 0, ink_left: float = 0.0) -> QIcon:
    """A two-mode QIcon: `color` at rest, `highlight` when the row is
    selected. The rail's own hover state is handled by the delegate (see
    main._RailDelegate) - QStyle only ever asks an icon for Normal,
    Disabled or Selected."""
    built = QIcon(pixmap(name, size, color, lead, ink_left))
    built.addPixmap(pixmap(name, size, highlight, lead, ink_left),
                    QIcon.Mode.Selected)
    return built


def clear_cache():
    """Drop every cached pixmap - for a screen change, where the ratio
    every key was cut at is no longer the one being drawn to."""
    _cache.clear()
