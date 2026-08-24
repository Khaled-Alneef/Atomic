"""How big a card is, decided from the screen it will be drawn on.

**The owner, 24 August 2026: "the cards sorting per row must be diff
from 2K to 4K to 1080P monitors, make it auto detect and adapt."**

Half of that was already true and half was not, and the half that was
not is the card *size*. Every grid in the app already fits as many
columns as the viewport holds - `poster_grid._columns_for`,
`tracker._discover_columns`, `details.GenreBrowsePage._columns_for` -
so a wider window has always drawn more cards per row. What none of them
did is grow the card, and 160x216 logical pixels is a number chosen on a
1920x1080 panel at 100%. On a 3840-wide desktop that is **twenty cards
across**, each of them the size it was on a screen half as wide.

So the card scales with the widest attached screen's *logical* width:

Computed, not estimated - these are what the code below answers, with
the column count taken against a maximised window less the sidebar:

    logical width       scale   poster     columns
    <= 1920 (1080p)     1.000   160x216      8
    2048 (2K at 125%)   1.023   164x221      9      <- this machine
    2560 (4K at 150%)   1.117   179x241     10
    3840 (4K at 100%)   1.350   216x292     14

Deliberately sub-linear. A card twice as wide on a screen twice as wide
would keep the column count fixed, which is not what was asked and not
what any media app does - a bigger screen should show *more*, just not
at the cost of each one being unreadably small. `SCALE_MAX` stops it at
a third bigger than the baseline, which is about where a poster stops
looking like a card in a grid and starts looking like a page of its own.

**Logical width, not physical.** Windows' scaling factor is already the
user saying how big things should be: this machine's 2560x1440 panel at
125% reports 2048 logical pixels, and a card sized against 2560 would be
drawn 25% smaller than asked for on every single element. Sharpness at
125% is a separate matter and is handled where it belongs, in
`images.device_ratio`.

**Why this is a function and not a constant.** No QApplication exists
when `windows.tracker` is imported (main.py imports the pages at module
scope, before it constructs the app), so a module-level constant here
could only ever answer for a screen it had not seen. Pages are built
long after that, so a call at build time gets the real number, and the
answer is cached the first time it is a real one.
"""

# The size every one of these numbers was chosen at, and the floor: a
# 1920x1080 screen at 100% gets exactly what it got before this module
# existed. Anything else is measured against this.
BASELINE_WIDTH = 1920
BASE_POSTER = (160, 216)
BASE_HERO_COVER = (196, 264)
BASE_SCHEDULE_COVER = (96, 130)

# How much of the extra width goes into the card. 0.35 is what puts a
# 2048-wide screen at 1.06 and a 3840-wide one at 1.35 - see the table
# in the module docstring.
SCALE_SHARE = 0.35
SCALE_MAX = 1.35

_scale = None


def scale() -> float:
    """How much bigger a card is on this screen than on a 1080p one.

    1.0 with no QApplication yet, and *not cached* in that case - the
    first call that can see a screen is the one that decides."""
    global _scale
    if _scale is not None:
        return _scale
    try:
        from PyQt6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is None:
            return 1.0
        widest = max([s.availableGeometry().width() for s in app.screens()]
                     or [BASELINE_WIDTH])
    except Exception:
        return 1.0
    extra = max(0.0, widest - BASELINE_WIDTH) / float(BASELINE_WIDTH)
    _scale = min(SCALE_MAX, 1.0 + extra * SCALE_SHARE)
    return _scale


def _sized(base):
    factor = scale()
    if factor <= 1.0:
        return tuple(base)
    return (int(round(base[0] * factor)), int(round(base[1] * factor)))


def poster_size():
    """The catalogue card's cover, in logical pixels."""
    return _sized(BASE_POSTER)


def hero_cover_size():
    """The cover a hero banner carries at its left."""
    return _sized(BASE_HERO_COVER)


def schedule_cover_size():
    """The Schedule page's small landscape tile."""
    return _sized(BASE_SCHEDULE_COVER)


def adopt():
    """Write this screen's card sizes into the modules that hold them.

    **One function, called once, from main() between the QApplication
    and the first page.** The alternative was turning thirty-odd
    `POSTER_SIZE` reads into `layout.poster_size()` calls across five
    files, and the alternative to *that* was a lazily-resolving object
    pretending to be a tuple. Both are worse than a list of names in one
    place that can be read in ten seconds.

    The reason a plain constant cannot do this: main.py imports the page
    modules at module scope, which is before it constructs the
    QApplication, so at import time there is no screen to ask.

    Every derived constant is recomputed here too - a card's width, the
    text width inside it - because they were derived once at import from
    the number this is replacing. Never raises: a failure here leaves
    every page at the 1080p sizes it had before, which is the behaviour
    this whole module is an improvement on."""
    try:
        if scale() <= 1.0:
            return 1.0          # a 1080p screen gets exactly what it had
        poster = poster_size()
        hero = hero_cover_size()
        schedule = schedule_cover_size()

        from helpers import widgets
        widgets.HERO_COVER_SIZE = hero

        from windows import tracker
        tracker.POSTER_SIZE = poster
        tracker.SCHEDULE_COVER_SIZE = schedule
        tracker.HERO_COVER_SIZE = hero
        tracker.DISCOVER_CARD_TEXT_WIDTH = poster[0] + 20 - 16

        from windows import home
        home.POSTER_SIZE = poster
        home.HERO_COVER_SIZE = hero

        from windows import link_grid
        link_grid.POSTER_ART_SIZE = poster
        link_grid.POSTER_CARD_WIDTH = poster[0] + 20

        from windows import games
        games.POSTER_ART_SIZE = poster
        games.CARD_COVER_SIZE = poster
        games.POSTER_CARD_WIDTH = poster[0] + 20

        from windows import details
        details._BROWSE_POSTER_SIZE = poster
        return scale()
    except Exception:
        return 1.0
