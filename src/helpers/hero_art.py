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
"""

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from . import images, logs

# AniList's own banner shape, so a composed ground and a real one are the
# same object as far as every caller and cache below is concerned.
GROUND_W, GROUND_H = 1900, 400

# The sharp panel: full composed height, a little wider than the cover's
# own aspect so it reads as a crop rather than as a pasted rectangle, its
# right edge inset from the banner's, and its left edge faded into the
# blur across most of its width.
_PANEL_WIDEN = 1.35
_PANEL_RIGHT_INSET = 0.06
_PANEL_FEATHER = 0.55

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
    width = max(1, round(GROUND_H * cover.width / cover.height * _PANEL_WIDEN))
    panel = _cover_to(cover, width, GROUND_H).convert("RGBA")
    # A horizontal alpha ramp, transparent at the panel's left edge and
    # opaque from _PANEL_FEATHER across. Drawn as a 1px-tall gradient and
    # stretched, which is one line of putpixel work rather than one per
    # pixel of a 400-row mask.
    ramp = Image.new("L", (width, 1))
    draw = ImageDraw.Draw(ramp)
    edge = max(1, int(width * _PANEL_FEATHER))
    for x in range(width):
        draw.point((x, 0), fill=255 if x >= edge else int(255 * x / edge))
    panel.putalpha(ramp.resize((width, GROUND_H), Image.Resampling.BILINEAR))
    return panel


def _composed_path(cover_path: Path) -> Path:
    digest = hashlib.sha1(str(cover_path).encode("utf-8")).hexdigest()
    return images.CACHE_DIR / f"{digest}-hero.jpg"


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
        ground = _ground_from(cover)
        panel = _panel_from(cover)
        left = int(GROUND_W * (1.0 - _PANEL_RIGHT_INSET)) - panel.width
        ground.paste(panel, (left, 0), panel)
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
