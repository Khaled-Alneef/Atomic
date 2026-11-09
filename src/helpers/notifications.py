"""What the bell left of the search field lists: a new episode of a saved
show, a new chapter of a saved reading title, and a new Atomic version.

**The owner's ask, 7 November 2026**: *"a notification small window appear
when a bell button left of the search bar on the top pressed to show the new
ep, ch released of the SAVED watch and read, and like downloads it shows the
number of notifications"*, with Settings > Notifications switching each kind
and the bell itself on and off.

**No new lookups.** Home already keeps every saved title's lists current in
the background (web.server._refresh_episode_lists from Cinemeta and TMDB,
_refresh_chapter_lists from the reading sites); `scan` asks those same two
to run - each is rate-limited per title, so asking again costs nothing - and
then reads only what is on disk: the episode list a show's details page
writes (`stremio.cached_meta`) and the chapter list the reader keeps
(`chapter_source.latest_number`).

**The first sight of a title is its baseline, not a notification.** A
library of fifteen titles would otherwise open on fifteen "new" items that
are months old. Each saved title's newest aired episode or newest chapter is
remembered under `marks`; an item is made only when a later scan finds a
newer one.

Thread-safe (scans run on a worker); fails soft everywhere - a file that
cannot be read means no new items, never an error."""

import re
import threading
import time
from datetime import datetime, timezone

from . import logs, storage

FILE = "notifications.json"

# The list stays short enough to read in a popup.
MAX_ITEMS = 60
# A show whose list jumped by a season at once (a TMDB fill) is one row per
# episode up to this many, not a wall of them.
MAX_PER_TITLE = 5
# An episode is a release only while it is recent. A list that is filled in
# later (TMDB adding an episode Cinemeta lacked) can grow by an episode that
# aired years ago - winding a mark back on his data offered Brooklyn
# Nine-Nine's 2021 finale as "new". Two weeks covers a weekly show missed
# for one launch-free week, and nothing older is news.
RECENT_DAYS = 14

WATCH_FILE = "series.json"
READ_FILE = "tracker.json"
WATCH_KINDS = ("anime", "series")
SKIP_STATUSES = ("dropped",)
# A saved entry's own switch - the owner's ask, 7 November 2026: "a
# notification turn on/off for each read and watch in the ep/ch list ...
# right next to the save button". Stored on the entry (details writes it
# through storage.update_entry), absent meaning on.
NOTIFY_FIELD = "notify"

_lock = threading.RLock()


def _load() -> dict:
    data = storage.load(FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("marks", {})
    data.setdefault("items", [])
    return data


def _save(data):
    data["items"] = data["items"][:MAX_ITEMS]
    storage.save(FILE, data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def title_enabled(entry) -> bool:
    """Whether this saved title's own switch (the bell beside Save on its
    episode/chapter page) lets it notify. On unless turned off."""
    return (entry or {}).get(NOTIFY_FIELD) is not False


def muted_ids() -> set:
    """The saved titles whose own switch is off."""
    out = set()
    for name in (WATCH_FILE, READ_FILE):
        try:
            rows = storage.load(name, [])
        except Exception:
            continue
        for entry in rows if isinstance(rows, list) else []:
            if isinstance(entry, dict) and entry.get("id") and not title_enabled(entry):
                out.add(entry["id"])
    return out


def items(include_watch=True, include_updates=True) -> list:
    """Every item, newest first, without the kinds switched off and
    without the titles whose own switch is off."""
    with _lock:
        rows = list(_load()["items"])
    muted = muted_ids() if include_watch else set()
    return [row for row in rows if isinstance(row, dict)
            and (include_watch or row.get("kind") == "update")
            and (include_updates or row.get("kind") != "update")
            and row.get("entry_id") not in muted]


def unread_count(include_watch=True, include_updates=True) -> int:
    return sum(1 for row in items(include_watch, include_updates) if not row.get("read"))


def mark_all_read():
    with _lock:
        data = _load()
        changed = False
        for row in data["items"]:
            if isinstance(row, dict) and not row.get("read"):
                row["read"] = True
                changed = True
        if changed:
            _save(data)


def clear():
    with _lock:
        data = _load()
        data["items"] = []
        _save(data)


def add_update(version: str) -> bool:
    """A new Atomic version, once per version. True when it was added."""
    version = str(version or "").strip()
    if not version:
        return False
    key = f"update:{version}"
    with _lock:
        data = _load()
        if any(row.get("id") == key for row in data["items"] if isinstance(row, dict)):
            return False
        data["items"].insert(0, {
            "id": key, "kind": "update", "title": f"Atomic {version}",
            "label": "A new version is available - install it in Settings",
            "at": _now_iso(), "read": False})
        _save(data)
    return True


# ---- what is new ---------------------------------------------------------

def _aired(entry):
    """[(season, episode, name, released iso)] for every episode of this
    saved show that has aired, oldest first, off the list on disk."""
    try:
        from . import stremio
        imdb = str(entry.get("imdb_id") or "")
        if not imdb.startswith("tt"):
            return []
        meta = stremio.cached_meta(imdb) or {}
    except Exception:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for video in meta.get("videos") or []:
        try:
            season, episode = int(video.get("season") or 0), int(video.get("episode") or 0)
            stamp = str(video.get("released") or video.get("firstAired") or "")
            if season < 1 or episode < 1 or not stamp:
                continue
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when > now:
                continue
        except (TypeError, ValueError):
            continue
        out.append((season, episode, str(video.get("name") or video.get("title") or ""),
                    when.isoformat()))
    out.sort(key=lambda row: (row[0], row[1]))
    return out


def _latest_chapter(entry):
    try:
        from . import chapter_source
        number = chapter_source.latest_number(entry)
        return float(number) if number is not None else None
    except Exception:
        return None


def _chapter_text(number) -> str:
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def _saved(file_name, kinds=None):
    rows = storage.load(file_name, [])
    rows = rows if isinstance(rows, list) else []
    out = []
    for entry in rows:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if str(entry.get("status") or "").strip().lower() in SKIP_STATUSES:
            continue
        if kinds and str(entry.get("type") or "").strip().lower() not in kinds:
            continue
        out.append(entry)
    return out


def refresh_sources(watch, read):
    """Ask Home's own background refreshers to bring these titles' lists up
    to date - rate-limited per title there, so a repeat is free."""
    try:
        from web import server
        server._refresh_episode_lists(watch)
        server._refresh_chapter_lists(read)
    except Exception:
        logs.exception("notifications: could not ask for list refreshes")


def scan(refresh=True) -> int:
    """Look for new episodes and chapters of saved titles. Returns how many
    items were added. Disk only, apart from `refresh` handing the saved
    titles to Home's refreshers (whose answers a later scan reads)."""
    try:
        watch = _saved(WATCH_FILE, WATCH_KINDS)
        read = _saved(READ_FILE)
    except Exception:
        logs.exception("notifications: could not read the saved lists")
        return 0
    if refresh:
        refresh_sources(watch, read)
    found = []          # (mark key, new mark, muted, [item, ...])
    for entry in watch:
        aired = _aired(entry)
        if not aired:
            continue
        latest = [aired[-1][0], aired[-1][1]]
        found.append((entry["id"], latest, not title_enabled(entry), [
            {"id": f"{entry['id']}:S{s}E{e}", "kind": "episode",
             "entry_id": entry["id"], "file": WATCH_FILE,
             "title": str(entry.get("title") or ""),
             "label": f"New Episode · S{s:02d}E{e:02d}" + (f" · {name}" if name else ""),
             "at": at, "read": False, "s": s, "e": e}
            for s, e, name, at in aired]))
    for entry in read:
        number = _latest_chapter(entry)
        if number is None:
            continue
        found.append((entry["id"], number, not title_enabled(entry), [
            {"id": f"{entry['id']}:c{_chapter_text(number)}", "kind": "chapter",
             "entry_id": entry["id"], "file": READ_FILE,
             "title": str(entry.get("title") or ""),
             "label": f"New Chapter · {_chapter_text(number)}",
             "at": _now_iso(), "read": False, "n": number}]))

    added = 0
    with _lock:
        data = _load()
        marks = data["marks"]
        have = {row.get("id") for row in data["items"] if isinstance(row, dict)}
        fresh = []
        for key, latest, muted, candidates in found:
            before = marks.get(key)
            marks[key] = latest
            if before is None:
                continue                    # first sight: the baseline
            if muted:
                # Its switch is off: the mark still moves, so turning it
                # back on starts from then, not with everything since.
                continue
            if isinstance(latest, list):
                oldest = datetime.now(timezone.utc).timestamp() - RECENT_DAYS * 86400
                newer = [row for row in candidates
                         if (row["s"], row["e"]) > tuple(before)
                         and datetime.fromisoformat(row["at"]).timestamp() >= oldest
                         ][-MAX_PER_TITLE:]
            else:
                newer = [row for row in candidates if row["n"] > float(before)]
            for row in newer:
                if row["id"] not in have:
                    row.pop("s", None), row.pop("e", None), row.pop("n", None)
                    fresh.append(row)
                    have.add(row["id"])
        if fresh:
            fresh.sort(key=lambda row: row.get("at") or "", reverse=True)
            data["items"] = fresh + data["items"]
            added = len(fresh)
        _save(data)
    if added:
        logs.info(f"notifications: {added} new ({', '.join(r['title'] + ' ' + r['label'] for r in fresh[:3])})")
    return added


def entry_for(item):
    """The saved entry an item names, or None if it has left the list.

    **Only ever one of the two saved lists.** A new-version item names no
    file, and `storage.load("")` is the data *directory*: opening it fails,
    and storage quarantines what it could not open by renaming it - the
    whole data folder became `Atomic.corrupt`. Caught on a test copy, 7
    November 2026, before any build carried it."""
    name = item.get("file")
    if name not in (WATCH_FILE, READ_FILE) or not item.get("entry_id"):
        return None
    try:
        for entry in storage.load(name, []) or []:
            if isinstance(entry, dict) and entry.get("id") == item.get("entry_id"):
                return entry
    except Exception:
        pass
    return None


# ---- what the window shows beside an item -------------------------------
# The owner, 7 November 2026: "add an image of the reading / watching that
# has a notification, also add "(2 Unread Chapters)" or like "(3 Unwatched
# Episodes)" right next to the new ch or ep number".

_ON_EPISODE_RE = re.compile(r"S(\d+)E(\d+)", re.I)
_ON_CHAPTER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def backlog(item) -> str:
    """"(3 Unwatched Episodes)" / "(1 Unread Chapter)" for the title an item
    names, or "" - nothing waiting, an update, or a title no longer saved.

    Counted off the number Home's card prints (web.server._progress_text),
    so the window and the card cannot disagree - **but that number means
    two different things, and the count has to know which.** A video card
    says the episode Continue will open, so that one is still waiting; a
    reading card has said the chapter he last *finished* since 1 October
    2026, so that one is not. Counting both from the number on, as this
    did at first, was "3 unread chapters while I have 2 ONLY" (the owner,
    9 November 2026). Measured on his data: The Eternal Supreme read to
    580 with 581 and 582 listed, shown as 3; The Great Mage read to 277
    with 278 and 282 listed, shown as 3; and every title he had caught up
    on read "(1 Unread Chapter)". Disk only."""
    kind = item.get("kind")
    if kind not in ("episode", "chapter"):
        return ""
    entry = entry_for(item)
    if entry is None:
        return ""
    try:
        from web import server
        on = str(server._progress_text(entry) or "")
    except Exception:
        return ""
    if kind == "episode":
        found = _ON_EPISODE_RE.search(on)
        start = (int(found.group(1)), int(found.group(2))) if found else (1, 1)
        # With nothing aired after it the card stays on the last episode
        # he finished (server._season_step): his Black Clover, S02E06 and
        # all caught up, read "(1 Unwatched Episode)".
        finished = (bool(entry.get("progress_verified"))
                    and str(entry.get("progress") or "").strip().upper() == on.strip().upper())
        count = sum(1 for s, e, _n, _a in _aired(entry)
                    if (s, e) > start or ((s, e) == start and not finished))
        unit = "Unwatched Episode"
    else:
        found = _ON_CHAPTER_RE.search(on)
        # None: nothing read yet, so every chapter on the list is waiting.
        read_to = float(found.group(1)) if found else None
        try:
            from . import chapter_source
            numbers = set()
            for chapter in chapter_source.stale_chapters(entry) or []:
                try:
                    numbers.add(float(chapter.get("number")))
                except (TypeError, ValueError):
                    continue
        except Exception:
            return ""
        count = sum(1 for number in numbers if read_to is None or number > read_to)
        unit = "Unread Chapter"
    if count <= 0:
        return ""
    return f"({count} {unit}{'' if count == 1 else 's'})"


def display_label(item, waiting: str) -> str:
    """The item's label with `waiting` right after its number: "New Chapter
    · 893 (2 Unread Chapters)", "New Episode · S02E05 (3 Unwatched
    Episodes) · The Highest vs. The Lowest"."""
    label = str(item.get("label") or "")
    if not waiting:
        return label
    parts = label.split(" · ")
    if len(parts) >= 2:
        parts[1] = f"{parts[1]} {waiting}"
        return " · ".join(parts)
    return f"{label} {waiting}"


def cover_path(item) -> str:
    """A cover file on disk for the title an item names, or ""."""
    entry = entry_for(item)
    if entry is None:
        return ""
    try:
        from . import art_paths, images
        path = art_paths.resolve_art_path(entry.get("cover_path"))
        if path:
            return path
        url = str(entry.get("cover_url") or "")
        if url.startswith("http"):
            cached = images.cache_path_for_url(url)
            if cached.is_file():
                return str(cached)
    except Exception:
        pass
    return ""


def when_text(iso: str) -> str:
    """"5m ago", "3h ago", "2d ago", or the date past a week."""
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = max(0.0, time.time() - when.timestamp())
    except (TypeError, ValueError):
        return ""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return when.astimezone().strftime("%d %b %Y")
