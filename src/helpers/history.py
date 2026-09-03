"""What has actually been watched and read - saved or not.

The tracker files answer "what am I keeping"; this one answers "what
have I opened". They are different questions, and until now only the
first had a store: a title reached through Discover could be played to
the end and leave no trace anywhere, and its episodes could not be
ticked off at all, because every progress writer keys off an entry id
that an unsaved title does not have (tracker._write_progress refuses
outright).

So this module keeps its own file, `history.json`, one record per title:

    {"key", "title", "type", "imdb_id", "url", "site_id",
     "cover_url", "cover_path", "entry_id",
     "watched": ["1:5", "1:6"] or ["c12"],
     "progress": "S01E06", "last_opened": "<iso>"}

`key` is the imdb id when there is one and a normalized title+type
otherwise - deliberately *not* the entry id, because the whole point is
to survive a title having no entry. A title later saved to the tracker
gains `entry_id` here, so the two stores stay one story rather than two
lists of the same shows.

`watched` is the explicit tick list - what the details page's
Mark as Watched writes and reads, for saved and unsaved titles alike.
For a saved title it is a *second* record of the same fact rather than
the only one: the entry's own `progress` number remains what the cards
and the schedule read, and this is what makes a per-episode tick
possible at all (a single number cannot say "I watched 5 and 7").

Everything here fails soft and never raises at a caller: a history file
that cannot be read costs a history list, and must never cost playback.
"""

import threading

from . import logs, storage

HISTORY_FILE = "history.json"

# The reading types, repeated rather than imported: windows.tracker
# imports this module, so importing it back would be a cycle.
MANGA_TYPES = ("Manga", "Manhwa", "Manhua")

# How many titles the file keeps. Bounded because nothing here ever gets
# deleted by hand and a details page reads the whole file; 500 titles is
# far past a person's real library and still a small file.
MAX_TITLES = 500

_lock = threading.RLock()


def _normalized(text) -> str:
    return " ".join(str(text or "").strip().lower().split())


def title_key(entry) -> str:
    """The stable identity of a title across saved/unsaved life.

    The IMDb id when there is one - it survives a retitle and is what
    two catalogs agree on - and a normalized title+type otherwise, which
    is all a manga has. Never the entry id: an unsaved title has none,
    and a saved one would change identity the day it was saved."""
    entry = entry or {}
    imdb_id = str(entry.get("imdb_id") or "").strip()
    if imdb_id:
        return f"imdb:{imdb_id}"
    kind = "read" if entry.get("type") in MANGA_TYPES else "watch"
    return f"{kind}:{_normalized(entry.get('title'))}"


def episode_key(season, episode) -> str:
    """One episode's tick key. Season 0 is kept as 0 rather than folded
    into 1 - a source with no season numbering is a real case, and
    pretending it is season 1 would collide two different episodes."""
    try:
        return f"{int(season or 0)}:{int(episode)}"
    except (TypeError, ValueError):
        return ""


def chapter_key(number) -> str:
    try:
        return f"c{float(number):g}"
    except (TypeError, ValueError):
        return ""


def _load() -> list:
    rows = storage.load(HISTORY_FILE, [])
    return rows if isinstance(rows, list) else []


def _save(rows):
    try:
        storage.save(HISTORY_FILE, rows[:MAX_TITLES])
    except Exception:
        # Must not raise - a history that cannot be written must never
        # cost playback - but it is logged now: Home's Watching row
        # silently stopped following the player when this failed
        # (review, 3 September 2026).
        logs.exception("Could not write history")


def _find(rows, key):
    for row in rows:
        if row.get("key") == key:
            return row
    return None


def _record_fields(entry) -> dict:
    """What a history row copies off an entry - enough to redraw the
    card and reopen the title without the tracker file."""
    return {
        "title": (entry.get("title") or "").strip(),
        "type": entry.get("type") or "",
        "imdb_id": entry.get("imdb_id") or "",
        "url": entry.get("url") or "",
        "site_id": entry.get("site_id") or None,
        "cover_url": entry.get("cover_url") or None,
        "cover_path": entry.get("cover_path") or None,
        "entry_id": entry.get("id") or None,
    }


def get(entry) -> dict:
    """This title's history record, or {} if it has none."""
    try:
        with _lock:
            return dict(_find(_load(), title_key(entry)) or {})
    except Exception:
        return {}


def touch(entry, *, progress=None) -> dict:
    """Note that this title was opened, moving it to the top of History.

    Called from the same places progress is recorded, and deliberately
    *also* for titles that record no progress at all (a film has no
    episode to be on, but watching it is exactly the thing History
    should remember)."""
    try:
        with _lock:
            rows = _load()
            key = title_key(entry)
            row = _find(rows, key)
            if row is None:
                row = {"key": key, "watched": []}
                rows.append(row)
            fields = _record_fields(entry)
            # Never overwrite a real value with an empty one: a
            # transient Discover dict carries no cover_path, and the
            # saved row's is worth keeping.
            row.update({k: v for k, v in fields.items() if v})
            if progress:
                row["progress"] = progress
            row["last_opened"] = storage.now_iso()
            rows.sort(key=lambda r: r.get("last_opened") or "", reverse=True)
            _save(rows)
            return dict(row)
    except Exception:
        return {}


def watched_keys(entry) -> set:
    """Every tick this title carries."""
    return set(get(entry).get("watched") or ())


def set_watched(entry, marks, watched: bool = True) -> bool:
    """Tick (or untick) one or more episodes/chapters of this title.

    Accepts several marks at once because "mark all as watched" is one
    user action and should be one write. Creates the title's record if
    it has none - marking something watched is itself a reason for it to
    be in History."""
    marks = [m for m in ([marks] if isinstance(marks, str) else marks or []) if m]
    if not marks:
        return False
    try:
        with _lock:
            rows = _load()
            key = title_key(entry)
            row = _find(rows, key)
            if row is None:
                row = {"key": key, "watched": []}
                row.update(_record_fields(entry))
                row["last_opened"] = storage.now_iso()
                rows.append(row)
            current = set(row.get("watched") or ())
            updated = (current | set(marks)) if watched else (current - set(marks))
            if updated == current:
                return False
            # Sorted so the file stays readable and diffable by eye -
            # this is a list a person may well open.
            row["watched"] = sorted(updated)
            row["updated_at"] = storage.now_iso()
            _save(rows)
            return True
    except Exception:
        return False


def recent(types=(), limit: int = 60) -> list:
    """History rows, newest first, optionally narrowed to entry types -
    which is how the Watch page shows only what is watched and the Read
    page only what is read."""
    try:
        with _lock:
            rows = _load()
    except Exception:
        return []
    wanted = tuple(types or ())
    out = [dict(r) for r in rows
           if not wanted or r.get("type") in wanted]
    out.sort(key=lambda r: r.get("last_opened") or "", reverse=True)
    return out[:limit]


def forget(key: str) -> bool:
    """Drop one title from History. The ticks go with it - they are part
    of the same record, and a title removed from History that kept
    invisible ticks would be a confusing thing to meet again."""
    try:
        with _lock:
            rows = _load()
            remaining = [r for r in rows if r.get("key") != key]
            if len(remaining) == len(rows):
                return False
            _save(remaining)
            return True
    except Exception:
        return False


def clear() -> bool:
    try:
        with _lock:
            _save([])
            return True
    except Exception:
        return False


def update_cover(key: str, path: str) -> bool:
    """Remember a cover that History fetched for itself, so the next
    build draws it from disk. Only fills or repairs `cover_path`; the
    URL the row was recorded with is left alone."""
    try:
        with _lock:
            rows = _load()
            row = _find(rows, key)
            if row is None or not path:
                return False
            if row.get("cover_path") == str(path):
                return True
            row["cover_path"] = str(path)
            _save(rows)
            return True
    except Exception:
        return False


def link_entry(entry) -> bool:
    """Record that this title now has a saved entry (or no longer does).
    Called after a save, so History and the tracker stay one story."""
    try:
        with _lock:
            rows = _load()
            row = _find(rows, title_key(entry))
            if row is None:
                return False
            row["entry_id"] = entry.get("id") or None
            _save(rows)
            return True
    except Exception:
        return False
