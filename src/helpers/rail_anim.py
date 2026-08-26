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
        "needle": _svg('<path d="m15.5 8.5-2.3 4.7-4.7 2.3 2.3-4.7'
                       ' 4.7-2.3Z"/>'),
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
    # One star, and three sparkles that arrive after it.
    "anime": {
        "star": _svg('<path d="m12 3 1.5 4.1L18 8.5l-4.5 1.4L12 14l-1.5-4.1'
                     'L6 8.5l4.5-1.4L12 3Z"/>'),
        "spark1": _svg('<path d="m18.5 14 .9 2.6L22 17.5l-2.6.9-.9 2.6-.9-2.6'
                       '-2.6-.9 2.6-.9.9-2.6Z"/>'),
        "spark2": _svg('<path d="m5.5 14 .7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8'
                       '-1.8-.7 1.8-.7.7-1.8Z"/>'),
        "spark3": _svg('<path d="m12 17.6.55 1.45 1.45.55-1.45.55L12 21.6'
                       'l-.55-1.45L10 19.6l1.45-.55L12 17.6Z"/>'),
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
    # Three tiles that hold, and one that pops.
    "apps": {
        "rest": _svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
                     '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
                     '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
        "pop": _svg('<rect x="14" y="3" width="7" height="7" rx="1.5"/>'),
    },
    # Pad, and the two buttons that are pressed in turn.
    "games": {
        "pad": _svg('<rect x="2" y="6" width="20" height="12" rx="5"/>'
                    '<path d="M7 10v4M5 12h4"/>'),
        "a": _svg('<path d="M15.5 11h.01"/>'),
        "b": _svg('<path d="M18 13.5h.01"/>'),
    },
    # An open book: the right page is what turns.
    "manga": {
        "left": _svg('<path d="M12 7C10.3 5.4 7.7 4.7 4 5v13c3.7-.3 6.3.4'
                     ' 8 2"/>'),
        "right": _svg('<path d="M12 7c1.7-1.6 4.3-2.3 8-2v13c-3.7-.3-6.3.4'
                      '-8 2"/>'),
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
    # Globe: the meridian turns inside a fixed outline.
    "websites": {
        "globe": _svg('<circle cx="12" cy="12" r="9"/>'
                      '<path d="M3.5 9h17M3.5 15h17"/>'),
        "meridian": _svg('<path d="M12 3a13.5 13.5 0 0 1 3.6 9 13.5 13.5 0 0'
                         ' 1-3.6 9 13.5 13.5 0 0 1-3.6-9A13.5 13.5 0 0 1 12'
                         ' 3Z"/>'),
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
    """Sparkles sprinkle outward, one after another."""
    _draw(p, layer("star"), box, scale=_overshoot(t, 0.06))
    for name, start, out in (("spark1", 0.00, (1.6, -1.2)),
                             ("spark2", 0.18, (-1.6, -0.8)),
                             ("spark3", 0.34, (0.0, 1.8))):
        s = _stagger(t, start)
        if s <= 0.0:
            continue
        _draw(p, layer(name), box, dx=out[0] * s, dy=out[1] * s,
              scale=0.5 + 0.5 * s, opacity=s)


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
    """One tile pops forward out of the grid."""
    _draw(p, layer("rest"), box)
    _draw(p, layer("pop"), box, dy=-1.6 * t, scale=_overshoot(t, 0.10))


def _games(p, box, t, layer):
    """Two buttons pressed in turn, on a pad that barely moves."""
    _draw(p, layer("pad"), box, degrees=2.0 * t)
    a = _stagger(t, 0.0, 0.4)
    b = _stagger(t, 0.3, 0.4)
    _draw(p, layer("a"), box, scale=1.0 - 0.35 * (a * (1.0 - a) * 4.0))
    _draw(p, layer("b"), box, scale=1.0 - 0.35 * (b * (1.0 - b) * 4.0))


def _manga(p, box, t, layer):
    """A page turns: the right leaf closes toward the spine."""
    _draw(p, layer("left"), box)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    unit = box.width() / 24.0
    spine = box.left() + 12.0 * unit
    p.translate(spine, 0.0)
    # Half-turned, not shut: at 0.85 the leaf collapsed to a sliver and
    # the icon read as a closed flag rather than a page in motion.
    p.scale(max(0.08, 1.0 - 0.5 * t), 1.0)
    p.translate(-spine, 0.0)
    pm = layer("right")
    if pm is not None and not pm.isNull():
        p.drawPixmap(box.topLeft(), pm)
    p.restore()


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
    """The meridian turns inside an outline that stays put - the globe
    spinning, not the icon rotating."""
    _draw(p, layer("globe"), box)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    unit = box.width() / 24.0
    axis = box.left() + 12.0 * unit
    # Narrowing the meridian and sliding it off the axis is what a
    # sphere turning actually looks like; rotating it would read as the
    # whole globe tipping over.
    #
    # **It has to *end* somewhere.** The first version drove this from a
    # half-sine, so the meridian narrowed mid-transition and came back -
    # rest and settled hover were pixel-identical, which measured as 0
    # moved pixels and would have read as an icon that does nothing at
    # all once the pointer stopped. It now travels to a pose and stays
    # in it, with the squash dipping a little further on the way.
    squash = 1.0 - 0.55 * t - 0.18 * (t * (1.0 - t) * 4.0)
    p.translate(axis + 2.2 * unit * t, 0.0)
    p.scale(max(0.10, squash), 1.0)
    p.translate(-axis, 0.0)
    pm = layer("meridian")
    if pm is not None and not pm.isNull():
        p.drawPixmap(box.topLeft(), pm)
    p.restore()


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
