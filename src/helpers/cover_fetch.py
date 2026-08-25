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
import urllib.parse

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


# Hosts already reported as refusing, so the log carries one line each
# rather than one per card. A set for the life of the process: this is a
# note about the machine's network, and it does not change within a run.
_refused = set()


def _note_refusal(url):
    """Say once, in the log, that a cover host would not answer.

    **This exists because the report that started all of this was
    undiagnosable.** A friend's install showed no art on Watch or Read
    and nothing anywhere said why - the app had no second source and no
    record of the first one failing, so the only way to find the cause
    was to reproduce it on another machine. One line per host per
    session makes the next such report answerable from the log."""
    try:
        host = urllib.parse.urlparse(url).netloc
        if not host or host in _refused:
            return
        _refused.add(host)
        logs.info(f"cover host would not answer: {host} "
                  f"(falling back to the catalogues)")
    except Exception:
        pass


# How long the second source is allowed, once the first has already
# failed. Shorter than the primary's retry on purpose: this runs on the
# shared 4-worker pool, and a page of blank covers must not hold that
# pool for a minute apiece.
FALLBACK_TIMEOUT = 8


def resolve(url, *, imdb_id="", title="", kind="", timeout=8):
    """A local path for one cover, asking every source there is.

    **Why there is more than one, and it is a report rather than a
    theory** (24 August 2026): a friend's fresh install showed no art at
    all on Watch or Read while the Watch schedule filled normally. Every
    Watch row's poster comes from a single host - `images.metahub.space`
    (measured: 35 of 36 Discover rows across anime/series/movie) - and
    the schedule's comes from s4.anilist.co, so one unreachable host is
    exactly the shape of "no images except the schedule page". The card
    had no second source to ask, and a blank tile is indistinguishable
    from a title with no art.

    Reproduced and fixed on the same measurement - a cold DATA_DIR, the
    real Watch page, `images.metahub.space` blackholed:

        host reachable                    90 cards, 90 covers
        host unreachable, before this     90 cards,  0 covers
        host unreachable, after this      90 cards, 90 covers

    And with **no TMDB key at all** as well as that host unreachable,
    which is the state a new install starts in: Attack on Titan, Solo
    Leveling and Frieren all still resolve, keylessly, through the
    reading catalogues; "The Boys" correctly resolves nothing rather
    than wearing a manga cover.

    The order is cheapest-and-surest first: the row's own URL, that URL
    again with a longer budget (measured: the reading hosts
    intermittently take more than 8s to hand over a 17-477KB image and
    the very next attempt works), then a catalogue that does not share a
    host with either.

    `kind` is what a row is, and it decides which catalogues may answer:

      * "reading" - MangaDex then AniList, through
        `manga_sites.cover_for_title`, the same strictly-matched lookup
        a coverless search result already uses;
      * "video"   - TMDB by IMDb id, the one key the app already asks
        for in Settings, and nothing else: a live-action series has no
        business inheriting art from a manga catalogue;
      * "anime"   - TMDB first, then the reading catalogues. **A keyless
        route matters most here and only here.** An anime and its manga
        are the same work under the same title, so AniList answering for
        "Attack on Titan" is that franchise's own art rather than
        somebody else's - and AniList needs no key, which is the whole
        point: a user who has pasted nothing still gets covers for the
        medium this app is mostly used for.

    Never raises, and never returns a *wrong* cover: every fallback
    matches on an id or on a guarded title, because this project has
    shipped a card wearing another series' art before."""
    url = str(url or "")
    if url:
        path = images.download(url, timeout=timeout)
        if path:
            return path
        path = images.download(url, timeout=20)
        if path:
            return path
        _note_refusal(url)
    title = (title or "").strip()
    imdb_id = (imdb_id or "").strip()
    if kind != "reading" and (imdb_id or title):
        try:
            from . import artwork
            second = artwork.poster_url(imdb_id, title,
                                        timeout=FALLBACK_TIMEOUT)
        except Exception:
            second = None
        if second:
            path = images.download(second, timeout=FALLBACK_TIMEOUT)
            if path:
                return path
    if kind != "video" and title:       # "reading" and "anime"
        try:
            from . import manga_sites
            second = manga_sites.cover_for_title(title,
                                                 timeout=FALLBACK_TIMEOUT)
        except Exception:
            second = None
        if second:
            path = images.download(second, timeout=FALLBACK_TIMEOUT)
            if path:
                return path
    return None


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
    # The covers queue, not the shared one: see lookup_pool.submit_cover
    # for why a page of them must never sit in front of a chapter list -
    # or behind the last page's backlog.
    lookup_pool.submit_cover(_worker, key, fetch, tuple(size))
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
            # Through `warm`, so the tile is cut at the *device* size the
            # UI will ask for - cutting it at the logical size meant the
            # decode was simply paid again on the UI thread on any
            # display above 100%.
            images.warm(str(path), size)
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
