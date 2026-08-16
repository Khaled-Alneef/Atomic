"""Tiny JSON persistence helper shared by the feature windows."""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import logs


def _default_data_dir() -> Path:
    """When PyInstaller freezes this into an EXE, __file__ resolves inside
    a temp extraction folder that's wiped after every run - saving there
    would silently lose all user data on every launch. Use a real,
    persistent per-user folder instead. Running from source (dev) keeps
    using the local data/ folder next to main.py, for convenience."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent / "data"

    base = Path(os.getenv("APPDATA") or Path.home())
    new_dir = base / "Atomic"
    old_dir = base / "PC App"
    if not new_dir.exists() and old_dir.exists():
        # One-time migration: the app used to be called "PC App" - carry
        # existing user data over to the new folder instead of the
        # frozen build silently starting empty after the rename.
        try:
            old_dir.rename(new_dir)
        except OSError:
            pass
    return new_dir


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(filename: str, default):
    """The saved file, or `default` when there isn't one.

    "utf-8-sig" rather than "utf-8": a byte-order mark at the start of
    the file is invisible to a text editor and makes json.load fail on
    the first character. That is not hypothetical - a BOM'd settings.json
    is exactly how a stored AniList username was lost. utf-8-sig strips a
    BOM if one is there and is otherwise identical, so the app now reads
    a file it used to declare unreadable.

    An unreadable file is moved aside rather than left in place, because
    the next save would otherwise overwrite it with whatever `default`
    the caller got here - which is how the file's contents were destroyed
    rather than merely unread."""
    path = DATA_DIR / filename
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logs.exception(f"Could not read {filename}; quarantining it and "
                       f"starting from the default")
        _quarantine(path)
        return default


def _quarantine(path: Path):
    """Move an unreadable file to <name>.corrupt so the next save can't
    silently overwrite it. Kept, not deleted: it is the only copy of
    whatever it holds, and hand-repairing one stray byte is a real
    recovery path. Any previous quarantine is replaced - the readable
    copy worth keeping is the .bak that save() leaves."""
    try:
        os.replace(path, path.with_name(path.name + ".corrupt"))
    except OSError:
        logs.exception(f"Could not quarantine {path.name}")


def save(filename: str, data):
    """Write `data`, keeping the previous contents recoverable.

    Two failures this shape rules out, both of which have real cost here:

      * A crash or a full disk part-way through a write used to leave a
        truncated file that no longer parses - and an unparseable file
        used to read back as "empty", which the next save then made true.
        Writing a temp file and renaming it into place means the visible
        file is only ever a complete one.
      * Everything the app saves is small (kilobytes of JSON), so keeping
        one .bak of the previous contents costs nothing measurable and
        turns "the file is wrong now" from permanent into a rename.

    Deliberately *not* refusing to write an empty list over a full one:
    emptying a page is something the user is allowed to do, and a rule
    that guesses at intent would eventually block a legitimate save. The
    .bak covers the accident without ever standing in the way."""
    path = DATA_DIR / filename
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError:
            # Not fatal: losing the safety copy is worse than not having
            # it, but refusing the save the user asked for is worse still.
            logs.exception(f"Could not back up {filename} before saving")
    os.replace(temp_path, path)


def update_entry(filename: str, entry_id, fields: dict, id_key: str = "id") -> bool:
    """Merge `fields` into one saved entry, re-reading the file first.

    Several pages are backed by the same file and each holds its own
    in-memory copy of it, loaded when that page was built (Home lists
    games/apps/websites/tracker entries that the Games/Apps/Websites/
    tracker pages own; Anime and Manga share tracker.json). Saving a
    whole list from one of them writes back a snapshot that can be
    minutes stale, silently undoing everything written since - which is
    how a game's freshly-extracted icon, or a whole batch of imported
    games, could vanish on the next unrelated edit. Touching only the
    one entry that actually changed can't do that.

    Returns False if no entry with that id is in the file (any more)."""
    entries = load(filename, [])
    for item in entries:
        if item.get(id_key) == entry_id:
            item.update(fields)
            save(filename, entries)
            return True
    return False


def _index_of(items, entry_id, id_key):
    return next((i for i, item in enumerate(items) if item.get(id_key) == entry_id), None)


def move_in_list(items, moved_id, target_id, id_key: str = "id") -> bool:
    """Move one entry to another's position, in place.

    "Onto" rather than "before"/"after": dragged downwards the entry lands
    just past its target, dragged upwards it takes the target's slot and
    pushes it down - which is what the card visibly does under the pointer
    either way. Doing that with one insert index instead of two cases is
    what the +1 below is.

    Returns False when either id is gone or they're the same entry, so a
    caller can skip a pointless save and redraw."""
    src = _index_of(items, moved_id, id_key)
    dst = _index_of(items, target_id, id_key)
    if src is None or dst is None or src == dst:
        return False
    # Decided here, on the original positions, and deliberately not
    # inline in the insert below: the target's index is re-found after
    # the pop, and comparing against *that* made dragging a card onto the
    # one immediately after it do nothing at all - src and the re-found
    # index come out equal, so the "moving down" case read as "moving
    # up" and the card was put straight back where it started.
    moving_down = src < dst
    item = items.pop(src)
    # Re-found: everything past src has shifted down by one.
    dst = _index_of(items, target_id, id_key)
    items.insert(dst + 1 if moving_down else dst, item)
    return True


def order_by_ids(items, ordered_ids, id_key: str = "id") -> None:
    """Rearrange `items` in place so the entries named in `ordered_ids`
    sit in that order - filling the slots those entries already occupy,
    and leaving every unnamed entry exactly where it was.

    The slot-filling matters because one file can back two pages:
    tracker.json holds Anime and Manga together, and the Anime page can
    only speak for the order of its own entries. Rebuilding the list as
    "named ones first, the rest after" would silently re-sort Manga from
    the Anime page."""
    wanted = {entry_id: None for entry_id in ordered_ids}
    slots = [i for i, item in enumerate(items) if item.get(id_key) in wanted]
    for item in items:
        if item.get(id_key) in wanted:
            wanted[item.get(id_key)] = item
    ordered = [wanted[entry_id] for entry_id in ordered_ids if wanted.get(entry_id) is not None]
    for slot, item in zip(slots, ordered):
        items[slot] = item


def move_entry(filename: str, moved_id, target_id, id_key: str = "id") -> bool:
    """move_in_list against the saved file, re-read first - same reason
    update_entry re-reads (see above): the page asking for this holds a
    copy of the list from when it was built, and writing that copy back
    would undo anything saved since."""
    items = load(filename, [])
    if not move_in_list(items, moved_id, target_id, id_key):
        return False
    save(filename, items)
    return True


def apply_custom_order(filename: str, ordered_ids, id_key: str = "id") -> None:
    """order_by_ids against the saved file, re-read first.

    Used when a page's sort is switched to Custom Order by dragging a
    card: the stored order has to become the order already on screen
    before the drop moves anything within it, or the page would jump to
    some older arrangement the moment it redraws."""
    items = load(filename, [])
    order_by_ids(items, ordered_ids, id_key)
    save(filename, items)
