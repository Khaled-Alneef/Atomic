"""Image loading, caching, and placeholder-tile helpers shared by the
Websites, Apps, Games, and Tracker windows.

Pillow does the actual image work (thumbnailing, the blank fallback
tile); the result is converted to a QPixmap right before it's handed to
a Qt widget.
"""

import hashlib
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap

from . import icon_extract, net, storage, theme

CACHE_DIR = storage.DATA_DIR / "image_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _asset_dir() -> Path:
    """Where images shipped *with the app* live (as opposed to downloaded
    covers): src/ when running from source, the unpacked bundle root when
    frozen, since Atomic.spec copies them to '.'.

    sys._MEIPASS rather than __file__: PyInstaller points __file__ inside
    the archive for a module in the PYZ, which is not a directory anything
    can be read out of."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


def tinted_asset(name: str, color: str, height: int, dpr: float = 1.0) -> QPixmap:
    """A bundled image recoloured to `color`, scaled for `dpr` and tagged
    with it so it isn't blurry on a non-100% display (same reason as the
    sidebar logo in main.py).

    Recoloured rather than shipped in the right colour: every other glyph
    on these buttons is text drawn from theme's palette, so a white PNG
    stayed white while its neighbours sat dimmer. SourceIn replaces colour
    and keeps alpha, so the shape and its antialiased edges survive.

    Scaled before it is filled - filling first would leave a flat block of
    colour for the scaler to blur."""
    source = QPixmap(str(_asset_dir() / name))
    if source.isNull():
        return source  # missing asset: an empty icon, not a crash
    scaled = source.scaledToHeight(max(1, int(height * dpr)),
                                   Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(scaled)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(scaled.rect(), QColor(color))
    painter.end()
    scaled.setDevicePixelRatio(dpr)
    return scaled


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    suffix = Path(url).suffix
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        suffix = ".img"
    return CACHE_DIR / f"{digest}{suffix}"


def download(url: str, timeout: int = 8):
    """Download `url` into the local cache (or reuse a previous download)
    and return its Path, or None on any failure."""
    path = cache_path_for_url(url)
    if path.exists():
        return path
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 PC-App/1.0"}
        )
        deadline = net.deadline_in(timeout)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = net.read_bytes(resp, deadline)
        path.write_bytes(data)
        return path
    except Exception:
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
    circle, which is not "rounded corners" any more."""
    return max(2, min(theme.RADIUS, min(size) // 4))


def _round_corners(img):
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
    scale = 4
    mask = Image.new("L", (img.width * scale, img.height * scale), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, img.width * scale - 1, img.height * scale - 1),
        radius=_corner_radius(img.size) * scale, fill=255)
    mask = mask.resize(img.size, Image.LANCZOS)
    rounded = img.copy()
    rounded.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return rounded


def blank_tile(size=(64, 64)):
    """An empty flat tile in the thumbnails' own rounded shape - the
    fallback whenever no real image/icon/cover is available. It replaced
    the coloured first-letter avatar at the owner's ask ("completely
    empty until the image loads, whole app"): a wall of letters read as
    content of its own, where a quiet SURFACE_HOVER slab reads as space
    an image will fill."""
    color = theme.SURFACE_HOVER.lstrip("#")
    rgb = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    img = Image.new("RGBA", size, rgb + (255,))
    return _round_corners(img)


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


def _fitted(path, size, stamp):
    """The decoded, resized PIL image for `path`, cached - with the
    corners already clipped (see _round_corners), so the rounding is
    paid once per decode and the cache holds the finished tile. Safe to
    call from a background thread - it touches no Qt types."""
    key = (str(path), stamp, size)
    if key in _FITTED:
        return _FITTED[key]
    img = load_thumbnail(path, size)
    if img is not None:
        img = _round_corners(img)
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
                _fitted(path, tuple(size), _stamp(path))
            except Exception:
                continue

    threading.Thread(target=worker, daemon=True).start()


def thumbnail_or_avatar(path, label_text, size=(64, 64)) -> QPixmap:
    """Best-effort thumbnail: try to load `path` as an image, and fall
    back to blank_tile's empty rounded slab. `label_text` no longer
    shapes the fallback (it drew a letter avatar once); the name and
    signature stay so no caller changes.

    Cached - QPixmap is implicitly shared, so handing the same one to
    several widgets copies a handle, not the pixels."""
    size = tuple(size)
    stamp = _stamp(path) if path else None
    key = (str(path) if path else None, stamp, size)
    cached = _PIXMAP.get(key)
    if cached is not None:
        return cached

    img = _fitted(path, size, stamp) if path else None
    if img is None:
        img = blank_tile(size)
    pixmap = to_pixmap(img)
    _evict(_PIXMAP)
    _PIXMAP[key] = pixmap
    return pixmap
