"""Tiny JSON persistence helper shared by the feature windows."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


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
    path = DATA_DIR / filename
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save(filename: str, data):
    path = DATA_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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
