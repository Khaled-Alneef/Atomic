"""Every reading row this machine has browsed off the owner's sites, kept
on disk - the reading side's helpers/catalog_index.

The owner, 21 September 2026: *"the anime page now loads fast and good,
but the series and movies and read pages are not!"*. Measured that day
on a copy of his data: reading_meta.json knows **211** titles are
Romance (161 manhwa, 28 manhua, 18 manga), and the Romance tick drew
**35** - the Manga page three - because a card needs its row (the site
URL that opens a chapter list, its cover, its site), and a row lived
only in the last browse (SWEEP_ROWS_TTL_S, 90s) and discover_cache.json's
few listings. Every title those verdicts were written for had been
browsed; its row was thrown away. 176 of 211 had no row anywhere.

`remember` is called with every browse the reading sweeps make
(discover._browsed_rows and the deep browse); `rows` hands them back
newest first. Bounded at MAX_ROWS, oldest out, written through
storage.save at most every WRITE_GAP_S and at exit. Seeded on first load
from the reading rows discover_cache.json already holds, so the machine
starts from everything it has on disk rather than from nothing.
"""

import atexit
import threading
import time

from . import logs, storage

FILE = "reading_index.json"
MAX_ROWS = 6000
WRITE_GAP_S = 15.0
# The card shape discover_reading_sites hands out (not manga_sites' own
# `cover_url` rows) - what reading_genre_cached serves straight to a grid.
FIELDS = ("title", "year", "poster", "imdb_id", "type", "url", "site_id", "site_name")

_lock = threading.Lock()
_rows = None             # lowercased title -> row, insertion-ordered
_dirty = False
_last_write = 0.0


def _slim(row):
    return {key: row.get(key) for key in FIELDS if row.get(key) is not None}


def _usable(row) -> bool:
    return (isinstance(row, dict) and str(row.get("title") or "").strip()
            and str(row.get("url") or "").startswith("http"))


def _load():
    global _rows
    if _rows is not None:
        return _rows
    rows = {}
    try:
        cache = storage.load("discover_cache.json", {}) or {}
        for key, entry in (cache.items() if isinstance(cache, dict) else ()):
            if not (key.startswith("medium:") or key in ("reading", "reading_latest")):
                continue
            for row in (entry or {}).get("rows") or []:
                if _usable(row):
                    rows[row["title"].strip().lower()] = _slim(row)
    except Exception:
        logs.exception("reading index: the discover cache could not seed it")
    try:
        for row in storage.load(FILE, []) or []:
            if _usable(row):
                key = row["title"].strip().lower()
                rows.pop(key, None)
                rows[key] = _slim(row)
    except Exception:
        logs.exception("reading index: could not be read")
    _rows = rows
    return rows


def remember(rows) -> int:
    """Keep browsed reading rows. Returns how many were new. Never raises."""
    global _dirty, _last_write
    added, due, snapshot = 0, False, None
    try:
        with _lock:
            index = _load()
            for row in rows or ():
                if not _usable(row):
                    continue
                key = row["title"].strip().lower()
                if key not in index:
                    added += 1
                index.pop(key, None)
                index[key] = _slim(row)
            while len(index) > MAX_ROWS:
                del index[next(iter(index))]
            if added:
                _dirty = True
            due = _dirty and time.monotonic() - _last_write >= WRITE_GAP_S
            if due:
                _last_write = time.monotonic()
                _dirty = False
                snapshot = list(index.values())
        if due:
            storage.save(FILE, snapshot)
    except Exception:
        logs.exception("reading index: could not remember rows")
    return added


def rows() -> list:
    """Every kept row, newest first (copies)."""
    with _lock:
        return [dict(row) for row in reversed(list(_load().values()))]


def flush():
    global _dirty, _last_write
    try:
        with _lock:
            if _rows is None or not _dirty:
                return
            _dirty = False
            _last_write = time.monotonic()
            snapshot = list(_rows.values())
        storage.save(FILE, snapshot)
    except Exception:
        logs.exception("reading index: could not write")


atexit.register(flush)
