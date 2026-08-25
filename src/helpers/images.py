"""Image loading, caching, and placeholder-tile helpers shared by the
Websites, Apps, Games, and Tracker windows.

Pillow does the actual image work (thumbnailing, the blank fallback
tile); the result is converted to a QPixmap right before it's handed to
a Qt widget.
"""

import os
import io
import math
import time
import hashlib
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap

# The rail icons are SVG now (25 August 2026), and QPixmap cannot read
# one - it answers a null pixmap, which every caller here reads as
# "missing asset". Guarded so a build that somehow ships without QtSvg
# degrades to the bullet fallback rather than failing to import at all;
# packaging/Atomic.spec names PyQt6.QtSvg as a hiddenimport because a
# guarded import is exactly what PyInstaller's static analysis skips
# (the same reason libtorrent is listed there).
try:
    from PyQt6.QtSvg import QSvgRenderer
except ImportError:                      # pragma: no cover - see above
    QSvgRenderer = None

from . import icon_extract, net, storage, theme

CACHE_DIR = storage.DATA_DIR / "image_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# **Chapter pages live in their own folder, and the reason is a
# launch-time pass that was destroying them.** `shrink_existing` runs at
# every startup and re-encodes anything in CACHE_DIR over
# SHRINK_MIN_BYTES down to the *cover* bounds - PORTRAIT_MAX_H is 1200,
# sized for poster art. The reader cached its pages in the same
# directory, and a webtoon page is one 800 x 17000 image: the next
# launch quietly re-encoded it to **56 x 1200**, which is the owner's
# "ch 550 in the Eternal Supreme still has the ch loading size issue"
# (measured on the real ReaderPage: every slot 56-71 x 1200 against
# sources of 800 x 13000-17000). Kingdom's mixed page widths were the
# same pass - a 1325x1900 scan shrunk to 838x1200 beside a 2760x1917
# spread the landscape bound left alone.
#
# `shrink_existing` and `trim_cache` scan with a non-recursive
# os.scandir, so a subdirectory is outside the shrinker by construction;
# PAGES_DIR is added to trim_cache's roots explicitly, because pages
# must still age out - deleting one only costs a re-download, where
# re-encoding one costs the artwork.
PAGES_DIR = CACHE_DIR / "pages"


def page_path_for_url(url: str):
    """Where a chapter page image for `url` is cached - like
    cache_path_for_url, but under PAGES_DIR so the cover-sized shrink
    passes never touch it."""
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    return PAGES_DIR / cache_path_for_url(url).name

# **Every tile is cut at device pixels, not logical ones.** Measured 24
# August 2026 on the owner's new panel (LG 27GS950, 2560x1440 at 125%,
# so DPR 1.25): `thumbnail_or_avatar` cut a 160x216 pixmap, left it
# tagged DPR 1, and every surface drew it into a 160x216 *logical* box -
# which Qt then blew up to 200x270 device pixels with a plain stretch.
# That is the owner's "why do the cover images seems pixels???", and it
# was invisible on the old 1920x1080 at 100% because there the two sizes
# are the same number.
#
# **The ratio follows the screen the window is actually on - not the
# sharpest screen attached.** The first version took the max across all
# screens, on the reasoning that over-cutting only costs a downscale,
# "which is sharp". It is not: the surfaces draw these pixmaps through
# painters with no SmoothPixmapTransform hint, so a 1.25-tagged tile on
# the 1.0 monitor was scaled 0.8x nearest-neighbour on every draw. That
# is the owner's very next report, same day: "the text and the images
# seems pixeled, when using 1080P monitor, but they look good when
# using 2K monitor" - good exactly where the tag matched the screen,
# crunchy exactly where it did not.
#
# So main.py sets this to the main window's own ratio at startup and
# again whenever the window changes screens (`set_device_ratio`), and
# pages - which rebuild from scratch on every visit - cut their tiles
# for the monitor they are being read on. The tile caches key on the
# ratio, so both monitors' variants coexist on disk; a page dragged
# across mid-life is soft until its next rebuild, which is the cost of
# not keeping two copies of every pixmap live.
#
# Cached, because it is read on worker threads (cover_fetch) where
# walking QGuiApplication's screen list does not belong.
_ratio = None
# Rounded up to this step so an odd scale factor does not give the tile
# cache its own private size. 0.25 rather than 0.5 because 1.25 is the
# single most common Windows setting and is this machine's: at 0.5 it
# rounded up to 1.5 and every tile was cut 44% larger than the screen
# could show, which is memory spent on a downscale nobody sees. Every
# Windows preset - 100/125/150/175/200% - lands on this step exactly.
_RATIO_STEP = 0.25
_RATIO_MAX = 2.0


def set_device_ratio(value) -> float:
    """Adopt the main window's devicePixelRatio - see the note above for
    why this follows the window rather than the sharpest screen. Called
    by main.py at startup and on every screen change; never raises."""
    global _ratio
    try:
        stepped = math.ceil(max(1.0, float(value)) / _RATIO_STEP) * _RATIO_STEP
        _ratio = min(_RATIO_MAX, stepped)
    except (TypeError, ValueError):
        _ratio = 1.0
    return _ratio


def device_ratio() -> float:
    """The devicePixelRatio every cut tile is sized for - see above.

    Before main.py has adopted the window's ratio this answers from the
    primary screen without caching, so a page built very early is still
    close and the adopted value wins the moment it exists. Never raises:
    with no QGuiApplication at all it answers 1.0."""
    if _ratio is not None:
        return _ratio
    try:
        from PyQt6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        if screen is None:
            return 1.0
        stepped = math.ceil(max(1.0, screen.devicePixelRatio())
                            / _RATIO_STEP) * _RATIO_STEP
        return min(_RATIO_MAX, stepped)
    except Exception:
        return 1.0


def tile_size(size):
    """`size` in the device pixels a tile for it must actually hold."""
    ratio = device_ratio()
    return (max(1, int(round(size[0] * ratio))),
            max(1, int(round(size[1] * ratio))))


def warm(path, size):
    """Decode and cache the tile for `path` at logical `size`, off the
    UI thread. The one place background prefetchers should call, so a
    prewarmed tile is cut at the same device size the UI will ask for -
    a mismatch means the decode is simply paid again on the UI thread."""
    ratio = device_ratio()
    return _fitted(str(path), tile_size(size), _stamp(str(path)),
                   radius=_corner_radius(size) * ratio)


def _asset_dir() -> Path:
    """Where images shipped *with the app* live (as opposed to downloaded
    covers): src/ when running from source, the unpacked bundle root when
    frozen, since Atomic.spec copies them to '.'.

    sys._MEIPASS rather than __file__: PyInstaller points __file__ inside
    the archive for a module in the PYZ, which is not a directory anything
    can be read out of."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


# Recoloured assets already cut, keyed (name, colour, height, ratio).
# Every caller asks for one of a handful of fixed combinations, and each
# miss reads a PNG off disk, scales it smoothly and composites a fill -
# measured 12ms a call, twice per tracker page build, on the path
# between pressing Watch and seeing it.
_tinted = {}


def _rendered_svg(path: Path, device_h: int) -> QPixmap:
    """`path` rasterised at exactly `device_h` device pixels tall, in
    whatever colour the file names - the caller fills it immediately
    after, so only the alpha it produces matters.

    Rendered *at* the target size rather than rendered once and scaled:
    an SVG has no native resolution to lose, so going through a raster
    intermediate would only add the resampling blur the whole
    device-pixel discipline in this file exists to avoid. The width
    follows the file's own viewBox aspect, because QSvgRenderer.render
    stretches to fill whatever viewport it is given and a square
    assumption would squash a non-square icon silently.

    A null pixmap on any failure - unreadable file, no QtSvg - which is
    the same answer QPixmap gives for a missing PNG, so callers need no
    new branch."""
    if QSvgRenderer is None:
        return QPixmap()
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return QPixmap()
    default = renderer.defaultSize()
    device_w = (max(1, round(device_h * default.width() / default.height()))
                if default.height() > 0 else device_h)
    image = QImage(device_w, device_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)


def tinted_asset(name: str, color: str, height: int, dpr: float = 1.0) -> QPixmap:
    """A bundled image recoloured to `color`, scaled for `dpr` and tagged
    with it so it isn't blurry on a non-100% display (same reason as the
    sidebar logo in main.py). Cached per (name, colour, height, ratio) -
    see _tinted.

    Recoloured rather than shipped in the right colour: every other glyph
    on these buttons is text drawn from theme's palette, so a white PNG
    stayed white while its neighbours sat dimmer. SourceIn replaces colour
    and keeps alpha, so the shape and its antialiased edges survive.

    Scaled before it is filled - filling first would leave a flat block of
    colour for the scaler to blur.

    **SVG and PNG both, through this one function.** The rail icons are
    SVG (25 August 2026) and the tracker's filter button is still a PNG;
    a second tinting helper for the new kind would have meant a second
    cache, and the cache is the whole reason this function exists - the
    rail delegate asks for a tint on every painted row."""
    key = (name, str(color), int(height), float(dpr))
    found = _tinted.get(key)
    if found is not None:
        return found
    path = _asset_dir() / name
    device_h = max(1, round(height * dpr))
    if str(name).lower().endswith(".svg"):
        scaled = _rendered_svg(path, device_h)
        if scaled.isNull():
            return scaled  # missing/unreadable asset: an empty icon, not a crash
    else:
        source = QPixmap(str(path))
        if source.isNull():
            return source  # missing asset: an empty icon, not a crash
        scaled = source.scaledToHeight(
            device_h, Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(scaled)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(scaled.rect(), QColor(color))
    painter.end()
    # Tagged with the ratio the cut actually landed on, not the ratio
    # asked for: at dpr 1.25 a 39px icon is 48.75 device pixels, which
    # rounds to 48 - tagging that 1.25 makes its logical height 38.4,
    # and Qt then rescales it a fraction on every draw, which is the
    # soft edge the owner reported as sidebar quality. height/logical
    # integral by construction means the draw is a 1:1 blit.
    scaled.setDevicePixelRatio(device_h / float(height))
    _tinted[key] = scaled
    return scaled


_logo_cache = {}


def logo_pixmap(path, height: int, dpr: float = 1.0) -> QPixmap:
    """A title-logo PNG at `path`, scaled to `height` logical pixels and
    tagged with `dpr` so it stays crisp on a non-100% display (the rule
    the sidebar logo and the hero backdrop both follow).

    Kept as-is, colour and transparency intact - unlike tinted_asset,
    which recolours a glyph: a logo *is* the artwork, drawn over the
    banner in its own colours. Returns a null pixmap for a missing or
    unreadable file, which a caller reads as "no logo, keep the text"."""
    key = (str(path), int(height), float(dpr))
    found = _logo_cache.get(key)
    if found is not None:
        return found
    source = QPixmap(str(path))
    if source.isNull():
        return source
    scaled = source.scaledToHeight(max(1, int(height * dpr)),
                                   Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    if len(_logo_cache) > 64:
        _logo_cache.clear()
    _logo_cache[key] = scaled
    return scaled


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    suffix = Path(url).suffix
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        suffix = ".img"
    return CACHE_DIR / f"{digest}{suffix}"


# ---- keeping the cache a size, not a landfill ---------------------------
#
# **The owner's report, 24 August 2026: "the size of the image_cache is
# insane! ... it was > 1.2 GB".** Measured on what had grown back in the
# hours after he deleted it: 155.8MB in 1107 files, of which only 22
# files (8.6MB) were referenced by anything saved, and 65 files over
# 400KB held 93.9MB - a 2852x4096 JPEG at 3.7MB and a 1686x2528 PNG at
# 4.8MB, stored in full to be drawn at 160x216 on a card or blurred into
# a 1900x400 ground. Re-encoded at the bounds below those same 65 files
# came to **16.4MB**, a 5.7x reduction with nothing visible lost,
# because nothing ever draws them larger than this.
#
# Two rules, both in this file because download() is the one write
# path every caller goes through:
#   1. nothing larger than it can be drawn is ever stored (_shrink);
#   2. the whole cache stays under CACHE_LIMIT_BYTES, evicting the
#      least recently *used* file first and never one a saved entry
#      points at (trim_cache) - run at launch and at close.
#
# A portrait is bounded by height: a card draws 216px, the hero ground
# is 400px tall, and the details page blurs it. A landscape backdrop is
# bounded by width: the owner called a w1280 backdrop blurry across a
# 2560px window (artwork.BACKDROP_SIZE's note), so 2560 is the floor of
# what that measurement allows, not a guess.
SHRINK_MIN_BYTES = 300_000
PORTRAIT_MAX_H = 1200
LANDSCAPE_MAX_W = 2560
CACHE_LIMIT_BYTES = 300 * 1024 * 1024
# How stale a file's mtime may be before a cache hit refreshes it. The
# trim evicts by mtime (Windows does not reliably keep atime), so a hit
# has to say "still used" - but not with a disk write on every view.
TOUCH_AFTER_S = 6 * 3600.0


def _shrink(data: bytes) -> bytes:
    """`data` re-encoded within the bounds above, or `data` itself when
    it is already small enough (or not an image at all - an icon, a
    favicon). JPEG unless the picture has transparency, which a logo or a
    cut-out cover does and a JPEG would flatten to black."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            w, h = opened.size
            portrait = h >= w
            bound = PORTRAIT_MAX_H if portrait else LANDSCAPE_MAX_W
            dim = h if portrait else w
            if dim <= bound:
                return data
            scale = bound / float(dim)
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
            has_alpha = opened.mode in ("RGBA", "LA") or (
                opened.mode == "P" and "transparency" in opened.info)
            resized = opened.convert("RGBA" if has_alpha else "RGB").resize(
                size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if has_alpha:
                resized.save(out, "PNG", optimize=True)
            else:
                resized.save(out, "JPEG", quality=86, optimize=True)
            shrunk = out.getvalue()
            return shrunk if len(shrunk) < len(data) else data
    except Exception:
        return data


def _touch(path):
    """Mark a cache file as used, for trim_cache's LRU - at most once per
    TOUCH_AFTER_S so a hit is normally free."""
    try:
        if time.time() - path.stat().st_mtime > TOUCH_AFTER_S:
            os.utime(path, None)
    except OSError:
        pass


def protected_paths() -> set:
    """Every image file a saved entry points at, lowercased - what
    trim_cache must never delete, whatever its age."""
    keep = set()
    for name in ("series.json", "tracker.json", "games.json", "apps.json",
                 "websites.json"):
        try:
            entries = storage.load(name, [])
        except Exception:
            continue
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            for key in ("cover_path", "hero_backdrop", "hero_logo", "icon_path"):
                value = entry.get(key)
                if value:
                    keep.add(os.path.normcase(str(value)))
    return keep


def shrink_existing(budget_s: float = 6.0) -> int:
    """Re-encode cache files already on disk that exceed the bounds - the
    owner's existing cache was full of 4K originals stored before
    _shrink existed. Oldest-modified first so a budgeted pass at each
    launch works through the backlog rather than re-checking the same
    recent files. Returns bytes saved. Never raises."""
    started = time.monotonic()
    saved = 0
    try:
        rows = []
        for entry in os.scandir(CACHE_DIR):
            if entry.is_file() and not entry.name.endswith(".part"):
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_size > SHRINK_MIN_BYTES:
                    rows.append((st.st_mtime, st.st_size, entry.path))
        rows.sort()
        for _, size, path in rows:
            if time.monotonic() - started > budget_s:
                break
            try:
                data = Path(path).read_bytes()
                shrunk = _shrink(data)
                if len(shrunk) >= len(data):
                    # Already within bounds: bump it so the next pass does
                    # not read it again before everything older.
                    os.utime(path, None)
                    continue
                tmp = Path(path + ".part")
                tmp.write_bytes(shrunk)
                tmp.replace(path)
                saved += size - len(shrunk)
            except Exception:
                continue
    except Exception:
        pass
    return saved


def trim_cache(limit: int = CACHE_LIMIT_BYTES, budget_s: float = 8.0) -> int:
    """Bring the image caches under `limit` bytes, oldest-used first.
    Returns the bytes freed. Never raises, never deletes a protected
    file or a download in progress, and stops at `budget_s` so a close
    is never held behind a slow disk."""
    started = time.monotonic()
    freed = 0
    try:
        # **logo_cache is deliberately NOT trimmed.** It holds one logo
        # and one backdrop per watched title - bounded by the library,
        # a few MB each - and trimming it is how Bleach TYBW's loading
        # screen lost its logo (24 August 2026): the evicted files'
        # refetch failed once and wrote permanent miss markers. The big
        # caches are the ones that grow without bound.
        roots = [CACHE_DIR, PAGES_DIR, _TILE_DIR]
        rows = []
        for root in roots:
            if not root.is_dir():
                continue
            for entry in os.scandir(root):
                if not entry.is_file() or entry.name.endswith(".part"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                rows.append((st.st_mtime, st.st_size, entry.path))
        total = sum(size for _, size, _ in rows)
        if total <= limit:
            return 0
        keep = protected_paths()
        rows.sort()
        for _, size, path in rows:
            if total <= limit or time.monotonic() - started > budget_s:
                break
            if os.path.normcase(path) in keep:
                continue
            try:
                os.unlink(path)
            except OSError:
                continue
            total -= size
            freed += size
    except Exception:
        pass
    return freed


def download(url: str, timeout: int = 8):
    """Download `url` into the local cache (or reuse a previous download)
    and return its Path, or None on any failure. Anything larger than the
    app can draw is stored shrunk - see _shrink.

    A host that has refused twice in the last ten minutes is skipped
    without a request, so the caller reaches its fallback now instead of
    after two timeouts - see net.host_refusing for the measurement."""
    path = cache_path_for_url(url)
    if path.exists():
        _touch(path)
        return path
    if net.host_refusing(url):
        return None
    try:
        # net.ascii_url, not the raw string: a cover whose filename
        # carries a non-ASCII character - an Arabic-Indic digit, a
        # superscript - makes urllib raise before it connects, so the
        # tile drew empty next to art that was there all along. Cached
        # under the URL as it was given, so this is a transport detail
        # rather than a second identity for the same image.
        req = urllib.request.Request(
            net.ascii_url(url), headers={"User-Agent": "Mozilla/5.0 PC-App/1.0"}
        )
        deadline = net.deadline_in(timeout)
        with net.urlopen(req, timeout=timeout) as resp:
            data = net.read_bytes(resp, deadline)
        # **Written beside the target and moved into place.** A direct
        # write_bytes is not atomic, so anything that interrupts it - and
        # a full disk is the one that actually happened here - leaves a
        # *truncated* file at the cache path. `download` then returns
        # that path forever (it only checks `exists()`), `load_thumbnail`
        # cannot decode it, and the card draws the blank tile on every
        # visit from then on. Measured 22 August 2026: the owner's C:
        # drive was at 0.0 GB free of 237.6 GB, and a planted 204-byte
        # JPEG reproduced the failure exactly - which is the owner's
        # "sometimes the images appear but when go back and come again
        # they disappear".
        if len(data) > SHRINK_MIN_BYTES:
            data = _shrink(data)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        net.note_host_success(url)
        return path
    except Exception:
        net.note_host_failure(url)
        # Never leave a half-written file where a later run will mistake
        # it for a finished download.
        try:
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
        except OSError:
            pass
        return None


def fetch_site_icon(url: str, timeout: int = 6):
    """Best-effort icon for a website, so Websites entries don't need a
    manually-chosen image: try the site's own favicon.ico first, then a
    public favicon-lookup service if that fails (unreachable, wrong
    content type, site doesn't serve one at the default path). Returns a
    cached Path, or None - callers fall back to thumbnail_or_avatar's
    blank tile either way."""
    parsed = urllib.parse.urlparse(url if "://" in url else f"//{url}", scheme="https")
    if not parsed.netloc:
        return None
    for candidate in (
        f"{parsed.scheme}://{parsed.netloc}/favicon.ico",
        f"https://www.google.com/s2/favicons?domain={parsed.netloc}&sz=128",
    ):
        path = download(candidate, timeout=timeout)
        if path and load_thumbnail(path) is not None:
            return path
    return None


def extract_app_icon(exe_path: str, size=(64, 64)):
    """Extract `exe_path`'s shell icon and cache it like a downloaded
    image, so Apps entries don't need a manually-chosen image. Returns a
    cached Path, or None if extraction fails (non-Windows, missing file,
    no icon)."""
    digest = hashlib.sha1(str(exe_path).encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"exeicon_{digest}.png"
    if path.exists():
        return path
    img = icon_extract.extract_icon(exe_path, max(size))
    if img is None:
        return None
    img.save(path)
    return path


def load_thumbnail(path, size=(64, 64)):
    """Open an image file and crop/scale it to fill `size`. Returns a
    PIL.Image, or None if it can't be read as an image."""
    try:
        img = Image.open(path).convert("RGBA")
        return ImageOps.fit(img, size, Image.LANCZOS)
    except Exception:
        return None


def _corner_radius(size) -> int:
    """The clip radius for a thumbnail at `size`: theme.RADIUS on
    poster-sized art, proportionally tighter on small icons - a 28px
    quick-list icon under the full 12px radius is most of the way to a
    circle, which is not "rounded corners" any more.

    **Takes the logical size.** A tile is cut at `size x device_ratio()`
    now, so passing the device size here would answer theme.RADIUS in
    device pixels - a visibly tighter corner on a 125% display than on a
    100% one. Callers scale the result instead (see `warm` and
    `thumbnail_or_avatar`), which keeps the corner the same *on screen*
    at every ratio."""
    return max(2, min(theme.RADIUS, min(size) // 4))


def _round_corners(img, radius=None):
    """Clip `img` to the app's rounded-corner tile shape, in place on a
    copy's alpha channel.

    Done here, at the one decode path every thumbnail passes through,
    rather than as a paint-time mask in each page - Harbor's tiles are
    the rounded artwork itself, and one clip in one place cannot drift
    per page. The mask is drawn 4x and LANCZOS'd down because PIL's
    rounded_rectangle is not antialiased, and at 1x the arc renders as
    a visible staircase against the near-black ground. Multiplied into
    the existing alpha rather than replacing it, so art that already
    carries transparency keeps it."""
    if radius is None:
        radius = _corner_radius(img.size)
    scale = 4
    mask = Image.new("L", (img.width * scale, img.height * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, img.width * scale - 1, img.height * scale - 1),
        radius=int(round(radius)) * scale, fill=255)
    mask = mask.resize(img.size, Image.LANCZOS)
    rounded = img.copy()
    rounded.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return rounded


def blank_tile(size=(64, 64), radius=None):
    """An empty flat tile in the thumbnails' own rounded shape - the
    fallback whenever no real image/icon/cover is available. It replaced
    the coloured first-letter avatar at the owner's ask ("completely
    empty until the image loads, whole app"): a wall of letters read as
    content of its own, where a quiet SURFACE_HOVER slab reads as space
    an image will fill."""
    color = theme.SURFACE_HOVER.lstrip("#")
    rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    img = Image.new("RGBA", size, rgb + (255,))
    return _round_corners(img, radius)


def to_pixmap(img: Image.Image) -> QPixmap:
    """Convert a PIL Image to a QPixmap. Copies the pixel data so the
    result stays valid after the source bytes go out of scope."""
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


# Every page is rebuilt from scratch on each visit, and again on every
# sort change or +/- chapter tap, so a grid of covers kept re-deriving
# pixels it had already produced - measured at ~14ms per image, which is
# what made opening a page with a lot on it feel slow. Two caches,
# because the two halves of that cost are paid in different places:
#
#   _FITTED  decode + LANCZOS resize, ~6.3ms - the expensive half, and
#            the only half that can be done off the UI thread (see
#            prewarm), since it's pure Pillow with no Qt involved.
#   _PIXMAP  the finished QPixmap, ~0.1ms to convert - has to be built
#            on the UI thread, so it's cached separately rather than
#            being something prewarm could hand over ready-made.
#
# Both are bounded so a big library browsed for a long session can't
# grow them without limit.
_FITTED = {}
_PIXMAP = {}
_CACHE_MAX = 512


def _stamp(path):
    """The file's mtime/size, so a cover that gets re-downloaded at the
    same path (see tracker's sharper-cover backfill) is re-read rather
    than served stale from an earlier render. None means missing or
    unreadable - callers fall through to the blank tile."""
    try:
        stat = Path(path).stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _evict(cache):
    if len(cache) >= _CACHE_MAX:
        # Plain FIFO rather than a real LRU: these all cost about the
        # same to rebuild, so tracking use order would buy nothing over
        # dropping whatever went in first.
        for old_key in list(cache)[:_CACHE_MAX // 4]:
            cache.pop(old_key, None)


# Finished tiles, on disk, so they survive the process.
#
# **Both caches above are in memory only, and that was the whole of a
# slow cold start.** Profiled on the owner's real library: building the
# main window cost 569ms of an 822ms launch, and the profile was almost
# entirely PIL - 148 ImagingDecoder.decode calls (126ms), 24 resizes
# (94ms), 36 converts (57ms), all under _fitted. Every launch decoded
# every full-size original again and threw the result away on exit.
#
# A tile is small, already scaled and already corner-clipped, so reading
# one back is a fraction of decoding a multi-megapixel JPEG and resizing
# it. Keyed on the source path, its mtime+size stamp and the target
# size, so an edited or replaced image can never be served stale.
_TILE_DIR = CACHE_DIR / "tiles"

# Bounded, and by count rather than bytes: a tile is a few KB, and the
# owner's library is hundreds of covers at two or three sizes each.
_TILE_MAX = 1500


def _tile_path(path, size, stamp, radius):
    # The radius is part of the key: the same pixel size can be asked
    # for with a different corner (see _corner_radius' note on device
    # ratios), and a tile is stored with its corners already clipped.
    key = f"{path}|{stamp}|{size[0]}x{size[1]}|r{int(round(radius))}"
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    return _TILE_DIR / f"{digest}.png"


def _prune_tiles():
    """Drop the oldest tiles once there are too many. Best-effort: a
    cache that cannot be pruned is a disk-space problem, never a reason
    to fail a page."""
    try:
        files = sorted(_TILE_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for old in files[:max(0, len(files) - _TILE_MAX)]:
        try:
            old.unlink()
        except OSError:
            pass


def _fitted(path, size, stamp, radius=None):
    """The decoded, resized PIL image for `path`, cached - with the
    corners already clipped (see _round_corners), so the rounding is
    paid once per decode and the cache holds the finished tile.

    Three tiers now: the process-lifetime dict, then the on-disk tile
    (see _TILE_DIR), then an actual decode. Safe to call from a
    background thread - it touches no Qt types, and a torn or
    half-written tile file is treated as a miss rather than an error."""
    if radius is None:
        radius = _corner_radius(size)
    key = (str(path), stamp, size, int(round(radius)))
    if key in _FITTED:
        return _FITTED[key]

    tile = _tile_path(path, size, stamp, radius)
    img = None
    try:
        if tile.is_file():
            with Image.open(tile) as opened:
                img = opened.convert("RGBA")
    except Exception:
        img = None          # corrupt or truncated: fall through and redo
        try:
            tile.unlink()
        except OSError:
            pass

    if img is None:
        img = load_thumbnail(path, size)
        if img is None:
            # **An undecodable file inside our own cache is deleted, not
            # remembered.** It is a download this app wrote, so the only
            # ways it can fail to decode are a truncated or interrupted
            # write - and leaving it there means `download` keeps
            # handing the same dead path back and the card is blank for
            # good. Removing it costs one re-download and is the only
            # thing that makes the failure self-heal. Never touches a
            # file outside CACHE_DIR: a user-chosen cover that will not
            # open is theirs, and deleting it would be data loss.
            try:
                candidate = Path(path).resolve()
                if candidate.is_relative_to(CACHE_DIR.resolve()):
                    candidate.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            # Deliberately not cached. A None here means "this failed
            # *this time*" - a half-written download, a disk that was
            # full a moment ago - and storing it would keep the blank
            # tile for the life of the process even after the retry
            # above would have succeeded.
            return None
        if img is not None:
            img = _round_corners(img, radius)
            try:
                _TILE_DIR.mkdir(parents=True, exist_ok=True)
                # Written beside the target and moved into place, so a
                # reader on another thread never sees a partial file.
                temporary = tile.with_suffix(".part")
                img.save(temporary, "PNG")
                temporary.replace(tile)
            except Exception:
                pass        # pure optimization - a miss just decodes again

    _evict(_FITTED)
    _FITTED[key] = img
    return img


def prewarm(specs):
    """Decode the images in `specs` - (path, size) pairs - ahead of time,
    on a background thread.

    The caches above make *returning* to a page instant, but the first
    visit still had to decode everything on the UI thread while the user
    waited. Doing it in the background right after launch means the
    heavy half is usually already done by the time a page is opened, and
    building it only has the ~0.1ms QPixmap conversion left to do.

    Fails soft and silently: this is pure optimization, and anything it
    misses just gets decoded on demand the way it always was."""
    def worker():
        for path, size in specs:
            if not path:
                continue
            try:
                warm(path, tuple(size))
            except Exception:
                continue
        # Once per launch, off the UI thread, after the tiles that
        # matter have been written.
        _prune_tiles()

    threading.Thread(target=worker, daemon=True).start()


def cached_thumbnail(path, size=(64, 64)):
    """The finished tile for `path` **if it is already in this process's
    cache**, else None. Never decodes, never opens the image, and costs
    one stat().

    Exists so a page rebuilt on a revisit can put its covers up in the
    frame it is built in, instead of queueing a worker round trip for a
    pixmap that is already in memory. Pages here rebuild from scratch on
    every visit, so without it every return to a page re-ran the whole
    lazy-cover dance and the covers visibly filled in again - the
    owner's "why do the images have to reload from the start when I
    switch page and come back", 25 August 2026. Measured on his data
    that day: Series asked for 90 covers on the first visit and the same
    90 on the second, every one already on disk, and Discover 169 then
    177.

    Returning None is the ordinary case on a cold page and is not a
    failure - the caller falls back to the pool exactly as before, which
    is what keeps a full JPEG decode off the UI thread (see
    _on_grid_needs_cover for the 18-23ms p95 that bought that rule)."""
    if not path:
        return None
    size = tuple(size)
    return _PIXMAP.get((str(path), _stamp(path), size, device_ratio()))


def thumbnail_or_avatar(path, label_text, size=(64, 64)) -> QPixmap:
    """Best-effort thumbnail: try to load `path` as an image, and fall
    back to blank_tile's empty rounded slab. `label_text` no longer
    shapes the fallback (it drew a letter avatar once); the name and
    signature stay so no caller changes.

    Cached - QPixmap is implicitly shared, so handing the same one to
    several widgets copies a handle, not the pixels.

    **`size` is logical.** The tile is cut at `size x device_ratio()`
    and the returned pixmap carries that ratio, so every caller that
    draws it into a `size`-shaped box - a QLabel at setFixedSize, a
    drawPixmap in poster_grid - gets it at full device resolution
    instead of a 1.25x stretch. See the note beside `device_ratio`."""
    size = tuple(size)
    ratio = device_ratio()
    cut = tile_size(size)
    radius = _corner_radius(size) * ratio
    stamp = _stamp(path) if path else None
    key = (str(path) if path else None, stamp, size, ratio)
    cached = _PIXMAP.get(key)
    if cached is not None:
        return cached

    img = _fitted(path, cut, stamp, radius=radius) if path else None
    failed = path and img is None
    if img is None:
        img = blank_tile(cut, radius)
    pixmap = to_pixmap(img)
    pixmap.setDevicePixelRatio(ratio)
    if failed:
        # A path that was given and would not decode is a *transient*
        # failure by the time it gets here - _fitted has just deleted
        # the damaged cache file, so the next call re-downloads it.
        # Caching the blank tile against this key would hold the empty
        # card for the life of the process and hide the repair.
        return pixmap
    _evict(_PIXMAP)
    _PIXMAP[key] = pixmap
    return pixmap
