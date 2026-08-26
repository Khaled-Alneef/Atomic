"""What each sidebar icon *does* when the pointer arrives.

The owner's ask, 26 August 2026, after a first pass that gave every icon
the same rotate-and-scale with different numbers: *"I should be able to
cover the text labels and identify which nav item I am hovering purely
from the icon animation."* So the compass finds north, the sparkles
sprinkle, the Atomic mark switches on inside the monitor, the gear
turns, the books rearrange, the page turns. One behaviour per icon,
written as its own function rather than a table of angles.

## How a moving part is made

An SVG file is one flat picture; a needle cannot rotate inside it. So an
icon that needs independent motion is declared here as **layers** - the
same artwork split across two or three inline SVGs on the same 24x24
grid, so drawing them into the same rect reassembles the icon exactly.
`LAYERS` holds those sources; `images.tinted_svg` rasterises and tints
each one *once* per (layer, colour, size, ratio) and hands back a
pixmap. Nothing here parses SVG while animating - per frame this is a
save, a transform, a blit and a restore.

Layers are inline strings rather than files on purpose: they are not
artwork anyone edits, they are the seams this module needs, and putting
twenty more entries in Atomic.spec for pieces of icons that already ship
would be twenty more chances for a build to be missing one.

## The contract with the delegate

`paint()` answers True when it drew the icon itself, and False when the
name has no profile - the delegate then falls back to drawing the flat
icon with the small generic transform it had before. So an icon added
later still lights up and simply does not have a story yet.

Hover progress arrives already eased. Nothing here holds state: every
value is a function of that one number, which is what makes leaving
smooth from wherever it currently is and why nothing can get stuck in a
transform. Selected rows do not animate - `hover` is the only input.
"""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QPainter

from . import images

# Every layer is drawn on the same 24-unit grid the icon pack uses, so
# stacking them reproduces the original picture exactly.
_SHELL = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"'
          ' viewBox="0 0 24 24" fill="none" stroke="#FFFFFF"'
          ' stroke-width="2" stroke-linecap="round"'
          ' stroke-linejoin="round">%s</svg>')

# A few layers want a solid shape rather than an outline - a lit window,
# a status dot - and say so themselves.
_FILL = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"'
         ' viewBox="0 0 24 24" fill="#FFFFFF" stroke="none">%s</svg>')


def _svg(body):
    return _SHELL % body


def _filled(body):
    return _FILL % body


LAYERS = {
    # The compass, split at the one seam that matters: a ring that stays
    # put and a needle that can point somewhere.
    "discover": {
        "ring": _svg('<circle cx="12" cy="12" r="9"/>'),
        # **Wider than the pack drew it** (the owner, 26 August 2026:
        # "it seems thin"). The shape is a needle along NE-SW with its
        # two side points at +/-1.2 units across the axis; those are the
        # only two numbers that set its width, and they are now 1.8 -
        # 3.4 units wide becomes 5.1, with the 9.9-unit length and both
        # tips left exactly where they were, so it still points at the
        # same angle and _discover's -45 degree swing is unaffected.
        "needle": _svg('<path d="M15.5 8.5 13.8 13.8 8.5 15.5'
                       ' 10.2 10.2Z"/>'),
    },
    # House, and a window that lights up inside it.
    "home": {
        "house": _svg('<path d="M3 11.5 12 4l9 7.5"/>'
                      '<path d="M5 10.5V20h14v-9.5"/>'
                      '<path d="M9.5 20v-6h5v6"/>'),
        "light": _filled('<rect x="10.6" y="11.4" width="2.8" height="2.4"'
                         ' rx="0.5"/>'),
    },
    # Lens and handle, so the glass can sweep while the grip follows.
    "search": {
        "lens": _svg('<circle cx="11" cy="11" r="7"/>'),
        "handle": _svg('<path d="m20 20-4-4"/>'),
    },
    # Camera body and its head, hinged where they meet.
    "movies": {
        "body": _svg('<rect x="2" y="7" width="14" height="11" rx="2"/>'),
        "head": _svg('<path d="M16 10.5 22 6.5v11l-6-4Z"/>'),
    },
    # Monitor, and the Atomic mark that appears on the screen. A plain
    # stroked "A" with the app's spark - the full logo is unreadable at
    # 20px and would be colour on a monochrome rail.
    "shows": {
        "set": _svg('<rect x="3" y="5" width="18" height="14" rx="2"/>'
                    '<path d="m8 2 4 3 4-3"/><path d="M8 22h8"/>'),
        "mark": _svg('<path d="M10 15.2 12 9.4l2 5.8"/>'
                     '<path d="M10.9 13.4h2.2"/>'),
    },
    # **A star at the centre of the box, and four sparkles on the
    # compass points.** The pack's star sat high - its ink spanned y
    # 3-14 of a 24 grid, so the icon read as top-heavy beside every
    # other row - and its two sparkles were diagonal and unmatched. The
    # main star is now a four-point star centred on (12, 12), and the
    # sparkles are drawn *at* north, east, south and west; the paint
    # function starts them nearer the middle and lets them travel out,
    # so the layer coordinates are the resting truth rather than an
    # offset applied later.
    "anime": {
        "star": _svg('<path d="M12 7.4 13.2 10.8 16.6 12 13.2 13.2 12 16.6'
                     ' 10.8 13.2 7.4 12 10.8 10.8Z"/>'),
        "up": _svg('<path d="M12 2.5 12.5 3.9 13.9 4.4 12.5 4.9 12 6.3'
                   ' 11.5 4.9 10.1 4.4 11.5 3.9Z"/>'),
        "right": _svg('<path d="M19.6 10.1 20.1 11.5 21.5 12 20.1 12.5'
                      ' 19.6 13.9 19.1 12.5 17.7 12 19.1 11.5Z"/>'),
        "down": _svg('<path d="M12 17.7 12.5 19.1 13.9 19.6 12.5 20.1 12 21.5'
                     ' 11.5 20.1 10.1 19.6 11.5 19.1Z"/>'),
        "left": _svg('<path d="M4.4 10.1 4.9 11.5 6.3 12 4.9 12.5 4.4 13.9'
                     ' 3.9 12.5 2.5 12 3.9 11.5Z"/>'),
    },
    # Set, and the signal that comes on inside it.
    "live-tv": {
        "set": _svg('<rect x="3" y="5" width="18" height="15" rx="2"/>'
                    '<path d="m8 2 4 3 4-3"/>'),
        "play": _svg('<path d="m10 9 5 3-5 3V9Z"/>'),
    },
    # Body, the tab that lifts, and the date that marks itself.
    "calendar": {
        "body": _svg('<rect x="3" y="5" width="18" height="16" rx="2"/>'
                     '<path d="M3 10h18"/>'
                     '<path d="M8 14h.01M12 14h.01M16 14h.01'
                     'M8 17h.01M12 17h.01"/>'),
        "tabs": _svg('<path d="M16 3v4M8 3v4"/>'),
        "mark": _filled('<circle cx="12" cy="17" r="1.4"/>'),
    },
    "schedule": {
        "body": _svg('<rect x="3" y="5" width="18" height="16" rx="2"/>'
                     '<path d="M3 10h18"/>'),
        "tabs": _svg('<path d="M16 3v4M8 3v4"/>'),
        "hand": _svg('<path d="M12 13.5V16l1.8 1.1"/>'),
    },
    # Three books that can move independently.
    "library": {
        "one": _svg('<path d="M4 4h5v16H4z"/>'),
        "two": _svg('<path d="M10.5 4h4v16h-4z"/>'),
        "three": _svg('<path d="m16 5 3-1 3 15-3 1-3-15Z"/>'),
    },
    # Frame, and the piece that snaps into it.
    "addons": {
        "frame": _svg('<path d="M8.5 3H5a2 2 0 0 0-2 2v3.5"/>'
                      '<path d="M15.5 3H19a2 2 0 0 1 2 2v3.5"/>'
                      '<path d="M21 15.5V19a2 2 0 0 1-2 2h-3.5"/>'
                      '<path d="M8.5 21H5a2 2 0 0 1-2-2v-3.5"/>'),
        "piece": _svg('<path d="M9 9h6v6H9z"/>'),
    },
    # One tile is the app being opened; the other three step back.
    "apps": {
        "rest": _svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
                     '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
                     '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
        "focus": _svg('<rect x="14" y="3" width="7" height="7" rx="1.5"/>'),
    },
    # **The whole pad turns, since 26 August 2026** - the owner's ask:
    # the right-hand side rises to the top on hover. The stick and the
    # buttons keep their own small motion inside that turn, which is
    # what stops the rotation reading as a spinning sticker.
    "games": {
        "shell": _svg('<rect x="2" y="6" width="20" height="12" rx="5"/>'),
        "stick": _svg('<path d="M7 10v4M5 12h4"/>'),
        "a": _svg('<path d="M15.5 11h.01"/>'),
        "b": _svg('<path d="M18 13.5h.01"/>'),
    },
    # **Comic panels, not a page turn.** The book itself barely moves;
    # what happens is inside it - the page rules itself into panels and
    # they light one after another, top-left to bottom-right.
    "manga": {
        "book": _svg('<path d="M12 7C10.3 5.4 7.7 4.7 4 5v13c3.7-.3 6.3.4'
                     ' 8 2"/>'
                     '<path d="M12 7c1.7-1.6 4.3-2.3 8-2v13c-3.7-.3-6.3.4'
                     '-8 2"/>'),
        # Kept well inside the page: its top edge is a *curve* from
        # (12,7) up to (20,5), so a rule drawn to the page's nominal
        # width crosses the outline and reads as a scribble over the
        # book rather than as panels on it.
        # **The panels span the whole book, and the spine is the
        # gutter.** Confining them to one page was tried twice and both
        # times read as a smudge rather than as panels - a single page
        # is about seven grid units wide, which at 26px is under eight
        # device pixels to hold a two-by-two grid in. Using the book's
        # own centre fold as the vertical divider gives each panel four
        # times the area, and the icon already draws that line.
        "rule": _svg('<path d="M5.6 12.6h12.8" stroke-width="1.4"/>'),
        # **All four quadrants, since 26 August 2026** - the owner's
        # report: two lit corners and two empty ones read as a book with
        # something missing rather than as a page of panels. The two new
        # ones are exact mirrors of the two that were already proven to
        # sit inside the outline (top-right mirrors top-left across the
        # fold, bottom-left mirrors bottom-right), so neither can cross
        # the curved page edge that the note above exists to warn about.
        "panel_tl": _filled('<rect x="5.8" y="7.4" width="5.3" height="4.2"'
                            ' rx="0.6"/>'),
        "panel_tr": _filled('<rect x="13" y="7.4" width="5.3" height="4.2"'
                            ' rx="0.6"/>'),
        "panel_bl": _filled('<rect x="5.8" y="13.6" width="5.3" height="4.0"'
                            ' rx="0.6"/>'),
        "panel_br": _filled('<rect x="13" y="13.6" width="5.3" height="4.0"'
                            ' rx="0.6"/>'),
    },
    # A strip you scroll: the frame holds, the content moves up.
    "manhwa": {
        "frame": _svg('<rect x="6" y="2" width="12" height="20" rx="2"/>'),
        "content": _svg('<path d="M6 9h12M6 15h12"/>'),
    },
    # Stacked issues: the top sheet slides off the pile.
    "manhua": {
        "back": _svg('<path d="M7 7V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v10a2 2'
                     ' 0 0 1-2 2h-2"/>'),
        "front": _svg('<rect x="3" y="7" width="14" height="14" rx="2"/>'),
        "ink": _svg('<path d="M7 12h6M7 16h6"/>'),
    },
    # **A page loading and becoming ready**, inside an outline that
    # never moves. The meridian is the resting state; on hover it gives
    # way to a spinner, and the spinner resolves into content.
    "websites": {
        "globe": _svg('<circle cx="12" cy="12" r="9"/>'),
        "meridian": _svg('<path d="M3.5 9h17M3.5 15h17"/>'
                         '<path d="M12 3a13.5 13.5 0 0 1 3.6 9 13.5 13.5 0 0'
                         ' 1-3.6 9 13.5 13.5 0 0 1-3.6-9A13.5 13.5 0 0 1 12'
                         ' 3Z"/>'),
        "arc": _svg('<path d="M12 7.3a4.7 4.7 0 0 1 4.7 4.7"'
                    ' stroke-width="2.6"/>'),
        "lines": _svg('<path d="M8 11h8M8 14h5" stroke-width="1.6"/>'),
        "cursor": _filled('<circle cx="16.4" cy="15.4" r="1.2"/>'),
    },
    # The gear is one piece and turns as one - the hub stays.
    "settings": {
        "teeth": _svg('<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8'
                      '-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21h-4'
                      'v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2'
                      ' 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4'
                      'h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1L7'
                      ' 4.2l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V3h4'
                      'v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8'
                      ' 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1v4'
                      'H21a1.7 1.7 0 0 0-1.6 1Z"/>'),
        "hub": _svg('<circle cx="12" cy="12" r="3"/>'),
    },
    # A bookmark that commits, and the tick that says so.
    "saved": {
        "ribbon": _svg('<path d="m19 21-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1'
                       ' 2 2v16Z"/>'),
        "tick": _svg('<path d="m9.2 10.6 2 2 3.6-3.8"/>'),
    },
    # A clock whose hand winds back.
    "history": {
        "face": _svg('<path d="M3.5 12a8.5 8.5 0 1 0 2.8-6.3L3 8.5"/>'
                     '<path d="M3 3.5V9h5.5"/>'),
        "hand": _svg('<path d="M12 7.5V12l3.5 2"/>'),
    },
}


def has_profile(name) -> bool:
    return name in LAYERS


# ---- painting helpers ---------------------------------------------------
def _draw(painter, pixmap, box, dx=0.0, dy=0.0, degrees=0.0, scale=1.0,
          opacity=1.0, pivot=None):
    """One cached layer, transformed about `pivot` (icon-grid units,
    default the icon's centre) and blitted."""
    if pixmap is None or pixmap.isNull() or opacity <= 0.003:
        return
    unit = box.width() / 24.0
    px, py = (12.0, 12.0) if pivot is None else pivot
    centre = QPointF(box.left() + px * unit, box.top() + py * unit)
    painter.save()
    painter.setOpacity(opacity)
    painter.translate(centre.x() + dx * unit, centre.y() + dy * unit)
    if degrees:
        painter.rotate(degrees)
    if scale != 1.0:
        painter.scale(scale, scale)
    painter.translate(-centre.x(), -centre.y())
    painter.drawPixmap(box.topLeft(), pixmap)
    painter.restore()


def _stagger(t, start, span=0.45):
    """`t` re-based so a layer starts at `start` and finishes by
    `start + span` - what makes three sparkles arrive one after another
    off a single progress value instead of together."""
    if span <= 0.0:
        return 1.0 if t >= start else 0.0
    out = (t - start) / span
    return 0.0 if out < 0.0 else (1.0 if out > 1.0 else out)


def _overshoot(t, peak=0.12):
    """1.0 at rest and at the end, with a small bulge in between - the
    'settles into place' shape, without a spring."""
    return 1.0 + peak * (t * (1.0 - t) * 4.0)


# ---- one function per icon ---------------------------------------------
def _discover(p, box, t, layer):
    """The needle finds north. The ring never moves.

    The pack's needle points north-east, so north is -45 degrees from
    where it rests; it overshoots a couple of degrees and settles."""
    _draw(p, layer("ring"), box)
    swing = -45.0 * t - 3.0 * (t * (1.0 - t) * 4.0)
    _draw(p, layer("needle"), box, degrees=swing)


def _home(p, box, t, layer):
    """The house wakes: it lifts, and a window lights inside it."""
    _draw(p, layer("house"), box, dy=-1.8 * t)
    _draw(p, layer("light"), box, dy=-1.8 * t, opacity=t)


def _search(p, box, t, layer):
    """The glass sweeps - out and back, not a single rotation - and the
    handle follows it."""
    sweep = 1.6 * (t * (1.0 - t) * 4.0)
    _draw(p, layer("lens"), box, dx=sweep, dy=-0.6 * t, scale=1.0 + 0.04 * t)
    _draw(p, layer("handle"), box, dx=sweep * 0.4)


def _movies(p, box, t, layer):
    """The camera pans: the head swings on the hinge where it meets the
    body, and the body holds still."""
    _draw(p, layer("body"), box)
    _draw(p, layer("head"), box, degrees=-9.0 * t, pivot=(16.0, 12.0))


def _shows(p, box, t, layer):
    """The set switches on and the Atomic mark comes up on the screen."""
    _draw(p, layer("set"), box, scale=1.0 + 0.03 * t)
    _draw(p, layer("mark"), box, opacity=t, scale=0.55 + 0.45 * t)


def _anime(p, box, t, layer):
    """A centred star that throws sparkles to the four compass points.

    The star holds the middle and only breathes - it must not travel,
    because it *is* the icon. The sparkles are drawn in their resting
    places by the layers; what this does is start them a little nearer
    the middle, so they read as having come out of the star, and let
    them arrive one after another rather than all at once."""
    # **It shrinks on hover rather than swelling.** It used to breathe
    # up by 6%, and at rest the star's points already come within 1.1
    # grid units of where the sparkles settle - so growing it closed
    # that to 0.8 and the two read as touching (the owner, 26 August
    # 2026). The arithmetic, in grid units: the top sparkle's stroked
    # edge reaches y=7.3 (nominal 6.3 plus the 2-unit stroke's half),
    # and the star's stroked point reaches y=6.4 (nominal 7.4 less the
    # same half) - so at rest they already overlap by ~0.9 units, and
    # growing the star made it worse. A settled 0.70 puts the star's
    # point at 8.3 and opens a real gap of about 0.5 units. Anything
    # milder does not separate them at all: 0.80 was tried first and
    # measured as still one unbroken run of ink down the centre column.
    # small sine is kept so the shrink has some life in it, and the
    # settled size differs from rest by a lot, which is the "a held
    # hover must not look like nothing happened" rule this file records
    # elsewhere.
    _draw(p, layer("star"), box,
          scale=1.0 - 0.30 * t + 0.03 * math.sin(math.pi * t))
    for name, start, (ux, uy) in (("up", 0.00, (0.0, -1.0)),
                                  ("right", 0.12, (1.0, 0.0)),
                                  ("down", 0.24, (0.0, 1.0)),
                                  ("left", 0.36, (-1.0, 0.0))):
        step = _stagger(t, start, 0.5)
        if step <= 0.0:
            continue
        # Inward at the start so they read as coming out of the star,
        # and **0.6 units further out than their resting place once
        # settled**. The star shrinking to 0.70 separates the two at
        # 96px but not at the size the rail actually draws: measured
        # 26 August 2026 at 29px, star-alone still left one unbroken
        # run of ink down the centre column, because 0.5 grid units is
        # 0.6px there. This pushes the settled gap to ~1.1 units, which
        # is the ~1.3px that makes it visible on the real icon. Not
        # more than 0.6: the top sparkle's stroked edge is then 0.9
        # units from the viewBox, and anything further clips it.
        travel = 2.4 * (1.0 - step) - 0.6 * step
        _draw(p, layer(name), box,
              dx=-ux * travel, dy=-uy * travel,
              # Settling at 0.85 rather than full size, and that is what
              # buys the room to move out at all: at full size the top
              # sparkle's stroked edge already sits 1.5 grid units from
              # the viewBox, which at 26px is 1.6px - so travelling
              # outward from there clipped it (measured, ink in row 0).
              # 0.85 pulls the outer edge back in by more than the 0.6
              # it then travels, and pulls the inner edge in too, so the
              # gap to the star widens from both sides at once.
              scale=0.45 + 0.40 * step, opacity=step,
              # Its own centre, not the icon's: scaling a sparkle about
              # the middle of the box would drag it off its point.
              pivot=(12.0 + ux * 7.6, 12.0 + uy * 7.6))

def _live_tv(p, box, t, layer):
    """The screen comes on and the play mark pulses once."""
    _draw(p, layer("set"), box, scale=1.0 + 0.03 * t)
    _draw(p, layer("play"), box, opacity=0.35 + 0.65 * t,
          scale=_overshoot(t, 0.18))


def _calendar(p, box, t, layer):
    """The tab lifts off the page and today marks itself."""
    _draw(p, layer("body"), box)
    _draw(p, layer("tabs"), box, dy=-1.6 * t)
    _draw(p, layer("mark"), box, opacity=t)


def _schedule(p, box, t, layer):
    """The same page, with the hand sweeping instead of a date."""
    _draw(p, layer("body"), box)
    _draw(p, layer("tabs"), box, dy=-1.6 * t)
    _draw(p, layer("hand"), box, degrees=28.0 * t, pivot=(12.0, 13.5),
          opacity=0.5 + 0.5 * t)


def _library(p, box, t, layer):
    """Books rearrange: one rises, its neighbour slides, the leaning one
    tips back."""
    _draw(p, layer("one"), box, dy=-2.0 * t)
    _draw(p, layer("two"), box, dx=1.2 * t)
    _draw(p, layer("three"), box, degrees=-5.0 * t, pivot=(19.0, 19.0))


def _addons(p, box, t, layer):
    """The loose piece travels in and locks into the frame."""
    _draw(p, layer("frame"), box)
    approach = 1.0 - t
    _draw(p, layer("piece"), box, dx=2.2 * approach, dy=-2.2 * approach,
          scale=1.0 - 0.06 * approach)


def _apps(p, box, t, layer):
    """One app opens: its tile comes forward and grows while the rest
    of the grid steps back."""
    _draw(p, layer("rest"), box, opacity=1.0 - 0.45 * t)
    _draw(p, layer("focus"), box, dy=-1.6 * t,
          scale=1.0 + 0.10 * t + 0.05 * math.sin(math.pi * t),
          # The tile's own centre, so it grows in place rather than
          # sliding toward the middle of the icon.
          pivot=(17.5, 6.5))

def _games(p, box, t, layer):
    """The pad turns a quarter to the left, bringing its right-hand
    side up to the top, while the stick and buttons work inside it.

    **A quarter turn, not a half.** The owner asked for "180 to the
    left ... the right side of the icon rotate to be on top", and those
    are two different angles: a half turn puts the right side on the
    *left* and leaves the pad upside down. The stated outcome - right
    side on top - is a quarter turn, so that is what this does. Change
    the -90 below to -180 if the half turn was meant literally.

    Negative degrees because Qt's y axis points down, so a negative
    angle is the counter-clockwise (leftward) one on screen; measured
    by drawing it, not by reading the sign convention.

    The turn is applied to the painter rather than per layer: `_draw`
    rotates and scales about one shared pivot, and the stick and buttons
    need their own pivots for their own motion."""
    unit = box.width() / 24.0
    centre_x = box.left() + 12.0 * unit
    centre_y = box.top() + 12.0 * unit
    p.save()
    p.translate(centre_x, centre_y)
    p.rotate(-90.0 * t)
    p.translate(-centre_x, -centre_y)
    _draw(p, layer("shell"), box)
    # Out and back inside the first six-tenths - **plus a small residue
    # that stays.** A sequence that returns everything exactly where it
    # started measures as zero moved pixels between rest and settled
    # hover, which is a held hover that looks like nothing happened; the
    # globe had the same fault and was caught the same way. So the stick
    # keeps a fraction of its travel and the buttons stay a shade in.
    swing = math.sin(math.pi * min(1.0, t / 0.6))
    _draw(p, layer("stick"), box, dx=0.35 * t + 0.95 * swing,
          pivot=(7.0, 12.0))
    press_a = math.sin(math.pi * _stagger(t, 0.28, 0.3))
    press_b = math.sin(math.pi * _stagger(t, 0.55, 0.3))
    _draw(p, layer("a"), box, scale=1.0 - 0.12 * t - 0.35 * press_a,
          pivot=(15.5, 11.0))
    _draw(p, layer("b"), box, scale=1.0 - 0.10 * t - 0.32 * press_b,
          pivot=(18.0, 13.5))
    p.restore()

def _manga(p, box, t, layer):
    """The page rules itself into comic panels and they light in
    reading order, top-left first.

    The book stays put - only the gutter shifts, and only by a fraction
    of a pixel's worth of grid, which is what makes the panels feel like
    they are settling rather than the icon wobbling."""
    _draw(p, layer("book"), box)
    # The rule slides a fraction as it arrives, which is the "panel
    # coming alive" nudge - the book itself never moves.
    _draw(p, layer("rule"), box, dy=-0.5 * (1.0 - t), opacity=t)
    # Reading order, left to right then down - four staggers across the
    # sweep rather than two, so the last one still lands by t=1.0.
    _draw(p, layer("panel_tl"), box, opacity=0.75 * _stagger(t, 0.04, 0.30))
    _draw(p, layer("panel_tr"), box, opacity=0.75 * _stagger(t, 0.22, 0.30))
    _draw(p, layer("panel_bl"), box, opacity=0.75 * _stagger(t, 0.40, 0.30))
    _draw(p, layer("panel_br"), box, opacity=0.75 * _stagger(t, 0.58, 0.34))

def _manhwa(p, box, t, layer):
    """A strip scrolls: the frame holds, the content travels up inside
    it and is clipped by the frame's own opening."""
    _draw(p, layer("frame"), box)
    unit = box.width() / 24.0
    p.save()
    p.setClipRect(QRectF(box.left() + 6.0 * unit, box.top() + 2.5 * unit,
                         12.0 * unit, 19.0 * unit))
    _draw(p, layer("content"), box, dy=-3.4 * t)
    p.restore()


def _manhua(p, box, t, layer):
    """The top issue slides off the pile and its ink follows."""
    _draw(p, layer("back"), box)
    _draw(p, layer("front"), box, dx=1.4 * t, dy=-1.0 * t)
    _draw(p, layer("ink"), box, dx=1.4 * t, dy=-1.0 * t, opacity=0.55 + 0.45 * t)


def _websites(p, box, t, layer):
    """A page loads and becomes ready, inside an outline that holds
    still.

    The meridian is the resting picture and gives way immediately; a
    spinner takes its place, sweeps once, and hands over to two lines of
    content and a cursor arriving on the page. Nothing spins the icon
    itself - that was the old behaviour and it read as a wheel."""
    _draw(p, layer("globe"), box)
    # Out of the way quickly - the middle frames read as mud while the
    # meridian is still half there under the spinner.
    _draw(p, layer("meridian"), box, opacity=max(0.0, 1.0 - 3.4 * t))
    load = _stagger(t, 0.0, 0.55)
    if 0.0 < load < 1.0:
        _draw(p, layer("arc"), box, degrees=320.0 * load,
              opacity=min(1.0, load * 4.0) * min(1.0, (1.0 - load) * 3.0))
    ready = _stagger(t, 0.5, 0.5)
    if ready > 0.0:
        _draw(p, layer("lines"), box, opacity=ready)
        _draw(p, layer("cursor"), box,
              dx=-1.6 * (1.0 - ready), dy=-1.2 * (1.0 - ready), opacity=ready)

def _settings(p, box, t, layer):
    """The gear turns and settles. The hub turns with it - a gear whose
    centre stayed still would read as broken."""
    angle = 28.0 * t + 3.0 * (t * (1.0 - t) * 4.0)
    _draw(p, layer("teeth"), box, degrees=angle)
    _draw(p, layer("hub"), box, degrees=angle)


def _saved(p, box, t, layer):
    """The bookmark seats itself and confirms."""
    _draw(p, layer("ribbon"), box, dy=1.2 * t)
    _draw(p, layer("tick"), box, dy=1.2 * t, opacity=_stagger(t, 0.35, 0.5))


def _history(p, box, t, layer):
    """The hand winds backwards; the face holds still."""
    _draw(p, layer("face"), box)
    _draw(p, layer("hand"), box, degrees=-26.0 * t, pivot=(12.0, 12.0))


PROFILES = {
    "discover": _discover,
    "home": _home,
    "search": _search,
    "movies": _movies,
    "shows": _shows,
    "anime": _anime,
    "live-tv": _live_tv,
    "calendar": _calendar,
    "schedule": _schedule,
    "library": _library,
    "addons": _addons,
    "apps": _apps,
    "games": _games,
    "manga": _manga,
    "manhwa": _manhwa,
    "manhua": _manhua,
    "websites": _websites,
    "settings": _settings,
    "saved": _saved,
    "history": _history,
}


def paint(painter, name, rect, hover, colour, height, dpr) -> bool:
    """Draw `name` into `rect` at eased `hover`. True when it drew.

    False for an icon with no profile, which is the delegate's cue to
    fall back to the flat pixmap it drew before - a new icon still
    lights up, it just has no story yet."""
    profile = PROFILES.get(name)
    layers = LAYERS.get(name)
    if profile is None or layers is None:
        return False

    def layer(key):
        source = layers.get(key)
        if source is None:
            return None
        return images.tinted_svg(f"{name}:{key}", source, colour, height, dpr)

    box = QRectF(rect)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    try:
        profile(painter, box, max(0.0, min(1.0, float(hover))), layer)
    finally:
        painter.restore()
    return True
