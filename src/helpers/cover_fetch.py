"""One answer to "draw this cover, and go get it if it is not here".

**The owner's report, 24 August 2026:** "if the images are loading for
the 1st time (no image cached), the main page and the history page ...
do not load the images by themselves, I need to load the same anime/
series/manga from the discover page or the searching page, then it
will load and start showing in the main page and the history page."

That is exactly what the code did. Home, the Saved grid, History and
Schedule all drew `images.thumbnail_or_avatar(entry["cover_path"])` and
nothing else - a path that pointed at a file the cache no longer held
(the owner had just wiped `image_cache`) drew the blank tile and nobody
fetched anything. Discover and Search *do* download, by URL, and the
cache is keyed by URL - so a visit there re-created the very file the
Home card was pointing at, and the card "started showing". History was
worse: it fetched by URL only when `cover_path` was *empty*, so a path
to a deleted file blocked the fetch for good, and its `cover_url` is not
always the one Discover used, which is the "sometimes it does not load
in history".

So every surface that draws a cover now goes through `ensure`: draw
what is on disk now, and if nothing is, fetch it on the shared pool,
hand the finished tile back on the UI thread, and write the path back
onto the record so the next build finds it without asking. One
implementation, because four copies of "download then setPixmap" is
how the History one ended up with the wrong condition.
"""

import os
import threading

from PyQt6.QtCore import QObject
from PyQt6.QtCore import pyqtSignal as Signal

from . import images, logs, lookup_pool


class _Signals(QObject):
    ready = Signal(str, str)        # key, local path


_signals = _Signals()
_lock = threading.Lock()
# key -> [(setter, title, size, persist), ...]. Several widgets can want
# the same cover at once (Home's hero and its grid, say); the fetch runs
# once and every waiter is told.
_waiting = {}
_connected = False


def _connect_once():
    global _connected
    if not _connected:
        _signals.ready.connect(_on_ready)
        _connected = True


def ensure(key, path, fetch, title, size, setter, persist=None):
    """Draw the cover at `path` through `setter` now if the file exists.
    Otherwise start `fetch` (a callable returning a local path or None,
    run on the lookup pool), and when it lands call `setter(pixmap)`
    and `persist(path)` on the UI thread.

    `key` names the cover (an entry id, a history key) so concurrent
    asks collapse into one fetch. `setter` must tolerate its widget
    having been deleted - it is wrapped, so a RuntimeError is swallowed.
    Never raises; a missing cover simply stays the blank tile."""
    _connect_once()
    path = str(path) if path else ""
    if path and os.path.exists(path):
        return False                # on disk - the caller already drew it
    if fetch is None or not key:
        return False
    key = str(key)
    with _lock:
        waiters = _waiting.get(key)
        if waiters is not None:
            waiters.append((setter, title, tuple(size), persist))
            return True
        _waiting[key] = [(setter, title, tuple(size), persist)]
    lookup_pool.submit(_worker, key, fetch, tuple(size))
    return True


def _worker(key, fetch, size):
    try:
        path = fetch()
    except Exception:
        path = None
    if path:
        try:
            # Cut the tile here, on the worker, so the UI-thread slot
            # only converts a cached tile to a QPixmap (see images._fitted).
            images._fitted(str(path), size, images._stamp(str(path)))
        except Exception:
            pass
    with _lock:
        if not path:
            _waiting.pop(key, None)
            return
    try:
        _signals.ready.emit(key, str(path))
    except RuntimeError:
        pass


def _on_ready(key, path):
    with _lock:
        waiters = _waiting.pop(key, None) or []
    for setter, title, size, persist in waiters:
        try:
            setter(images.thumbnail_or_avatar(path, title, size))
        except RuntimeError:
            pass                    # the widget was rebuilt under the fetch
        except Exception:
            logs.exception("cover setter failed")
        if persist is not None:
            try:
                persist(path)
            except Exception:
                logs.exception("cover persist failed")
