"""Every video catalogue row this machine has ever fetched, by kind, on
disk - so a genre tick answers from what is already here.

The owner, 8 September 2026: *"when I select some filter in the watch
or read pages, it loads but super super slow"*. Measured on his own log
of the night before: a Romance tick on the Anime page filled through the
page's scroll top-up, thirty Cinemeta rows a batch at 1-6s a batch on
his line, for fifty seconds - while his session had already fetched 781
anime rows with their genres and kept none of them. discover_cache.json
holds the first page of each kind (30 rows) and nothing deeper.

`remember` is called for every catalogue answer discover_video returns
(browse pages, scroll top-ups, genre walks); `rows_for` filters a kind's
rows by genre in memory, which is where server._genre_video now starts
before it asks Cinemeta for anything. Bounded at MAX_ROWS a kind, oldest
first out; written to disk at most every WRITE_GAP_S through
storage.save (atomic). Only rows carrying genres are worth keeping -
search rows come back with none (discover's own note) and are skipped
by the caller."""

import atexit
import threading
import time

from . import logs, storage

FILE = "catalog_index.json"
KINDS = ("anime", "series", "movie")
MAX_ROWS = 4000
WRITE_GAP_S = 15.0

_lock = threading.Lock()
_index = None            # kind -> {imdb_id: row}, insertion-ordered
_dirty = False
_last_write = 0.0


def _load():
    global _index
    if _index is not None:
        return _index
    stored = {}
    try:
        stored = storage.load(FILE, {}) or {}
    except Exception:
        stored = {}
    index = {kind: {} for kind in KINDS}
    # The shipped seed first (helpers/catalog_seed: Cinemeta's first
    # pages of each kind, the way helpers/reading_seed starts the
    # reading verdicts), so a fresh install's first tick answers from
    # the index too. It is the oldest layer: what this machine fetched
    # itself is inserted after it, and rows_for reads newest first.
    try:
        from . import catalog_seed
        for kind in KINDS:
            for values in catalog_seed.ROWS.get(kind) or ():
                row = dict(zip(catalog_seed.FIELDS, values))
                if row.get("imdb_id"):
                    index[kind][str(row["imdb_id"])] = row
    except Exception:
        logs.exception("catalog index: the seed could not be read")
    for kind in KINDS:
        rows = stored.get(kind) if isinstance(stored, dict) else None
        for row in rows or []:
            if isinstance(row, dict) and row.get("imdb_id"):
                index[kind].pop(str(row["imdb_id"]), None)
                index[kind][str(row["imdb_id"])] = row
    _index = index
    return index


def remember(kind: str, rows) -> int:
    """Keep `rows` (Cinemeta rows with genres) under `kind`. Returns how
    many were new. Never raises."""
    global _dirty, _last_write
    if kind not in KINDS:
        return 0
    added = 0
    try:
        with _lock:
            index = _load()
            bucket = index[kind]
            for row in rows or ():
                if not isinstance(row, dict) or not row.get("imdb_id"):
                    continue
                if not row.get("genres"):
                    continue
                key = str(row["imdb_id"])
                if key in bucket:
                    continue
                bucket[key] = {k: row.get(k) for k in
                               ("imdb_id", "title", "type", "year", "poster",
                                "imdbRating", "genres")}
                added += 1
            while len(bucket) > MAX_ROWS:
                del bucket[next(iter(bucket))]
            if added:
                _dirty = True
            due = _dirty and time.monotonic() - _last_write >= WRITE_GAP_S
            if due:
                _last_write = time.monotonic()
                _dirty = False
                snapshot = {k: list(v.values()) for k, v in index.items()}
        if due:
            storage.save(FILE, snapshot)
    except Exception:
        logs.exception("catalog index: could not remember rows")
    return added


def flush():
    """Write what is pending now (a page closing, a test)."""
    global _dirty, _last_write
    try:
        with _lock:
            if _index is None or not _dirty:
                return
            _dirty = False
            _last_write = time.monotonic()
            snapshot = {k: list(v.values()) for k, v in _index.items()}
        storage.save(FILE, snapshot)
    except Exception:
        logs.exception("catalog index: could not write")


# Rows remembered inside the last WRITE_GAP_S of a session would
# otherwise be lost with it.
atexit.register(flush)


def rows_for(kind: str, genre: str, limit: int = 200) -> list:
    """The kind's remembered rows carrying `genre`, newest first. An
    anime row carries AniList's genres as well as Cinemeta's
    (helpers/anime_genres - Cinemeta tags 94 of his 1,505 anime rows
    Romance, the two together 357)."""
    wanted = str(genre or "").strip().lower()
    if kind not in KINDS or not wanted:
        return []
    from . import anime_genres
    with _lock:
        bucket = _load().get(kind) or {}
        found = [row for row in reversed(list(bucket.values()))
                 if any(str(g).strip().lower() == wanted
                        for g in anime_genres.merged(row))]
    return found[:limit]


def size(kind: str = "") -> int:
    with _lock:
        index = _load()
        if kind:
            return len(index.get(kind) or {})
        return sum(len(v) for v in index.values())
