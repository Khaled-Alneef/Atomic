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


def _sparkle(cx, cy, r):
    """A hollow four-point sparkle centred on (cx, cy), reaching `r`.

    **Concave edges, no fill, four tips** - the owner's spec, 26 August
    2026, which rules out a five-point star, a filled diamond and the
    straight-edged four-point star this icon used before. The centre is
    genuinely empty: `_svg`'s shell is `fill="none"`, so what draws is
    the outline and nothing is painted over the middle to fake it.

    Each quarter is one cubic whose two controls sit at 0.05r and 0.36r
    from the centre line, which is what bows the edge inward and tapers
    the tips. Written as arithmetic rather than three hand-typed paths
    because the same shape is needed at three sizes, and hand-tuning
    them separately is how they would drift out of family.
    """
    def pt(dx, dy):
        return f"{cx + dx * r:.2f} {cy + dy * r:.2f}"
    near, far = 0.05, 0.36
    return ("M" + pt(0, -1)
            + "C" + pt(near, -far) + " " + pt(far, -near) + " " + pt(1, 0)
            + "C" + pt(far, near) + " " + pt(near, far) + " " + pt(0, 1)
            + "C" + pt(-near, far) + " " + pt(-far, near) + " " + pt(-1, 0)
            + "C" + pt(-far, -near) + " " + pt(-near, -far) + " " + pt(0, -1)
            + "Z")


# Where the star sits once a hover has settled, and how big; and how far
# under size the face is drawn.
#
# **Both are the answer to one measurement, not taste.** The owner's cat
# artwork fills its viewBox - at full size its bbox is x 2-22, y 3-23 of
# a 26px row - so a star laid over it was drawn almost entirely on ink
# that was already there and added *2 device pixels* between rest and a
# settled hover. Invisible on the rail, present in the file: the same
# trap this module has recorded twice before.
#
# Swept face scale against star placement and took the best that clips
# nowhere (added pixels at 26px, which is the size that matters):
#
#     face 0.80  star (20.4, 4.4, 2.7)   10px
#     face 0.80  star (20.6, 4.2, 3.1)   12px
#     face 0.74  star (20.4, 4.4, 2.7)   13px
#     face 0.74  star (20.6, 4.2, 3.1)   15px   <- this
#
# 15px of a 676px icon is a small sparkle, and it is about the ceiling
# while the face keeps this much of the box.
# 4.65 is the 3.1 the star used to be, half as big again (the owner's
# ask). It had to move in as well as grow: at its old centre the larger
# star reached x=25.25 of 24 and measured as clipping the edge at both
# 26px and 120px. Swept for the best placement that clips nowhere -
# 22 device pixels of change at 26px against the 15 it used to add.
ANIME_STAR = (19.0, 5.6, 4.65)
ANIME_FACE_SCALE = 0.78

# **The artwork has to be moved inside the viewBox before it is drawn.**
#
# The owner, 26 August 2026: the cat's outline "on its left side seems
# thinner than it is supposed to be". It was, and by half. His path runs
# to x=0 exactly, so with a 2-unit stroke centred on it, one unit of
# that stroke falls outside the 24-unit viewBox and the renderer cuts
# it. Measured on the head layer alone at 240px:
#
#     y=108   leftmost run 13px    rightmost run 21px
#     y=132   leftmost run 11px    rightmost run 21px
#     y=156   leftmost run 11px    rightmost run 20px
#
# - the left edge drawn at half the weight of the right one, on every
# row, at every size. Nudging the whole group in and down by a fraction
# and taking 8% off gives the stroke somewhere to go. It goes on the
# *group*, so the eyes and the lids move with the head and stay in
# register with it; three separately-placed layers would drift.
# **A heavier stroke than the shell's 2**, at the owner's ask, 26 August
# 2026 ("make its outlines and eyes thicker"), and it has further to
# travel than it looks: the group is drawn at 0.9 and the pixmap then at
# ANIME_FACE_SCALE, so the shell's 2 arrived on the rail as 1.33 grid
# units - thinner than every other icon, which get the full 2. 3.1 comes
# out at 3.1 x 0.9 x 0.78 = 2.18, so the cat is now slightly heavier
# than its neighbours rather than markedly lighter.
#
# It also thickens the eyes for free: they are `v.5` ticks with round
# caps, so the stroke width *is* their size. Measured at 240px, the eye
# layer goes from 718 to 1366 pixels of ink between 2.0 and 2.9.
#
# The cost, measured, is the star: a bigger, heavier face covers more of
# where it lands, and it adds 14 device pixels at 26px against the 22 it
# added before. Bottom-right would give 28, but that is a different
# icon, not a thicker one - ask before moving it.
ANIME_FIT = ('transform="translate(1.35 0.55) scale(0.9)"'
             ' stroke-width="3.1"') 


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
    # **A clapperboard, since 26 August 2026** - the owner's ask, with
    # a picture: the video camera and the reel beside it are both gone.
    #
    # The stick is its own layer so it can hinge. The geometry is set by
    # how far it has to swing without leaving the box: pivoting an
    # 19-unit bar about one end sweeps a long arc, and at 20 degrees the
    # free corner came out at y=-0.7, clipped off the top. The stick was
    # moved down to start at 6.8 and the swing held to 15 degrees, which
    # puts that corner at y=2.0 with its stroke inside the edge.
    #
    # The hinged end is at 20.5 and not at 21.5 for the mirror of that
    # reason, caught the same way: rotating about the right end swings
    # the corner *above* the pivot outward as well as up, by 1.1 units,
    # and at 21.5 that put ink in the last column at a 26px row (rows
    # 7-10 of column 25, measured). Both numbers are the edge of the box
    # rather than taste.
    "movies": {
        "board": _svg('<rect x="3" y="11" width="17.5" height="9.5" rx="2"/>'),
        "stick": _svg('<rect x="3" y="6.8" width="17.5" height="4.2" rx="1"/>'),
        "stripes": _svg('<path d="M8 6.8 6.2 11M12.5 6.8 10.7 11'
                        'M17 6.8 15.2 11"/>'),
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
    # **The owner's own artwork**, src/assets/icons/anime-icon.svg,
    # inlined here rather than loaded as a file: rail_anim's layers are
    # strings so each can be tinted and transformed on its own every
    # frame, which a single flat file cannot be. Path for path as he
    # drew it - only the eyes are split out, so they can blink.
    #
    # It is drawn under size (ANIME_FACE_SCALE): his path reaches x=0
    # and y=24, so at full size the stroke falls outside the box on
    # three sides, and there would be no corner for the star.
    #
    # The blink is a second pair of eyes rather than the same pair
    # squashed, because `_draw` scales both axes together and an eye
    # shrunk in both is a small eye, not a shut one.
    "anime": {
        "head": _svg(f'<g {ANIME_FIT}><path d="M12 5c.67 0 1.35.09 2 .26'
                     ' 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 11 0'
                     ' 5.5-2.5 10-10 10S0 19.5 0 14c0-4 1.82-10.42'
                     ' 3.42-11 1.39-.58 4.64.26 6.42 2.26C10.65 5.09'
                     ' 11.33 5 12 5z"/></g>'),
        "eyes": _svg(f'<g {ANIME_FIT}><path d="M8 14v.5"/>'
                     '<path d="M16 14v.5"/></g>'),
        # **Thinner than the group's stroke**, at the owner's ask: a shut
        # eye is a line, and at the 3.1 the rest of the cat is drawn with
        # it read as a bar. The width is set on the paths, which
        # overrides the `stroke-width` ANIME_FIT puts on the group - the
        # open eyes are `v.5` ticks whose size *is* that stroke, so they
        # have to keep it.
        "blink": _svg(f'<g {ANIME_FIT}>'
                      '<path d="M6.9 14.25h2.2" stroke-width="1.9"/>'
                      '<path d="M14.9 14.25h2.2" stroke-width="1.9"/></g>'),
        # Filled, not outlined: at three units across, an outline is two
        # strokes with nothing between them and reads as a smudge on a
        # 26px row. Tinted by paint() like every other layer, so it is
        # the icon's own colour - the owner asked for that explicitly.
        "star": _filled(f'<path d="{_sparkle(*ANIME_STAR)}"/>'),
    },
    # A tray, and an arrow that falls into it.
    "downloads": {
        "tray": _svg('<path d="M4 15.5v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>'),
        "arrow": _svg('<path d="M12 3.5v11"/>'
                      '<path d="m7.6 10.3 4.4 4.4 4.4-4.4"/>'),
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
    # **All four tiles pop, one after another, and settle back** - the
    # owner's ask, 26 August 2026, replacing the single tile that used
    # to come forward while the other three dimmed. Four layers rather
    # than one, because each has to scale about *its own* centre: a
    # shared layer scaled about the icon's middle slides the corners
    # toward it instead of growing them where they are.
    "apps": {
        "tl": _svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/>'),
        "tr": _svg('<rect x="14" y="3" width="7" height="7" rx="1.5"/>'),
        "bl": _svg('<rect x="3" y="14" width="7" height="7" rx="1.5"/>'),
        "br": _svg('<rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
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
    """The clapper opens to the left: the board holds still and the
    stick swings up off it, hinged at its right-hand end.

    Positive degrees is clockwise in Qt's y-down frame, and about a
    pivot at the *right* end that lifts the *left* one - which is the
    way round the owner asked for. Verified by drawing it rather than by
    reasoning about the sign, since the two are easy to swap.

    The stripes travel with the stick, on the same pivot: they are
    painted on it, and a stick that swung while its markings stayed put
    would read as two objects."""
    _draw(p, layer("board"), box)
    angle = 15.0 * t + 2.0 * (t * (1.0 - t) * 4.0)
    _draw(p, layer("stick"), box, degrees=angle, pivot=(20.5, 11.0))
    _draw(p, layer("stripes"), box, degrees=angle, pivot=(20.5, 11.0))


def _shows(p, box, t, layer):
    """The set switches on and the Atomic mark comes up on the screen."""
    _draw(p, layer("set"), box, scale=1.0 + 0.03 * t)
    _draw(p, layer("mark"), box, opacity=t, scale=0.55 + 0.45 * t)


def _anime(p, box, t, layer):
    """The face blinks once and a star arrives over its right ear.

    The blink is a triangular pulse rather than a sine: an eye shuts and
    opens quickly and spends no time half closed, which a sine does for
    most of its travel. Centred just before halfway so it reads as a
    reaction to the star appearing rather than as a tic.

    Both ends of the pulse are exactly zero, so at rest and at a settled
    hover the eyes are open - what differs between those two is the
    star, which stays."""
    face = ANIME_FACE_SCALE
    _draw(p, layer("head"), box, scale=face)
    # **A switch, not a cross-fade.** Fading one into the other drew
    # both at half opacity through the middle of the blink, and measured
    # as twice the ink in the eye band - a dot with a lid ghosted over
    # it, which is not what a blink looks like. Swapping outright at the
    # halfway point gives a shut eye for about 34ms of the 170ms sweep,
    # which is roughly what a real one takes.
    # **They close and stay closed** - the owner's ask, 26 August 2026,
    # replacing the blink that was here. A blink is a pulse and has to
    # be timed; this is a state, so it only needs a point to change at.
    # Reversing is free: `t` runs back down on the way out and they open
    # again at the same point.
    #
    # A switch rather than a fade, for the reason the blink needed one
    # too - drawing the dot and the lid together at half opacity ghosts
    # rather than closes.
    _draw(p, layer("blink" if t >= 0.30 else "eyes"), box, scale=face)
    step = _stagger(t, 0.12, 0.55)
    if step <= 0.0:
        return
    # Out of the ear it sits over, so it reads as thrown rather than
    # faded in on the spot.
    _draw(p, layer("star"), box,
          dx=-1.4 * (1.0 - step), dy=1.4 * (1.0 - step),
          scale=0.35 + 0.65 * step, opacity=step,
          pivot=ANIME_STAR[:2])


def _downloads(p, box, t, layer):
    """Hover, the arrow goes, a beat with none, then one falls from
    above into the place the first one left.

    The owner's sequence, given three times and finally in these words:
    "hover -> arrow hide -> arrow comes down from up to its original
    place -> end hover". Three phases against the 340ms ramp:

        0.00-0.18   the resting arrow fades out          ~61ms
        0.18-0.30   nothing but the tray                 ~41ms
        0.30-1.00   the new one falls into place        ~238ms

    The earlier version had the same shape inside a 170ms ramp and spent
    twenty milliseconds on the empty beat, which is not long enough to
    be seen - it read as one continuous slide, which is what he kept
    reporting.

    **It lands exactly home, and rest and settled are identical.** That
    is deliberate here and against this file's usual rule: he asked for
    "its original place", and the animation is the thing being asked
    for, not a difference to be left behind at the end of it."""
    _draw(p, layer("tray"), box)
    _draw(p, layer("arrow"), box, opacity=max(0.0, 1.0 - t / 0.18))
    fall = _stagger(t, 0.30, 0.70)
    if fall > 0.0:
        _draw(p, layer("arrow"), box, dy=-9.0 * (1.0 - fall), opacity=fall)


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
    """The four tiles pop up in turn and come back down.

    Reading order, a fifth of the sweep apart, so it runs across the
    grid rather than all at once. Each pop is a half-sine - out and
    fully back - with a small residue kept, because a sequence that
    returns everything exactly where it started measures as zero moved
    pixels between rest and a held hover, which is the fault this file
    has been caught by twice before."""
    # **Smooth and slow** (the owner's ask): each tile takes nearly
    # three quarters of the sweep rather than half, the starts are
    # closer together so the four read as one wave instead of four
    # separate hops, and the travel is gentler. At the 340ms ramp one
    # tile's pop is about 245ms.
    for name, start, pivot in (("tl", 0.00, (6.5, 6.5)),
                               ("tr", 0.08, (17.5, 6.5)),
                               ("bl", 0.16, (6.5, 17.5)),
                               ("br", 0.24, (17.5, 17.5))):
        step = _stagger(t, start, 0.72)
        pop = math.sin(math.pi * step)
        _draw(p, layer(name), box,
              dy=-1.4 * pop - 0.3 * t,
              scale=1.0 + 0.16 * pop + 0.05 * t,
              pivot=pivot)

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
    # **A spin, not a nudge** - 120 degrees at the owner's ask, 26
    # August 2026, up from 28. Positive is clockwise in Qt's y-down
    # frame, which is the "to the right" he asked for.
    # **300 degrees**, up from 120 - the owner asked again for a spin,
    # so 120 was evidently reading as a turn rather than as one. Not
    # 360: a gear is symmetric about its teeth, so a whole turn lands on
    # a pose identical to the one it started from and a held hover would
    # look like nothing had happened. 300 lands well off any tooth.
    angle = 300.0 * t + 8.0 * (t * (1.0 - t) * 4.0)
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
    "downloads": _downloads,
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

    # **A square the size of the pixmap, not the rect the view hands
    # over.** `_draw` blits each layer at its rendered size from
    # box.topLeft(), but works out its unit - and so every pivot and
    # every offset - from box.width(). The view's decoration rect is the
    # row's whole content area, so those two disagree, and the wider the
    # row the further every transformed layer is thrown.
    #
    # Measured 26 August 2026 on the owner's "the cat icon moves weirdly
    # while folding": through one fold the rect went 178x29 to 30x29
    # with the icon size fixed at 29, so the unit swung from 7.4 down to
    # 1.25 while the pixmap never changed size. A layer scaled about
    # (12,12) was being scaled about a point that slid ~150px left
    # across the fold.
    #
    # It shows on the cat and not on its neighbours because the cat is
    # the only icon carrying a scale at rest (ANIME_FACE_SCALE); the
    # others scale only on hover, so at hover=0 their transform is
    # identity and the wrong unit multiplies nothing.
    side = float(min(rect.width(), rect.height()))
    if side <= 0.0:
        return False
    box = QRectF(float(rect.left()), float(rect.top()), side, side)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    try:
        profile(painter, box, max(0.0, min(1.0, float(hover))), layer)
    finally:
        painter.restore()
    return True
