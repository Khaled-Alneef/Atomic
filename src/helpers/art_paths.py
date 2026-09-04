"""Where a shelf entry's artwork actually is - and getting it back when
it is not there.

Qt-free on purpose: web/backend serves the shelves from a process that
may have no Qt in it (run.py), and `images` imports PyQt6 at the top, so
the path logic the web shelves and the Qt shelves share lives here and
`images` re-exports it.

**The defect this exists for, measured 2 September 2026 on the owner's
data.** games.json carried every `cover` and `icon` as an absolute path
into the *source tree's* src/data/image_cache, and apps.json every
`image` as an absolute path into %APPDATA%/Atomic/image_cache where no
such file existed any more:

  * running from source, `images.CACHE_DIR` is fixed at import to
    src/data/image_cache (helpers/__init__ imports it before
    web/backend re-points storage.DATA_DIR at %APPDATA%), so a cover
    fetched from the source run landed beside the code while the JSON
    naming it was saved beside the user's data - on the next machine,
    or after the repo moves, every one of those paths is dead
    (`cover_url` with the src paths hidden: games 0/10, apps 0/5);
  * `images.protected_paths` kept only the tracker's four keys, so the
    cache trim evicted every extracted exe icon and one favicon - which
    is the blank Stremio tile the owner photographed, four apps hiding
    the same loss behind their iTunes `art`.

Two answers, both here. `resolve_art_path` falls back from a dead
absolute path to the same *file name* under every cache directory this
app has ever written to, so a JSON row survives the cache moving.
`heal_missing_art` re-derives what cannot be found at all - an app's
icon out of its exe (1.3-24ms, measured), a game's cover through
game_art, a site's favicon - once per entry per session, and writes the
one field back with storage.update_entry.
"""

import hashlib
import os
import threading
from pathlib import Path

from . import lookup_pool, storage

# Every key that names a picture on disk, by shelf file. `protected_paths`
# reads this so the cache trim can never again evict something a saved
# entry points at, whatever the entry calls it.
ART_KEYS = {
    "games.json": ("cover", "icon"),
    "apps.json": ("image", "art"),
    "websites.json": ("image",),
    "tracker.json": ("cover_path", "hero_backdrop", "hero_logo", "icon_path"),
    "series.json": ("cover_path", "hero_backdrop", "hero_logo", "icon_path"),
}


def _source_data_dir() -> Path:
    # storage's own default when not frozen: src/data. Frozen, this
    # resolves inside the unpacked bundle and simply does not exist.
    return Path(storage.__file__).resolve().parent.parent / "data"


def cache_dirs():
    """Every image_cache this app writes to, existing ones only, the
    live DATA_DIR first. The source tree's is in the list because the
    owner's games were imported from a source run and that is where
    their covers went; %APPDATA%'s is there for the mirror case, a
    source run reading data the frozen app wrote."""
    roots = [Path(storage.DATA_DIR)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Atomic")
    roots.append(_source_data_dir())
    seen, found = set(), []
    for root in roots:
        cache = root / "image_cache"
        key = os.path.normcase(str(cache))
        if key in seen:
            continue
        seen.add(key)
        if cache.is_dir():
            found.append(cache)
    return found


def resolve_art_path(value) -> str:
    """`value` if that file exists, else the same file name under one of
    `cache_dirs`, else ''. A URL or an empty value answers '' - this is
    for paths on disk only."""
    value = str(value or "").strip()
    if not value or value.startswith(("http://", "https://")):
        return ""
    if os.path.isfile(value):
        return value
    name = Path(value).name
    if not name:
        return ""
    for cache in cache_dirs():
        candidate = cache / name
        if candidate.is_file():
            return str(candidate)
    return ""


def exe_icon_path(exe_path) -> Path:
    """Where an extracted exe icon is stored - the same name
    images.extract_app_icon uses, so the two never disagree about a
    file, but under the *live* DATA_DIR rather than the CACHE_DIR
    `images` fixed at import (see the module note)."""
    digest = hashlib.sha1(str(exe_path).encode("utf-8")).hexdigest()
    cache = Path(storage.DATA_DIR) / "image_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"exeicon_{digest}.png"


def extract_exe_icon(exe_path) -> str:
    """The icon file for `exe_path`, extracted now if it is not on disk.
    '' when the exe is missing or has no icon. 1.3ms warm, 24ms the
    first time in a process (measured on stremio-shell-ng.exe, 2
    September 2026) - cheap enough to do on the thread that asks."""
    exe_path = str(exe_path or "")
    if not exe_path or not os.path.isfile(exe_path):
        return ""
    target = exe_icon_path(exe_path)
    if target.is_file():
        return str(target)
    try:
        from . import icon_extract
        image = icon_extract.extract_icon(exe_path, 64)
        if image is None:
            return ""
        temporary = target.with_suffix(".part")
        image.save(temporary, "PNG")
        temporary.replace(target)
        return str(target)
    except Exception:
        return ""


def _first_target(entry, kind):
    for target in (entry.get("targets") or []):
        if isinstance(target, dict) and target.get("type") == kind:
            value = str(target.get("target") or "").strip()
            if value:
                return value
    return ""


def shelf_file(entry) -> str:
    """Which shelf file an entry came from, read off its shape - the
    web shelf route hands cover_url a bare row. An app has an app
    target, a website a site target, a game a `path` and no targets."""
    if not isinstance(entry, dict):
        return ""
    if entry.get("targets"):
        return "apps.json" if _first_target(entry, "app") else "websites.json"
    if entry.get("path") and "cover" in entry:
        return "games.json"
    return ""


# Entries whose art this process has already tried to restore: an entry
# that cannot be healed (its exe is gone, Steam has never heard of it)
# must not cost a job on every page build.
_attempted = set()
_pending = set()
_lock = threading.Lock()


def _finish(key, file, entry_id, field, path):
    if path and entry_id:
        try:
            storage.update_entry(file, entry_id, {field: str(path)})
        except Exception:
            pass
    with _lock:
        _pending.discard(key)


def _heal_worker(key, file, entry_id, field, fetch):
    # Never raises - an exception here would kill the pool worker.
    try:
        path = fetch()
    except Exception:
        path = None
    _finish(key, file, entry_id, field, path)


def heal_missing_art(entry, file=None, inline_icon=False):
    """Restore the picture `entry` names but no longer has. Returns the
    restored path when it was made on this thread (an exe icon with
    `inline_icon`), '' otherwise - either nothing to do, or a network
    fetch queued on lookup_pool's cover queue that writes the field
    back itself when it lands.

    Once per entry per process. `inline_icon` is for the web shelf
    route, which answers a page once and is not asked again: the icon
    is milliseconds, so the first render carries it rather than the
    next visit."""
    if not isinstance(entry, dict):
        return ""
    file = file or shelf_file(entry)
    entry_id = entry.get("id")
    if not file or not entry_id:
        return ""
    key = (file, str(entry_id))
    with _lock:
        if key in _attempted:
            return ""
        _attempted.add(key)

    if file == "apps.json":
        # **The store artwork is the picture; the exe icon is the
        # stand-in.** The owner, 3 September 2026: "make sure that you
        # will take it from an API for the good quality like it was in
        # Qt". windows/link_grid._backfill_app_art is the Qt half of
        # this and asks app_art for every entry without `art`; this
        # never did, so an app with no `art` (Stremio, on his machine)
        # was left with a 64x64 .exe icon for ever - or, when even that
        # file had gone, with nothing.
        #
        # Both halves run: the icon is extracted inline where it is
        # wanted (`inline_icon`, milliseconds, so the very first render
        # is not blank) *and* the API lookup is queued, which writes
        # `art` back and wins on the next build because cover_url now
        # prefers it.
        name = str(entry.get("name") or entry.get("title") or "").strip()
        want_art = name and not resolve_art_path(entry.get("art"))
        icon = ""
        if not resolve_art_path(entry.get("image")):
            exe = _first_target(entry, "app")
            if exe and os.path.isfile(exe) and inline_icon:
                icon = extract_exe_icon(exe)
                if icon:
                    entry["image"] = icon
                    _finish(key, file, entry_id, "image", icon)
        if not want_art:
            if icon:
                return icon
            if resolve_art_path(entry.get("image")):
                return ""
            exe = _first_target(entry, "app")
            if not exe or not os.path.isfile(exe):
                return ""
            fetch = lambda: extract_exe_icon(exe)           # noqa: E731
            field = "image"
            with _lock:
                _pending.add(key)
            lookup_pool.submit_cover(_heal_worker, key, file, entry_id,
                                     field, fetch)
            return ""

        def fetch():
            from . import app_art               # pulls images, so lazy
            return app_art.fetch_art(name)
        field = "art"
        with _lock:
            _pending.add(key)
        lookup_pool.submit_cover(_heal_worker, key, file, entry_id, field,
                                 fetch)
        # The icon made above, so this render has something to draw
        # while the artwork is on its way.
        return icon
    elif file == "games.json":
        if resolve_art_path(entry.get("cover")):
            return ""
        name = str(entry.get("name") or "")
        if not name:
            return ""
        install = entry.get("path")

        def fetch():
            from . import game_art                # pulls images, so lazy
            return game_art.fetch_cover(name, install_path=install)
        field = "cover"
    elif file == "websites.json":
        if resolve_art_path(entry.get("image")):
            return ""
        site = _first_target(entry, "site")
        if not site:
            return ""

        def fetch():
            from . import images                  # Qt import, so lazy
            return images.fetch_site_icon(site)
        field = "image"
    else:
        return ""

    with _lock:
        _pending.add(key)
    lookup_pool.submit_cover(_heal_worker, key, file, entry_id, field, fetch)
    return ""
