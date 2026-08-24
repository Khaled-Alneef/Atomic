"""Compose a wide hero ground out of a portrait cover.

**Why this exists.** The owner asked (22 August 2026) for banner/header
images on the manga/manhwa/manhua surfaces - Home's hero and Discover's
FEATURED / TOP RESULT. AniList publishes a real `bannerImage` (1900x400)
and it is the right answer whenever it exists, but measured over 43 of
his own reading titles it exists for 29; 9 more carry only the portrait
`coverImage` (~460x624) and 5 match nothing at all. **MangaDex cannot
help here**: its `/manga` record carries no banner, header or backdrop
field of any kind (every attribute key checked live, and its published
OpenAPI document has zero lines mentioning one), only `cover_art`.

So for roughly a fifth of reading titles the hero has a portrait image
and a landscape hole, and the two are not interchangeable. Handing a
460x624 cover to widgets.HeroBanner, which scales
KeepAspectRatioByExpanding, produces a 1266x1717 image of which the
banner shows 300 rows: **the middle 17% of the picture, upscaled 2.75x**.
Rendered and looked at, that is a soft unrecognisable band of coat and
shoulder - not obviously broken, and saying nothing about the series.

What this module draws instead was chosen by rendering four candidates
at the real 1266x300 with the real scrim and comparing them:

  A  flat panel                       honest, but the hero reads unfinished
  B  the cover expanded (what shipped) a soft middle band of the cover
  C  the cover blurred as a ground     a pleasant wash that says nothing
  D  blur + the sharp cover as a card  good, but see below
  F  blur + the sharp cover feathered  chosen

D and F both read well; F won on both looks and robustness. **The
robustness is the part that is not a matter of taste.** The hero's box
ratio is not fixed - its width is the window minus the sidebar (220, or
~64 folded) minus 114 of margins, so at its fixed 300px height the box
runs from about 3.15:1 (1280 window, sidebar out) to 7.9:1 (2560
maximized, folded). A composed image is baked once, on a worker, with no
idea which of those it will be drawn into, and HeroBanner crops the
difference. D's poster is a *framed card*, so a crop cuts a visible
edge: at 60% of the composed height it exactly touched top and bottom at
7.9:1 with nothing to spare. F's panel is full-bleed and feathered, so a
crop is simply a different crop of a photograph and there is no edge to
cut. Rendered at 946 / 1266 / 2370 px wide, F was intact at all three.

Composed at AniList's own banner shape (1900x400) and saved as JPEG on
purpose: HeroBanner decodes this file on the UI thread through
widgets._decoded_backdrop, and matching the size and format of the real
banners it sits beside means no surface pays more for a composed ground
than for a downloaded one.

**Revised 22 August 2026** - the owner, on seeing F live: "make it shows
the whole cover image on the right side of the banner and the bg is the
same image but blurred as you are using now". The blur stays; the sharp
panel is now the *entire* cover contained at the composed height instead
of a widened centre-crop of it, with a narrower feather so the artwork
it now fades over stays readable. The constants above _PANEL_RIGHT_INSET
carry what was rendered and measured for the new shape.
"""

import hashlib
import re
from pathlib import Path

from PIL import Image

from . import images, logs

# AniList's own banner shape, so a composed ground and a real one are the
# same object as far as every caller and cache below is concerned.
GROUND_W, GROUND_H = 1900, 400

# The sharp panel: the *whole* cover, contained at the composed height at
# its own aspect - the owner's ask, 22 August 2026: "make it shows the
# whole cover image on the right side of the banner". The first design
# widened the cover 1.35x and centre-cropped it to the full height, which
# cut the top and bottom of the artwork - a volume number, a title logo,
# a raised sword arm, all rendered and seen missing.
#
# Right inset 0.18, up from 0.06, and the number is the banner's crop,
# not taste: HeroBanner scales KeepAspectRatioByExpanding and centres, so
# at its narrowest real box (946x300, a 1280 window with the sidebar out)
# only the centre 66.4% of the composed width survives - anything right
# of x=0.832 is cut. 0.18 puts the cover's right edge at 0.82, 17 screen
# pixels inside that crop; the old 0.06 lost the panel's right 11% there
# and got away with it only because cropping a full-bleed panel leaves no
# edge to see. A whole cover has four of them.
# Rendered at 946 / 1266 / 2370 and looked at: intact at the first
# two; at 2370 (7.9:1) the banner's *vertical* crop trims the cover's top
# and bottom ~20% - inherent to any full-height content in a 4.75:1 file
# at that box, and exactly what a real AniList banner gets there.
#
# **No feather at all, 23 August 2026** - the owner, seeing the faded
# left edge on screen: "you are making the left side of the image uses
# fade, do not make it use fade make it the same as the right side". The
# panel is fully opaque and both its vertical edges are hard.
#
# The ramp (0.55, then 0.25) existed to hide the seam of a *widened,
# cropped* panel, which no longer exists - and over a contained whole
# cover it was simply eating the left quarter of the artwork. Kept in the
# history here because "add a feather" is the obvious instinct when a
# pasted panel looks hard-edged, and it has now been tried and rejected
# by the person looking at it.
_PANEL_RIGHT_INSET = 0.18
# A landscape source contained at full height would smother the ground -
# cap the panel at half the composed width (no-op for every portrait
# cover; a 460x624 cover lands at 295px).
_PANEL_MAX_WIDTH = 0.5

# Bumped whenever the composition above changes shape, and written into
# the cached filename: wide_ground's disk cache is keyed on the *source*
# cover path, so without this a recomposition ships and every hero keeps
# serving the old JPEG forever - the change would silently do nothing.
# Version 1 files were "{digest}-hero.jpg" (no number).
# 4: the sharp cover panel is no longer pasted in (see wide_ground). This
# bump is what makes that change visible at all - every entry that has
# ever shown a hero carries the version-3 JPEG's path on it, and without
# a new number they would all go on serving a ground with a second cover
# baked into it.
_COMPOSE_VERSION = 4

_COMPOSED_NAME_RE = re.compile(r"-hero(\d*)\.jpg$")

# The blur is a shrink and a re-grow, not a Gaussian: a real large-radius
# blur over 1900x400 costs tens of milliseconds and this is
# indistinguishable at the radius wanted - the image is a colour wash by
# the time it is drawn under a 242-alpha scrim.
_BLUR_WIDTH = 40


def _cover_to(source: Image.Image, width: int, height: int) -> Image.Image:
    """`source` scaled to *cover* a width x height box and centre-cropped
    to it - Qt's KeepAspectRatioByExpanding, in Pillow."""
    scale = max(width / source.width, height / source.height)
    grown = source.resize((max(1, round(source.width * scale)),
                           max(1, round(source.height * scale))),
                          Image.Resampling.LANCZOS)
    left = (grown.width - width) // 2
    top = (grown.height - height) // 2
    return grown.crop((left, top, left + width, top + height))


def _ground_from(cover: Image.Image) -> Image.Image:
    small = cover.resize(
        (_BLUR_WIDTH, max(1, round(_BLUR_WIDTH * cover.height / cover.width))),
        Image.Resampling.LANCZOS)
    return _cover_to(small.resize((cover.width, cover.height),
                                  Image.Resampling.BICUBIC), GROUND_W, GROUND_H)


def _panel_from(cover: Image.Image) -> Image.Image:
    # The whole cover, *contained* - scaled to fit inside the composed
    # height at its own aspect, never cropped. A portrait cover fills the
    # height exactly; anything squarer is capped by _PANEL_MAX_WIDTH and
    # letterboxed against the blur by wide_ground's vertical centring.
    scale = min(GROUND_H / cover.height,
                GROUND_W * _PANEL_MAX_WIDTH / cover.width)
    panel = cover.resize((max(1, round(cover.width * scale)),
                          max(1, round(cover.height * scale))),
                         Image.Resampling.LANCZOS).convert("RGBA")
    # **No fade on the left edge - the owner's ask, 23 August 2026:** "you
    # are making the left side of the image uses fade, do not make it use
    # fade make it the same as the right side". The panel used to carry a
    # horizontal alpha ramp so its left edge dissolved into the blur; both
    # vertical edges are now hard, so the cover reads as one whole picture
    # sitting on the ground rather than as something half-melted into it.
    #
    # The feather was there to hide the seam of a *cropped, widened* panel
    # (see the pre-23-August design). It buys nothing now that the whole
    # cover is contained at its own aspect: what it actually did was eat
    # the left quarter of real artwork - measured on the owner's covers,
    # a fade of 0.25 of the panel width.
    return panel


def _composed_path(cover_path: Path) -> Path:
    digest = hashlib.sha1(str(cover_path).encode("utf-8")).hexdigest()
    return images.CACHE_DIR / f"{digest}-hero{_COMPOSE_VERSION}.jpg"


def stale_ground(path) -> bool:
    """Whether `path` names a ground composed by an *older* version of
    this module - true for "...-hero.jpg" (version 1) now that the
    composition is version 2.

    For the caller that remembers a resolved backdrop on the entry
    (home's hero): a remembered path that is a real banner or a TMDB
    backdrop is never stale, but a composed ground from before a
    composition change still exists on disk and would be served forever -
    the cache key bump alone cannot reach a path already written into an
    entry. Cheap on purpose (a filename test, no decode) so it can run
    on the UI thread at page build."""
    match = _COMPOSED_NAME_RE.search(Path(path).name if path else "")
    if not match:
        return False
    return int(match.group(1) or 1) < _COMPOSE_VERSION


def ground_ready(cover_path):
    """The composed ground for `cover_path` **if it is already on disk**,
    else None. A stat, never a compose - safe on the UI thread.

    For a caller that wants the ground now if it is free and is willing
    to fetch it on a worker otherwise (windows.details' chapter-list
    ground, which is opened straight from a card whose cover Home or the
    tracker has usually already composed)."""
    try:
        target = _composed_path(Path(cover_path))
        return target if target.exists() else None
    except Exception:
        return None


def wide_ground(cover_path):
    """A `GROUND_W x GROUND_H` hero ground composed from the portrait
    cover at `cover_path`, as a Path, or None if it cannot be made.

    Cached on disk beside the cover it was made from, so a revisit costs
    a stat rather than a decode-blur-recompose. **Call this on a worker
    thread** - it is Pillow only, no Qt, precisely so it can be.

    Fails soft to None like everything else that feeds a hero: the caller
    then keeps the flat panel, which is a surface with no picture on it
    rather than an error."""
    try:
        cover_path = Path(cover_path)
        target = _composed_path(cover_path)
        if target.exists():
            return target
        with Image.open(cover_path) as opened:
            cover = opened.convert("RGB")
        # **Blur only - the sharp cover panel is no longer pasted in.**
        # The heroes became cover-left / details-right on 23 August 2026
        # (widgets.hero_split), so the cover is now a real widget on the
        # banner. Baking a second copy of the same picture into the
        # artwork put *two* covers on every reading hero - measured on
        # the owner's own Home: Swordmaster's Youngest Son drew its cover
        # at the left in the new slot and again at 0.18 from the right,
        # inside the ground. This module's whole panel apparatus
        # (_panel_from, _PANEL_RIGHT_INSET, _PANEL_MAX_WIDTH) is what the
        # redesign made redundant; it is kept below, unused, because the
        # feather it deliberately does *not* apply is a decision the
        # owner made by eye and would otherwise be re-made wrongly.
        ground = _ground_from(cover)
        # Written beside the target and moved into place, the same reason
        # images.download does it: an interrupted write leaves a truncated
        # file that every later run finds with exists() and cannot decode,
        # and a full disk is the failure that actually happened here once.
        temporary = target.with_suffix(".jpg.part")
        ground.save(temporary, "JPEG", quality=88, optimize=True)
        temporary.replace(target)
        return target
    except Exception:
        logs.exception("could not compose a hero ground from a cover")
        return None


def reading_ground(title: str, cover_path=None, cover_url: str = "",
                   timeout: int = 8):
    """The hero ground for one reading title, as `(path, kind)`.

    `kind` is **"banner"** for a real AniList landscape image (the title
    is usually part of the artwork), **"cover"** for a ground *composed*
    from a portrait cover (no title in it), or `None` when nothing was
    found and the caller keeps HeroBanner's flat panel. The caller needs
    the distinction: the owner's ask, 22 August 2026, is that a reading
    hero drawn from a real AniList banner drops its text title - "take
    the whole banner from Anilist directly ... remove the name entirely"
    - while a composed-cover ground keeps the title written over it, "as
    is right now" (image 2). Only a real banner earns the removal.

    The chain, in one place because Home's hero and Discover's FEATURED /
    TOP RESULT are the same question asked on two pages and had already
    drifted apart once:

      1. AniList's own banner for the manga (a real 1900x400 image), or
         failing that the same franchise's anime banner - both come back
         from one POST, see anilist.manga_art. "Kingdom (WAN)" resolves
         through here to the Kingdom banner: the scanlation tag is
         stripped by anilist.manga_art's search_variants, which is the
         owner's "Kingdom (WAN) is Kingdom, the team is WAN".
      2. AniList's portrait cover, composed into a ground by wide_ground.
      3. `cover_path` - a cover already on this disk. Every tracked
         reading entry carries one, so a title AniList cannot match at
         all still gets a ground, for no request and no download.
      4. `cover_url` - a cover the caller has the address of but not the
         bytes. Discover's reading rows carry MangaDex's own cover URL on
         every row, so this costs one download and no lookup there.
      5. MangaDex's cover by title. Measured over the owner's own titles
         this rescues exactly one of the five AniList matched nothing
         for - real but small, and worth having only because it is two
         lines over a function that already existed.

    **Call this on a worker thread**, never in a slot: it makes up to two
    HTTP requests and a Pillow decode. Never raises - every step fails
    soft to the next, and running out of steps means `(None, None)`."""
    from . import anilist

    title = (title or "").strip()
    try:
        url, kind = anilist.manga_art(title, timeout) if title else (None, None)
        if url:
            found = images.download(url)
            if found:
                if kind == "banner":
                    return found, "banner"
                composed = wide_ground(found)
                if composed:
                    return composed, "cover"
    except Exception:
        logs.exception("anilist artwork lookup failed for a hero")

    try:
        if cover_path and Path(cover_path).exists():
            composed = wide_ground(cover_path)
            if composed:
                return composed, "cover"
    except OSError:
        pass        # an unreadable path is simply not a fallback

    composed = _ground_from_cover_url(cover_url)
    if composed:
        return composed, "cover"
    # Only now is MangaDex asked - every step above costs no lookup, so
    # the paid one must not be evaluated before them.
    composed = _ground_from_cover_url(
        _mangadex_cover(title, timeout) if title else "")
    return (composed, "cover") if composed else (None, None)


def _ground_from_cover_url(cover_url: str):
    if not cover_url:
        return None
    try:
        found = images.download(cover_url)
        return wide_ground(found) if found else None
    except Exception:
        logs.exception("could not build a hero ground from a fallback cover")
        return None


def _mangadex_cover(title: str, timeout: int) -> str:
    """MangaDex's cover for a title, or "" - the last resort, and the
    only thing MangaDex can contribute to a *banner*, since it publishes
    no landscape art of any kind."""
    from . import mangadex
    try:
        return mangadex.fetch_cover_url(title, timeout) or ""
    except Exception:
        return ""
