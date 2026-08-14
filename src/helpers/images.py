"""Image loading, caching, and placeholder-avatar helpers shared by the
Websites, Apps, Games, and Tracker windows.

Pillow does the actual image work (thumbnailing, letter avatars); the
result is converted to a QPixmap right before it's handed to a Qt widget.
"""

import hashlib
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from PyQt6.QtGui import QImage, QPixmap

from . import icon_extract, storage, theme

CACHE_DIR = storage.DATA_DIR / "image_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_AVATAR_COLORS = [theme.ACCENT, "#2e86de", "#10ac84", "#ee5253", "#8e44ad", "#e67e22", "#0abde3"]


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
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
    letter avatar either way."""
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


def letter_avatar(text: str, size=(64, 64)):
    """A colored circle with the first letter of `text` - the fallback
    used whenever no real image/icon/cover is available."""
    letter = (text or "?").strip()[:1].upper() or "?"
    seed = sum(text.encode("utf-8")) if text else 0
    color = _AVATAR_COLORS[seed % len(_AVATAR_COLORS)]

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size[0] - 1, size[1] - 1), fill=color)
    try:
        font = ImageFont.truetype("segoeuib.ttf", int(size[1] * 0.45))
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size[0] - tw) / 2 - bbox[0], (size[1] - th) / 2 - bbox[1]),
        letter, font=font, fill="#ffffff",
    )
    return img


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
    unreadable - callers fall through to the letter avatar."""
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
    """The decoded, resized PIL image for `path`, cached. Safe to call
    from a background thread - it touches no Qt types."""
    key = (str(path), stamp, size)
    if key in _FITTED:
        return _FITTED[key]
    img = load_thumbnail(path, size)
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
    back to a letter avatar generated from `label_text`.

    Cached - QPixmap is implicitly shared, so handing the same one to
    several widgets copies a handle, not the pixels."""
    size = tuple(size)
    stamp = _stamp(path) if path else None
    key = (str(path) if path else None, stamp, label_text, size)
    cached = _PIXMAP.get(key)
    if cached is not None:
        return cached

    img = _fitted(path, size, stamp) if path else None
    if img is None:
        img = letter_avatar(label_text, size)
    pixmap = to_pixmap(img)
    _evict(_PIXMAP)
    _PIXMAP[key] = pixmap
    return pixmap
